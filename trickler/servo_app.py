"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral
"""
import argparse
import helpers
import configparser
import os
import time
import logging

import motors

from flask import Flask, render_template, request, redirect, url_for

# Default argument values.
DEFAULTS = dict(
    verbose = False,
)

parser = argparse.ArgumentParser(description='Run OpenTrickler Flask Servo App.')
parser.add_argument('config_file')
# default=None so "not given" can be told from "given as false": with
# store_true alone the flag is False when absent, and `args.verbose is not
# None` was then always true, so the config file's verbose never applied.
parser.add_argument('--verbose', action='store_true', default=None)
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
    
logging.info('Starting OpenTrickler Flask Servo App daemon...')

app = Flask(__name__)

def move_servo(gpio_pin, angle, min_pulse, max_pulse):
    """Move the servo to the specified angle, then stop driving it.

    This page is for setting a powder measure up, so each request is a one-off move on
    whatever pin was typed into the form. Goes through the same ServoMotor the trickler
    uses, so the pulse width for an angle is identical to what a charge would produce --
    a setup page that lies about that is worse than none.

    The servo is released again rather than held: the pin is a shared resource, and the
    trickler cannot dump powder while this process holds its line.
    """
    servo = motors.ServoMotor(
        config,
        servo_pin=gpio_pin,
        servo_angle=angle,
        initial_angle=angle,
        max_angle=180,
        min_pulse_width=min_pulse,
        max_pulse_width=max_pulse)
    try:
        servo.move_to(angle)
        # Give it time to travel before the line is released.
        time.sleep(1)
    finally:
        servo.off()
    logging.debug('Servo moved to angle %s on GPIO %s (%s us)',
                  angle, gpio_pin, servo.pulse_width(angle))


def check_form(gpio_pin, angle, min_pulse, max_pulse):
    """Returns a complaint about the submitted values, or None if they are usable.

    Checked here so a typo reads as a sentence on the page rather than as an error from
    three libraries down.
    """
    if not 0 <= gpio_pin <= 27:
        return 'GPIO %s is not a pin on the header (0-27).' % gpio_pin
    if min_pulse >= max_pulse:
        return ('The minimum pulse width (%s us) has to be less than the maximum (%s us).'
                % (min_pulse, max_pulse))
    for name, pulse in (('minimum', min_pulse), ('maximum', max_pulse)):
        if not motors.MIN_SERVO_PULSE_US <= pulse <= motors.MAX_SERVO_PULSE_US:
            return ('A %s pulse width of %s us is outside the %s-%s us a hobby servo '
                    'accepts.' % (name, pulse, motors.MIN_SERVO_PULSE_US,
                                  motors.MAX_SERVO_PULSE_US))
    if not 0 <= angle <= 180:
        return 'The angle has to be between 0 and 180.'
    return None


@app.route('/servo/')
def index():
    # Default values for the form
    return render_template('servo.html', gpio_pin='', angle='', min_pulse='', max_pulse='', error=None)

@app.route('/servo/move', methods=['POST'])
def move():
    # Retrieve form data
    gpio_pin = request.form['gpio_pin']
    angle = request.form['angle']
    min_pulse = request.form['min_pulse']
    max_pulse = request.form['max_pulse']

    # Move the servo. This is a bench tool driving unknown hardware from typed-in
    # numbers, so anything that goes wrong belongs on the page next to the form that
    # caused it -- never as a 500 with the reason buried in the journal.
    error = None
    try:
        values = (int(gpio_pin), float(angle), float(min_pulse), float(max_pulse))
    except ValueError:
        error = 'GPIO pin, angle and pulse widths all have to be numbers.'
    else:
        error = check_form(*values)
        if error is None:
            try:
                move_servo(*values)
            except Exception as exc:
                logging.exception('Servo move failed.')
                if 'busy' in str(exc).lower():
                    # lgpio raises its own exception type for this, not a gpiozero one,
                    # so it is recognised by message rather than by class.
                    error = ('GPIO %s is busy. The trickler holds the servo line while '
                             'it dumps powder -- wait for the charge to finish, or turn '
                             'auto mode off, and try again.' % gpio_pin)
                else:
                    error = 'Could not move the servo on GPIO %s: %s: %s' % (
                        gpio_pin, type(exc).__name__, exc)

    # Pass the form data back to the template to retain the values
    return render_template('servo.html', gpio_pin=gpio_pin, angle=angle, min_pulse=min_pulse, max_pulse=max_pulse, error=error)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)