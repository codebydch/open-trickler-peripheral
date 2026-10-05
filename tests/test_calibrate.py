"""The calibration routine, driven through run_pass() on the lumpy simulator.

The machine is `SimulatedMachine(tube=True)`: the one that drops 0, 0, 0, 0, 0.02 and then
0.08 from identical pulses, as the bench did. The routine runs trickler 1 only and never
the servo, pauses for the container, and ends with a recommendation.
"""
import decimal
import logging
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import calibrate
import helpers
import main
import scales
import PID

from tests import fakes


D = decimal.Decimal


class CalibrationTestCase(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.config = fakes.load_config(history_path=os.path.join(self.directory, 'charges.csv'))
        # Small simulation passes: the recommender is tested on its own; here it only has
        # to finish.
        self.config['calibration']['first_pass'] = '4'
        self.config['calibration']['second_pass'] = '6'
        self.constants = fakes.constants_for(self.config)
        self.memcache = fakes.FakeMemcache()
        self.models = main.FeedModels(self.memcache, self.constants)
        patcher = mock.patch.object(calibrate, 'RECOMMEND_THREADED', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def machine(self, start='0.00', tube=True, **tube_args):
        machine = fakes.SimulatedMachine(start, tube=tube_args or tube)
        with mock.patch.object(scales.serial, 'Serial', return_value=machine.port):
            scale = scales.ANDScale(self.config)
        measure = fakes.SimulatedMeasure(machine, drop=40.0)
        self.memcache.update({'auto_mode': False, 'target_weight': D('55.00'),
                              'target_unit': scale.unit})
        return machine, scale, measure

    def command(self, **request):
        request.setdefault('command', 'calibrate')
        request.setdefault('profile', 'TestPowder')
        request.setdefault('capacity', 200)
        request.setdefault('pulses_per_cell', 3)
        self.memcache['trickler_command'] = request

    def run_passes(self, machine, scale, measure, until=None, limit=2000, on_pass=None):
        """Runs passes on simulated time until `until()` is true or the routine ends."""
        with mock.patch.object(time, 'sleep', machine.virtual_sleep()), \
             mock.patch.object(time, 'time', machine.virtual_clock()):
            pid = PID.PID(*(float(self.config['PID'][k]) for k in ('Kp', 'Ki', 'Kd')))
            hw = main.Hardware(pid, machine.motor1, machine.motor2, measure, scale,
                               logging.getLogger('pid_tune'))
            last = None
            for i in range(limit):
                last = main.run_pass(self.config, self.memcache, self.constants, hw,
                                     self.models, last)
                if on_pass:
                    on_pass(i)
                calibration = self.models.calibration
                if until is not None and until(calibration):
                    break
                if calibration is not None and calibration.finished:
                    break
        return self.models.calibration

    @property
    def status(self):
        return self.memcache.get('calibration_status')


class WholeRoutineTest(CalibrationTestCase):

    def test_runs_to_a_recommendation(self):
        machine, scale, measure = self.machine()
        self.command()
        calibration = self.run_passes(machine, scale, measure)

        self.assertTrue(calibration.finished)
        self.assertEqual(calibration.phase, 'done', calibration.message)
        self.assertIsNone(calibration.error)
        results = self.status['results']
        self.assertEqual(results['pulses'], 3 * 3 * 3)
        self.assertEqual(len(results['cells']), 9)
        self.assertIsNotNone(results['recommendation'])
        recommended = results['recommendation']['recommended']['settings']
        for key in ('pulse_pwm', 'pulse_fast_pwm', 'pulse_on_time', 'pulse_trickle_weight',
                    'stall_pwm', 'pulse_rate', 'pulse_dead_time'):
            self.assertIn(key, recommended)
        self.assertIn('current', results['recommendation'])

    def test_the_servo_never_runs_and_auto_mode_is_switched_off(self):
        machine, scale, measure = self.machine()
        self.memcache['auto_mode'] = True
        self.command()
        self.run_passes(machine, scale, measure)
        self.assertEqual(measure.cycles, 0, 'the user manages the powder in the container')
        self.assertFalse(self.memcache['auto_mode'])

    def test_only_trickler_one_runs(self):
        machine, scale, measure = self.machine()
        self.command()
        self.run_passes(machine, scale, measure)
        self.assertFalse(any(speed > 0 for speed in machine.motor2.commands),
                         'trickler 2 must never run; switching it off is fine')
        self.assertGreater(len(machine.motor1.commands), 20)
        self.assertEqual(machine.motor1.speed, 0.0, 'left off at the end')

    def test_every_pulse_is_in_the_record(self):
        machine, scale, measure = self.machine()
        self.command()
        self.run_passes(machine, scale, measure)
        rows = helpers.read_pulses(os.path.join(self.directory, 'pulses.csv'))
        self.assertEqual(len(rows), 27)
        self.assertTrue(all(r['source'] == 'calibration' and r['profile'] == 'TestPowder'
                            for r in rows))
        self.assertEqual({float(r['pwm']) for r in rows}, {25.0, 30.0, 45.0})
        self.assertTrue(all(r['tail'] != '' for r in rows), 'the tail is measured on every pulse')

    def test_the_results_are_kept_with_the_profile(self):
        machine, scale, measure = self.machine()
        self.command()
        self.run_passes(machine, scale, measure)
        learned = helpers.read_json(os.path.join(self.directory, 'learned.json'))
        self.assertIn('calibration', learned['TestPowder'])
        self.assertIn('rate', learned['TestPowder'], 'the fitted rate seeds the next charge')

    def test_the_sweep_sees_the_lumps(self):
        """The point of calibrating on this simulator: cells differ by speed, and the
        fitted rate at 45% is above the one at 25%."""
        machine, scale, measure = self.machine()
        self.command(pulses_per_cell=6)
        calibration = self.run_passes(machine, scale, measure, limit=4000)
        rates = calibration.results['rates']
        self.assertGreater(rates['45.0'], rates['25.0'])
        self.assertTrue(all(rate > 0 for rate in rates.values()))


class PhaseTest(CalibrationTestCase):

    def test_priming_finds_the_stall_speed_near_the_machines(self):
        machine, scale, measure = self.machine()
        self.command()
        calibration = self.run_passes(machine, scale, measure,
                                      until=lambda c: c is not None and c.stall_pwm is not None)
        self.assertIsNotNone(calibration.continuous_rate)
        self.assertGreater(calibration.continuous_rate, 0.05)
        # The fake stalls at 20%; the search steps down 2% from 25%.
        self.assertAlmostEqual(calibration.stall_pwm, 20.0, delta=5.0)

    def test_no_powder_arriving_gives_up_with_a_reason(self):
        machine, scale, measure = self.machine(ramp_rate=0.0)
        self.command()
        calibration = self.run_passes(machine, scale, measure)
        self.assertEqual(calibration.phase, 'failed')
        self.assertIn('No powder', calibration.error)
        self.assertEqual(machine.motor1.speed, 0.0)

    def test_the_container_limit_pauses_and_continue_resumes(self):
        machine, scale, measure = self.machine(start='39.00')
        self.command(capacity=40.0)
        calibration = self.run_passes(
            machine, scale, measure, until=lambda c: c is not None and c.phase == 'paused')
        self.assertEqual(calibration.prompt, 'empty_container')
        self.assertLessEqual(float(scale.weight), 40.0, 'paused before the limit, not after')
        self.assertGreaterEqual(float(scale.weight), 39.3, 'but not long before it')
        self.assertEqual(machine.motor1.speed, 0.0)
        pulses_before = calibration._index

        # Stays paused until told otherwise.
        self.run_passes(machine, scale, measure, limit=20,
                        until=lambda c: False)
        self.assertEqual(calibration.phase, 'paused')

        # Empty the container and continue.
        machine.true_weight = D('0.00')
        machine.settle(1.0)
        self.memcache['trickler_command'] = {'command': 'calibrate_continue'}
        calibration = self.run_passes(machine, scale, measure, limit=4000)
        self.assertEqual(calibration.phase, 'done', calibration.message)
        self.assertGreater(calibration._index, pulses_before)

    def test_lifting_the_pan_pauses(self):
        machine, scale, measure = self.machine()
        self.command()

        def lift_mid_sweep(i):
            c = self.models.calibration
            if c is not None and c.phase == 'sweep' and c._index == 4:
                machine.true_weight = D('-5.00')
        calibration = self.run_passes(
            machine, scale, measure, on_pass=lift_mid_sweep,
            until=lambda c: c is not None and c.phase == 'paused')
        self.assertEqual(calibration.prompt, 'pan_missing')
        self.assertEqual(machine.motor1.speed, 0.0)

    def test_abort_stops_the_motor_at_once(self):
        machine, scale, measure = self.machine()
        self.command()

        def abort_mid_sweep(i):
            c = self.models.calibration
            if c is not None and c.phase == 'sweep' and c._index == 3:
                self.memcache['trickler_command'] = {'command': 'calibrate_abort'}
        calibration = self.run_passes(machine, scale, measure, on_pass=abort_mid_sweep)
        self.assertEqual(calibration.phase, 'aborted')
        self.assertTrue(calibration.finished)
        self.assertEqual(machine.motor1.speed, 0.0)

    def test_a_second_request_while_running_is_ignored(self):
        machine, scale, measure = self.machine()
        self.command()
        with self.assertLogs(level='WARNING') as logs:
            def ask_again(i):
                if i == 5:
                    self.command()
            self.run_passes(machine, scale, measure, on_pass=ask_again)
        self.assertTrue(any('already running' in line for line in logs.output))

    def test_the_status_is_published_for_the_page(self):
        machine, scale, measure = self.machine()
        self.command()
        self.run_passes(machine, scale, measure, until=lambda c: c is not None and c.phase == 'sweep')
        status = self.status
        self.assertEqual(status['phase'], 'sweep')
        self.assertEqual(status['profile'], 'TestPowder')
        self.assertEqual(status['pulses_total'], 27)
        self.assertFalse(status['finished'])


class SettingsTest(unittest.TestCase):

    def test_the_shipped_section_reads(self):
        cal = calibrate.calibration_settings(fakes.load_config())
        self.assertEqual(cal.speeds, (25.0, 30.0, 45.0))
        self.assertEqual(cal.durations, (0.15, 0.25, 0.40))
        self.assertEqual(cal.pulses_per_cell, 10)

    def test_a_config_without_the_section_gets_the_defaults(self):
        config = fakes.load_config()
        config.remove_section('calibration')
        cal = calibrate.calibration_settings(config)
        self.assertEqual(cal.speeds, (25.0, 30.0, 45.0))

    def test_the_status_key_falls_back(self):
        config = fakes.load_config()
        config.remove_option('memcache_vars', 'CALIBRATION_STATUS')
        self.assertEqual(calibrate.status_key(fakes.constants_for(config)), 'calibration_status')
        self.assertEqual(calibrate.status_key(fakes.constants_for(fakes.load_config())),
                         'calibration_status')


if __name__ == '__main__':
    unittest.main()


class FitTest(CalibrationTestCase):
    """The fit counts what a pulse delivered in all, tail included.

    On the bench the tails were as large as the doses, and a fit on the doses alone
    read the feed rate at half, which made every candidate simulate slow."""

    def calibration_with_cells(self, cells):
        machine, scale, measure = self.machine()
        self.command()
        calibration = self.run_passes(machine, scale, measure,
                                      until=lambda c: c is not None and c.phase == 'sweep')
        calibration.cells.clear()
        for key, values in cells.items():
            calibration.cells[key].extend(values)
        calibration.records = [{'pwm': k[0], 'on_time': k[1], 'dose': d, 'tail': t}
                               for k, vals in cells.items() for d, t in vals]
        calibration.phase = 'fit'
        with mock.patch.object(time, 'sleep', machine.virtual_sleep()), \
             mock.patch.object(time, 'time', machine.virtual_clock()):
            calibration.step()
        return calibration

    def test_the_rate_includes_the_tails(self):
        # 0.25 s pulses at 30%: 0.01 by the read and 0.03 in the tail, every time.
        cells = {(30.0, 0.25): [(0.01, 0.03)] * 10, (30.0, 0.40): [(0.02, 0.04)] * 10,
                 (25.0, 0.25): [(0.0, 0.0)] * 10, (45.0, 0.25): [(0.04, 0.04)] * 10}
        calibration = self.calibration_with_cells(cells)
        rates = calibration.results['rates']
        # Delivered at 30%: 10 x 0.04 + 10 x 0.06 = 1.0 over the moving time of 20 pulses.
        dead = calibration.results['dead_times']['30.0']
        moving = 10 * (0.25 - dead) + 10 * (0.40 - dead)
        self.assertAlmostEqual(rates['30.0'], 1.0 / moving, places=3)
        self.assertGreater(rates['30.0'], 0.3 / moving * 1.5, 'the doses alone would read 0.3')
