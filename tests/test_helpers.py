"""Settings validation and the in-place config file rewrite."""
import configparser
import os
import shutil
import tempfile
import unittest

import helpers

from tests import CONFIG_PATH


class CleanSettingsTest(unittest.TestCase):
    """The tuning page must not be able to command something the hardware won't take."""

    def setUp(self):
        self.current = {s.name: s.default for s in helpers.TRICKLER_SETTINGS}

    def test_out_of_range_is_clamped_not_refused(self):
        values, errors = helpers.clean_trickler_settings({'pulse_pwm': '250'}, self.current)
        self.assertEqual(values['pulse_pwm'], '100')
        self.assertIn('clamped', errors['pulse_pwm'])

    def test_negative_is_clamped(self):
        values, _ = helpers.clean_trickler_settings({'cutoff_weight': '-5'}, self.current)
        self.assertEqual(values['cutoff_weight'], '0')

    def test_junk_leaves_the_value_alone(self):
        values, errors = helpers.clean_trickler_settings({'pulse_aim': 'banana'}, self.current)
        self.assertEqual(values['pulse_aim'], self.current['pulse_aim'])
        self.assertIn('not a number', errors['pulse_aim'])

    def test_omitted_keys_keep_their_current_value(self):
        values, errors = helpers.clean_trickler_settings({'pulse_pwm': '30'}, self.current)
        self.assertEqual(values['fine_trickle_weight'], self.current['fine_trickle_weight'])
        self.assertEqual(errors, {})

    def test_every_setting_is_always_returned(self):
        """A partial submission must still leave a complete, runnable set."""
        values, _ = helpers.clean_trickler_settings({}, self.current)
        self.assertEqual(set(values), {s.name for s in helpers.TRICKLER_SETTINGS})

    def test_defaults_are_inside_their_own_limits(self):
        for setting in helpers.TRICKLER_SETTINGS:
            with self.subTest(setting=setting.name):
                self.assertGreaterEqual(float(setting.default), setting.minimum)
                self.assertLessEqual(float(setting.default), setting.maximum)


class UpdateIniTest(unittest.TestCase):
    """The config file is mostly comments explaining each value; they have to survive."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.ini')
        os.close(handle)
        shutil.copy(CONFIG_PATH, self.path)
        self.original = open(self.path, encoding='utf-8').read()

    def tearDown(self):
        os.unlink(self.path)

    def _read(self):
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(self.path)
        return config

    def test_comments_survive(self):
        helpers.update_ini_section(self.path, 'trickler', {'pulse_pwm': '27'})
        updated = open(self.path, encoding='utf-8').read()
        self.assertEqual(self.original.count('#'), updated.count('#'))

    def test_only_the_named_keys_change(self):
        before = self._read()
        helpers.update_ini_section(self.path, 'trickler', {'pulse_pwm': '27'})
        after = self._read()
        self.assertEqual(after['trickler']['pulse_pwm'], '27')
        for key in before['trickler']:
            if key != 'pulse_pwm':
                self.assertEqual(before['trickler'][key], after['trickler'][key])

    def test_other_sections_are_untouched(self):
        before = self._read()
        helpers.update_ini_section(self.path, 'trickler', {'cutoff_weight': '0.05'})
        after = self._read()
        self.assertEqual(dict(before['motor1']), dict(after['motor1']))
        self.assertEqual(dict(before['memcache_vars']), dict(after['memcache_vars']))

    def test_a_key_not_yet_present_is_added(self):
        helpers.update_ini_section(self.path, 'trickler', {'brand_new_key': '1.25'})
        self.assertEqual(self._read()['trickler']['brand_new_key'], '1.25')

    def test_a_missing_section_is_created(self):
        helpers.update_ini_section(self.path, 'history', {'path': '/tmp/charges.csv'})
        self.assertEqual(self._read()['history']['path'], '/tmp/charges.csv')


class ShippedConfigTest(unittest.TestCase):
    """The config in the repo and the built-in fallbacks must not drift apart."""

    def test_shipped_config_matches_the_schema_defaults(self):
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(CONFIG_PATH)
        for setting in helpers.TRICKLER_SETTINGS:
            with self.subTest(setting=setting.name):
                self.assertIn(setting.name, config['trickler'],
                              'shipped config is missing a tunable setting')
                self.assertEqual(float(config['trickler'][setting.name]),
                                 float(setting.default))


if __name__ == '__main__':
    unittest.main()
