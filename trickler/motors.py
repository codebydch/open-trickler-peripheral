#!/usr/bin/env python3
"""
Copyright (c) Ammolytics and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

OpenTrickler
https://github.com/ammolytics/projects/tree/develop/trickler

OpenTrickler forked and updated here:
https://github.com/codebydch/open-trickler-peripheral
"""
import time
import atexit
import enum
import logging

import gpiozero # pylint: disable=import-error;
import pigpio

class TricklerMotor:
    """Controls a small vibration DC motor with the PWM controller on the Pi."""

    def __init__(self, motor, config, **kwargs):
        """Constructor."""
        # Store memcache client if provided.
        self._memcache = kwargs.get('memcache')
        # Pull default values from config, giving preference to provided arguments.
        self._constants = enum.Enum('memcache_vars', dict(config['memcache_vars']))

        self.motor_pin = kwargs.get('motor_pin', config['motor' + str(motor)]['trickler_pin'])
        self.min_pwm = float(kwargs.get('min_pwm', config['motor' + str(motor)]['trickler_min_pwm']))
        self.max_pwm = float(kwargs.get('max_pwm', config['motor' + str(motor)]['trickler_max_pwm']))

        self.pwm = gpiozero.PWMOutputDevice(self.motor_pin)
        logging.debug(
            'Created pwm motor on PIN %r with min %r and max %r: %r',
            self.motor_pin,
            self.min_pwm,
            self.max_pwm,
            self.pwm)
        atexit.register(self._graceful_exit)

    def _graceful_exit(self):
        """Graceful exit function, turn off motor and close GPIO pin."""
        logging.debug('Closing trickler motor...')
        self.pwm.off()
        self.pwm.close()

    def update(self, target_pwm):
        """Change PWM speed of motor (int), enforcing clamps."""
        logging.debug('Updating target_pwm to %r', target_pwm)
        target_pwm = max(min(int(target_pwm), self.max_pwm), self.min_pwm)
        logging.debug('Adjusted clamped target_pwm to %r', target_pwm)
        self.set_speed(target_pwm / 100)

    def set_speed(self, speed):
        """Sets the PWM speed (float) and circumvents any clamps."""
        # Speed must be 0 - 1.
        if 0 <= speed <= 1:
            logging.debug('Setting speed from %r to %r', self.speed, speed)
            self.pwm.value = speed
            if self._memcache:
                self._memcache.set(self._constants.TRICKLER_MOTOR_SPEED.value, self.speed)
        else:
            logging.debug('invalid motor speed: %r must be between 0 and 1.', speed)

    def off(self):
        """Turns motor off."""
        self.set_speed(0)

    @property
    def speed(self):
        """Returns motor speed (float)."""
        return self.pwm.value

class ServoMotor:
    """Controls a server motor for a Powder Measure with the PWM controller on the Pi."""

    def __init__(self, config, **kwargs):
        """Constructor."""
        # Store memcache client if provided.
        self._memcache = kwargs.get('memcache')
        # Pull default values from config, giving preference to provided arguments.
        self._constants = enum.Enum('memcache_vars', dict(config['memcache_vars']))

        self.servo_pin = int(kwargs.get('servo_pin', config['servo']['servo_pin']))
        self.servo_angle = float(kwargs.get('servo_angle', config['servo']['servo_angle']))
        self.initial_angle = float(kwargs.get('initial_angle', config['servo']['initial_angle']))
        self.max_angle = float(kwargs.get('max_angle', config['servo']['max_angle']))
        self.min_pulse_width = float(kwargs.get('min_pulse_width', config['servo']['min_pulse_width']))
        self.max_pulse_width = float(kwargs.get('max_pulse_width', config['servo']['max_pulse_width']))

        # Initialize pigpio and Flask
        self.servo = pigpio.pi()
        #self.set_initial_angle()
        logging.debug(
            'Created servo motor on PIN %r with angles %r and %r',
            self.servo_pin,
            self.initial_angle,
            self.servo_angle)
        atexit.register(self._graceful_exit)

    def _graceful_exit(self):
        """Graceful exit function, turn off servo, and release PIGPIO."""
        logging.debug('Closing servo motor...')
        self.off()
        self.stop()

    def set_initial_angle(self):
        """Sets servo initial angle."""
        pulse_width = self.min_pulse_width + (self.initial_angle / self.max_angle) * (self.max_pulse_width - self.min_pulse_width)
        self.servo.set_servo_pulsewidth(self.servo_pin, pulse_width)

    def run_servo(self):
        """Moves servo to wanted angle."""
        pulse_width = self.min_pulse_width + (self.servo_angle / self.max_angle) * (self.max_pulse_width - self.min_pulse_width)
        self.servo.set_servo_pulsewidth(self.servo_pin, pulse_width)
            
    def off(self):
        """Turns servo off."""
        self.servo.set_servo_pulsewidth(self.servo_pin, 0)

    def stop(self):
        """Releases pigpio resources."""
        self.servo.stop()

