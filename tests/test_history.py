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
        self.assertEqual(settings.pulse_on_time, 0.2)

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
