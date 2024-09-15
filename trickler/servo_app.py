"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral
"""
import argparse
import helpers
import configparser
import os
import pigpio
import logging
from flask import Flask, render_template, request, redirect, url_for

# Default argument values.
DEFAULTS = dict(
    verbose = False,
)

parser = argparse.ArgumentParser(description='Run OpenTrickler Flask Servo App.')
parser.add_argument('config_file')
parser.add_argument('--verbose', action='store_true')
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

# Initialize pigpio and Flask
pi = pigpio.pi()
app = Flask(__name__)

def move_servo(gpio_pin, angle, min_pulse, max_pulse):
    """Move the servo to the specified angle."""
    pulse_width = min_pulse + (angle / 180.0) * (max_pulse - min_pulse)
    pi.set_servo_pulsewidth(gpio_pin, pulse_width)
    logging.debug(f"Servo moved to angle {angle} (Pulse width: {pulse_width} μs) on GPIO {gpio_pin}")

@app.route('/servo/')
def index():
    # Default values for the form
    return render_template('servo.html', gpio_pin='', angle='', min_pulse='', max_pulse='')

@app.route('/servo/move', methods=['POST'])
def move():
    # Retrieve form data
    gpio_pin = request.form['gpio_pin']
    angle = request.form['angle']
    min_pulse = request.form['min_pulse']
    max_pulse = request.form['max_pulse']

    # Move the servo
    move_servo(int(gpio_pin), float(angle), int(min_pulse), int(max_pulse))

    # Pass the form data back to the template to retain the values
    return render_template('servo.html', gpio_pin=gpio_pin, angle=angle, min_pulse=min_pulse, max_pulse=max_pulse)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)