"""The daemon's idle loop, one pass at a time, against the simulated machine.

run_pass() is what main() repeats forever: read the state, act on a command if the web
app left one, and charge when the pan is on, settled, under target and auto mode is on.
It had no tests before it was extracted; these are the cases a calibration mode must not
break when it is added beside the charge.
"""
import decimal
import logging
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import helpers
import main
import scales
import PID

from tests import fakes


D = decimal.Decimal


class PassTestCase(unittest.TestCase):

    def setUp(self):
        self.config = fakes.load_config()
        self.constants = fakes.constants_for(self.config)
        self.memcache = fakes.FakeMemcache()
        self.models = main.FeedModels(self.memcache, self.constants)

    def machine(self, start, target='45.00', auto_mode=True, drop=40.0):
        """A simulated machine with its scale, measure and memcache state ready."""
        machine = fakes.SimulatedMachine(start)
        with mock.patch.object(scales.serial, 'Serial', return_value=machine.port):
            scale = scales.ANDScale(self.config)
        measure = fakes.SimulatedMeasure(machine, drop=drop)
        self.memcache.update({
            'auto_mode': auto_mode,
            'target_weight': D(target),
            'target_unit': scale.unit,
        })
        return machine, scale, measure

    def run_passes(self, machine, scale, measure, count=10):
        """Runs `count` passes on simulated time. A charge, if one starts, completes
        inside its pass."""
        with mock.patch.object(time, 'sleep', machine.virtual_sleep()), \
             mock.patch.object(time, 'time', machine.virtual_clock()):
            # Built under the virtual clock, like run_charge() does: a PID holding a
            # timestamp from the wall clock never updates its output again.
            pid = PID.PID(*(float(self.config['PID'][k]) for k in ('Kp', 'Ki', 'Kd')))
            hw = main.Hardware(pid, machine.motor1, machine.motor2, measure, scale,
                               logging.getLogger('pid_tune'))
            last_status = None
            for _ in range(count):
                last_status = main.run_pass(
                    self.config, self.memcache, self.constants, hw, self.models, last_status)
        machine.settle(0.5)
        return hw


