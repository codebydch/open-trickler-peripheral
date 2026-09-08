"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral
"""
import logging
import helpers
import argparse
import configparser
import enum

from flask import Flask, render_template, request, redirect, url_for, jsonify
from pymemcache.client import base
from decimal import Decimal, InvalidOperation

# Default argument values.
DEFAULTS = dict(
    verbose = False,
)

parser = argparse.ArgumentParser(description='Run OpenTrickler Flask App.')
parser.add_argument('--target_weight', type=Decimal, default=0.0)
parser.add_argument('config_file')
# default=None so "not given" can be told from "given as false": with
# store_true alone the flag is False when absent, and `args.verbose is not
# None` was then always true, so the config file's verbose never applied.
parser.add_argument('--verbose', action='store_true', default=None)
parser.add_argument('--auto_mode', action='store_true')
args = parser.parse_args()
    
config = configparser.ConfigParser()
config.optionxform = str
if args.config_file:
    config.read(args.config_file)

# Order of priority is 1) command-line argument, 2) config file, 3) default.
VERBOSE = DEFAULTS['verbose'] or config['general']['verbose']
if args.verbose is not None:
    VERBOSE = args.verbose

# Configure Python logging.
LOG_LEVEL = logging.INFO
if VERBOSE:
    LOG_LEVEL = logging.DEBUG
helpers.setup_logging(LOG_LEVEL)  
    
logging.info('Starting OpenTrickler Flask App daemon...')
target_weight = Decimal('0.0')
if args.target_weight is not None:
    target_weight = args.target_weight
 
logging.info('Target Weight is set as %s', target_weight)
auto_mode = False
if args.auto_mode is not None:
    auto_mode = args.auto_mode   

logging.info('Auto Mode is set as %s', auto_mode)

app = Flask(__name__)
memcache_client = helpers.get_mc_client()
constants = enum.Enum('memcache_vars', config['memcache_vars'])

def get_memcache_value(key, default):
    value = memcache_client.get(key)
    if value is None:
        return default
    else:
        return value

def set_memcache_value(key, value):
    logging.info('Changing %s to %s', key, value)
    memcache_client.set(key, value)

def safe_get(key, default=None):
    """Reads a memcache value, treating anything unreadable as absent.

    Some values are pickled objects written by the trickler process; if one can't be
    unpickled here the status display should just go quiet rather than 500.
    """
    try:
        value = memcache_client.get(key)
    except Exception:
        logging.debug('Could not read %s from memcache.', key, exc_info=True)
        return default
    return default if value is None else value


def current_trickler_settings():
    """The tuning values in force: live overrides first, then config file, then defaults.

    Returns (values, live) where live says whether any value is currently overridden
    from this page rather than coming from the config file.
    """
    overrides = safe_get(constants.TRICKLER_SETTINGS.value, {})
    if not isinstance(overrides, dict):
        overrides = {}
    configured = dict(config['trickler']) if config.has_section('trickler') else {}
    values = {}
    for setting in helpers.TRICKLER_SETTINGS:
        values[setting.name] = str(
            overrides.get(setting.name, configured.get(setting.name, setting.default)))
    return values, bool(overrides)


def active_profile():
    """The powder profile in force: the live selection, else the config file's."""
    selected = safe_get(constants.ACTIVE_PROFILE.value)
    if selected is None and config.has_section('profiles'):
        selected = config['profiles'].get('active', '')
    return selected or ''


def learned_rate(profile=None, fast=False):
    """A feed rate learned for a profile, or None if it hasn't learned one yet.

    One per pulse speed: the fine speed that finishes a charge, and the fast one used
    while there is still a way to go. They are measured separately because a vibratory
    feeder's throughput against drive is not reliably linear.
    """
    profile = active_profile() if profile is None else profile
    key = (constants.TRICKLER_FAST_PULSE_RATE.value if fast
           else constants.TRICKLER_PULSE_RATE.value)
    return safe_get('%s:%s' % (key, profile) if profile else key)


def history_path():
    """Where the trickler is writing charge history, or '' if it is switched off."""
    if not config.has_section('history'):
        return ''
    if not config['history'].getboolean('enabled', True):
        return ''
    return config['history'].get('path', '')


def render_config(errors=None, notice=None):
    """Renders the tuning page with whatever values are currently in force."""
    values, live = current_trickler_settings()
    return render_template(
        'config.html',
        settings=helpers.TRICKLER_SETTINGS,
        values=values,
        live=live,
        errors=errors or {},
        notice=notice,
        profiles=helpers.list_profiles(config),
        profile=active_profile(),
        learned_rate=learned_rate(),
        learned_fast_rate=learned_rate(fast=True))


@app.route('/app/history')
def history():
    """Every charge the trickler has recorded, with the spread that matters."""
    rows = helpers.read_charges(history_path()) if history_path() else []
    wanted = request.args.get('profile', '')
    if wanted:
        rows = [row for row in rows if row.get('profile') == wanted]
    return render_template(
        'history.html',
        rows=list(reversed(rows))[:100],
        stats=helpers.charge_statistics(rows),
        profiles=sorted({row.get('profile', '') for row in helpers.read_charges(history_path())} - {''}) if history_path() else [],
        selected=wanted,
        enabled=bool(history_path()))


@app.route('/app/history.json')
def history_json():
    """The same data as /app/history, for anything that would rather have it raw."""
    rows = helpers.read_charges(history_path()) if history_path() else []
    return jsonify(rows=rows, stats=helpers.charge_statistics(rows))


