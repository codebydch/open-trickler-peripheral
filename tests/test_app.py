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
        # The copy that survives a reboot is the daemon's, so it is asked to forget it.
        self.assertEqual(self.memcache['trickler_command'],
                         {'command': 'reset_learned', 'profile': ''})


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


class HistoryPageTest(AppTestCase):

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, 'charges.csv')
        self.pulses = os.path.join(self.directory, 'pulses.csv')
        helpers.update_ini_section(self.ini, 'history', {
            'enabled': 'True', 'path': self.path, 'pulses_path': self.pulses})
        app.config.read(self.ini)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def record(self, error, outcome='complete', profile='', landed=''):
        helpers.append_charge(self.path, {
            'timestamp': '2026-09-01T10:00:00', 'profile': profile, 'outcome': outcome,
            'target': '45.00', 'final': '45.01', 'error': str(error), 'unit': 'GRAINS',
            'pulses': '6', 'seconds': '7.2', 'learned_rate': '0.28', 'landed': landed})

    def test_the_page_shows_what_landed(self):
        self.record(-0.02, landed='45.04')
        body = self.client.get('/app/history').get_data(as_text=True)
        self.assertIn('45.04', body)
        self.assertIn('+0.040', body, 'the error shown is against what landed')
        self.assertIn('Landed heavy', body)

    def test_empty_history_says_so_rather_than_failing(self):
        page = self.client.get('/app/history')
        self.assertEqual(page.status_code, 200)
        self.assertIn('No completed charges', page.get_data(as_text=True))

    def test_shows_the_statistics(self):
        for error in (-0.01, 0.00, 0.01, 0.30):
            self.record(error)
        body = self.client.get('/app/history').get_data(as_text=True)
        # Three of four inside the default 0.02 tolerance.
        self.assertIn('75%', body)

    def test_filters_by_profile(self):
        self.record(0.01, profile='Varget')
        self.record(0.40, profile='H4350')
        body = self.client.get('/app/history?profile=Varget').get_data(as_text=True)
        self.assertIn('+0.010', body)
        self.assertNotIn('+0.400', body)

    def test_json_endpoint(self):
        self.record(0.01)
        payload = json.loads(self.client.get('/app/history.json').get_data(as_text=True))
        self.assertEqual(len(payload['rows']), 1)
        self.assertEqual(payload['stats']['count'], 1)

    def record_pulses(self, count, on_time, dose, profile=''):
        helpers.append_pulses(self.pulses, [{
            'timestamp': '2026-09-01T10:00:00', 'profile': profile, 'pwm': '25',
            'on_time': str(on_time), 'moving_time': str(on_time - 0.12),
            'remainder': '0.30', 'dose': str(dose), 'rate': '0.2', 'unit': 'GRAINS',
        } for _ in range(count)])

    def test_the_page_shows_the_pulse_fit(self):
        # 0.02 gn at 0.2 s and 0.06 at 0.4 s: 0.2 gn/s, and a spin-up of 0.1 s.
        self.record_pulses(12, 0.2, 0.02)
        self.record_pulses(12, 0.4, 0.06)
        body = self.client.get('/app/history').get_data(as_text=True)
        self.assertIn('id="pulse-fit"', body)
        self.assertIn('0.200', body)
        self.assertIn('0.100', body)

    def test_the_page_says_what_is_missing_for_a_fit(self):
        self.record_pulses(12, 0.2, 0.02)
        body = self.client.get('/app/history').get_data(as_text=True)
        self.assertIn('id="pulse-fit"', body)
        self.assertIn('two different lengths', body)

    def test_no_pulses_means_no_pulse_section(self):
        body = self.client.get('/app/history').get_data(as_text=True)
        self.assertNotIn('id="pulse-fit"', body)

    def test_the_fit_is_per_profile(self):
        self.record_pulses(12, 0.2, 0.02, profile='Varget')
        self.record_pulses(12, 0.4, 0.06, profile='Varget')
        self.record_pulses(12, 0.4, 0.30, profile='H4350')
        payload = json.loads(
            self.client.get('/app/pulses.json?profile=Varget').get_data(as_text=True))
        self.assertEqual(len(payload['rows']), 24)
        self.assertAlmostEqual(payload['fit']['rate'], 0.2, places=6)
        everything = json.loads(self.client.get('/app/pulses.json').get_data(as_text=True))
        self.assertEqual(len(everything['rows']), 36)


