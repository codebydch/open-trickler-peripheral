#!/usr/bin/env python3
"""
Copyright (c) Ammolytics and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

OpenTrickler
https://github.com/ammolytics/projects/tree/develop/trickler
"""
import array
import collections
import csv
import decimal
import logging
import math
import os
import re
import struct
import tempfile

import pymemcache.client.base
import pymemcache.serde


def get_mc_client(server='127.0.0.1:11211'):
    """Returns a memcache client instance."""
    return pymemcache.client.base.Client(
        server,
        serde=pymemcache.serde.PickleSerde(),
        connect_timeout=10,
        timeout=2)


def setup_logging(level=logging.DEBUG):
    """Returns a configured logger instance."""
    logging.basicConfig(
        level=level,
        format='%(asctime)s.%(msecs)06dZ %(levelname)-4s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S')


def is_even(dec):
    """Returns True if a decimal.Decimal is even, False if odd."""
    exp = dec.as_tuple().exponent
    factor = 10 ** (exp * -1)
    return (dec * factor) % 2 == 0


def noop(*args, **kwargs):
    """Simple noop function."""
    return None


def bool_to_bytes(value):
    """Converts bool to bytes."""
    data_bytes = array.array('B', [0] * 1)
    struct.pack_into("<B", data_bytes, 0, value)
    return data_bytes


def bytes_to_bool(data_bytes):
    """Converts bytes to boolean."""
    value = data_bytes[0]
    return bool(value)


def str_to_bytes(value):
    """Converts str to bytes."""
    data_bytes = array.array('B', [])
    data_bytes.frombytes(value.encode('utf-8'))
    return data_bytes


def bytes_to_str(data_bytes):
    """Converts bytes to str."""
    return data_bytes.decode('utf-8')


def decimal_to_bytes(value):
    """Converts decimal to bytes."""
    value = str(value)
    return str_to_bytes(value)


def bytes_to_decimal(data_bytes):
    """Converts bytes to decimal."""
    value = bytes_to_str(data_bytes)
    return decimal.Decimal(value)


def enum_to_bytes(value_enum):
    """Converts enum to bytes."""
    data_bytes = array.array('B', [0] * 1)
    struct.pack_into("<B", data_bytes, 0, value_enum.value)
    return data_bytes


def bytes_to_enum(enum_cls, data_bytes):
    """Converts bytes to enum."""
    value = data_bytes[0]
    return enum_cls(value)


# Tuning values from the [trickler] section that can be changed from the control panel,
# together with the range each one is allowed to take. Kept here rather than in either
# program so the web app and the trickler agree on the list, the defaults and the
# limits, and so a slipped decimal point in the form can't command something the
# hardware won't survive.
TricklerSetting = collections.namedtuple(
    'TricklerSetting',
    ('name', 'label', 'default', 'minimum', 'maximum', 'step', 'description'))


