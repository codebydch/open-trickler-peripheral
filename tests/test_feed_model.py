"""What a powder teaches the machine, and where that is kept.

A FeedModel outlives the charge that built it and is saved two ways: to learned.json,
which survives a reboot, and to memcache, which the tuning page reads. Before it existed
the learned rate lived in memcache alone, and every reboot forgot every powder.
"""
import decimal
import os
import shutil
import tempfile
import unittest
from unittest import mock

import helpers
import main
import scales

from tests import fakes


D = decimal.Decimal


class FeedModelTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, 'learned.json')

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def settings(self, profile='', persistent=True, memcache=None, **overrides):
        """Trickler settings whose learned-state file is in this test's scratch directory."""
        config = fakes.load_config(
            history_path=os.path.join(self.directory, 'charges.csv') if persistent else None,
            profiles={profile: {}} if profile else None,
            active_profile=profile or None,
            **overrides)
        constants = fakes.constants_for(config) if memcache is not None else None
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        settings = main.trickler_settings(
            config, memcache, constants, scale, scales.ANDScale.Units.GRAINS)
        return settings, constants

    def test_a_learned_rate_survives_to_a_new_model(self):
        """The reboot case: no memcache, only the file."""
        settings, _ = self.settings()
        model = main.FeedModel(settings)
        model.learn(0.2, 0.06, False, 0.02)
        self.assertTrue(model.measured)

        again = main.FeedModel(settings)
        self.assertTrue(again.measured, 'a rate was learned; the first pulse is not a probe')
        self.assertAlmostEqual(again.rate, model.rate)

    def test_the_file_puts_a_lost_rate_back_into_memcache(self):
        """memcached restarts with the Pi; the tuning page should still show the rate."""
        memcache = fakes.FakeMemcache()
        settings, constants = self.settings(memcache=memcache)
        main.FeedModel(settings, memcache, constants).learn(0.2, 0.06, False, 0.02)
        self.assertIn('trickler_pulse_rate', memcache)

        fresh = fakes.FakeMemcache()
        model = main.FeedModel(settings, fresh, constants)
        self.assertTrue(model.measured)
        self.assertAlmostEqual(fresh['trickler_pulse_rate'], model.rate)

    def test_memcache_wins_over_the_file(self):
        """memcache is the live copy; the file is what survives."""
        settings, _ = self.settings()
        main.FeedModel(settings).learn(0.2, 0.06, False, 0.02)   # file says 0.3
        memcache = fakes.FakeMemcache({'trickler_pulse_rate': 0.9})
        settings, constants = self.settings(memcache=memcache)
        self.assertAlmostEqual(main.FeedModel(settings, memcache, constants).rate, 0.9)

    def test_both_speeds_are_kept(self):
        settings, _ = self.settings()
        model = main.FeedModel(settings)
        model.learn(0.2, 0.06, False, 0.02)
        model.learn(0.2, 0.30, True, 0.02)
        again = main.FeedModel(settings)
        self.assertAlmostEqual(again.fast_rate, model.fast_rate)
        self.assertAlmostEqual(again.rate, model.rate)

    def test_reset_forgets_both_stores(self):
        memcache = fakes.FakeMemcache()
        settings, constants = self.settings(memcache=memcache)
        model = main.FeedModel(settings, memcache, constants)
        model.learn(0.2, 0.06, False, 0.02)
        model.learn(0.2, 0.30, True, 0.02)

        model.reset(settings)
        self.assertFalse(model.measured)
        self.assertIsNone(model.fast_rate)
        self.assertAlmostEqual(model.rate, float(settings.pulse_rate))
        self.assertNotIn('trickler_pulse_rate', memcache)
        self.assertNotIn('trickler_fast_pulse_rate', memcache)
        self.assertNotIn('', helpers.read_json(self.path))
        self.assertFalse(main.FeedModel(settings).measured)

    def test_new_charge_empties_the_window_and_reseeds_an_unmeasured_rate(self):
        settings, _ = self.settings(persistent=False)
        model = main.FeedModel(settings)
        model.window[False].append((0.1, 0.02))
        model.probe_scale[True] = 4.0
        retuned, _ = self.settings(persistent=False, pulse_rate=0.7)
        model.new_charge(retuned)
        self.assertEqual(len(model.window[False]), 0)
        self.assertEqual(model.probe_scale[True], 1.0)
        self.assertAlmostEqual(model.rate, 0.7, msg='the tuning page changed the guess')

    def test_new_charge_keeps_a_measured_rate(self):
        settings, _ = self.settings(persistent=False)
        model = main.FeedModel(settings)
        model.learn(0.2, 0.06, False, 0.02)
        learned = model.rate
        retuned, _ = self.settings(persistent=False, pulse_rate=0.7)
        model.new_charge(retuned)
        self.assertAlmostEqual(model.rate, learned, msg='a measurement beats a guess')

    def test_profiles_have_their_own_entries(self):
        varget, _ = self.settings(profile='Varget')
        h4350, _ = self.settings(profile='H4350')
        main.FeedModel(varget).learn(0.2, 0.06, False, 0.02)
        main.FeedModel(h4350).learn(0.2, 0.02, False, 0.02)
        data = helpers.read_json(self.path)
        self.assertEqual(set(data), {'Varget', 'H4350'})
        self.assertGreater(main.FeedModel(varget).rate, main.FeedModel(h4350).rate)

    def test_an_unwritable_file_does_not_stop_learning(self):
        settings, _ = self.settings()
        model = main.FeedModel(settings)
        with mock.patch.object(helpers, 'write_json', side_effect=OSError('read-only')):
            with self.assertLogs(level='WARNING'):
                model.learn(0.2, 0.08, False, 0.02)   # 0.4 gn/s, up from the 0.3 seed
        self.assertTrue(model.measured)
        self.assertAlmostEqual(model.rate, 0.4)

    def test_recording_switched_off_means_no_file(self):
        settings, _ = self.settings(persistent=False)
        self.assertEqual(settings.learned_path, '')
        main.FeedModel(settings).learn(0.2, 0.06, False, 0.02)
        self.assertEqual(os.listdir(self.directory), [])

    def test_a_corrupt_file_reads_as_nothing_learned(self):
        settings, _ = self.settings()
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('not json')
        model = main.FeedModel(settings)
        self.assertFalse(model.measured)
        model.learn(0.2, 0.06, False, 0.02)
        self.assertIn('', helpers.read_json(self.path), 'and it is written over')


