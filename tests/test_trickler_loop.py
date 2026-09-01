"""The control loop, end to end against a simulated machine.

The scale here is a real `ANDScale` reading real frames off a fake serial port. That is
the point of this module: the one failure that took the machine down in the field was
`main.py` calling an attribute `scales.py` did not have, which no amount of testing
against a stubbed scale object would have found.
"""
import decimal
import logging
import time
import unittest
from unittest import mock

import main
import scales
import PID

from tests import fakes


D = decimal.Decimal


def run_charge(machine, target, config=None, memcache=None, pid=None):
    """Runs one charge to completion against `machine`, on simulated time."""
    config = config or fakes.load_config()
    memcache = memcache if memcache is not None else fakes.FakeMemcache({'auto_mode': True})
    with mock.patch.object(scales.serial, 'Serial', return_value=machine.port):
        scale = scales.ANDScale(config)
    quiet = logging.getLogger('pid_tune')

    with mock.patch.object(time, 'sleep', machine.virtual_sleep()), \
         mock.patch.object(time, 'time', machine.virtual_clock()):
        # Build the PID inside the patched clock. It stamps last_time on construction,
        # and a controller holding a timestamp from a different clock computes a negative
        # delta and then never updates its output again.
        pid = pid or PID.PID(*(float(config['PID'][k]) for k in ('Kp', 'Ki', 'Kd')))
        pid.SetPoint = 100.0
        main.trickler_loop(
            config, memcache, fakes.constants_for(config), pid,
            machine.motor1, machine.motor2, scale, target,
            scales.ANDScale.Units.GRAINS, quiet)
    machine.settle(0.5)
    return scale


class ChargeAccuracyTest(unittest.TestCase):
    """What the whole exercise was for."""

    def test_lands_within_a_scale_division(self):
        for start in ('43.80', '44.50', '44.90'):
            with self.subTest(start=start):
                machine = fakes.SimulatedMachine(start)
                run_charge(machine, D('45.00'))
                error = machine.true_weight - D('45.00')
                self.assertLess(abs(error), D('0.02'), 'landed %s off target' % error)

    def test_never_overshoots_badly_across_trickler_speeds(self):
        """A faster trickler must not blow past the target."""
        for rate in (0.15, 0.30, 0.60):
            with self.subTest(rate=rate):
                machine = fakes.SimulatedMachine('43.80', fine_rate=rate, coarse_rate=rate * 2)
                run_charge(machine, D('45.00'))
                error = machine.true_weight - D('45.00')
                self.assertLess(abs(error), D('0.05'), 'landed %s off target' % error)

    def test_a_wrong_seed_rate_is_learned_away(self):
        """The feeder measures what it delivers, so the configured starting rate should
        barely matter."""
        errors = []
        for seed in (0.05, 0.3, 3.0):
            machine = fakes.SimulatedMachine('44.50')
            run_charge(machine, D('45.00'), config=fakes.load_config(pulse_rate=seed))
            errors.append(machine.true_weight - D('45.00'))
        for error in errors:
            self.assertLess(abs(error), D('0.02'), 'seed changed the outcome: %s' % errors)


