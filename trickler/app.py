"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral
"""
import logging
import helpers
import argparse
import enum

from flask import Flask, render_template, request, redirect, url_for, jsonify
from pymemcache.client import base
from decimal import Decimal, InvalidOperation

parser = argparse.ArgumentParser(description='Run OpenTrickler Flask App.')
parser.add_argument('--target_weight', type=Decimal, default=0.0)
parser.add_argument('config_file')
# default=None so "not given" can be told from "given as false": with
# store_true alone the flag is False when absent, and `args.verbose is not
# None` was then always true, so the config file's verbose never applied.
parser.add_argument('--verbose', action='store_true', default=None)
parser.add_argument('--auto_mode', action='store_true')
args = parser.parse_args()
    
config = helpers.load_config(args.config_file)

# --verbose on the command line wins, else the config file decides.
helpers.setup_logging(helpers.log_level(config, args.verbose))
    
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
    return helpers.history_files(config).charges


def pulses_path():
    """Where the trickler is recording pulses, or '' if recording is switched off."""
    return helpers.history_files(config).pulses


def recorded_pulses(profile='', source=''):
    """Every recorded pulse, oldest first, for one profile or all, one source or all.

    `source` is 'charge' for the final approach of normal charges and 'calibration' for
    the routine; rows written before the column existed count as charges.
    """
    rows = helpers.read_pulses(pulses_path()) if pulses_path() else []
    if profile:
        rows = [row for row in rows if row.get('profile') == profile]
    if source:
        rows = [row for row in rows if (row.get('source') or 'charge') == source]
    return rows


# The settings a calibration recommends, in the order the page shows them. The first six
# are what the recommender moves (powder_model.RECOMMENDED_KEYS, not imported here: that
# module imports the daemon, hardware libraries and all); the last three are what the
# routine measured and the recommendation carries along.
CALIBRATION_FIELDS = ('pulse_pwm', 'pulse_fast_pwm', 'pulse_fast_until', 'pulse_on_time',
                      'pulse_trickle_weight', 'pulse_aim', 'stall_pwm', 'pulse_rate',
                      'pulse_dead_time')
# Phases the daemon reports while the routine is actually running. 'requested' is this
# app's own placeholder between Start and the daemon's first report.
CALIBRATION_RUNNING = ('prime', 'stall', 'reprime', 'sweep', 'paused', 'fit', 'recommending')
DEFAULT_CAPACITY = 200.0


def calibration_status():
    """What the calibration routine last reported, or None if nothing has."""
    status = safe_get(helpers.calibration_status_key(constants))
    return status if isinstance(status, dict) else None


def calibration_running(status):
    """Whether a routine is under way, as far as this app can tell."""
    return bool(status) and (not status.get('finished', True)
                             and (status.get('phase') in CALIBRATION_RUNNING
                                  or status.get('phase') == 'requested'))


def last_calibration(profile):
    """A profile's stored calibration record from learned.json, or None.

    The daemon owns that file; this only reads it, so the page can still show the last
    results after memcached has forgotten them (a reboot, usually).
    """
    path = helpers.history_files(config).learned
    if not path:
        return None
    entry = helpers.read_json(path).get(profile, {})
    results = entry.get('calibration') if isinstance(entry, dict) else None
    return results if isinstance(results, dict) else None


def configured_capacity():
    """The container capacity last used, from [calibration], else the shipped guess."""
    try:
        return float(config.get('calibration', 'capacity', fallback=DEFAULT_CAPACITY))
    except ValueError:
        return DEFAULT_CAPACITY


def configured_pulses_per_cell():
    try:
        return int(config.get('calibration', 'pulses_per_cell', fallback=10))
    except ValueError:
        return 10


def render_calibrate(profile=None, notice=None, errors=None):
    """Renders the calibration page for a profile, with the latest results it has."""
    profile = active_profile() if profile is None else profile
    status = calibration_status()
    results = None
    source = None
    if status and status.get('phase') == 'done' and isinstance(status.get('results'), dict):
        results, source = status['results'], 'status'
    else:
        stored = last_calibration(profile)
        if stored:
            results, source = stored, 'stored'
    capacity = configured_capacity()
    if results and results.get('capacity'):
        capacity = float(results['capacity'])
    recommendation = results.get('recommendation') if results else None
    current, _ = current_trickler_settings()
    proposed = {}
    if recommendation and isinstance(recommendation.get('recommended'), dict):
        proposed = dict(recommendation['recommended'].get('settings') or {})
    for name in CALIBRATION_FIELDS:
        proposed.setdefault(name, current.get(name))
    labels = {setting.name: setting for setting in helpers.TRICKLER_SETTINGS}
    return render_template(
        'calibrate.html',
        profiles=helpers.list_profiles(config),
        profile=profile,
        status=status,
        running=calibration_running(status),
        results=results,
        results_source=source,
        recommendation=recommendation,
        proposed=proposed,
        current=current,
        fields=[labels[name] for name in CALIBRATION_FIELDS],
        capacity='%g' % capacity,
        pulses_per_cell=configured_pulses_per_cell(),
        auto_mode=bool(safe_get(constants.AUTO_MODE.value, False)),
        notice=notice,
        errors=errors or {})


def send_command(command, **fields):
    """Leaves a command for the daemon, which takes it on its next idle pass."""
    memcache_client.set(helpers.command_key(constants), dict(fields, command=command))


def select_profile(name):
    """Makes a profile the live one and remembers it in the config file.

    Returns a note about the file if it could not be written, else ''.
    """
    set_memcache_value(constants.ACTIVE_PROFILE.value, name)
    try:
        helpers.update_ini_section(args.config_file, 'profiles', {'active': name})
    except OSError as exc:
        return ' The selection will not survive a restart: %s could not be written (%s).' % (
            args.config_file, exc)
    return ''


def back_to_calibrate(profile, notice):
    """Redirects to the page after a POST, so the browser is left on a GET.

    The page reloads itself when the routine finishes; a reload of a POST result
    re-submits the form, and the first bench run of the page started a second
    calibration that way the moment the first one was done.
    """
    return redirect(url_for('calibrate_page', profile=profile, notice=notice))


@app.route('/app/calibrate/')
def calibrate_page():
    """Calibrating the trickler for a powder: start, follow, review and apply."""
    return render_calibrate(profile=request.args.get('profile'),
                            notice=request.args.get('notice') or None)


@app.route('/app/calibrate/status')
def calibrate_status():
    """The routine's progress for the page to poll, plus the readings it shows beside it."""
    status = calibration_status() or {}
    weight = safe_get(constants.SCALE_WEIGHT.value)
    return jsonify(
        dict(status,
             running=calibration_running(status),
             scale_weight=None if weight is None else str(weight),
             auto_mode=bool(safe_get(constants.AUTO_MODE.value, False))))


