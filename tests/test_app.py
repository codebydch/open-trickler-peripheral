"""The control panel and tuning page."""
import configparser
import decimal
import json
import os
import shutil
import sys
import tempfile
import unittest

import helpers

from tests import CONFIG_PATH, fakes, quiet_logging


D = decimal.Decimal


# app.py does all of its setup at import time -- argument parsing, config loading and
# building the memcache client -- so it can only be imported once, against one fake. Set
# that up here rather than per test class.
_HANDLE, INI_PATH = tempfile.mkstemp(suffix='.ini')
os.close(_HANDLE)
shutil.copy(CONFIG_PATH, INI_PATH)

MEMCACHE = fakes.FakeMemcache()
helpers.get_mc_client = lambda *a, **k: MEMCACHE
sys.argv = ['app.py', INI_PATH]

import app  # noqa: E402  (imported late, once the fakes above are in place)

# app.py runs logging.basicConfig() at import, which resets the root log level.
quiet_logging()


def tearDownModule():
    os.unlink(INI_PATH)


class AppTestCase(unittest.TestCase):
    """Common setup: a clean memcache and a pristine config file for every test."""

    def setUp(self):
        self.memcache = MEMCACHE
        self.app = app
        self.client = app.app.test_client()
        self.ini = INI_PATH
        self.memcache.clear()
        shutil.copy(CONFIG_PATH, INI_PATH)


class TuningPageTest(AppTestCase):

    def test_renders_every_setting(self):
        page = self.client.get('/app/config/')
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        for setting in helpers.TRICKLER_SETTINGS:
            self.assertIn('name="%s"' % setting.name, body)

    def test_post_applies_live_and_persists(self):
        form = {s.name: s.default for s in helpers.TRICKLER_SETTINGS}
        form['pulse_min_on_time'] = '0.02'
        self.client.post('/app/config/update', data=form)

        self.assertEqual(self.memcache['trickler_settings']['pulse_min_on_time'], '0.02')
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(self.ini)
        self.assertEqual(config['trickler']['pulse_min_on_time'], '0.02')

    def test_post_clamps_and_says_so(self):
        form = {s.name: s.default for s in helpers.TRICKLER_SETTINGS}
        form['pulse_pwm'] = '999'
        body = self.client.post('/app/config/update', data=form).get_data(as_text=True)
        self.assertEqual(self.memcache['trickler_settings']['pulse_pwm'], '100')
        self.assertIn('clamped', body)

    def test_saving_keeps_the_config_comments(self):
        original = open(CONFIG_PATH, encoding='utf-8').read()
        form = {s.name: s.default for s in helpers.TRICKLER_SETTINGS}
        self.client.post('/app/config/update', data=form)
        self.assertEqual(original.count('#'),
                         open(self.ini, encoding='utf-8').read().count('#'))

    def test_revert_drops_the_live_overrides(self):
        self.memcache['trickler_settings'] = {'pulse_pwm': '99'}
        self.client.post('/app/config/update', data={'reset_overrides': '1'})
        self.assertNotIn('trickler_settings', self.memcache)

    def test_clearing_the_learned_rate(self):
        self.memcache['trickler_pulse_rate'] = 0.5
        self.client.post('/app/config/update', data={'reset_learned': '1'})
        self.assertNotIn('trickler_pulse_rate', self.memcache)


class StatusTest(AppTestCase):

    def test_reports_the_live_values(self):
        self.memcache.update({
            'scale_weight': D('44.96'),
            'scale_is_stable': True,
            'target_weight': D('45.00'),
            'auto_mode': True,
            'trickler_motor_speed': 0.25,
            'trickler_pulse_rate': 0.8123456,
        })
        status = json.loads(self.client.get('/app/status').get_data(as_text=True))
        self.assertEqual(status['scale_weight'], '44.96')
        self.assertEqual(status['target_weight'], '45.00')
        self.assertEqual(status['motor_speed'], 0.25)
        self.assertEqual(status['pulse_rate'], 0.8123)
        self.assertTrue(status['auto_mode'])

    def test_missing_values_are_null_not_an_error(self):
        status = json.loads(self.client.get('/app/status').get_data(as_text=True))
        self.assertIsNone(status['scale_weight'])
        self.assertFalse(status['auto_mode'])

    def test_an_unreadable_value_does_not_fail_the_request(self):
        """Some values are pickled by the trickler process; the page should go quiet
        rather than return a 500 if one cannot be read back."""
        class Exploding(fakes.FakeMemcache):
            def get(self, key, default=None):
                raise ValueError('cannot unpickle')

        original = self.app.memcache_client
        self.app.memcache_client = Exploding()
        try:
            response = self.client.get('/app/status')
            self.assertEqual(response.status_code, 200)
        finally:
            self.app.memcache_client = original


if __name__ == '__main__':
    unittest.main()