@app.route('/app/profile', methods=['POST'])
def update_profile():
    """Selects, saves or deletes a powder profile."""
    name = (request.form.get('profile') or '').strip()

    if 'select' in request.form:
        set_memcache_value(constants.ACTIVE_PROFILE.value, name)
        notice = 'Using %s.' % name if name else 'Using the plain [trickler] settings.'
        try:
            helpers.update_ini_section(args.config_file, 'profiles', {'active': name})
        except OSError as exc:
            notice += ' It will not survive a restart: %s could not be written (%s).' % (
                args.config_file, exc)
        return render_config(notice=notice)

    if 'save' in request.form:
        if not name:
            return render_config(notice='Give the profile a name before saving it.')
        # Store the values currently in force, plus whatever rate has been learned, so a
        # profile captures a setup that is known to work.
        values, _ = current_trickler_settings()
        rate = learned_rate(name) or learned_rate()
        if rate:
            values['pulse_rate'] = '%g' % float(rate)
        # Only the fine rate is stored: it is the one [trickler] has a setting for.
        # The fast rate lives in memcache and is re-measured with a single probe pulse
        # after a reboot, which costs less than another setting to keep in step.
        try:
            helpers.update_ini_section(args.config_file, helpers.profile_section(name), values)
            config.read(args.config_file)
        except OSError as exc:
            return render_config(notice='Could not write %s: %s' % (args.config_file, exc))
        set_memcache_value(constants.ACTIVE_PROFILE.value, name)
        return render_config(
            notice='Saved %s, including a learned feed rate of %s.'
                   % (name, values.get('pulse_rate', 'the configured default')))

    return render_config(notice='Nothing to do.')


@app.route('/app/config/')
def trickler_config():
    """Tuning page for the trickler's final-approach settings."""
    return render_config()


@app.route('/app/config/update', methods=['POST'])
def update_trickler_config():
    """Applies submitted tuning values live, and writes them back to the config file."""
    if 'reset_learned' in request.form:
        profile = active_profile()
        for key in (constants.TRICKLER_PULSE_RATE.value,
                    constants.TRICKLER_FAST_PULSE_RATE.value):
            memcache_client.delete('%s:%s' % (key, profile) if profile else key)
        logging.info('Cleared the learned pulse rates.')
        return render_config(
            notice='Learned pulse rates cleared. The next charge starts from the '
                   'starting feed rate below and measures both speeds again.')

    if 'reset_overrides' in request.form:
        memcache_client.delete(constants.TRICKLER_SETTINGS.value)
        logging.info('Cleared live trickler setting overrides.')
        return render_config(notice='Reverted to the values in the config file.')

    current, _ = current_trickler_settings()
    values, errors = helpers.clean_trickler_settings(request.form, current)
    set_memcache_value(constants.TRICKLER_SETTINGS.value, values)

    notice = 'Applied. The next charge will use these values.'
    try:
        helpers.update_ini_section(args.config_file, 'trickler', values)
    except OSError as exc:
        logging.warning('Could not write %s: %s', args.config_file, exc)
        notice = ('Applied for now, but %s could not be written (%s), so these values '
                  'will be lost on restart.' % (args.config_file, exc))
    return render_config(errors=errors, notice=notice)


@app.route('/app/status')
def status():
    """Live readings for the tuning page, polled by the browser."""
    weight = safe_get(constants.SCALE_WEIGHT.value)
    speed = safe_get(constants.TRICKLER_MOTOR_SPEED.value)
    rate = learned_rate()
    fast_rate = learned_rate(fast=True)
    target = safe_get(constants.TARGET_WEIGHT.value)
    return jsonify(
        scale_weight=None if weight is None else str(weight),
        scale_is_stable=bool(safe_get(constants.SCALE_IS_STABLE.value, False)),
        target_weight=None if target is None else str(target),
        auto_mode=bool(safe_get(constants.AUTO_MODE.value, False)),
        motor_speed=None if speed is None else round(float(speed), 3),
        pulse_rate=None if rate is None else round(float(rate), 4),
        fast_pulse_rate=None if fast_rate is None else round(float(fast_rate), 4),
        profile=active_profile(),
        # Set when the trickler stopped because the powder measure dropped nothing.
        dump_error=safe_get(constants.DUMP_ERROR.value, '') or '')


@app.route('/app/')
def index():
    target_weight = get_memcache_value('target_weight', Decimal('0.00'))
    auto_mode = get_memcache_value('auto_mode', False)
    return render_template('index.html', target_weight=target_weight, auto_mode=auto_mode,
                           dump_error=safe_get(constants.DUMP_ERROR.value, '') or '')

@app.route('/app/update', methods=['POST'])
def update():
    if 'set_weight' in request.form:
        weight_str = request.form['target_weight']
        try:
            target_weight = Decimal(weight_str).quantize(Decimal('0.01'))
            set_memcache_value('target_weight', target_weight)
        except InvalidOperation:
            pass  # Handle invalid input gracefully
    elif 'toggle' in request.form:
        auto_mode = not get_memcache_value('auto_mode', False)
        set_memcache_value('auto_mode', auto_mode)
        if auto_mode:
            # Switching auto mode back on is how you say the jam is cleared -- the
            # trickler switched it off itself when it gave up on the measure.
            set_memcache_value(constants.DUMP_ERROR.value, '')
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)