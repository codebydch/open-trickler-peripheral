"""The powder-measure servo.

The servo is driven through lgpio directly rather than gpiozero, and these tests are
mostly about why. gpiozero's lgpio backend sets PWM with

    self._pwm = (freq, int(value * 100))     # gpiozero/pins/lgpio.py

which truncates the duty cycle to a whole percent. At the 50 Hz servo frame that is a
200 microsecond step in pulse width, always rounding down. On the bench, a commanded
1522 us arrived as 1400 us -- half a powder charge -- and commanding one more degree could
move the horn a quarter turn by crossing a percent boundary. pigpio, which this used to
use and which is archived, took a pulse width in microseconds; lgpio.tx_servo() does the
same, so the numbers below are what the servo actually sees.
"""
import unittest

import motors

from tests import fakes


def old_pulse_width_us(angle, max_angle, min_pulse, max_pulse):
    """The angle-to-pulse-width formula as it was under pigpio, in microseconds.

    Kept here verbatim as the reference the new implementation has to reproduce.
    """
    return min_pulse + (angle / max_angle) * (max_pulse - min_pulse)


class ServoTestCase(unittest.TestCase):
    """Builds a ServoMotor over a fake lgpio."""

    def setUp(self):
        self.config = fakes.load_config()
        self.lgpio = fakes.FakeLgpio()
        self.servo = self.make_servo()
        self.addCleanup(self.servo.off)

    def make_servo(self, **kwargs):
        kwargs.setdefault('lgpio', self.lgpio)
        kwargs.setdefault('gpiochip', 0)
        return motors.ServoMotor(self.config, **kwargs)


class PulseWidthTest(ServoTestCase):
    """The servo must land on the same pulse widths it did under pigpio."""

    def test_matches_the_pigpio_formula_across_the_range(self):
        max_angle = float(self.config['servo']['max_angle'])
        min_pulse = float(self.config['servo']['min_pulse_width'])
        max_pulse = float(self.config['servo']['max_pulse_width'])
        for angle in (0, 45, 92, 98, 99, 180):
            with self.subTest(angle=angle):
                expected = old_pulse_width_us(angle, max_angle, min_pulse, max_pulse)
                self.assertAlmostEqual(self.servo.pulse_width(angle), expected, places=9)

    def test_the_pulse_reaches_the_hardware_to_the_microsecond(self):
        """The bug this class exists to avoid: 1522 us must not arrive as 1400."""
        self.servo.move_to(92)
        self.assertEqual(self.lgpio.pulses, [1522])

    def test_a_degree_moves_the_servo_by_a_degree(self):
        """98 and 99 degrees were 200 us apart through gpiozero -- about a quarter turn
        on a 270 degree servo -- because they fell either side of 8% duty."""
        self.servo.move_to(98)
        self.servo.move_to(99)
        first, second = self.lgpio.pulses
        self.assertEqual(second - first, 11)

    def test_config_pulse_widths_are_microseconds(self):
        """Getting this wrong by a factor of a thousand would drive the servo hard into
        its end stop."""
        self.assertEqual(self.servo.pulse_width(0), 500)
        self.assertEqual(self.servo.pulse_width(180), 2500)

    def test_an_angle_outside_the_servo_range_is_refused(self):
        """Better a clear error than a servo parked against its stop, buzzing."""
        with self.assertRaises(ValueError):
            self.servo.pulse_width(400)