class ReadyToChargeTest(PassTestCase):

    def test_nothing_happens_with_auto_mode_off(self):
        machine, scale, measure = self.machine('0.00', auto_mode=False)
        self.run_passes(machine, scale, measure)
        self.assertEqual(machine.motor1.commands, [])
        self.assertEqual(measure.cycles, 0)

    def test_nothing_happens_once_the_target_is_reached(self):
        machine, scale, measure = self.machine('45.00')
        self.run_passes(machine, scale, measure)
        self.assertEqual(machine.motor1.commands, [])
        self.assertEqual(measure.cycles, 0)

    def test_an_empty_pan_is_dumped_into_then_trickled(self):
        # The simulated motors extrapolate linearly to full PWM, far faster than the real
        # ones, so the drop leaves about a grain to trickle, as the charge tests do.
        machine, scale, measure = self.machine('0.00', drop=44.0)
        self.run_passes(machine, scale, measure)
        self.assertEqual(measure.cycles, 1, 'one dump, then the tricklers')
        self.assertLess(abs(machine.true_weight - D('45.00')), D('0.05'))

    def test_a_pan_already_half_full_is_not_dumped_into_again(self):
        """Stops the servo from dumping twice when a reading dips below target."""
        machine, scale, measure = self.machine('44.50')
        self.run_passes(machine, scale, measure)
        self.assertEqual(measure.cycles, 0)
        self.assertLess(abs(machine.true_weight - D('45.00')), D('0.05'))

    def test_a_charge_finished_a_grain_light_is_not_started_again(self):
        """The shipped cutoff_weight stops one division short on purpose, so the pan
        reads under target when the charge is done. That used to read as "ready" on the
        very next pass: a new charge, complete before its first pulse, recorded as one
        more complete charge -- every pass, until the pan was lifted."""
        machine, scale, measure = self.machine('44.50')
        with mock.patch.object(main, 'trickler_loop', wraps=main.trickler_loop) as loop:
            self.run_passes(machine, scale, measure, count=30)
        self.assertEqual(loop.call_count, 1, 'one charge, then the pan is finished')
        self.assertLess(abs(machine.true_weight - D('45.00')), D('0.05'))

    def test_a_charge_that_blows_up_costs_a_charge_not_the_daemon(self):
        machine, scale, measure = self.machine('44.50')
        with mock.patch.object(main, 'trickler_loop', side_effect=RuntimeError('boom')):
            with self.assertLogs(level='ERROR') as logs:
                self.run_passes(machine, scale, measure, count=3)
        self.assertTrue(any('Charge failed' in line for line in logs.output))
        self.assertEqual(machine.motor1.speed, 0.0)
        self.assertEqual(machine.motor2.speed, 0.0)

    def test_a_changing_reading_is_logged_at_most_once_a_second(self):
        """Lifting and emptying the pan changes the reading on every frame; that was
        sixty lines in nine seconds on the bench."""
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.unit = scales.ANDScale.Units.GRAINS
        scale.is_stable = False
        scale.is_fresh = True
        readings = iter(D(str(w)) for w in range(0, 2000))
        type(scale).weight = mock.PropertyMock(side_effect=lambda: next(readings))
        hw = main.Hardware(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), scale,
                           logging.getLogger('pid_tune'))
        self.memcache.update({'auto_mode': False, 'target_weight': D('45.00'),
                              'target_unit': scale.unit})
        clock = [1000.0]

        def tick():
            clock[0] += 0.1
            return clock[0]
        with mock.patch.object(time, 'time', tick), self.assertLogs(level='INFO') as logs:
            last = None
            for _ in range(30):     # three seconds of a reading that never repeats
                last = main.run_pass(self.config, self.memcache, self.constants, hw,
                                     self.models, last)
        status_lines = [line for line in logs.output if 'target: ' in line]
        self.assertLessEqual(len(status_lines), 4)
        self.assertGreaterEqual(len(status_lines), 3, 'the state must still be logged')

    def test_the_status_is_logged_only_when_it_changes(self):
        machine, scale, measure = self.machine('45.00')
        with self.assertLogs(level='INFO') as logs:
            self.run_passes(machine, scale, measure, count=20)
        status_lines = [line for line in logs.output if 'target: ' in line]
        self.assertLess(len(status_lines), 20)


class CommandTest(PassTestCase):

    def test_reset_learned_forgets_the_rate_and_is_consumed(self):
        self.memcache['trickler_pulse_rate'] = 0.5
        machine, scale, measure = self.machine('45.00', auto_mode=False)
        settings = main.trickler_settings(
            self.config, self.memcache, self.constants, scale, scale.unit)
        model = self.models.for_settings(settings)
        self.assertTrue(model.measured, 'the stored rate was picked up')

        self.memcache['trickler_command'] = {'command': 'reset_learned', 'profile': ''}
        self.run_passes(machine, scale, measure, count=1)
        self.assertNotIn('trickler_command', self.memcache, 'taken, so it is not redone')
        self.assertNotIn('trickler_pulse_rate', self.memcache)
        self.assertFalse(model.measured)
        self.assertAlmostEqual(model.rate, float(settings.pulse_rate))

    def test_reset_learned_for_a_named_profile(self):
        self.memcache['trickler_pulse_rate:Varget'] = 0.5
        self.memcache['trickler_pulse_rate'] = 0.4
        machine, scale, measure = self.machine('45.00', auto_mode=False)
        self.memcache['trickler_command'] = {'command': 'reset_learned', 'profile': 'Varget'}
        self.run_passes(machine, scale, measure, count=1)
        self.assertNotIn('trickler_pulse_rate:Varget', self.memcache)
        self.assertIn('trickler_pulse_rate', self.memcache, 'other profiles are left alone')

    def test_an_unknown_command_is_dropped_with_a_warning(self):
        machine, scale, measure = self.machine('45.00', auto_mode=False)
        self.memcache['trickler_command'] = {'command': 'make_coffee'}
        with self.assertLogs(level='WARNING'):
            self.run_passes(machine, scale, measure, count=1)
        self.assertNotIn('trickler_command', self.memcache)

    def test_the_key_falls_back_when_the_config_predates_it(self):
        """A live config without TRICKLER_COMMAND must not crash the daemon."""
        config = fakes.load_config()
        config.remove_option('memcache_vars', 'TRICKLER_COMMAND')
        constants = fakes.constants_for(config)
        self.assertFalse(hasattr(constants, 'TRICKLER_COMMAND'))
        self.assertEqual(helpers_command_key(constants), 'trickler_command')


