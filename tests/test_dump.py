"""Working the powder measure, and noticing when it jams.

A jam on this machine is all or nothing: a kernel of powder catches in the drum, the
handle stops dead, and the servo is not strong enough to shear it. So a jammed cycle
delivers *nothing* -- there is no partial drop to weigh against an expectation, which is
why the check is a fixed floor rather than something learned per powder.

What the check is really protecting against is the old behaviour: the dump was never
looked at, so an empty pan went straight to the trickler loop, which would try to build
a whole 45 grain charge one vibration pulse at a time.
"""
import decimal
import time
import unittest
from unittest import mock

import main
import scales

from tests import fakes


D = decimal.Decimal


class DumpTestCase(unittest.TestCase):

    def setUp(self):
        self.machine = fakes.SimulatedMachine('0.00')
        self.config = fakes.load_config()
        with mock.patch.object(scales.serial, 'Serial', return_value=self.machine.port):
            self.scale = scales.ANDScale(self.config)
        self.memcache = fakes.FakeMemcache()
        self.constants = fakes.constants_for(self.config)

    def settings(self, **overrides):
        for key, value in overrides.items():
            self.config['trickler'][key] = str(value)
        return main.trickler_settings(
            self.config, self.memcache, self.constants, self.scale,
            scales.ANDScale.Units.GRAINS)

    def dump(self, measure, **overrides):
        settings = self.settings(**overrides)
        with mock.patch.object(time, 'sleep', self.machine.virtual_sleep()), \
             mock.patch.object(time, 'time', self.machine.virtual_clock()):
            return main.dump_powder(measure, self.scale, settings)


class GoodDumpTest(DumpTestCase):

    def test_one_cycle_when_the_powder_drops(self):
        measure = fakes.SimulatedMeasure(self.machine, drop=25.0)
        dropped, attempts = self.dump(measure)
        self.assertEqual(attempts, 1)
        self.assertEqual(measure.cycles, 1)
        self.assertGreater(dropped, D('20'))

    def test_the_servo_is_released_after_every_cycle(self):
        """It holds its GPIO line while driven, and the servo page cannot open a line
        this process is still holding."""
        measure = fakes.SimulatedMeasure(self.machine, jams=None)
        self.dump(measure, max_dump_attempts=3)
        self.assertEqual(measure.released, measure.cycles)

    def test_what_dropped_is_measured_not_assumed(self):
        measure = fakes.SimulatedMeasure(self.machine, drop=12.5)
        dropped, _ = self.dump(measure)
        self.assertAlmostEqual(float(dropped), 12.5, delta=0.05)


class JamTest(DumpTestCase):

    def test_a_jam_that_never_clears_stops_after_the_configured_attempts(self):
        measure = fakes.SimulatedMeasure(self.machine, jams=None)
        dropped, attempts = self.dump(measure, max_dump_attempts=3)
        self.assertEqual(attempts, 3)
        self.assertEqual(measure.cycles, 3)
        self.assertEqual(dropped, D('0.00'))

    def test_a_jam_that_clears_on_the_second_try(self):
        measure = fakes.SimulatedMeasure(self.machine, drop=25.0, jams=1)
        dropped, attempts = self.dump(measure, max_dump_attempts=3)
        self.assertEqual(attempts, 2)
        self.assertGreater(dropped, D('20'))

    def test_one_attempt_means_one(self):
        """Each attempt holds the servo against the jam, so this is worth being able to
        turn down to a single try."""
        measure = fakes.SimulatedMeasure(self.machine, jams=None)
        _, attempts = self.dump(measure, max_dump_attempts=1)
        self.assertEqual(attempts, 1)

    def test_the_check_can_be_switched_off(self):
        measure = fakes.SimulatedMeasure(self.machine, jams=None)
        _, attempts = self.dump(measure, stall_drop_weight=0, max_dump_attempts=3)
        self.assertEqual(attempts, 1, 'with the check off, a dump is a dump')


class GramsTest(DumpTestCase):
    """The threshold is configured in grains, like every other weight here."""

    def test_the_threshold_converts_with_the_target_unit(self):
        settings = main.trickler_settings(
            self.config, self.memcache, self.constants, self.scale,
            scales.ANDScale.Units.GRAMS)
        self.assertAlmostEqual(
            float(settings.stall_drop_weight),
            float(D(self.config['trickler']['stall_drop_weight']) / main.GRAINS_PER_GRAM),
            places=6)


class StopOnJamTest(DumpTestCase):
    """What the control loop does with a jam, which is the point of the whole check."""

    def stop_or_go(self, measure, **overrides):
        settings = self.settings(**overrides)
        with mock.patch.object(time, 'sleep', self.machine.virtual_sleep()), \
             mock.patch.object(time, 'time', self.machine.virtual_clock()):
            return main.dump_or_stop(
                measure, self.scale, settings, self.memcache, self.constants,
                (self.machine.motor1, self.machine.motor2))

    def test_a_good_dump_lets_the_charge_go_on(self):
        self.memcache.set(self.constants.AUTO_MODE.value, True)
        measure = fakes.SimulatedMeasure(self.machine, drop=25.0)
        self.assertTrue(self.stop_or_go(measure))
        self.assertTrue(self.memcache.get(self.constants.AUTO_MODE.value))

    def test_a_good_dump_clears_a_previous_jam(self):
        self.memcache.set(self.constants.DUMP_ERROR.value, 'jammed earlier')
        self.stop_or_go(fakes.SimulatedMeasure(self.machine, drop=25.0))
        self.assertFalse(self.memcache.get(self.constants.DUMP_ERROR.value))

    def test_a_jam_stops_the_charge(self):
        self.memcache.set(self.constants.AUTO_MODE.value, True)
        measure = fakes.SimulatedMeasure(self.machine, jams=None)
        self.assertFalse(self.stop_or_go(measure, max_dump_attempts=2))

    def test_a_jam_switches_auto_mode_off(self):
        """Otherwise the next pass drives the servo straight back into the jam."""
        self.memcache.set(self.constants.AUTO_MODE.value, True)
        self.stop_or_go(fakes.SimulatedMeasure(self.machine, jams=None))
        self.assertFalse(self.memcache.get(self.constants.AUTO_MODE.value))

    def test_a_jam_says_why_where_someone_will_see_it(self):
        self.stop_or_go(fakes.SimulatedMeasure(self.machine, jams=None))
        message = self.memcache.get(self.constants.DUMP_ERROR.value)
        self.assertIn('jammed', message)

    def test_the_tricklers_never_run_on_an_empty_pan(self):
        """The regression that matters: before this, an empty pan went to the trickler
        loop, which would have tried to build a 45 grain charge by vibration."""
        self.stop_or_go(fakes.SimulatedMeasure(self.machine, jams=None))
        self.machine.settle(2.0)
        self.assertEqual(self.machine.true_weight, D('0.00'))
        self.assertEqual(self.machine.motor1.speed, 0)
        self.assertEqual(self.machine.motor2.speed, 0)


if __name__ == '__main__':
    unittest.main()