@app.route('/app/calibrate/start', methods=['POST'])
def calibrate_start():
    """Asks the daemon to calibrate: the profile, the container's capacity, the cell size."""
    name = (request.form.get('new_profile') or request.form.get('profile') or '').strip()
    errors = {}
    try:
        capacity = float(request.form.get('capacity', ''))
        if capacity < 20:
            errors['capacity'] = 'Give the container capacity in grains; 20 is the least that makes sense.'
    except ValueError:
        errors['capacity'] = 'Give the container capacity in grains.'
        capacity = None
    try:
        pulses_per_cell = int(request.form.get('pulses_per_cell', ''))
        if not 3 <= pulses_per_cell <= 50:
            errors['pulses_per_cell'] = 'Between 3 and 50 pulses per cell.'
    except ValueError:
        errors['pulses_per_cell'] = 'Give a whole number of pulses per cell.'
        pulses_per_cell = None
    if errors:
        return render_calibrate(profile=name, errors=errors,
                                notice='Not started. Check the values marked below.')
    status = calibration_status()
    if calibration_running(status):
        return render_calibrate(profile=name, notice=(
            'A calibration is already running. Wait for it to finish, or abort it.'))
    if safe_get(constants.AUTO_MODE.value, False):
        return render_calibrate(profile=name, notice=(
            'Switch auto mode off first. Calibration runs the trickler on its own, and '
            'must not race a charge.'))

    notice = 'Calibration requested for %s.' % (name or 'the plain [trickler] settings')
    notice += select_profile(name)
    try:
        helpers.update_ini_section(args.config_file, 'calibration', {'capacity': '%g' % capacity})
        config.read(args.config_file)
    except OSError as exc:
        notice += ' The capacity could not be remembered: %s could not be written (%s).' % (
            args.config_file, exc)
    # A placeholder until the daemon's first report, so the page shows something is
    # happening and a second Start is refused meanwhile.
    memcache_client.set(helpers.calibration_status_key(constants), {
        'phase': 'requested', 'progress': 0.0, 'prompt': None, 'finished': False,
        'profile': name, 'results': None, 'error': None,
        'message': 'Waiting for the trickler to pick the request up.'})
    send_command('calibrate', profile=name, capacity=capacity, pulses_per_cell=pulses_per_cell)
    logging.info('Requested a calibration: profile=%r capacity=%s pulses_per_cell=%s',
                 name, capacity, pulses_per_cell)
    return back_to_calibrate(name, notice)


