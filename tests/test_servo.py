"""The powder-measure servo.

These drive a real gpiozero AngularServo over gpiozero's mock pin factory, so the pulse
widths asserted below are the ones the hardware would actually see.

The point of the pulse-width test is the migration off pigpio: pigpio was archived by its
author and is not packaged from Raspberry Pi OS Trixie onwards, so the servo moved to
gpiozero. Powder measures are set up by eye against particular angles, so the mapping had
to come across unchanged.
"""
import unittest

import gpiozero
from gpiozero.pins.mock import MockFactory, MockPWMPin

import motors

from tests import fakes


def old_pulse_width_us(angle, max_angle, min_pulse, max_pulse):
    """The angle-to-pulse-width formula as it was under pigpio, in microseconds.

    Kept here verbatim as the reference the new implementation has to reproduce.
    """
    return min_pulse + (angle / max_angle) * (max_pulse - min_pulse)


class ServoTestCase(unittest.TestCase):
    """Swaps in gpiozero's mock pins, and puts the real factory back afterwards."""

    def setUp(self):
        self._real_factory = gpiozero.Device.pin_factory
        gpiozero.Device.pin_factory = MockFactory(pin_class=MockPWMPin)

    def tearDown(self):
        gpiozero.Device.pin_factory.reset()
        gpiozero.Device.pin_factory = self._real_factory


class PulseWidthTest(ServoTestCase):
    """The servo must land on the same pulse widths it did under pigpio."""

    def test_matches_the_pigpio_formula_across_the_range(self):
        config = fakes.load_config()
        servo = motors.ServoMotor(config)
        max_angle = float(config['servo']['max_angle'])
        min_pulse = float(config['servo']['min_pulse_width'])
        max_pulse = float(config['servo']['max_pulse_width'])
        try:
            for angle in (0, 45, 92, 90, 180):
                with self.subTest(angle=angle):
                    servo.servo.angle = angle
                    expected = old_pulse_width_us(angle, max_angle, min_pulse, max_pulse)
                    self.assertAlmostEqual(servo.servo.pulse_width * 1e6, expected, places=6)
        finally:
            servo.stop()

    def test_config_microseconds_become_gpiozero_seconds(self):
        """The config is in microseconds; gpiozero wants seconds. Getting this wrong by
        a factor of a million would drive the servo hard into its end stop."""
        servo = motors.ServoMotor(fakes.load_config())
        try:
            self.assertAlmostEqual(servo.servo.min_pulse_width, 0.0005)
            self.assertAlmostEqual(servo.servo.max_pulse_width, 0.0025)
        finally:
            servo.stop()


class MovementTest(ServoTestCase):

    def setUp(self):
        super().setUp()
        self.config = fakes.load_config()
        self.servo = motors.ServoMotor(self.config)
        self.addCleanup(self.servo.stop)

    def test_starts_detached(self):
        """Constructing the object must not twitch the measure."""
        self.assertIsNone(self.servo.servo.angle)

    def test_run_servo_goes_to_the_dump_angle(self):
        self.servo.run_servo()
        self.assertAlmostEqual(self.servo.servo.angle,
                               float(self.config['servo']['servo_angle']))

    def test_set_initial_angle_goes_back(self):
        self.servo.run_servo()
        self.servo.set_initial_angle()
        self.assertAlmostEqual(self.servo.servo.angle,
                               float(self.config['servo']['initial_angle']))

    def test_off_releases_the_servo(self):
        """Detached rather than held: an idle servo on software-timed PWM buzzes and
        hunts, and the measure holds its own position."""
        self.servo.run_servo()
        self.assertIsNotNone(self.servo.servo.angle)
        self.servo.off()
        self.assertIsNone(self.servo.servo.angle)

    def test_overridable_from_kwargs(self):
        # Release the pin first: gpiozero reserves it, so two servos on GPIO17 collide.
        self.servo.stop()
        servo = motors.ServoMotor(self.config, servo_angle=120, initial_angle=10)
        self.addCleanup(servo.stop)
        servo.run_servo()
        self.assertAlmostEqual(servo.servo.angle, 120)
        servo.set_initial_angle()
        self.assertAlmostEqual(servo.servo.angle, 10)


class ShutdownTest(ServoTestCase):
    """The atexit handler runs after the pin may already have been released."""

    def test_off_after_stop_does_not_raise(self):
        servo = motors.ServoMotor(fakes.load_config())
        servo.stop()
        servo.off()

    def test_the_graceful_exit_handler_is_safe_to_repeat(self):
        """It is registered with atexit, and also runs if something else closes the
        servo first. Neither should produce a traceback on shutdown."""
        servo = motors.ServoMotor(fakes.load_config())
        servo._graceful_exit()
        servo._graceful_exit()

    def test_stop_is_idempotent(self):
        servo = motors.ServoMotor(fakes.load_config())
        servo.stop()
        servo.stop()


class NoDirectGpioLibraryTest(unittest.TestCase):
    """Nothing should talk to a GPIO library directly any more.

    Checked against the module's own namespace rather than sys.modules: gpiozero probes
    its candidate pin factories on import, so `pigpio` can legitimately be loaded by
    something other than us.
    """

    def test_motors_does_not_import_pigpio(self):
        self.assertFalse(
            hasattr(motors, 'pigpio'),
            'motors.py imports pigpio, which is archived and is not available from '
            'Raspberry Pi OS Trixie onwards')

    def test_the_servo_goes_through_gpiozero(self):
        self.assertTrue(hasattr(motors, 'gpiozero'))


if __name__ == '__main__':
    unittest.main()
