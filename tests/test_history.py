"""Charge history: recording, rotation, statistics, and powder profiles."""
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

from tests import fakes
from tests.test_trickler_loop import run_charge


D = decimal.Decimal


class TempPathTest(unittest.TestCase):
    """Base for tests that need a scratch history file."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, 'charges.csv')

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def row(self, error='0.01', outcome='complete', profile=''):
        return {
            'timestamp': '2026-09-01T10:00:00', 'profile': profile, 'outcome': outcome,
            'target': '45.00', 'final': '45.01', 'error': error, 'unit': 'GRAINS',
            'pulses': '6', 'seconds': '7.2', 'learned_rate': '0.28',
        }


class AppendAndReadTest(TempPathTest):

    def test_round_trip(self):
        helpers.append_charge(self.path, self.row())
        rows = helpers.read_charges(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['error'], '0.01')
        self.assertEqual(rows[0]['unit'], 'GRAINS')

    def test_rows_accumulate_oldest_first(self):
        for error in ('0.01', '0.02', '0.03'):
            helpers.append_charge(self.path, self.row(error=error))
        self.assertEqual([r['error'] for r in helpers.read_charges(self.path)],
                         ['0.01', '0.02', '0.03'])

    def test_rotation_keeps_the_newest(self):
        for error in ('0.01', '0.02', '0.03', '0.04'):
            helpers.append_charge(self.path, self.row(error=error), max_rows=2)
        self.assertEqual([r['error'] for r in helpers.read_charges(self.path)],
                         ['0.03', '0.04'])

    def test_the_directory_is_created(self):
        nested = os.path.join(self.directory, 'a', 'b', 'charges.csv')
        helpers.append_charge(nested, self.row())
        self.assertTrue(os.path.exists(nested))

    def test_a_missing_file_reads_as_no_history(self):
        self.assertEqual(helpers.read_charges(os.path.join(self.directory, 'nope.csv')), [])

    def test_the_file_is_readable_by_the_web_app(self):
        """The trickler writes it as root; the web app reads it as pi."""
        helpers.append_charge(self.path, self.row())
        self.assertTrue(os.stat(self.path).st_mode & 0o044)


class PulseRecordTest(TempPathTest):
    """One row per pulse, through the same writer as the charges."""

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.directory, 'pulses.csv')

    @staticmethod
    def pulse(on_time='0.2', dose='0.02', pwm='25', profile=''):
        return {
            'timestamp': '2026-09-01T10:00:00', 'profile': profile, 'pwm': pwm,
            'on_time': on_time, 'moving_time': '0.08', 'remainder': '0.30',
            'dose': dose, 'rate': '0.217', 'unit': 'GRAINS',
        }

    def test_round_trip(self):
        helpers.append_pulses(self.path, [self.pulse(), self.pulse(dose='0.00')])
        rows = helpers.read_pulses(self.path)
        self.assertEqual([r['dose'] for r in rows], ['0.02', '0.00'])
        self.assertEqual(tuple(rows[0]), helpers.PULSE_COLUMNS)

    def test_rotation_keeps_the_newest(self):
        for dose in ('0.01', '0.02', '0.03'):
            helpers.append_pulses(self.path, [self.pulse(dose=dose)], max_rows=2)
        self.assertEqual([r['dose'] for r in helpers.read_pulses(self.path)],
                         ['0.02', '0.03'])

    def test_a_missing_file_reads_as_no_pulses(self):
        self.assertEqual(helpers.read_pulses(self.path), [])


class PulseFitTest(unittest.TestCase):
    """The two-point solve for feed rate and spin-up, from the record instead of by hand."""

    @staticmethod
    def pulses(count, on_time, dose, pwm='25'):
        return [PulseRecordTest.pulse(on_time=str(on_time), dose=str(dose), pwm=pwm)
                for _ in range(count)]

    def bench(self):
        # The bench's own numbers: 120 pulses at 0.2 s averaging 0.0178 gn, 32 at 0.4 s
        # averaging 0.0613, which the owner solved by hand to 0.217 gn/s and 0.118 s.
        return self.pulses(120, 0.2, 0.0178) + self.pulses(32, 0.4, 0.0613)

    def test_reproduces_the_bench_fit(self):
        fit = helpers.pulse_fit(self.bench())
        self.assertAlmostEqual(fit['rate'], 0.2175, delta=0.0005)
        self.assertAlmostEqual(fit['dead_time'], 0.118, delta=0.001)
        self.assertEqual(fit['pulses'], 152)
        self.assertEqual(fit['pwm'], 25.0)

    def test_one_length_is_not_a_fit(self):
        fit = helpers.pulse_fit(self.pulses(120, 0.2, 0.0178))
        self.assertIsNone(fit['rate'])
        self.assertEqual(len(fit['buckets']), 1)

    def test_a_handful_of_pulses_at_the_second_length_is_not_enough(self):
        """Powder lands in whole grains, so the mean of three pulses is mostly chance."""
        fit = helpers.pulse_fit(self.pulses(120, 0.2, 0.0178) + self.pulses(3, 0.4, 0.0613))
        self.assertIsNone(fit['rate'])
        self.assertEqual(fit['pulses'], 123, 'the small group is still listed')

    def test_only_the_busiest_speed_is_fitted(self):
        """Fast and fine pulses on one line would be a line through two machines."""
        rows = self.bench() + self.pulses(40, 0.2, 0.05, pwm='45') + self.pulses(40, 0.4, 0.15, pwm='45')
        fit = helpers.pulse_fit(rows)
        self.assertEqual(fit['pwm'], 25.0)
        self.assertAlmostEqual(fit['rate'], 0.2175, delta=0.0005)

    def test_longer_pulses_that_delivered_less_are_noise_not_a_line(self):
        fit = helpers.pulse_fit(self.pulses(50, 0.2, 0.04) + self.pulses(50, 0.4, 0.02))
        self.assertIsNone(fit['rate'])
        self.assertIsNone(fit['dead_time'])

    def test_unparseable_rows_are_skipped_not_fatal(self):
        rows = self.bench() + [{'pwm': '25', 'on_time': 'junk', 'dose': '0.02'}, {}]
        self.assertAlmostEqual(helpers.pulse_fit(rows)['rate'], 0.2175, delta=0.0005)

    def test_no_pulses(self):
        fit = helpers.pulse_fit([])
        self.assertEqual(fit['pulses'], 0)
        self.assertIsNone(fit['rate'])


class HistoryFilesTest(unittest.TestCase):
    """The daemon and the web app must agree on where the files are."""

    @staticmethod
    def config(**history):
        config = fakes.load_config()
        config.remove_section('history')
        if history is not None:
            config.add_section('history')
            for key, value in history.items():
                config['history'][key] = value
        return config

    def test_the_pulse_file_defaults_to_beside_the_charge_file(self):
        """A config written before pulses_path existed records pulses from the first
        charge after an update, rather than silently not at all."""
        files = helpers.history_files(self.config(path='/var/lib/opentrickler/charges.csv'))
        self.assertEqual(files.pulses, '/var/lib/opentrickler/pulses.csv')
        self.assertEqual(files.learned, '/var/lib/opentrickler/learned.json')
        self.assertEqual(files.max_rows, 500)
        self.assertEqual(files.pulses_max_rows, 5000)

    def test_an_explicit_pulse_path_is_used(self):
        files = helpers.history_files(self.config(
            path='/a/charges.csv', pulses_path='/b/p.csv', pulses_max_rows='99'))
        self.assertEqual(files.pulses, '/b/p.csv')
        self.assertEqual(files.pulses_max_rows, 99)

    def test_switched_off_means_no_files_at_all(self):
        files = helpers.history_files(self.config(enabled='False', path='/a/charges.csv'))
        self.assertEqual(files, ('', '', '', 0, 0))

    def test_no_section_means_no_files(self):
        config = fakes.load_config()
        config.remove_section('history')
        self.assertEqual(helpers.history_files(config).charges, '')


class StatisticsTest(unittest.TestCase):

    def rows(self, errors, outcome='complete'):
        return [{'outcome': outcome, 'error': str(e)} for e in errors]

    def test_mean_and_sigma(self):
        stats = helpers.charge_statistics(self.rows([-0.02, 0.00, 0.02]))
        self.assertEqual(stats['count'], 3)
        self.assertAlmostEqual(stats['mean'], 0.0)
        # Population sigma of (-0.02, 0, 0.02).
        self.assertAlmostEqual(stats['sigma'], 0.0163299, places=6)
        self.assertAlmostEqual(stats['low'], -0.02)
        self.assertAlmostEqual(stats['high'], 0.02)

    def test_proportion_within_tolerance(self):
        stats = helpers.charge_statistics(self.rows([0.00, 0.01, 0.05, -0.30]))
        self.assertAlmostEqual(stats['within'], 0.5)

    def test_what_landed_beats_the_completion_reading(self):
        """At complete the pan read a grain light; two seconds later it was a grain
        heavy. The statistics must follow the pan."""
        row = {'outcome': 'complete', 'target': '45.00', 'final': '44.98',
               'error': '-0.02', 'landed': '45.02'}
        self.assertAlmostEqual(helpers.charge_error(row), 0.02)
        stats = helpers.charge_statistics([row])
        self.assertAlmostEqual(stats['mean'], 0.02)
        self.assertEqual(stats['heavy'], 1.0)

    def test_rows_without_landed_still_count(self):
        rows = [{'outcome': 'complete', 'target': '45.00', 'final': '44.98',
                 'error': '-0.02', 'landed': ''}]
        stats = helpers.charge_statistics(rows)
        self.assertAlmostEqual(stats['mean'], -0.02)
        self.assertEqual(stats['heavy'], 0.0)

    def test_only_completed_charges_count(self):
        rows = self.rows([0.01]) + self.rows([9.99], outcome='aborted')
        stats = helpers.charge_statistics(rows)
        self.assertEqual(stats['count'], 1)
        self.assertAlmostEqual(stats['mean'], 0.01)

    def test_no_completed_charges(self):
        stats = helpers.charge_statistics(self.rows([1.0], outcome='aborted'))
        self.assertEqual(stats['count'], 0)
        self.assertIsNone(stats['mean'])

    def test_unparseable_rows_are_skipped_not_fatal(self):
        rows = [{'outcome': 'complete', 'error': 'banana'},
                {'outcome': 'complete', 'error': '0.02'}]
        self.assertEqual(helpers.charge_statistics(rows)['count'], 1)


class RecordingTest(TempPathTest):
    """A real charge should leave a correct row behind."""

    def test_a_completed_charge_is_recorded(self):
        machine = fakes.SimulatedMachine('44.50')
        run_charge(machine, D('45.00'), config=fakes.load_config(history_path=self.path))

        rows = helpers.read_charges(self.path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['outcome'], 'complete')
        self.assertEqual(D(row['target']), D('45.00'))
        self.assertAlmostEqual(float(row['error']),
                               float(D(row['final']) - D('45.00')), places=6)
        self.assertGreater(int(row['pulses']), 0)
        self.assertEqual(row['unit'], 'GRAINS')

    def test_an_aborted_charge_records_the_abort(self):
        machine = fakes.SimulatedMachine('44.50')
        run_charge(machine, D('45.00'),
                   config=fakes.load_config(history_path=self.path),
                   memcache=fakes.FakeMemcache({'auto_mode': False}))
        rows = helpers.read_charges(self.path)
        self.assertTrue(rows, 'an abandoned charge should still be visible')
        self.assertNotEqual(rows[0]['outcome'], 'complete')

    def test_history_can_be_switched_off(self):
        machine = fakes.SimulatedMachine('44.50')
        run_charge(machine, D('45.00'), config=fakes.load_config())
        self.assertFalse(os.path.exists(self.path))

    def test_an_unwritable_path_does_not_stop_the_charge(self):
        """A history file that can't be written is a nuisance. A trickler that stops
        working because of it is not acceptable."""
        machine = fakes.SimulatedMachine('44.50')
        config = fakes.load_config(history_path=self.path)
        with mock.patch.object(helpers, 'append_charge',
                               side_effect=OSError('read-only file system')):
            with self.assertLogs(level='WARNING'):
                run_charge(machine, D('45.00'), config=config)
        # The charge still finished on target.
        self.assertLess(abs(machine.true_weight - D('45.00')), D('0.05'))

    def test_every_pulse_is_recorded(self):
        """The pulse file is what the rate and spin-up are fitted from, so it has to hold
        exactly the pulses that were fired, with what each one did."""
        machine = fakes.SimulatedMachine('44.50')
        config = fakes.load_config(history_path=self.path)
        run_charge(machine, D('45.00'), config=config)

        charge = helpers.read_charges(self.path)[0]
        pulses = helpers.read_pulses(os.path.join(self.directory, 'pulses.csv'))
        self.assertEqual(len(pulses), int(charge['pulses']))
        self.assertGreater(len(pulses), 0)
        for pulse in pulses:
            self.assertEqual(tuple(pulse), helpers.PULSE_COLUMNS)
            self.assertEqual(pulse['unit'], 'GRAINS')
            self.assertGreater(float(pulse['on_time']), float(pulse['moving_time']))
            self.assertGreater(float(pulse['rate']), 0)
            # What the pulse delivered is what the scale showed, in whole divisions.
            divisions = float(pulse['dose']) / 0.02
            self.assertAlmostEqual(divisions, round(divisions), places=6)
        # Pulses are aimed at a shrinking remainder, so the record runs downward.
        remainders = [float(p['remainder']) for p in pulses]
        self.assertGreater(remainders[0], remainders[-1])

    def test_pulses_are_written_once_per_charge(self):
        """The file is rewritten whole, and an SD card does not want that per pulse."""
        machine = fakes.SimulatedMachine('44.50')
        config = fakes.load_config(history_path=self.path)
        with mock.patch.object(helpers, 'append_pulses',
                               wraps=helpers.append_pulses) as append:
            run_charge(machine, D('45.00'), config=config)
        self.assertEqual(append.call_count, 1)

    def test_what_landed_is_recorded(self):
        """The completion reading is taken with powder still in the air; the history
        needs the pan a couple of seconds later to know light from heavy."""
        machine = fakes.SimulatedMachine('44.50')
        at_complete = []
        real_landed = main.landed_weight

        def remember_then_wait(scale, wait, clock=None):
            at_complete.append(scale.weight)
            return real_landed(scale, wait, clock)
        with mock.patch.object(main, 'landed_weight', remember_then_wait):
            run_charge(machine, D('45.00'), config=fakes.load_config(history_path=self.path))
        row = helpers.read_charges(self.path)[0]
        self.assertNotEqual(row['landed'], '')
        self.assertEqual(D(row['final']), at_complete[0],
                         'final is the reading the charge ended on, not a later one')
        self.assertGreaterEqual(D(row['landed']), D(row['final']),
                                'powder only ever lands, it does not leave')
        self.assertAlmostEqual(float(row['landed']), float(machine.true_weight), delta=0.03)

    def test_landed_is_blank_when_switched_off(self):
        machine = fakes.SimulatedMachine('44.50')
        run_charge(machine, D('45.00'),
                   config=fakes.load_config(history_path=self.path, landed_wait=0))
        self.assertEqual(helpers.read_charges(self.path)[0]['landed'], '')

    def test_landed_is_blank_when_the_pan_is_lifted(self):
        machine = fakes.SimulatedMachine('44.50')
        config = fakes.load_config(history_path=self.path)
        real_landed = main.landed_weight

        def lift_then_read(scale, wait, clock=None):
            machine.true_weight = D('-5')
            return real_landed(scale, wait, clock)
        with mock.patch.object(main, 'landed_weight', lift_then_read):
            run_charge(machine, D('45.00'), config=config)
        self.assertEqual(helpers.read_charges(self.path)[0]['landed'], '')

    def test_recorded_pulses_say_where_they_came_from(self):
        machine = fakes.SimulatedMachine('44.50')
        run_charge(machine, D('45.00'), config=fakes.load_config(history_path=self.path))
        pulses = helpers.read_pulses(os.path.join(self.directory, 'pulses.csv'))
        self.assertTrue(all(p['source'] == 'charge' for p in pulses))

    def test_pulses_are_off_when_history_is_off(self):
        config = fakes.load_config()
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        settings = main.trickler_settings(
            config, None, None, scale, scales.ANDScale.Units.GRAINS)
        self.assertEqual(settings.pulses_path, '')

    def test_an_unwritable_pulse_file_does_not_stop_the_charge(self):
        machine = fakes.SimulatedMachine('44.50')
        config = fakes.load_config(history_path=self.path)
        with mock.patch.object(helpers, 'append_pulses',
                               side_effect=OSError('read-only file system')):
            with self.assertLogs(level='WARNING'):
                run_charge(machine, D('45.00'), config=config)
        self.assertLess(abs(machine.true_weight - D('45.00')), D('0.05'))
        self.assertTrue(helpers.read_charges(self.path), 'the charge itself is still recorded')


class ProfileTest(unittest.TestCase):
    """Profiles layer over [trickler] and carry their own learned rate."""

    def settings_for(self, config, memcache=None):
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.resolution = D('0.02')
        return main.trickler_settings(
            config, memcache, fakes.constants_for(config), scale,
            scales.ANDScale.Units.GRAINS)

    def test_no_profile_behaves_as_before(self):
        settings = self.settings_for(fakes.load_config())
        self.assertEqual(settings.profile, '')
        self.assertEqual(settings.pulse_trickle_weight, D('0.5'))

    def test_a_profile_overrides_the_trickler_section(self):
        config = fakes.load_config(
            profiles={'Varget': {'pulse_trickle_weight': '0.9', 'pulse_rate': '0.42'}},
            active_profile='Varget')
        settings = self.settings_for(config)
        self.assertEqual(settings.profile, 'Varget')
        self.assertEqual(settings.pulse_trickle_weight, D('0.9'))
        self.assertEqual(settings.pulse_rate, D('0.42'))

    def test_settings_not_in_the_profile_fall_through(self):
        config = fakes.load_config(profiles={'Varget': {'pulse_rate': '0.42'}},
                                   active_profile='Varget')
        settings = self.settings_for(config)
        # Read from the config rather than written here, so tuning a default doesn't
        # look like a broken fall-through.
        self.assertEqual(settings.pulse_on_time,
                         float(config['trickler']['pulse_on_time']))

    def test_live_overrides_still_beat_the_profile(self):
        config = fakes.load_config(profiles={'Varget': {'pulse_trickle_weight': '0.9'}},
                                   active_profile='Varget')
        memcache = fakes.FakeMemcache({'trickler_settings': {'pulse_trickle_weight': '0.2'}})
        self.assertEqual(self.settings_for(config, memcache).pulse_trickle_weight, D('0.2'))

    def test_memcache_selection_beats_the_config_file(self):
        config = fakes.load_config(profiles={'A': {}, 'B': {}}, active_profile='A')
        memcache = fakes.FakeMemcache({'active_profile': 'B'})
        self.assertEqual(self.settings_for(config, memcache).profile, 'B')

    def test_listing_and_reading_profiles(self):
        config = fakes.load_config(profiles={'H4350': {'pulse_rate': '0.2'}, 'Varget': {}})
        self.assertEqual(helpers.list_profiles(config), ['H4350', 'Varget'])
        self.assertEqual(helpers.profile_settings(config, 'H4350'), {'pulse_rate': '0.2'})
        self.assertEqual(helpers.profile_settings(config, 'missing'), {})


class LearnedRateScopingTest(unittest.TestCase):
    """Each powder keeps its own learned rate rather than blending into an average."""

    def test_the_key_is_scoped_by_profile(self):
        constants = fakes.constants_for(fakes.load_config())
        self.assertEqual(main.learned_rate_key(constants, ''), 'trickler_pulse_rate')
        self.assertEqual(main.learned_rate_key(constants, 'Varget'),
                         'trickler_pulse_rate:Varget')

    def test_switching_profile_switches_the_rate(self):
        memcache = fakes.FakeMemcache({
            'trickler_pulse_rate:Varget': 0.44,
            'trickler_pulse_rate:H4350': 0.19,
        })
        for profile, expected in (('Varget', 0.44), ('H4350', 0.19)):
            config = fakes.load_config(profiles={profile: {}}, active_profile=profile)
            constants = fakes.constants_for(config)
            scale = mock.Mock()
            scale.Units = scales.ANDScale.Units
            settings = main.trickler_settings(
                config, memcache, constants, scale, scales.ANDScale.Units.GRAINS)
            feeder = main.PulseFeeder(mock.Mock(), scale, settings,
                                      memcache, constants)
            self.assertAlmostEqual(feeder.rate, expected)

    def test_a_learned_rate_is_written_back_to_its_own_profile(self):
        memcache = fakes.FakeMemcache()
        config = fakes.load_config(profiles={'Varget': {}}, active_profile='Varget')
        constants = fakes.constants_for(config)
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.resolution = D('0.02')
        settings = main.trickler_settings(
            config, memcache, constants, scale, scales.ANDScale.Units.GRAINS)
        feeder = main.PulseFeeder(mock.Mock(), scale, settings, memcache, constants)
        feeder._learn(0.2, D('0.06'))
        self.assertIn('trickler_pulse_rate:Varget', memcache)
        self.assertNotIn('trickler_pulse_rate', memcache)


if __name__ == '__main__':
    unittest.main()