# Handle command-line execution.
if __name__ == '__main__':
    import argparse
    import configparser

    import helpers


    # Default argument values.
    DEFAULTS = dict(
        verbose = False
    )

    parser = argparse.ArgumentParser(description='Test motors.')
    parser.add_argument('config_file')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--trickler_motor', type=int)
    parser.add_argument('--trickler_motor_pin', type=int)
    parser.add_argument('--max_pwm', type=float)
    parser.add_argument('--min_pwm', type=float)
    parser.add_argument('--servo_motor_pin', type=int)
    parser.add_argument('--servo_angle', type=int)
    parser.add_argument('--initial_angle', type=float)
    parser.add_argument('--max_angle', type=float)
    parser.add_argument('--min_pulse_width', type=float)
    parser.add_argument('--max_pulse_width', type=float)
    args = parser.parse_args()

    # Parse the config file.
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(args.config_file)

    # Order of priority is 1) command-line argument, 2) config file, 3) default.
    kwargs = {}
    VERBOSE = DEFAULTS['verbose'] or config['general']['verbose']
    motor = 1
    if args.verbose is not None:
        kwargs['verbose'] = args.verbose
        VERBOSE = args.verbose
    if args.trickler_motor is not None:
        kwargs['motor'] = args.trickler_motor
        motor = args.trickler_motor
    if args.trickler_motor_pin is not None:
        kwargs['motor_pin'] = args.trickler_motor_pin
    if args.max_pwm is not None:
        kwargs['max_pwm'] = args.max_pwm
    if args.min_pwm is not None:
        kwargs['min_pwm'] = args.min_pwm
    if args.servo_motor_pin is not None:
        kwargs['servo_pin'] = args.servo_motor_pin
    if args.servo_angle is not None:
        kwargs['servo_angle'] = args.servo_angle
    if args.initial_angle is not None:
        kwargs['initial_angle'] = args.initial_angle
    if args.max_angle is not None:
        kwargs['max_angle'] = args.max_angle
    if args.min_pulse_width is not None:
        kwargs['min_pulse_width'] = args.min_pulse_width
    if args.max_pulse_width is not None:
        kwargs['max_pulse_width'] = args.max_pulse_width
        
    # Configure Python logging.
    LOG_LEVEL = logging.INFO
    if VERBOSE:
        LOG_LEVEL = logging.DEBUG
    helpers.setup_logging(LOG_LEVEL)

    # Setup memcache.
    memcache_client = helpers.get_mc_client()

    # Create a TricklerMotor instance and then run it at different speeds.
    motor = TricklerMotor(
        motor,
        config=config,
        memcache=memcache_client,
        **kwargs)
    # Create a ServoMotor instance and then run it.
    servo_motor = ServoMotor(
        config=config,
        memcache=memcache_client,
        **kwargs)
    print('Running servo and spinning up trickler motor in 1 second...')
    time.sleep(1)
    servo_motor.run_servo()
    time.sleep(1.5)
    servo_motor.set_initial_angle()
    for x in range(1, 101):
        motor.set_speed(x / 100)
        time.sleep(.05)
    for x in range(100, 0, -1):
        motor.set_speed(x / 100)
        time.sleep(.05)
    motor.off()
    print('Done.')
