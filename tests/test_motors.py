"""Motor speed clamping."""
import unittest
from unittest import mock

import motors

from tests import fakes


class FakePWM:
    """Stands in for gpiozero.PWMOutputDevice."""

    def __init__(self, pin):
        self.pin = pin
        self.value = 0.0

    def off(self):
        self.value = 0.0

    def close(self):
        pass


def make_motor(index=1, **overrides):
    with mock.patch.object(motors.gpiozero, 'PWMOutputDevice', FakePWM):
        return motors.TricklerMotor(index, fakes.load_config(), **overrides)


class UpdateTest(unittest.TestCase):

    def test_clamps_to_the_configured_range(self):
        motor = make_motor(min_pwm=25, max_pwm=80)
        motor.update(200)
        self.assertEqual(motor.speed, 0.80)
        motor.update(10)
        self.assertEqual(motor.speed, 0.25)
        motor.update(50)
        self.assertEqual(motor.speed, 0.50)

    def test_non_positive_target_stops_the_motor(self):
        """A PID asking for nothing must not be clamped back up to the minimum and left
        feeding powder."""
        motor = make_motor(min_pwm=25, max_pwm=100)
        motor.update(50)
        self.assertEqual(motor.speed, 0.50)
        motor.update(0)
        self.assertEqual(motor.speed, 0.0)
        motor.update(-30)
        self.assertEqual(motor.speed, 0.0)

    def test_min_pwm_wins_over_a_lower_max_pwm(self):
        """A trap worth pinning down: the clamps are applied max-then-min, so setting
        max_pwm below min_pwm does nothing at all rather than slowing the motor."""
        motor = make_motor(min_pwm=25, max_pwm=10)
        motor.update(50)
        self.assertEqual(motor.speed, 0.25)

    def test_set_speed_bypasses_the_clamps(self):
        """The pulse feeder relies on this to run below trickler_min_pwm."""
        motor = make_motor(min_pwm=25, max_pwm=100)
        motor.set_speed(0.10)
        self.assertEqual(motor.speed, 0.10)

    def test_set_speed_rejects_out_of_range_values(self):
        motor = make_motor()
        motor.set_speed(0.5)
        motor.set_speed(1.5)
        self.assertEqual(motor.speed, 0.5)

    def test_off(self):
        motor = make_motor()
        motor.update(60)
        motor.off()
        self.assertEqual(motor.speed, 0.0)


if __name__ == '__main__':
    unittest.main()