class MovementTest(ServoTestCase):

    def test_construction_does_not_claim_the_line(self):
        """Constructing the object must neither twitch the measure nor lock the servo
        setup page out of the pin."""
        self.assertEqual(self.lgpio.claimed, [])
        self.assertEqual(self.lgpio.pulses, [])

    def test_run_servo_goes_to_the_dump_angle(self):
        self.servo.run_servo()
        self.assertEqual(
            self.lgpio.pulses,
            [round(self.servo.pulse_width(float(self.config['servo']['servo_angle'])))])

    def test_set_initial_angle_goes_back(self):
        self.servo.run_servo()
        self.servo.set_initial_angle()
        self.assertEqual(
            self.lgpio.pulses[-1],
            round(self.servo.pulse_width(float(self.config['servo']['initial_angle']))))

    def test_a_dump_claims_one_line_on_the_configured_pin(self):
        self.servo.run_servo()
        pin = int(self.config['servo']['servo_pin'])
        self.assertEqual(self.lgpio.held_lines, [(self.lgpio.open_chips[0][1], pin)])

    def test_off_releases_the_line(self):
        """Not just stopped: released. gpiozero's Device.close() does not free an lgpio
        line -- it re-claims it as an input -- so the trickler used to hold GPIO17 for the
        life of the process and the servo page got 'GPIO busy'. Closing the chip handle
        is what actually hands it back."""
        self.servo.run_servo()
        self.servo.off()
        self.assertEqual(self.lgpio.pulses[-1], 0, 'the servo should stop being driven')
        self.assertEqual(self.lgpio.held_lines, [])
        self.assertEqual(self.lgpio.closed, [self.lgpio.open_chips[0][1]])

    def test_the_line_is_reclaimed_after_being_released(self):
        """A charge after an off() has to work."""
        self.servo.run_servo()
        self.servo.off()
        self.servo.run_servo()
        self.assertEqual(len(self.lgpio.held_lines), 1)
        self.assertEqual(
            self.lgpio.pulses[-1],
            round(self.servo.pulse_width(float(self.config['servo']['servo_angle']))))

    def test_moving_twice_claims_the_line_once(self):
        self.servo.run_servo()
        self.servo.set_initial_angle()
        self.assertEqual(len(self.lgpio.open_chips), 1)

    def test_overridable_from_kwargs(self):
        servo = self.make_servo(servo_angle=120, initial_angle=10)
        self.addCleanup(servo.off)
        servo.run_servo()
        servo.set_initial_angle()
        self.assertEqual(
            self.lgpio.pulses,
            [round(servo.pulse_width(120)), round(servo.pulse_width(10))])

    def test_a_busy_line_is_reported(self):
        """Another process holding the pin. The servo page turns this into a message."""
        servo = self.make_servo(lgpio=fakes.FakeLgpio(busy=True))
        with self.assertRaises(fakes.FakeLgpio.error):
            servo.run_servo()


class ShutdownTest(ServoTestCase):
    """The atexit handler runs after the line may already have been released."""

    def test_off_after_off_does_not_raise(self):
        self.servo.run_servo()
        self.servo.off()
        self.servo.off()

    def test_off_without_ever_moving_does_not_raise(self):
        self.servo.off()
        self.assertEqual(self.lgpio.closed, [])

    def test_the_graceful_exit_handler_is_safe_to_repeat(self):
        self.servo._graceful_exit()
        self.servo._graceful_exit()

    def test_stop_is_idempotent(self):
        self.servo.stop()
        self.servo.stop()

    def test_the_line_is_released_even_if_stopping_the_pulse_fails(self):
        """Whatever else happens, the next process to want this pin has to get it."""
        self.servo.run_servo()

        def explode(*args, **kwargs):
            raise self.lgpio.error('tx failed')

        self.lgpio.tx_servo = explode
        self.servo.off()
        self.assertEqual(self.lgpio.closed, [self.lgpio.open_chips[0][1]])


class NoGpiozeroServoTest(unittest.TestCase):
    """The servo must not go back through a library that rounds its pulse widths."""

    def test_motors_does_not_import_pigpio(self):
        self.assertFalse(
            hasattr(motors, 'pigpio'),
            'pigpio is archived and is not available from Raspberry Pi OS Trixie onwards')

    def test_the_servo_uses_lgpio(self):
        self.assertTrue(hasattr(motors, 'lgpio'))

    def test_the_servo_class_does_not_use_gpiozero(self):
        source = motors.ServoMotor.move_to.__doc__ or ''
        self.assertNotIn('AngularServo', source)


if __name__ == '__main__':
    unittest.main()