def helpers_command_key(constants):
    import helpers
    return helpers.command_key(constants)


if __name__ == '__main__':
    unittest.main()


class ApplyCalibrationTest(PassTestCase):
    """Applying a calibration seeds the profile's rates at the speeds now in force."""

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.config = fakes.load_config(
            history_path=os.path.join(self.directory, 'charges.csv'))
        self.constants = fakes.constants_for(self.config)
        self.models = main.FeedModels(self.memcache, self.constants)
        helpers.write_json(os.path.join(self.directory, 'learned.json'), {
            'Varget': {'calibration': {
                'stall_pwm': 18, 'rates': {'25.0': 0.1, '30.0': 0.15, '45.0': 0.25}}}})

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_the_rates_at_the_applied_speeds_are_taken_as_measured(self):
        machine, scale, measure = self.machine('45.00', auto_mode=False)
        self.memcache['trickler_command'] = {
            'command': 'calibrate_apply', 'profile': 'Varget',
            'pulse_pwm': 30.0, 'pulse_fast_pwm': 45.0}
        self.run_passes(machine, scale, measure, count=1)
        self.assertAlmostEqual(self.memcache['trickler_pulse_rate:Varget'], 0.15)
        self.assertAlmostEqual(self.memcache['trickler_fast_pulse_rate:Varget'], 0.25)
        stored = helpers.read_json(os.path.join(self.directory, 'learned.json'))['Varget']
        self.assertAlmostEqual(stored['rate'], 0.15)
        self.assertIn('calibration', stored, 'the record is kept beside the rates')

    def test_one_speed_means_no_fast_rate(self):
        machine, scale, measure = self.machine('45.00', auto_mode=False)
        self.memcache['trickler_command'] = {
            'command': 'calibrate_apply', 'profile': 'Varget',
            'pulse_pwm': 30.0, 'pulse_fast_pwm': 30.0}
        self.run_passes(machine, scale, measure, count=1)
        self.assertAlmostEqual(self.memcache['trickler_pulse_rate:Varget'], 0.15)
        self.assertNotIn('trickler_fast_pulse_rate:Varget', self.memcache)

    def test_a_speed_the_sweep_did_not_visit_is_left_to_be_probed(self):
        machine, scale, measure = self.machine('45.00', auto_mode=False)
        self.memcache['trickler_command'] = {
            'command': 'calibrate_apply', 'profile': 'Varget',
            'pulse_pwm': 35.0, 'pulse_fast_pwm': 45.0}
        with self.assertLogs(level='WARNING'):
            self.run_passes(machine, scale, measure, count=1)
        self.assertNotIn('trickler_pulse_rate:Varget', self.memcache)


class EmptyChargeTest(PassTestCase):
    """A charge the tricklers cannot feed stands the machine down, like a jam does."""

    def test_an_empty_verdict_switches_auto_mode_off_and_says_why(self):
        # Powder is never delivered by the tricklers: an empty charge every time.
        machine, scale, measure = self.machine('45.00', target='45.50')
        with mock.patch.object(main, 'pulse_phase', return_value='empty'):
            self.run_passes(machine, scale, measure, count=5)
        self.assertFalse(self.memcache['auto_mode'])
        self.assertIn('delivered nothing', self.memcache['dump_error'])

    def test_no_second_charge_starts_on_the_same_pan(self):
        machine, scale, measure = self.machine('45.00', target='45.50')
        with mock.patch.object(main, 'pulse_phase', return_value='empty') as phase:
            self.run_passes(machine, scale, measure, count=5)
        self.assertEqual(phase.call_count, 1, 'thirty in five minutes was the bench record')
