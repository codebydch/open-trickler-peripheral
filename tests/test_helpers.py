"""Settings validation, the in-place config file rewrite, and the log level."""
import configparser
import logging
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

    def test_a_key_with_an_empty_value_is_replaced_cleanly(self):
        """[profiles] active is empty by default. A separator pattern that swallows the
        newline writes the value onto its own line and corrupts the file."""
        helpers.update_ini_section(self.path, 'profiles', {'active': 'Varget'})
        self.assertEqual(self._read()['profiles']['active'], 'Varget')

    def test_a_missing_section_is_created(self):
        helpers.update_ini_section(self.path, 'profile:Varget', {'pulse_rate': '0.42'})
        self.assertEqual(self._read()['profile:Varget']['pulse_rate'], '0.42')


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


class LoadConfigTest(unittest.TestCase):
    """Reading the config file, and what happens when it isn't there.

    This is the failure that took four services down at once: the live config stopped
    being tracked, a `git pull` therefore removed it from checkouts that had it, and
    every daemon died with `KeyError: 'general'` -- a section name, from a file that was
    not on disk, with the path it wanted nowhere in the traceback.
    """

    def test_a_real_config_loads(self):
        config = helpers.load_config(CONFIG_PATH)
        self.assertIn('general', config)
        self.assertIn('trickler', config)

    def test_key_case_is_preserved(self):
        """memcache_vars is turned into an enum of its keys, so lower-casing them
        silently renames every variable the daemons share."""
        config = helpers.load_config(CONFIG_PATH)
        self.assertTrue(any(key != key.lower() for key in config['memcache_vars']),
                        'expected at least one capitalised key to check against')

    def test_a_missing_file_says_which_file(self):
        path = os.path.join(tempfile.gettempdir(), 'opentrickler-not-here.ini')
        self.assertFalse(os.path.exists(path))
        with self.assertRaises(SystemExit) as caught:
            helpers.load_config(path)
        self.assertIn(path, str(caught.exception))

    def test_a_missing_file_says_how_to_fix_it(self):
        """The journal is where this gets read, so the message has to carry the fix."""
        with self.assertRaises(SystemExit) as caught:
            helpers.load_config('/nonexistent/opentrickler_config.ini')
        self.assertIn('.example', str(caught.exception))

    def test_a_file_that_cannot_be_read_is_not_silently_empty(self):
        """A directory stands in for any unopenable path -- wrong permissions, a broken
        symlink. configparser skips them all as quietly as a missing file."""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                helpers.load_config(directory)

    def test_an_empty_file_is_not_an_error(self):
        """It parses; it just has no sections. Whatever reads a section next is what
        should complain, and it can say which section it wanted."""
        handle, path = tempfile.mkstemp(suffix='.ini')
        os.close(handle)
        self.addCleanup(os.unlink, path)
        self.assertEqual(helpers.load_config(path).sections(), [])


class ShippedDefaultsTest(unittest.TestCase):
    """Relationships between the shipped tuning values that have to hold on any machine.

    Not a taste test -- someone tuning their own trickler can set whatever they like from
    the control panel. These are the ones where a shipped default that breaks the
    relationship makes the machine visibly worse out of the box.
    """

    def setUp(self):
        self.defaults = {s.name: float(s.default) for s in helpers.TRICKLER_SETTINGS}

    def test_the_shortest_pulse_outlasts_the_motor_spin_up(self):
        """A pulse that ends before the motor has started moving powder delivers
        nothing, however many times it is fired. Shipped as 0.03 s against a spin-up
        measured at 0.118, which is how the last few grains of a charge turned into a
        run of pulses that each did nothing.
        """
        self.assertGreater(self.defaults['pulse_min_on_time'],
                           self.defaults['pulse_dead_time'],
                           'the shortest pulse must be longer than the spin-up')

    def test_the_longest_pulse_can_place_more_than_one_grain(self):
        """Otherwise the cap, not the feed rate, decides how long a charge takes."""
        moving = self.defaults['pulse_on_time'] - self.defaults['pulse_dead_time']
        self.assertGreater(moving, 2 * (self.defaults['pulse_min_on_time'] -
                                        self.defaults['pulse_dead_time']))

    def test_the_feed_rate_window_spans_more_than_the_scale_step(self):
        """The control loop reads several times faster than the scale updates, so a
        short window measures the scale's 0.02 gn step rather than a feed rate, and
        continuous trickling hands over to the pulse feeder at a different weight every
        charge. Ten samples is roughly half a second at the loop's rate.
        """
        self.assertGreaterEqual(self.defaults['rate_window'], 10)


if __name__ == '__main__':
    unittest.main()


class LogLevelTest(unittest.TestCase):
    """`verbose = False` in the ini has to mean INFO.

    It used to be read as the string 'False', which is true, so every daemon ran at DEBUG
    whatever the file said. The trickler then logged every scale frame, twenty a second,
    and journald rotated the lines worth keeping out of the journal within minutes.
    """

    @staticmethod
    def config(verbose=None):
        config = configparser.ConfigParser()
        config.add_section('general')
        if verbose is not None:
            config['general']['verbose'] = verbose
        return config

    def test_false_in_the_file_means_info(self):
        for value in ('False', 'false', 'no', '0'):
            with self.subTest(value=value):
                self.assertEqual(helpers.log_level(self.config(value)), logging.INFO)

    def test_true_in_the_file_means_debug(self):
        for value in ('True', 'yes', '1'):
            with self.subTest(value=value):
                self.assertEqual(helpers.log_level(self.config(value)), logging.DEBUG)

    def test_nothing_in_the_file_means_info(self):
        self.assertEqual(helpers.log_level(self.config()), logging.INFO)
        self.assertEqual(helpers.log_level(configparser.ConfigParser()), logging.INFO)

    def test_the_command_line_wins(self):
        self.assertEqual(helpers.log_level(self.config('False'), True), logging.DEBUG)
        self.assertEqual(helpers.log_level(self.config('True'), False), logging.INFO)

    def test_junk_means_info_and_says_so(self):
        with self.assertLogs(level='WARNING'):
            self.assertEqual(helpers.log_level(self.config('sometimes')), logging.INFO)

    def test_the_shipped_config_runs_at_info(self):
        self.assertEqual(helpers.log_level(helpers.load_config(CONFIG_PATH)), logging.INFO)


class ProfileKeyTest(unittest.TestCase):
    """memcache keys may not contain whitespace, and profiles are named by people."""

    def test_a_plain_name_is_appended_as_it_was(self):
        self.assertEqual(helpers.profile_key('trickler_pulse_rate', 'Varget'),
                         'trickler_pulse_rate:Varget')

    def test_no_profile_means_the_bare_key(self):
        self.assertEqual(helpers.profile_key('trickler_pulse_rate', ''), 'trickler_pulse_rate')
        self.assertEqual(helpers.profile_key('trickler_pulse_rate', None), 'trickler_pulse_rate')

    def test_spaces_and_punctuation_are_encoded(self):
        key = helpers.profile_key('trickler_pulse_rate', 'Hodgdon H1000 (lot 3)')
        self.assertNotRegex(key, r'[\s()]')
        self.assertEqual(key, 'trickler_pulse_rate:Hodgdon%20H1000%20%28lot%203%29')

    def test_different_names_never_collide(self):
        self.assertNotEqual(helpers.profile_key('k', 'a b'), helpers.profile_key('k', 'a%20b'))