TRICKLER_SETTINGS = (
    TricklerSetting(
        'fine_trickle_weight', 'Coarse trickler off at', '1.5', 0.0, 50.0, 0.1,
        'Grains still to go when the second trickler shuts off and motor 1 finishes the '
        'charge alone. Must be more than both motors can throw during the scale\'s lag.'),
    TricklerSetting(
        'pulse_trickle_weight', 'Pulse feeding starts at', '0.5', 0.0, 10.0, 0.05,
        'Grains still to go when continuous trickling stops for good. Below this every '
        'pulse is weighed before the next one is fired.'),
    TricklerSetting(
        'pulse_on_time', 'Longest pulse', '0.2', 0.005, 2.0, 0.005,
        'Seconds. Upper limit on how long a single pulse may run.'),
    TricklerSetting(
        'pulse_min_on_time', 'Shortest pulse', '0.03', 0.005, 2.0, 0.005,
        'Seconds. The smallest pulse worth firing. This sets the finest dose the machine '
        'can place, and so the best accuracy it can reach.'),
    TricklerSetting(
        'pulse_off_time', 'Pause after each pulse', '0.1', 0.0, 5.0, 0.05,
        'Seconds to wait for the powder to land before weighing what the pulse delivered.'),
    TricklerSetting(
        'settle_min_time', 'Minimum settle wait', '0.3', 0.0, 5.0, 0.05,
        'Seconds after a pulse before a stable reading is believed. Below the scale\'s '
        'own reporting lag, a pulse gets weighed before its powder has landed, which '
        'reads as delivering nothing and makes the feeder over-pulse.'),
    TricklerSetting(
        'pulse_pwm', 'Pulse speed', '25', 0.0, 100.0, 1.0,
        'PWM %. Motor speed while pulsing. Never actually driven below the stall speed.'),
    TricklerSetting(
        'stall_pwm', 'Stall speed', '20', 0.0, 100.0, 1.0,
        'PWM %. The speed below which the vibratory motor moves no powder at all.'),
    TricklerSetting(
        'pulse_rate', 'Starting feed rate', '0.3', 0.01, 20.0, 0.01,
        'Grains per second of pulsing. Only a first guess: the feeder measures each pulse '
        'and learns the real figure from there.'),
    TricklerSetting(
        'pulse_aim', 'Pulse aim', '0.7', 0.1, 1.0, 0.05,
        'Fraction of the remaining weight each pulse aims at. Below 1 so a mis-estimate '
        'lands under the target rather than over it.'),
    TricklerSetting(
        'settle_timeout', 'Settle timeout', '1.0', 0.1, 10.0, 0.1,
        'Seconds to wait for the scale to report stable before using whatever it last said.'),
    TricklerSetting(
        'cutoff_weight', 'Stop short by', '0.01', 0.0, 1.0, 0.01,
        'Grains. A floor under the feeder\'s own stopping rule. Raise it if charges still '
        'run heavy, lower it toward 0 if they run light.'),
    TricklerSetting(
        'rate_window', 'Feed rate samples', '4', 2.0, 20.0, 1.0,
        'Scale readings averaged when judging how fast powder is landing during the '
        'continuous phases.'),
    TricklerSetting(
        'lookahead_time', 'Look ahead', '0.35', 0.0, 2.0, 0.05,
        'Seconds. How far ahead to project the feed rate when deciding to slow down. '
        'Covers powder in flight plus the scale\'s reporting lag.'),
)


# Fallbacks for the [trickler] section, so a config file written before that section
# existed still runs instead of crashing on startup.
DEFAULT_TRICKLER_SETTINGS = {s.name: s.default for s in TRICKLER_SETTINGS}


def clean_trickler_settings(form, current):
    """Validates submitted tuning values against TRICKLER_SETTINGS.

    Returns (values, errors). Values holds every setting as a string, falling back to
    `current` for anything the form left out. Out-of-range numbers are clamped rather
    than refused, so a submission always leaves the trickler in a runnable state; the
    returned errors say what was changed and why.
    """
    values = {}
    errors = {}
    for setting in TRICKLER_SETTINGS:
        raw = form.get(setting.name, current.get(setting.name, setting.default))
        try:
            number = float(raw)
        except (TypeError, ValueError):
            errors[setting.name] = 'not a number, left unchanged'
            values[setting.name] = str(current.get(setting.name, setting.default))
            continue
        clamped = min(max(number, setting.minimum), setting.maximum)
        if clamped != number:
            errors[setting.name] = 'must be between %g and %g, clamped to %g' % (
                setting.minimum, setting.maximum, clamped)
        values[setting.name] = '%g' % clamped
    return values, errors


def update_ini_section(path, section, values):
    """Rewrites `key = value` lines inside one section of an ini file, in place.

    configparser would drop every comment in the file, and the config file is mostly
    comments explaining what each value does, so the lines are edited directly instead.
    Keys not already in the section are appended to it, and a missing section is added
    at the end. The file is replaced atomically so a failure part way through can't
    leave the trickler with a half-written config.
    """
    with open(path, 'r', encoding='utf-8') as handle:
        lines = handle.readlines()

    header = '[%s]' % section
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        lines.append('\n%s\n' % header)
        lines.extend('%s = %s\n' % (k, values[k]) for k in sorted(values))
    else:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith('[')), len(lines))
        written = set()
        for i in range(start + 1, end):
            # Horizontal whitespace only: \s would match the line's own newline, so a
            # key with an empty value ("active =") had its terminator swallowed into the
            # separator and the replacement was written across two lines.
            match = re.match(r'^([^\S\n]*)([A-Za-z0-9_]+)([^\S\n]*=[^\S\n]*)', lines[i])
            if match and match.group(2) in values:
                key = match.group(2)
                lines[i] = '%s%s%s%s\n' % (
                    match.group(1), key, match.group(3), values[key])
                written.add(key)
        missing = ['%s = %s\n' % (k, values[k]) for k in sorted(values) if k not in written]
        lines[end:end] = missing

    directory = os.path.dirname(os.path.abspath(path)) or '.'
    handle = tempfile.NamedTemporaryFile(
        'w', encoding='utf-8', dir=directory, delete=False)
    try:
        with handle:
            handle.writelines(lines)
        os.replace(handle.name, path)
    except BaseException:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