@app.route('/app/calibrate/continue', methods=['POST'])
def calibrate_continue():
    """The container has been emptied, or the pan put back."""
    send_command('calibrate_continue')
    return redirect(url_for('calibrate_page'))


@app.route('/app/calibrate/abort', methods=['POST'])
def calibrate_abort():
    status = calibration_status()
    if status and status.get('phase') == 'requested':
        # The daemon has not picked it up; there is nothing to abort but the request.
        memcache_client.delete(helpers.command_key(constants))
        memcache_client.delete(helpers.calibration_status_key(constants))
        return back_to_calibrate(status.get('profile') or active_profile(), 'Request withdrawn.')
    send_command('calibrate_abort')
    return redirect(url_for('calibrate_page'))


@app.route('/app/calibrate/dismiss', methods=['POST'])
def calibrate_dismiss():
    """Clears a finished routine's report. Its record in learned.json stays."""
    memcache_client.delete(helpers.calibration_status_key(constants))
    return back_to_calibrate(active_profile(),
                             'Dismissed. The results are still in the record for the profile.')


@app.route('/app/calibrate/apply', methods=['POST'])
def calibrate_apply():
    """Puts the recommended (or hand-edited) settings in force and saves them.

    The same two writes as the tuning page -- live overrides for the next charge and the
    config file for the one after a reboot -- except that, given a profile name, the file
    write goes to that profile's section, since a calibration is a fact about one
    powder. Then the daemon is told, so the profile's learned rates are seeded from the
    calibration at the speeds now in force rather than probed again.
    """
    name = (request.form.get('save_profile') or '').strip()
    current, _ = current_trickler_settings()
    submitted = {key: request.form[key] for key in CALIBRATION_FIELDS if key in request.form}
    values, errors = helpers.clean_trickler_settings(submitted, current)
    set_memcache_value(constants.TRICKLER_SETTINGS.value, values)

    section = helpers.profile_section(name) if name else 'trickler'
    notice = 'Applied. The next charge uses these values'
    try:
        helpers.update_ini_section(args.config_file, section, values)
        config.read(args.config_file)
        notice += ', saved to [%s].' % section
    except OSError as exc:
        logging.warning('Could not write %s: %s', args.config_file, exc)
        notice += ', but %s could not be written (%s), so they will be lost on restart.' % (
            args.config_file, exc)
    if name:
        notice += select_profile(name)
    send_command('calibrate_apply', profile=name,
                 pulse_pwm=float(values['pulse_pwm']), pulse_fast_pwm=float(values['pulse_fast_pwm']))
    if errors:
        notice += ' Some values were adjusted: ' + ' '.join(errors.values())
    return back_to_calibrate(name, notice)


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
    for row in rows:
        # What the charge really was, when the daemon waited to see it land.
        try:
            row['true_error'] = helpers.charge_error(row)
        except (KeyError, TypeError, ValueError):
            row['true_error'] = None
    return render_template(
        'history.html',
        rows=list(reversed(rows))[:100],
        stats=helpers.charge_statistics(rows),
        # The fit is per powder: a stick and a ball powder on one line is not a line.
        fit=helpers.pulse_fit(recorded_pulses(wanted)),
        profiles=sorted({row.get('profile', '') for row in helpers.read_charges(history_path())} - {''}) if history_path() else [],
        selected=wanted,
        enabled=bool(history_path()))


@app.route('/app/history.json')
def history_json():
    """The same data as /app/history, for anything that would rather have it raw."""
    rows = helpers.read_charges(history_path()) if history_path() else []
    return jsonify(rows=rows, stats=helpers.charge_statistics(rows))


@app.route('/app/pulses.json')
def pulses_json():
    """The recorded pulses and the fit through them, for anything that wants the numbers.

    `rows` is capped at the newest 500; the fit is over everything recorded for the
    profile, since a fit over a sample of the record would be a different fit from the
    page's.
    """
    rows = recorded_pulses(request.args.get('profile', ''), request.args.get('source', ''))
    return jsonify(rows=rows[-500:], fit=helpers.pulse_fit(rows))


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
        # The memcache copy goes now, so the page reflects it at once. The daemon owns
        # the copy that survives a reboot, so it is asked to forget that one.
        for key in (constants.TRICKLER_PULSE_RATE.value,
                    constants.TRICKLER_FAST_PULSE_RATE.value):
            memcache_client.delete('%s:%s' % (key, profile) if profile else key)
        memcache_client.set(helpers.command_key(constants),
                            {'command': 'reset_learned', 'profile': profile})
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