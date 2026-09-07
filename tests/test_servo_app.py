"""The servo setup page at /servo/.

This page is a bench tool: it drives unknown hardware from numbers typed into a form, so
anything that goes wrong has to appear on the page next to the form that caused it. It
used to return a bare 500 whenever the driver complained -- which it does routinely, since
the trickler holds the servo's GPIO line while it dumps powder -- leaving the reason
buried in the journal.
"""
import os
import shutil
import sys
import tempfile
import unittest

import motors

from tests import CONFIG_PATH, fakes, quiet_logging


# servo_app.py parses arguments and reads the config at import time, so it can only be
# imported once, against one config file. Same approach as test_app.py.
_HANDLE, INI_PATH = tempfile.mkstemp(suffix='.ini')
os.close(_HANDLE)
shutil.copy(CONFIG_PATH, INI_PATH)
sys.argv = ['servo_app.py', INI_PATH]

import servo_app  # noqa: E402  (imported late, once the config above is in place)

quiet_logging()


def tearDownModule():
    os.unlink(INI_PATH)


class ServoAppTestCase(unittest.TestCase):
    """Swaps the lgpio module the servo talks to for a fake."""

    def setUp(self):
        self.client = servo_app.app.test_client()
        self.lgpio = fakes.FakeLgpio()
        self._real_lgpio = motors.lgpio
        motors.lgpio = self.lgpio
        self.addCleanup(setattr, motors, 'lgpio', self._real_lgpio)
        # The page waits a second for the servo to travel before releasing the line.
        # Real time, and there is nothing here that needs it to pass.
        real_sleep = servo_app.time.sleep
        servo_app.time.sleep = lambda seconds: None
        self.addCleanup(setattr, servo_app.time, 'sleep', real_sleep)

    def move(self, **overrides):
        form = dict(gpio_pin='17', angle='92', min_pulse='500', max_pulse='2500')
        form.update(overrides)
        return self.client.post('/servo/move', data=form)


class MoveTest(ServoAppTestCase):

    def test_a_move_sends_the_same_pulse_a_charge_would(self):
        """A setup page that maps angles differently from the trickler is worse than
        none: the measure gets set up against a position charges never reproduce."""
        response = self.move(angle='92')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.lgpio.pulses[0], 1522)

    def test_the_line_is_released_afterwards(self):
        """Otherwise the Flask app holds GPIO17 and the trickler cannot dump powder --
        the same bug as the trickler holding it, pointed the other way."""
        self.move()
        self.assertEqual(self.lgpio.held_lines, [])
        self.assertEqual(self.lgpio.pulses[-1], 0)


class ErrorTest(ServoAppTestCase):
    """None of these may produce a 500."""

    def assert_message(self, response, *fragments):
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        for fragment in fragments:
            self.assertIn(fragment, body)

    def test_a_busy_line_explains_itself(self):
        motors.lgpio = fakes.FakeLgpio(busy=True)
        self.assert_message(self.move(), 'busy', 'dumps powder')

    def test_a_driver_failure_is_reported_not_500(self):
        def explode(*args, **kwargs):
            raise self.lgpio.error('something went wrong')

        self.lgpio.gpio_claim_output = explode
        self.assert_message(self.move(), 'Could not move the servo', 'something went wrong')

    def test_pulse_widths_the_wrong_way_round(self):
        self.assert_message(self.move(min_pulse='2500', max_pulse='500'), 'less than')

    def test_a_pulse_width_outside_what_a_servo_accepts(self):
        self.assert_message(self.move(max_pulse='9000'), 'outside')

    def test_a_pin_that_is_not_on_the_header(self):
        self.assert_message(self.move(gpio_pin='99'), 'not a pin on the header')

    def test_values_that_are_not_numbers(self):
        self.assert_message(self.move(angle='ninety'), 'have to be numbers')

    def test_a_failed_move_does_not_leave_the_line_held(self):
        def explode(*args, **kwargs):
            raise self.lgpio.error('tx failed')

        self.lgpio.tx_servo = explode
        self.move()
        self.assertEqual(self.lgpio.held_lines, [])

    def test_the_form_values_survive_an_error(self):
        """So you can fix one field and try again, instead of retyping all four."""
        response = self.move(gpio_pin='99', angle='120')
        self.assert_message(response, 'value="120"')


class IndexTest(ServoAppTestCase):

    def test_the_page_loads(self):
        response = self.client.get('/servo/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Servo Control', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