# --- Charge history -------------------------------------------------------------------

# One row per charge. Written only by the trickler daemon (which runs as root) and read
# by the web app (which runs as pi); a single writer keeps the file permissions simple.
HISTORY_COLUMNS = (
    'timestamp',
    'profile',
    'outcome',
    'target',
    'final',
    'error',
    'unit',
    'pulses',
    'seconds',
    'learned_rate',
)


def append_charge(path, row, max_rows=500):
    """Appends one charge to the history file, trimming the oldest rows past `max_rows`.

    Creates the file and its directory on first use. Raises OSError if the file cannot be
    written; callers on the trickler side should log and carry on rather than let a full
    SD card stop someone reloading.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    rows = read_charges(path)
    rows.append({column: str(row.get(column, '')) for column in HISTORY_COLUMNS})
    # Keep the newest max_rows so the file can't grow without bound on an SD card.
    if max_rows and len(rows) > max_rows:
        rows = rows[-max_rows:]

    handle = tempfile.NamedTemporaryFile(
        'w', encoding='utf-8', dir=directory or '.', delete=False, newline='')
    try:
        with handle:
            writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(handle.name, path)
        os.chmod(path, 0o644)
    except BaseException:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


def read_charges(path):
    """Returns every recorded charge as a list of dicts, oldest first.

    A missing or unreadable file reads as no history rather than an error: the page
    should say "nothing recorded yet", not fail.
    """
    try:
        with open(path, 'r', encoding='utf-8', newline='') as handle:
            return [dict(row) for row in csv.DictReader(handle)
                    if row.get('timestamp')]
    except (OSError, csv.Error):
        logging.debug('No readable charge history at %s', path, exc_info=True)
        return []


def charge_statistics(rows, tolerance=0.02):
    """Summarises completed charges: how many, how far off, and how many were good.

    `within` is the share of charges inside +/- `tolerance` of target, which is the
    number that actually answers "is this machine accurate enough?".
    """
    errors = []
    for row in rows:
        if row.get('outcome') != 'complete':
            continue
        try:
            errors.append(float(row['error']))
        except (KeyError, TypeError, ValueError):
            continue

    if not errors:
        return {'count': 0, 'mean': None, 'sigma': None, 'low': None, 'high': None,
                'within': None, 'tolerance': tolerance}

    mean = sum(errors) / len(errors)
    # Population standard deviation: this is the whole record, not a sample of it.
    sigma = math.sqrt(sum((e - mean) ** 2 for e in errors) / len(errors))
    within = sum(1 for e in errors if abs(e) <= tolerance) / len(errors)
    return {
        'count': len(errors),
        'mean': mean,
        'sigma': sigma,
        'low': min(errors),
        'high': max(errors),
        'within': within,
        'tolerance': tolerance,
    }


# --- Powder profiles ------------------------------------------------------------------

# Profiles live in the config file as [profile:Name] sections. The web app writes them
# through update_ini_section() -- it already writes that file -- and the trickler only
# reads. Keeping one writer avoids a root-owned file the web app could never update.
PROFILE_PREFIX = 'profile:'


def profile_section(name):
    """The config section name holding a profile's settings."""
    return '%s%s' % (PROFILE_PREFIX, name)


def list_profiles(config):
    """Names of every profile defined in the config file, sorted."""
    return sorted(section[len(PROFILE_PREFIX):] for section in config.sections()
                  if section.startswith(PROFILE_PREFIX))


def profile_settings(config, name):
    """The [trickler] overrides a profile carries, or {} if it has none."""
    if not name:
        return {}
    section = profile_section(name)
    return dict(config[section]) if config.has_section(section) else {}