class ProfilePageTest(AppTestCase):

    def test_selecting_a_profile_sets_it_live_and_persists_it(self):
        helpers.update_ini_section(self.ini, 'profile:Varget', {'pulse_rate': '0.42'})
        app.config.read(self.ini)
        self.client.post('/app/profile', data={'profile': 'Varget', 'select': '1'})

        self.assertEqual(self.memcache['active_profile'], 'Varget')
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(self.ini)
        self.assertEqual(config['profiles']['active'], 'Varget')

    def test_saving_captures_the_settings_and_the_learned_rate(self):
        self.memcache['trickler_pulse_rate'] = 0.375
        self.client.post('/app/profile', data={'profile': 'Varget', 'save': '1'})

        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(self.ini)
        self.assertIn('profile:Varget', config.sections())
        self.assertEqual(float(config['profile:Varget']['pulse_rate']), 0.375)
        self.assertEqual(self.memcache['active_profile'], 'Varget')

    def test_saving_without_a_name_is_refused(self):
        body = self.client.post('/app/profile', data={'save': '1'}).get_data(as_text=True)
        self.assertIn('Give the profile a name', body)

    def test_clearing_the_learned_rate_clears_the_active_profile_one(self):
        self.memcache['active_profile'] = 'Varget'
        self.memcache['trickler_pulse_rate:Varget'] = 0.44
        self.memcache['trickler_pulse_rate'] = 0.11
        self.client.post('/app/config/update', data={'reset_learned': '1'})
        self.assertNotIn('trickler_pulse_rate:Varget', self.memcache)
        self.assertIn('trickler_pulse_rate', self.memcache,
                      'the unscoped rate belongs to a different profile')

    def test_status_reports_the_profile_scoped_rate(self):
        self.memcache['active_profile'] = 'Varget'
        self.memcache['trickler_pulse_rate:Varget'] = 0.44
        self.memcache['trickler_pulse_rate'] = 0.11
        status = json.loads(self.client.get('/app/status').get_data(as_text=True))
        self.assertEqual(status['pulse_rate'], 0.44)
        self.assertEqual(status['profile'], 'Varget')


