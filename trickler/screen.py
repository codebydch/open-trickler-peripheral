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

class MiniPiTFTApp:
    def __init__(self, disp, button1_pin, button2_pin, font_path, colors, config, memcache_client, target_weight, auto_mode):
        self.constants = enum.Enum('memcache_vars', config['memcache_vars'])
        self.disp = disp
        self.button1 = Button(button1_pin, hold_time=3)
        self.button2 = Button(button2_pin, hold_time=3)
        self.font_path = font_path
        self.colors = colors
        self.memcache_client = memcache_client

        # Initialize variables
        self.target_weight = target_weight
        self.auto_mode = auto_mode
        
        self.set_memcache_value(self.constants.TARGET_WEIGHT.value, self.target_weight)
        self.set_memcache_value(self.constants.AUTO_MODE.value, self.auto_mode)

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
        
        # Draw the target weight
        font = ImageFont.truetype(self.font_path, 40)
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
        self.update_display()

    def shutdown_pi(self):
        os.system("/sbin/shutdown -h now")

    def run(self):
        self.update_display()
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
    parser.add_argument('--verbose', action='store_true')
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
    
    # Create the ST7789 display:
    disp = st7789.ST7789(
        board.SPI(),
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
