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
import os

import gpiozero

try:
    import lgpio
except ImportError:  # Not a Pi -- only the servo needs it, and tests inject a fake.
    lgpio = None


# Pulse widths outside this range are past what a hobby servo will accept, and driving
# one there parks it against its end stop where it buzzes, heats and stalls.
MIN_SERVO_PULSE_US = 400
MAX_SERVO_PULSE_US = 2600


def gpiochip_number():
    """Which /dev/gpiochip the header pins are on.

    The Pi 5 moved them to gpiochip4; everything before it uses gpiochip0.
    """
    if os.path.exists('/dev/gpiochip4'):
        try:
            with open('/proc/device-tree/model', 'rb') as model_file:
                model = model_file.read().decode('utf-8', 'replace')
        except OSError:
            model = ''
        if 'Raspberry Pi 5' in model:
            return 4
    return 0

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
    """Controls the powder measure's servo.

    This talks to lgpio directly rather than going through gpiozero, which is unusual in
    this project and deliberate. gpiozero drives a servo with `PWMOutputDevice`, and its
    lgpio backend truncates the duty cycle to a whole percent:

        self._pwm = (freq, int(value * 100))     # gpiozero/pins/lgpio.py

    At the 50 Hz servo frame that is a 200 microsecond step in pulse width, always
    rounding down -- about 27 degrees of travel on a 270 degree servo. It is why the
    measure dropped half a charge: a commanded 1522 us became 1400 us, and why one degree
    of commanded angle could move the horn a quarter turn, by crossing a percent boundary.

    lgpio.tx_servo() takes the pulse width in microseconds, which is what pigpio's
    set_servo_pulsewidth() did before it was archived, so the timing is back to what this
    machine was set up against.

    Owning the gpiochip handle also fixes pin sharing. gpiozero's Device.close() does not
    release an lgpio line -- LGPIOPin.close() re-claims it as an input, and the line is
    only freed when the factory's chip handle closes -- so the trickler held GPIO17 for
    its whole life and the servo setup page got 'GPIO busy'. Closing our own handle in
    off() hands the line back for real.
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

        # Injectable so the tests can drive a fake in place of the hardware.
        self._lgpio = kwargs.get('lgpio', lgpio)
        self._chip = kwargs.get('gpiochip', None)
        # The chip handle is held only while the servo is moving. See the class docstring.
        self._handle = None
        self.angle = None
        logging.debug(
            'Created servo motor on PIN %r with angles %r and %r',
            self.servo_pin,
            self.initial_angle,
            self.servo_angle)
        atexit.register(self._graceful_exit)

    def _graceful_exit(self):
        """Graceful exit function, stops the servo and releases the GPIO line."""
        logging.debug('Closing servo motor...')
        self.off()

    def pulse_width(self, angle):
        """The pulse width in microseconds for an angle, as pigpio was given directly.

        Linear between the two configured pulse widths, which is both what the pigpio
        version of this class computed by hand and what gpiozero's AngularServo does.
        """
        span = self.max_pulse_width - self.min_pulse_width
        pulse = self.min_pulse_width + (angle / self.max_angle) * span
        if not MIN_SERVO_PULSE_US <= pulse <= MAX_SERVO_PULSE_US:
            raise ValueError(
                'Angle %g maps to a %g us pulse, outside the %g-%g us a servo accepts. '
                'Check servo_angle, max_angle and the pulse widths in the config.'
                % (angle, pulse, MIN_SERVO_PULSE_US, MAX_SERVO_PULSE_US))
        return pulse

    def _open(self):
        """Claims the GPIO line, if it is not already held."""
        if self._handle is not None:
            return self._handle
        if self._lgpio is None:
            raise RuntimeError(
                'The lgpio module is not available, so the servo cannot be driven. '
                'Install it with: sudo apt install python3-lgpio')
        chip = gpiochip_number() if self._chip is None else self._chip
        self._handle = self._lgpio.gpiochip_open(chip)
        self._lgpio.gpio_claim_output(self._handle, self.servo_pin)
        return self._handle

    def move_to(self, angle):
        """Moves the servo to an angle and holds it there until off()."""
        pulse = self.pulse_width(angle)
        handle = self._open()
        logging.debug('Servo to %g degrees (%g us pulse)', angle, pulse)
        self._lgpio.tx_servo(handle, self.servo_pin, int(round(pulse)))
        self.angle = angle

    def set_initial_angle(self):
        """Sets servo initial angle."""
        self.move_to(self.initial_angle)

    def run_servo(self):
        """Moves servo to wanted angle."""
        self.move_to(self.servo_angle)

    def off(self):
        """Stops driving the servo and releases the GPIO line.

        Three reasons to do this as soon as the servo has finished moving: a held servo
        buzzes and hunts around its setpoint, wasting power and heating the motor; the
        measure holds its own position mechanically; and the servo setup page is a
        separate process, which cannot open a line this one still holds.

        Safe to call when the servo was never opened, or has already been released.
        """
        if self._handle is None:
            return
        try:
            # Zero stops the pulse train, so the servo stops being driven.
            self._lgpio.tx_servo(self._handle, self.servo_pin, 0)
            self._lgpio.gpio_free(self._handle, self.servo_pin)
        except Exception:  # The line is being released either way; never mask that.
            logging.debug('Servo did not stop cleanly.', exc_info=True)
        finally:
            try:
                self._lgpio.gpiochip_close(self._handle)
            finally:
                self._handle = None
                self.angle = None

    def stop(self):
        """Releases the GPIO line. Safe to call more than once."""
        self.off()


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
    # default=None so "not given" can be told from "given as false": with
    # store_true alone the flag is False when absent, and `args.verbose is not
    # None` was then always true, so the config file's verbose never applied.
    parser.add_argument('--verbose', action='store_true', default=None)
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
    config = helpers.load_config(args.config_file)

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
    time.sleep(1)
    servo_motor.off()
    for x in range(1, 101):
        motor.set_speed(x / 100)
        time.sleep(.05)
    for x in range(100, 0, -1):
        motor.set_speed(x / 100)
        time.sleep(.05)
    motor.off()
    print('Done.')