class FeedModelsTest(unittest.TestCase):
    """One model per profile, for the life of the daemon."""

    def settings(self, profile=''):
        config = fakes.load_config(profiles={profile: {}} if profile else None,
                                   active_profile=profile or None)
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        return main.trickler_settings(config, None, None, scale, scales.ANDScale.Units.GRAINS)

    def test_the_same_profile_gets_the_same_model(self):
        models = main.FeedModels()
        self.assertIs(models.for_settings(self.settings()), models.for_settings(self.settings()))

    def test_different_profiles_get_different_models(self):
        models = main.FeedModels()
        self.assertIsNot(models.for_settings(self.settings('Varget')),
                         models.for_settings(self.settings('H4350')))

    def test_the_feeder_learns_into_the_shared_model(self):
        """What one charge learns, the next one starts from."""
        models = main.FeedModels()
        settings = self.settings()
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.resolution = D('0.02')
        first = main.PulseFeeder(mock.Mock(), scale, settings, model=models.for_settings(settings))
        first._learn(0.2, D('0.06'))
        second = main.PulseFeeder(mock.Mock(), scale, settings, model=models.for_settings(settings))
        self.assertTrue(second._rate_measured)
        self.assertAlmostEqual(second.rate, first.rate)
        self.assertEqual(len(second.model.window[False]), 0, 'the window is still per charge')


if __name__ == '__main__':
    unittest.main()
