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
parser.add_argument('--verbose', action='store_true')
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
    except Exception: # pylint: disable=broad-except;
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


def render_config(errors=None, notice=None):
    """Renders the tuning page with whatever values are currently in force."""
    values, live = current_trickler_settings()
    return render_template(
        'config.html',
        settings=helpers.TRICKLER_SETTINGS,
        values=values,
        live=live,
        errors=errors or {},
        notice=notice)


@app.route('/app/config/')
def trickler_config():
    """Tuning page for the trickler's final-approach settings."""
    return render_config()


@app.route('/app/config/update', methods=['POST'])
def update_trickler_config():
    """Applies submitted tuning values live, and writes them back to the config file."""
    if 'reset_learned' in request.form:
        memcache_client.delete(constants.TRICKLER_PULSE_RATE.value)
        logging.info('Cleared the learned pulse rate.')
        return render_config(
            notice='Learned pulse rate cleared. The next charge starts from the '
                   'starting feed rate below and learns again from there.')

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
    rate = safe_get(constants.TRICKLER_PULSE_RATE.value)
    target = safe_get(constants.TARGET_WEIGHT.value)
    return jsonify(
        scale_weight=None if weight is None else str(weight),
        scale_is_stable=bool(safe_get(constants.SCALE_IS_STABLE.value, False)),
        target_weight=None if target is None else str(target),
        auto_mode=bool(safe_get(constants.AUTO_MODE.value, False)),
        motor_speed=None if speed is None else round(float(speed), 3),
        pulse_rate=None if rate is None else round(float(rate), 4))


@app.route('/app/')
def index():
    target_weight = get_memcache_value('target_weight', Decimal('0.00'))
    auto_mode = get_memcache_value('auto_mode', False)
    return render_template('index.html', target_weight=target_weight, auto_mode=auto_mode)

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
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)