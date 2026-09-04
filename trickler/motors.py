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

import gpiozero

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
        # A zero or negative target means the controller wants no more powder. Turn the
        # motor off rather than clamping back up to the minimum speed and continuing to
        # feed.
        if target_pwm <= 0:
            logging.debug('target_pwm %r is not positive, turning motor off.', target_pwm)
            self.off()
            return
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
    """Controls a servo motor for a Powder Measure with the PWM controller on the Pi.

    Uses gpiozero rather than a GPIO library directly. gpiozero picks its own pin
    factory -- it prefers lgpio -- so this keeps working across the backend changes
    Raspberry Pi OS has been through. pigpio, which this used to use, is archived and is
    not available at all from Trixie onwards.
    """

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

        # AngularServo maps angle to pulse width linearly between the two bounds, which
        # is exactly the calculation this class used to do by hand. Note the config is in
        # microseconds and gpiozero wants seconds.
        self.servo = gpiozero.AngularServo(
            self.servo_pin,
            initial_angle=None,
            min_angle=0,
            max_angle=self.max_angle,
            min_pulse_width=self.min_pulse_width / 1e6,
            max_pulse_width=self.max_pulse_width / 1e6)
        logging.debug(
            'Created servo motor on PIN %r with angles %r and %r',
            self.servo_pin,
            self.initial_angle,
            self.servo_angle)
        atexit.register(self._graceful_exit)

    def _graceful_exit(self):
        """Graceful exit function, turn off servo, and release the pin."""
        logging.debug('Closing servo motor...')
        self.off()
        self.stop()

    def set_initial_angle(self):
        """Sets servo initial angle."""
        self.servo.angle = self.initial_angle

    def run_servo(self):
        """Moves servo to wanted angle."""
        self.servo.angle = self.servo_angle

    def off(self):
        """Stops driving the servo, leaving it unpowered.

        Worth doing whenever the servo has finished moving: an idle servo held on a
        software-timed PWM signal can buzz and hunt around its setpoint, which wastes
        power and heats the motor for no benefit. The powder measure holds its own
        position mechanically.
        """
        # The atexit handler calls this after stop() has already released the pin, and
        # detaching a closed device raises. Nothing to turn off in that case anyway.
        if not self.servo.closed:
            self.servo.detach()

    def stop(self):
        """Releases the GPIO pin. Safe to call more than once."""
        self.servo.close()


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
