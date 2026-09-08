#!/usr/bin/env python3
"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral
"""
import time
import digitalio
import board
import logging
import enum
import helpers
import os

from PIL import Image, ImageDraw, ImageFont
from gpiozero import Button
from adafruit_rgb_display import st7789
from pymemcache.client import base
from decimal import Decimal


# How often to check memcache for changes made elsewhere, in seconds.
REFRESH_INTERVAL = 0.25


class MiniPiTFTApp:
    def __init__(self, disp, button1_pin, button2_pin, font_path, colors, config, memcache_client, target_weight, auto_mode):
        self.constants = enum.Enum('memcache_vars', config['memcache_vars'])
        self.disp = disp
        self.button1 = Button(button1_pin, hold_time=3)
        self.button2 = Button(button2_pin, hold_time=3)
        self.font_path = font_path
        self.colors = colors
        self.memcache_client = memcache_client

        # Adopt whatever is already set rather than stamping these defaults over it.
        # Starting the screen used to reset the target weight and switch auto mode off,
        # so a screen restart silently cancelled what the user had dialled in.
        self.target_weight = self.get_memcache_value(
            self.constants.TARGET_WEIGHT.value, target_weight)
        self.auto_mode = self.get_memcache_value(
            self.constants.AUTO_MODE.value, auto_mode)

        self.set_memcache_value(self.constants.TARGET_WEIGHT.value, self.target_weight)
        self.set_memcache_value(self.constants.AUTO_MODE.value, self.auto_mode)

        # Set when the trickler gives up on a jammed powder measure. Read from memcache
        # in the run loop, like the two values above, since the trickler is what sets it.
        self.dump_error = self.get_memcache_value(self.constants.DUMP_ERROR.value, '')

        # Loaded once. update_display() used to re-parse the font file from the SD card
        # on every redraw, and the loop below now redraws whenever anything changes.
        self.font = ImageFont.truetype(self.font_path, 40)
        self.alert_font = ImageFont.truetype(self.font_path, 20)

        self.digit_index = 0  # To track which digit is being edited

    def get_memcache_value(self, key, default):
        value = self.memcache_client.get(key)
        if value is None:
            return default
        else:
            return value

    def set_memcache_value(self, key, value):
        logging.info('Changing %s to %s', key, value)
        self.memcache_client.set(key, value)

    def update_display(self):
        image = Image.new('RGB', (self.disp.width, self.disp.height), self.colors['BLACK'])
        draw = ImageDraw.Draw(image)
        
        # Whatever else is on the screen, say when the machine has stood down on a jam.
        # Two short lines, because the panel is only 135px wide: at 20pt the widest of
        # them measures 109px, while 'MEASURE JAMMED' on one line needs 188px. The full
        # explanation stays on the control panel, which has room for a sentence.
        if self.dump_error:
            draw.rectangle((0, 0, self.disp.width, 52), fill=self.colors['RED'])
            for line, line_y in (('MEASURE', 4), ('JAMMED', 28)):
                line_bbox = draw.textbbox((0, 0), line, font=self.alert_font)
                line_x = (self.disp.width - (line_bbox[2] - line_bbox[0])) // 2
                draw.text((line_x, line_y), line, font=self.alert_font,
                          fill=self.colors['WHITE'])

        # Draw the target weight
        font = self.font
        text = f"{self.target_weight:05.2f}"
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        text_x = (self.disp.width - text_width) // 2
        text_y = 60
        draw.text((text_x, text_y), text, font=font, fill=self.colors['WHITE'])
        
        # Draw the underline for the current digit
        underline_y = text_y + text_height + 10
        char_widths = [draw.textbbox((0, 0), char, font=font)[2] - draw.textbbox((0, 0), char, font=font)[0] for char in text]
        underline_x = text_x + sum(char_widths[:self.digit_index])
        draw.line((underline_x, underline_y, underline_x + char_widths[self.digit_index], underline_y), fill=self.colors['WHITE'], width=2)
        
        # Draw the auto/manual mode squares
        if self.auto_mode:
            draw.rectangle((10, 200, 60, 230), outline=self.colors['GREEN'], fill=self.colors['GREEN'])
        else:
            draw.rectangle((10, 200, 60, 230), outline=self.colors['RED'], fill=self.colors['RED'])
        
        self.disp.image(image)

    def increment_digit(self):
        target_weight_str = f"{self.target_weight:05.2f}"
        digits = list(target_weight_str)
        
        if digits[self.digit_index].isdigit():
            digits[self.digit_index] = str((int(digits[self.digit_index]) + 1) % 10)
        
        self.target_weight = Decimal("".join(digits))
        self.set_memcache_value(self.constants.TARGET_WEIGHT.value, self.target_weight)
        self.update_display()

    def move_to_next_digit(self):
        self.digit_index = (self.digit_index + 1) % 5 # 5 for the numbers (4) and decimal point (1)
        if self.digit_index == 2:  # Skip over the decimal point
            self.digit_index = 3
        self.update_display()

    def toggle_auto_mode(self):
        self.auto_mode = not self.auto_mode
        self.set_memcache_value(self.constants.AUTO_MODE.value, self.auto_mode)
        if self.auto_mode:
            # Switching auto mode on is how you say a jam has been cleared -- the
            # trickler switched it off itself when it gave up on the measure. Same rule
            # as the control panel's toggle.
            self.dump_error = ''
            self.set_memcache_value(self.constants.DUMP_ERROR.value, '')
        self.update_display()

    def refresh(self):
        """Picks up changes made anywhere else, and reports whether anything moved.

        The screen is not the only thing that writes these: the control panel sets the
        target weight and toggles auto mode, and the trickler switches auto mode off by
        itself when the powder measure jams. Without this the display would sit on
        whatever the buttons last did, and the next both-button press would toggle from a
        stale value -- taking two presses to turn auto mode back on after a jam.

        Adopting is safe against the digit editor because every button press writes
        straight to memcache. What this holds and what memcache holds only differ when
        somebody else has written, which is exactly the case worth adopting.
        """
        changed = False

        target = self.memcache_client.get(self.constants.TARGET_WEIGHT.value)
        if target is None:
            # memcached restarted and lost everything. Put back what we know rather than
            # adopting nothing, the same way __init__ does.
            self.set_memcache_value(self.constants.TARGET_WEIGHT.value, self.target_weight)
        elif target != self.target_weight:
            try:
                # Anything that won't format would break update_display() on every pass.
                self.target_weight = Decimal(str(target))
                changed = True
            except (ArithmeticError, TypeError, ValueError):
                logging.warning('Ignoring an unusable target weight: %r', target)

        auto_mode = self.memcache_client.get(self.constants.AUTO_MODE.value)
        if auto_mode is None:
            self.set_memcache_value(self.constants.AUTO_MODE.value, self.auto_mode)
        elif bool(auto_mode) != self.auto_mode:
            self.auto_mode = bool(auto_mode)
            changed = True

        dump_error = self.memcache_client.get(self.constants.DUMP_ERROR.value) or ''
        if bool(dump_error) != bool(self.dump_error):
            self.dump_error = dump_error
            changed = True

        return changed

    def shutdown_pi(self):
        os.system("/sbin/shutdown -h now")

    def run(self):
        self.update_display()
        next_refresh = time.time()
        # Loop to update display with button presses
        while True:
            if self.button2.is_held:
                self.shutdown_pi()
                break
            elif self.button1.is_pressed and self.button2.is_pressed:
                self.toggle_auto_mode()
                time.sleep(0.5)  # Debounce delay
            elif self.button1.is_pressed:
                self.increment_digit()
                time.sleep(0.1)  # Debounce delay
            elif self.button2.is_pressed:
                self.move_to_next_digit()
                time.sleep(0.1)  # Debounce delay

            # Follow changes made from the control panel or by the trickler itself. On a
            # timer, and only redrawing when something actually moved: a blind redraw
            # every pass would push a full frame over SPI ten times a second for nothing.
            if time.time() >= next_refresh:
                next_refresh = time.time() + REFRESH_INTERVAL
                if self.refresh():
                    self.update_display()

            time.sleep(0.1)  # Sleep for a short period to avoid high CPU usage

if __name__ == "__main__":
    import argparse
    import configparser

    # Default argument values.
    DEFAULTS = dict(
        verbose = False,
        button1_gpio = 23,
        button2_gpio = 24,
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )

    parser = argparse.ArgumentParser(description='Run OpenTrickler Screen.')
    parser.add_argument('--target_weight', type=Decimal, default=0.0)
    parser.add_argument('config_file')
    # default=None so "not given" can be told from "given as false": with
    # store_true alone the flag is False when absent, and `args.verbose is not
    # None` was then always true, so the config file's verbose never applied.
    parser.add_argument('--verbose', action='store_true', default=None)
    parser.add_argument('--auto_mode', action='store_true')
    args = parser.parse_args()
    
    config = configparser.ConfigParser()
    config.optionxform = str
    if args.config_file:
        config.read(args.config_file)

    # Order of priority is 1) command-line argument, 2) config file, 3) default.
    VERBOSE = DEFAULTS['verbose'] or config['general']['verbose']
    if args.verbose is not None:
        VERBOSE = args.verbose

    # Configure Python logging.
    LOG_LEVEL = logging.INFO
    if VERBOSE:
        LOG_LEVEL = logging.DEBUG
    helpers.setup_logging(LOG_LEVEL)  
    
    logging.info('Starting OpenTrickler Screen daemon...')
    logging.info('Setting up Screen...')
    button1_gpio = DEFAULTS['button1_gpio'] or config['buttons']['button1_gpio']
    button2_gpio = DEFAULTS['button2_gpio'] or config['buttons']['button2_gpio']
    
    logging.info('Button1 GPIO pin is set as %s', button1_gpio)
    logging.info('Button2 GPIO pin is set as %s', button2_gpio)
    target_weight = Decimal('0.0')
    if args.target_weight is not None:
        target_weight = args.target_weight
 
    logging.info('Target Weight is set as %s', target_weight)
    auto_mode = False
    if args.auto_mode is not None:
        auto_mode = args.auto_mode   

    logging.info('Auto Mode is set as %s', auto_mode)
    # Configuration for CS and DC pins for Raspberry Pi
    cs_pin = digitalio.DigitalInOut(board.CE0)
    dc_pin = digitalio.DigitalInOut(board.D25)
    reset_pin = None
    BAUDRATE = 64000000  # The pi can be very fast!
    
    # SPI is off in a stock Raspberry Pi OS image, and the failure surfaces three
    # libraries down as "/dev/spidev0.0 does not exist". Say what to do about it instead
    # of crash-looping on that traceback.
    try:
        spi = board.SPI()
    except OSError as exc:
        logging.error('The screen needs the SPI interface, which is not enabled: %s. '
                      'Enable it with `sudo raspi-config nonint do_spi 0`, then reboot.', exc)
        raise SystemExit(1) from exc

    # Create the ST7789 display:
    disp = st7789.ST7789(
        spi,
        cs=cs_pin,
        dc=dc_pin,
        rst=reset_pin,
        baudrate=BAUDRATE,
        width=135,
        height=240,
        x_offset=53,
        y_offset=40,
    )
    
    backlight = digitalio.DigitalInOut(board.D22)
    backlight.switch_to_output()
    backlight.value = True
    
    colors = {
        'WHITE': (255, 255, 255),
        'BLACK': (0, 0, 0),
        'RED': (255, 0, 0),
        'GREEN': (0, 255, 0)
    }
    
    font_path = DEFAULTS['font_path'] or config['screen']['font_path']
    
    logging.info('Screen is setup.')
    
    # Initialize memcache client
    memcache_client = helpers.get_mc_client()
    
    app = MiniPiTFTApp(disp, button1_gpio, button2_gpio, font_path, colors, config, memcache_client, target_weight, auto_mode)
    app.run()