class ExitPathTest(unittest.TestCase):
    """Whatever ends a charge, the motors must stop."""

    def _assert_motors_off(self, machine):
        self.assertEqual(machine.motor1.speed, 0.0, 'motor 1 left running')
        self.assertEqual(machine.motor2.speed, 0.0, 'motor 2 left running')

    def test_motors_stop_on_completion(self):
        machine = fakes.SimulatedMachine('44.50')
        run_charge(machine, D('45.00'))
        self._assert_motors_off(machine)

    def test_motors_stop_when_auto_mode_is_switched_off(self):
        machine = fakes.SimulatedMachine('43.00')
        run_charge(machine, D('45.00'), memcache=fakes.FakeMemcache({'auto_mode': False}))
        self._assert_motors_off(machine)

    def test_motors_stop_when_the_pan_is_removed(self):
        machine = fakes.SimulatedMachine('-1.00')
        run_charge(machine, D('45.00'))
        self._assert_motors_off(machine)

    def test_motors_stop_when_the_loop_raises(self):
        """The `finally` has to hold even for a failure nobody predicted."""
        machine = fakes.SimulatedMachine('44.50')
        with mock.patch.object(main, 'pulse_phase', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                run_charge(machine, D('45.00'))
        self._assert_motors_off(machine)


class LegacyANDScale(scales.ANDScale):
    """An A&D scale as it was before the is_fresh flag existed.

    `scales.py` is a file people customise, and the deployed copy is not always the one
    the control loop was written against -- exactly the mismatch that took the machine
    down. Accessing `is_fresh` on this raises, as it did then.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.is_fresh

    def update(self):
        super().update()
        del self.is_fresh


class StaleReadingTest(unittest.TestCase):
    """Reads that produce no weight are routine, not a reason to abandon a charge."""

    def test_a_silent_scale_eventually_stops_the_charge(self):
        machine = fakes.SimulatedMachine('44.50')
        machine.mute = True
        machine.port.reset_input_buffer()
        with self.assertLogs(level='WARNING') as logs:
            run_charge(machine, D('45.00'))
        self.assertTrue(any('serial link' in line for line in logs.output), logs.output)
        self.assertGreaterEqual(
            machine.elapsed, main.STALE_READ_TIMEOUT,
            'gave up before the timeout; a few unreadable frames is normal traffic')

    def test_a_scale_without_is_fresh_still_completes_a_charge(self):
        """The regression that cost an evening on the bench."""
        machine = fakes.SimulatedMachine('44.50')
        config = fakes.load_config()
        with mock.patch.object(scales.serial, 'Serial', return_value=machine.port):
            scale = LegacyANDScale(config)
        self.assertFalse(hasattr(scale, 'is_fresh'), 'test scale should lack the flag')

        with mock.patch.object(time, 'sleep', machine.virtual_sleep()), \
             mock.patch.object(time, 'time', machine.virtual_clock()):
            pid = PID.PID(*(float(config['PID'][k]) for k in ('Kp', 'Ki', 'Kd')))
            pid.SetPoint = 100.0
            main.trickler_loop(
                config, fakes.FakeMemcache({'auto_mode': True}),
                fakes.constants_for(config), pid,
                machine.motor1, machine.motor2, scale, D('45.00'),
                scales.ANDScale.Units.GRAINS, logging.getLogger('pid_tune'))

        machine.settle(0.5)
        self.assertEqual(machine.motor1.speed, 0.0)
        self.assertLess(abs(machine.true_weight - D('45.00')), D('0.05'))


class SeedMemcacheTest(unittest.TestCase):
    """Startup must not throw away settings the user already entered."""

    def test_absent_keys_are_seeded(self):
        memcache = fakes.FakeMemcache()
        main.seed_memcache(memcache, {'target_weight': D('0.0'), 'auto_mode': False})
        self.assertEqual(memcache['target_weight'], D('0.0'))

    def test_existing_values_are_kept(self):
        memcache = fakes.FakeMemcache({'target_weight': D('45.0'), 'auto_mode': True})
        main.seed_memcache(memcache, {'target_weight': D('0.0'), 'auto_mode': False})
        self.assertEqual(memcache['target_weight'], D('45.0'))
        self.assertTrue(memcache['auto_mode'])

    def test_explicit_values_win(self):
        memcache = fakes.FakeMemcache({'target_weight': D('45.0')})
        main.seed_memcache(memcache, {'target_weight': D('12.5')},
                           overwrite={'target_weight': True})
        self.assertEqual(memcache['target_weight'], D('12.5'))


if __name__ == '__main__':
    unittest.main()


class PulseLearningTest(unittest.TestCase):
    """What the feeder is allowed to learn from."""

    def feeder(self, resolution='0.02', rate='0.30'):
        config = fakes.load_config(pulse_rate=rate)
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.resolution = D(resolution)
        settings = main.trickler_settings(
            config, None, None, scale, scales.ANDScale.Units.GRAINS)
        return main.PulseFeeder(mock.Mock(), scale, settings)

    def test_a_measurable_dose_corrects_the_rate(self):
        feeder = self.feeder()
        feeder._learn(0.2, D('0.10'))
        self.assertGreater(feeder.rate, 0.30, 'a fat dose should raise the estimate')

    def test_a_sub_resolution_dose_teaches_nothing(self):
        """A dose below one scale division reads as zero whether it was zero or most of a
        division. Learning from it drags the estimate toward nothing and the feeder then
        over-pulses; the short pulses at the end of every charge are all like this."""
        feeder = self.feeder()
        before = feeder.rate
        feeder._learn(0.05, D('0.00'))
        self.assertEqual(feeder.rate, before)

    def test_a_sub_resolution_dose_still_counts_as_unproductive(self):
        """Otherwise a jam would never trip the give-up counter."""
        feeder = self.feeder()
        for _ in range(3):
            feeder._learn(0.05, D('0.00'))
        self.assertEqual(feeder.empty_pulses, 3)

    def test_a_negative_dose_counts_but_does_not_corrupt_the_rate(self):
        feeder = self.feeder()
        before = feeder.rate
        feeder._learn(0.2, D('-0.04'))
        self.assertEqual(feeder.rate, before)
        self.assertEqual(feeder.empty_pulses, 1)