class CalibratePageTest(AppTestCase):
    """Starting, following, reviewing and applying a calibration from the browser."""

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.learned = os.path.join(self.directory, 'learned.json')
        helpers.update_ini_section(self.ini, 'history', {
            'enabled': 'True', 'path': os.path.join(self.directory, 'charges.csv'),
            'learned_path': self.learned})
        app.config.read(self.ini)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    @staticmethod
    def results(profile='Varget', **extra):
        """A finished routine's results, shaped as calibrate.py publishes them."""
        def candidate(pwm, fast, seconds, heavy, meets):
            return {'settings': {'pulse_pwm': pwm, 'pulse_fast_pwm': fast,
                                 'pulse_fast_until': 0.1, 'pulse_on_time': 0.25,
                                 'pulse_trickle_weight': 0.5, 'pulse_aim': 0.85},
                    'prediction': {'seconds': seconds, 'heavy': heavy, 'light': 0.3,
                                   'unfinished': 0, 'pulses': 6.0},
                    'meets_limit': meets}
        recommended = candidate(45.0, 45.0, 12.8, 0.22, True)
        recommended['settings'].update(stall_pwm=18.0, pulse_rate=0.25, pulse_dead_time=0.12)
        return dict({
            'profile': profile, 'capacity': 180.0, 'stall_pwm': 18.0,
            'continuous_rate': 0.31, 'pulses': 90,
            'cells': [{'speed': 30.0, 'on_time': 0.25, 'pulses': 10, 'zeros': 0.4,
                       'mean_dose': 0.034, 'bursts': 0.1, 'max_dose': 0.08,
                       'mean_tail': 0.004}],
            'rates': {'30.0': 0.15, '45.0': 0.25},
            'dead_times': {'30.0': 0.11, '45.0': 0.12},
            'recommendation': {
                'heavy_limit': 0.25,
                'current': candidate(30.0, 45.0, 19.1, 0.3, False),
                'recommended': recommended,
                'runners_up': [candidate(30.0, 45.0, 14.0, 0.2, True)],
                'evaluated': 162,
            }}, **extra)

    def status(self, phase='sweep', finished=False, prompt=None, results=None):
        self.memcache['calibration_status'] = {
            'phase': phase, 'progress': 0.5, 'message': 'Sweeping.', 'prompt': prompt,
            'pulses_done': 40, 'pulses_total': 90, 'profile': 'Varget',
            'results': results, 'error': None, 'finished': finished}

    def test_the_page_renders_with_nothing_to_show(self):
        page = self.client.get('/app/calibrate/')
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('No calibration', body)
        self.assertIn('value="200"', body, 'the shipped capacity')

    def test_start_sends_the_request_and_remembers_the_capacity(self):
        body = self.client.post('/app/calibrate/start', data={
            'profile': '', 'new_profile': 'Varget', 'capacity': '180',
            'pulses_per_cell': '12'}, follow_redirects=True).get_data(as_text=True)
        self.assertEqual(self.memcache['trickler_command'], {
            'command': 'calibrate', 'profile': 'Varget', 'capacity': 180.0,
            'pulses_per_cell': 12})
        self.assertEqual(self.memcache['active_profile'], 'Varget')
        self.assertEqual(self.memcache['calibration_status']['phase'], 'requested')
        self.assertIn('requested', body)
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(self.ini)
        self.assertEqual(config['calibration']['capacity'], '180')

    def test_start_needs_a_sensible_capacity(self):
        body = self.client.post('/app/calibrate/start', data={
            'profile': '', 'capacity': '5', 'pulses_per_cell': '10'}).get_data(as_text=True)
        self.assertNotIn('trickler_command', self.memcache)
        self.assertIn('Not started', body)

    def test_start_is_refused_while_one_is_running(self):
        self.status()
        body = self.client.post('/app/calibrate/start', data={
            'profile': '', 'capacity': '200', 'pulses_per_cell': '10'}).get_data(as_text=True)
        self.assertNotIn('trickler_command', self.memcache)
        self.assertIn('already running', body)

    def test_start_is_refused_with_auto_mode_on(self):
        self.memcache['auto_mode'] = True
        body = self.client.post('/app/calibrate/start', data={
            'profile': '', 'capacity': '200', 'pulses_per_cell': '10'}).get_data(as_text=True)
        self.assertNotIn('trickler_command', self.memcache)
        self.assertIn('auto mode off', body)

    def test_the_status_endpoint_mirrors_the_daemon(self):
        self.status(prompt='empty_container')
        self.memcache['scale_weight'] = D('178.42')
        status = json.loads(self.client.get('/app/calibrate/status').get_data(as_text=True))
        self.assertEqual(status['phase'], 'sweep')
        self.assertEqual(status['prompt'], 'empty_container')
        self.assertTrue(status['running'])
        self.assertEqual(status['scale_weight'], '178.42')

    def test_continue_and_abort_are_commands(self):
        self.status()
        self.client.post('/app/calibrate/continue')
        self.assertEqual(self.memcache['trickler_command'], {'command': 'calibrate_continue'})
        self.client.post('/app/calibrate/abort')
        self.assertEqual(self.memcache['trickler_command'], {'command': 'calibrate_abort'})

    def test_aborting_a_request_the_daemon_has_not_taken_withdraws_it(self):
        self.client.post('/app/calibrate/start', data={
            'profile': '', 'capacity': '200', 'pulses_per_cell': '10'})
        self.client.post('/app/calibrate/abort')
        self.assertNotIn('trickler_command', self.memcache)
        self.assertNotIn('calibration_status', self.memcache)

    def test_results_render_from_the_daemon_report(self):
        self.status(phase='done', finished=True, results=self.results())
        body = self.client.get('/app/calibrate/').get_data(as_text=True)
        self.assertIn('id="cells"', body)
        self.assertIn('id="recommendation"', body)
        self.assertIn('12.8', body, 'the predicted seconds')
        self.assertIn('value="180"', body, 'the capacity the routine ran with')
        self.assertIn('Discard', body)

    def test_results_render_from_the_record_when_memcache_has_forgotten(self):
        helpers.write_json(self.learned, {'Varget': {
            'rate': 0.15, 'calibration': self.results(updated='2026-10-03T21:00:00')}})
        body = self.client.get('/app/calibrate/?profile=Varget').get_data(as_text=True)
        self.assertIn('Last calibration', body)
        self.assertIn('2026-10-03T21:00:00', body)
        self.assertNotIn('Discard', body, 'nothing in memcache to discard')

    def test_a_failed_run_is_reported(self):
        self.memcache['calibration_status'] = {
            'phase': 'failed', 'finished': True, 'prompt': None, 'results': None,
            'error': 'No powder arrived in 20 s.', 'message': 'Failed.', 'progress': 0.1}
        body = self.client.get('/app/calibrate/').get_data(as_text=True)
        self.assertIn('No powder arrived', body)

    def test_dismiss_clears_the_report_but_not_the_record(self):
        helpers.write_json(self.learned, {'Varget': {'calibration': self.results()}})
        self.status(phase='done', finished=True, results=self.results())
        self.client.post('/app/calibrate/dismiss')
        self.assertNotIn('calibration_status', self.memcache)
        self.assertIn('calibration', helpers.read_json(self.learned)['Varget'])

    def test_apply_puts_the_values_in_force_and_saves_them_to_the_profile(self):
        self.status(phase='done', finished=True, results=self.results())
        form = {key: str(value) for key, value in
                self.results()['recommendation']['recommended']['settings'].items()}
        form['save_profile'] = 'Varget'
        body = self.client.post('/app/calibrate/apply', data=form,
                                follow_redirects=True).get_data(as_text=True)
        self.assertEqual(self.memcache['trickler_settings']['pulse_pwm'], '45')
        self.assertEqual(self.memcache['trickler_settings']['stall_pwm'], '18')
        self.assertEqual(self.memcache['active_profile'], 'Varget')
        self.assertEqual(self.memcache['trickler_command'], {
            'command': 'calibrate_apply', 'profile': 'Varget',
            'pulse_pwm': 45.0, 'pulse_fast_pwm': 45.0})
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(self.ini)
        self.assertEqual(config['profile:Varget']['pulse_pwm'], '45')
        self.assertEqual(config['profile:Varget']['pulse_trickle_weight'], '0.5')
        self.assertIn('[profile:Varget]', body)

    def test_apply_without_a_name_writes_the_plain_section(self):
        form = {'pulse_pwm': '35', 'save_profile': ''}
        self.client.post('/app/calibrate/apply', data=form)
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(self.ini)
        self.assertEqual(config['trickler']['pulse_pwm'], '35')
        self.assertEqual(self.memcache['trickler_command']['profile'], '')

    def test_apply_clamps_like_the_tuning_page(self):
        body = self.client.post('/app/calibrate/apply', data={
            'pulse_pwm': '999', 'save_profile': ''}, follow_redirects=True).get_data(as_text=True)
        self.assertEqual(self.memcache['trickler_settings']['pulse_pwm'], '100')
        self.assertIn('adjusted', body)

    def test_every_post_that_acts_leaves_the_browser_on_a_get(self):
        """The page fetches itself again when the routine finishes. On the first bench
        run it was left on the result of the Start form, and that fetch re-submitted
        it: the moment one calibration was done, another began."""
        for path, form in (('/app/calibrate/start', {'profile': 'Varget', 'capacity': '200',
                                                     'pulses_per_cell': '10'}),
                           ('/app/calibrate/apply', {'pulse_pwm': '30', 'save_profile': 'Varget'}),
                           ('/app/calibrate/dismiss', {})):
            self.memcache.clear()
            response = self.client.post(path, data=form)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn('/app/calibrate/?', response.headers['Location'], path)
            self.assertIn('profile=Varget', response.headers['Location'], path)
        self.client.post('/app/calibrate/start', data={
            'profile': '', 'capacity': '200', 'pulses_per_cell': '10'})
        response = self.client.post('/app/calibrate/abort')
        self.assertEqual(response.status_code, 302, 'withdrawing a request')

    def test_the_page_refreshes_itself_with_a_get_not_a_reload(self):
        body = self.client.get('/app/calibrate/').get_data(as_text=True)
        self.assertNotIn('reload(', body)
        self.assertIn("location.replace(", body)

    def test_the_notice_survives_the_redirect(self):
        body = self.client.get('/app/calibrate/?notice=Hello+there').get_data(as_text=True)
        self.assertIn('Hello there', body)

    def test_the_tuning_page_links_here(self):
        body = self.client.get('/app/config/').get_data(as_text=True)
        self.assertIn('/app/calibrate/', body)
