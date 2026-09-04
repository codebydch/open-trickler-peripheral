#!/usr/bin/env python3
"""
Copyright (c) Ammolytics and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

OpenTrickler
https://github.com/ammolytics/projects/tree/develop/trickler

OpenTrickler forked and updated here:
https://github.com/codebydch/open-trickler-peripheral
"""
import collections
import configparser
import datetime
import decimal
import enum
import logging
import time

import helpers
import PID
import motors
import scales


# Components:
# 0. Server (Pi)
# 1. Scale (serial)
# 2. Trickler (gpio/PWM)
# 3. Dump (gpio/servo)
# 4. API
# 6. Bluetooth?
# 7: Powder pan/cup?


# A stand-in for a config section that isn't in the file, so the typed getters below can
# be used without checking has_section() first.
EMPTY_SECTION = configparser.ConfigParser()['DEFAULT']


# Grains per gram, used to convert the grain-based trickler thresholds in the
# config file when the scale is set to grams.
GRAINS_PER_GRAM = decimal.Decimal('15.4323583529')

# How strongly each measured pulse pulls the learned feed rate toward what was just
# observed. Low enough to ride out one odd reading, high enough to adapt to a different
# powder within a few pulses.
PULSE_RATE_LEARN = 0.4

# Floor for the learned feed rate, so a run of pulses that delivered nothing can't drive
# it to zero and break the on-time calculation.
MIN_PULSE_RATE = 0.02

# Give up on a charge after this many pulses in a row deliver nothing measurable. That
# means an empty hopper or a jammed tube, not something more trickling will fix.
MAX_EMPTY_PULSES = 8

# Backstop on the final approach. Even with the give-up counters above, a pathological
# cycle should not wedge the daemon on one charge.
MAX_PULSE_PHASE_SECONDS = 120.0

# Give up on a charge after this long with no usable scale reading at all. Individual
# reads fail routinely -- the serial line is read faster than the scale writes to it --
# so this has to be far longer than any normal gap between frames.
STALE_READ_TIMEOUT = 5.0

TricklerSettings = collections.namedtuple('TricklerSettings', (
    'fine_trickle_weight',
    'pulse_trickle_weight',
    'pulse_on_time',
    'pulse_min_on_time',
    'pulse_off_time',
    'pulse_pwm',
    'settle_min_time',
    'stall_pwm',
    'pulse_rate',
    'pulse_aim',
    'settle_timeout',
    'cutoff_weight',
    'rate_window',
    'lookahead_time',
    'profile',
    'history_path',
    'history_max_rows',
))


class FeedRateEstimator:
    """Estimates how fast powder is landing in the pan, in target units per second.

    Scale readings lag behind reality: powder is still in the air, and the scale needs
    time to settle before it reports what has landed. Knowing the current feed rate
    lets the loop work out how much powder is already on its way and stop the motors
    before the reading reaches the target, instead of after.
    """

    def __init__(self, window):
        """Constructor. Keeps the last `window` readings to average the rate over."""
        self._samples = collections.deque(maxlen=max(2, window))

    def add(self, weight, timestamp=None):
        """Records a scale reading."""
        self._samples.append((timestamp if timestamp is not None else time.time(), weight))

    def rate(self):
        """Returns the weight gained per second over the sample window, never negative."""
        if len(self._samples) < 2:
            return decimal.Decimal('0')
        old_time, old_weight = self._samples[0]
        new_time, new_weight = self._samples[-1]
        elapsed = decimal.Decimal(str(new_time - old_time))
        if elapsed <= 0:
            return decimal.Decimal('0')
        return max(decimal.Decimal('0'), (new_weight - old_weight) / elapsed)


class PulseFeeder:
    """Feeds powder in short pulses and learns how much each one delivers.

    A vibratory motor can't be driven below its stall point, so the only way to control
    how much powder lands is to control how long it runs. How much a given run time
    delivers depends on the powder -- stick and ball meter very differently -- so it is
    measured from the scale as the charge finishes rather than set in the config file.
    Each pulse is aimed at part of what's left, weighed once the pan settles, and the
    result corrects the estimate used to size the next one.
    """

    def __init__(self, motor, scale, settings, memcache=None, constants=None):
        """Constructor. Seeds the feed rate from memcache if a previous charge learned one."""
        self._motor = motor
        self._scale = scale
        self._settings = settings
        self._memcache = memcache
        self._constants = constants
        # Grains (or grams) delivered per second of motor on-time.
        self._rate = float(settings.pulse_rate)
        self._rate_key = None
        if memcache is not None and constants is not None:
            # Scoped to the profile, so switching powder switches the estimate instead of
            # blending two powders into an average that fits neither.
            self._rate_key = learned_rate_key(constants, settings.profile)
            learned = memcache.get(self._rate_key)
            if learned:
                self._rate = max(float(learned), MIN_PULSE_RATE)
        self.empty_pulses = 0
        self.pulses = 0

    @property
    def settings(self):
        """The trickler settings this feeder was built with."""
        return self._settings

    @property
    def rate(self):
        """Weight delivered per second of motor on-time, as currently learned."""
        return self._rate

    @property
    def min_dose(self):
        """Smallest amount of powder a single pulse can deliver."""
        return decimal.Decimal(str(self._rate * self._settings.pulse_min_on_time))

    def done(self, remainder):
        """True once another pulse would miss the target by more than stopping short does."""
        return remainder <= max(self.min_dose / 2, self._settings.cutoff_weight)

    def settled_weight(self, min_wait=0.0):
        """Reads the scale until it reports a stable weight, or the settle time runs out.

        `min_wait` is the time that must pass before a stable reading is believed. It
        matters more than it looks: for the first moment after a pulse the powder is
        still in the air, so the pan is undisturbed and the scale happily reports the
        *old* weight as stable. Trusting that reads the pulse as having delivered
        nothing, and the one after it as having delivered double.
        """
        start = time.time()
        deadline = start + self._settings.settle_timeout
        while time.time() < deadline:
            self._scale.update()
            if time.time() - start < min_wait:
                continue
            if getattr(self._scale, 'is_fresh', True) and self._scale.is_stable:
                break
        return self._scale.weight

    def feed(self, remainder):
        """Fires one pulse aimed at part of `remainder`, returning what it actually delivered."""
        # Aim short of what's left so a mis-estimate lands under the target rather than
        # over it. The next pulse closes whatever remains.
        wanted = float(remainder) * self._settings.pulse_aim
        on_time = wanted / self._rate if self._rate > 0 else self._settings.pulse_on_time
        on_time = min(max(on_time, self._settings.pulse_min_on_time), self._settings.pulse_on_time)

        before = self._scale.weight
        self.pulses += 1
        self._motor.set_speed(self._pulse_speed())
        time.sleep(on_time)
        self._motor.off()
        # Let the powder land before asking the scale what happened.
        time.sleep(self._settings.pulse_off_time)
        dose = self.settled_weight(self._settings.settle_min_time) - before
        self._learn(on_time, dose)
        return on_time, dose

    def _pulse_speed(self):
        """Pulse speed as a 0-1 PWM value, never below the point where the motor stalls."""
        pwm = min(max(self._settings.pulse_pwm, self._settings.stall_pwm), 100.0)
        return pwm / 100

    def _learn(self, on_time, dose):
        """Folds one measured pulse into the running feed-rate estimate."""
        if dose < 0:
            # Pan knocked, or the scale drifted down. The rate estimate can't learn
            # anything from it, but it still counts as a pulse that got us no closer --
            # otherwise a run of them loops here forever.
            logging.debug('Ignoring negative pulse dose: %r', dose)
            self.empty_pulses += 1
            return
        self.empty_pulses = 0 if dose > 0 else self.empty_pulses + 1

        # A dose smaller than the scale can resolve carries no information: it reads as
        # zero whether it was zero or most of a division. Learning from it drags the rate
        # down towards nothing, and the estimate stops describing the machine. The short
        # pulses at the very end of a charge are all like this.
        try:
            resolution = decimal.Decimal(getattr(self._scale, 'resolution', 0))
        except (TypeError, ValueError, decimal.InvalidOperation):
            # A scale class that doesn't report a resolution just learns from everything,
            # as it did before this guard existed.
            resolution = decimal.Decimal(0)
        if dose < resolution:
            logging.debug('Pulse dose %r is below the scale resolution %r; not learning '
                          'from it.', dose, resolution)
            return

        observed = float(dose) / on_time
        self._rate = max(self._rate + (observed - self._rate) * PULSE_RATE_LEARN, MIN_PULSE_RATE)
        logging.debug(
            'pulse on_time: %r dose: %r -> rate: %r (min dose %r)',
            on_time, dose, self._rate, self.min_dose)
        if self._memcache is not None and self._rate_key is not None:
            self._memcache.set(self._rate_key, self._rate)


def learned_rate_key(constants, profile):
    """The memcache key holding the learned feed rate for one powder profile."""
    base = constants.TRICKLER_PULSE_RATE.value
    return '%s:%s' % (base, profile) if profile else base


def record_charge(settings, target_weight, final_weight, target_unit, outcome,
                  pulses, seconds, learned_rate):
    """Writes one finished charge to the history file.

    Never raises. A history file that cannot be written is a nuisance; a trickler that
    stops working because of it is not acceptable, so failures are logged and dropped.
    """
    if not settings.history_path:
        return
    try:
        helpers.append_charge(settings.history_path, {
            'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
            'profile': settings.profile,
            'outcome': outcome,
            'target': target_weight,
            'final': final_weight,
            'error': final_weight - target_weight,
            'unit': getattr(target_unit, 'name', target_unit),
            'pulses': pulses,
            'seconds': round(seconds, 1),
            'learned_rate': round(learned_rate, 4),
        }, settings.history_max_rows)
    except Exception:
        # Called from a finally block, where raising would mask whatever actually ended
        # the charge. Nothing here is worth losing that for.
        logging.warning('Could not record the charge to %s', settings.history_path,
                        exc_info=True)


def seed_memcache(memcache, values, overwrite=None):
    """Writes `values` into memcache, keeping anything that is already set.

    Startup should not destroy live state: another process, or the user, may already have
    set a target weight and turned auto mode on. `overwrite` names the keys to write
    regardless, for values that were given explicitly on the command line.
    """
    overwrite = overwrite or {}
    for key, value in values.items():
        if overwrite.get(key) or memcache.get(key) is None:
            memcache.set(key, value)
        else:
            logging.debug('Keeping existing memcache value for %s', key)


def trickler_settings(config, memcache, constants, scale, target_unit):
    """Reads the trickler thresholds in force right now, converted to the target unit.

    Values set from the control panel win, then the config file, then the built-in
    defaults. The control panel writes to memcache, and this runs once per charge, so a
    change made while tuning takes effect on the very next throw without restarting the
    service.

    The weights in the config file are given in grains, since that's the unit they were
    tuned in, so they need converting when the scale is set to grams.
    """
    overrides = {}
    profile = ''
    if memcache is not None and constants is not None:
        overrides = memcache.get(constants.TRICKLER_SETTINGS.value)
        if not isinstance(overrides, dict):
            overrides = {}
        profile = memcache.get(constants.ACTIVE_PROFILE.value) or ''
    if not profile and config.has_section('profiles'):
        profile = config['profiles'].get('active', '')
    history = config['history'] if config.has_section('history') else EMPTY_SECTION
    configured = config['trickler'] if config.has_section('trickler') else {}
    # Live overrides win, then the selected powder's profile, then the plain [trickler]
    # section, then the built-in defaults.
    section = collections.ChainMap(
        overrides,
        helpers.profile_settings(config, profile),
        configured,
        helpers.DEFAULT_TRICKLER_SETTINGS)
    factor = decimal.Decimal('1')
    if target_unit == scale.Units.GRAMS:
        factor = decimal.Decimal('1') / GRAINS_PER_GRAM
    return TricklerSettings(
        fine_trickle_weight=decimal.Decimal(section['fine_trickle_weight']) * factor,
        pulse_trickle_weight=decimal.Decimal(section['pulse_trickle_weight']) * factor,
        pulse_on_time=float(section['pulse_on_time']),
        pulse_min_on_time=float(section['pulse_min_on_time']),
        pulse_off_time=float(section['pulse_off_time']),
        pulse_pwm=float(section['pulse_pwm']),
        settle_min_time=float(section['settle_min_time']),
        stall_pwm=float(section['stall_pwm']),
        # The seed rate is in grains per second; convert it the same way as the weights.
        pulse_rate=decimal.Decimal(section['pulse_rate']) * factor,
        pulse_aim=float(section['pulse_aim']),
        settle_timeout=float(section['settle_timeout']),
        cutoff_weight=decimal.Decimal(section['cutoff_weight']) * factor,
        rate_window=int(section['rate_window']),
        lookahead_time=decimal.Decimal(section['lookahead_time']),
        profile=profile,
        history_path=history.get('path', '') if history.getboolean('enabled', True) else '',
        history_max_rows=history.getint('max_rows', 500))


def pulse_phase(memcache, constants, feeder, scale, target_weight, target_unit):
    """Finishes the charge one measured pulse at a time, off settled scale readings.

    Continuous trickling can't be trusted this close to the target: a decision made now
    doesn't reach the scale for a couple of tenths of a second, by which point the
    charge has moved past where it was aimed. Pulsing removes the guesswork -- nothing
    is fed until the last thing fed has been weighed.

    Returns the outcome: 'complete' if the charge reached target, otherwise why it
    stopped. The caller records it, so an abandoned charge is visible in the history
    rather than silently absent.
    """
    logging.info('Starting final approach. Learned rate: %r', feeder.rate)
    weight = feeder.settled_weight(feeder.settings.settle_min_time)
    started = time.time()

    while 1:
        if time.time() - started > MAX_PULSE_PHASE_SECONDS:
            logging.warning(
                'Final approach ran for %ss without finishing, stopping. remainder: %s %s',
                MAX_PULSE_PHASE_SECONDS, target_weight - weight, target_unit)
            return 'timeout'

        # Stop running if auto mode is disabled.
        if not memcache.get(constants.AUTO_MODE.value):
            logging.debug('auto mode disabled.')
            return 'aborted'

        # Stop running if scale's unit no longer matches target unit.
        if scale.unit != target_unit:
            logging.debug('Target unit does not match scale unit.')
            return 'aborted'

        # Stop running if pan removed.
        if weight < 0:
            logging.debug('Pan removed.')
            return 'aborted'

        remainder = target_weight - weight
        if feeder.done(remainder):
            logging.info(
                'Charge complete. scale: %s %s remainder: %s (smallest pulse %s)',
                weight, scale.unit, remainder, feeder.min_dose)
            return 'complete'

        if feeder.empty_pulses >= MAX_EMPTY_PULSES:
            logging.warning(
                '%s pulses in a row delivered nothing, stopping. Check the hopper and '
                'tube. remainder: %s %s',
                feeder.empty_pulses, remainder, target_unit)
            return 'empty'

        on_time, dose = feeder.feed(remainder)
        weight = scale.weight
        logging.info(
            'remainder: %s %s scale: %s %s pulsed %.3fs -> %s',
            remainder, target_unit, weight, scale.unit, on_time, dose)


def trickler_loop(config, memcache, constants, pid, trickler_motor1, trickler_motor2, scale, target_weight, target_unit, pidtune_logger):
    """Main trickler control loop run when all devices are ready, target weight is set, and auto-mode is on."""
    settings = trickler_settings(config, memcache, constants, scale, target_unit)
    logging.debug('trickler settings: %r', settings)
    feed_rate = FeedRateEstimator(settings.rate_window)
    feeder = PulseFeeder(trickler_motor1, scale, settings, memcache, constants)
    stale_since = None
    started = time.time()
    outcome = None
    pidtune_logger.info('timestamp, input (motor %), output (weight %)')
    logging.info('Starting trickling process...')

    # Note(eric): All `break` calls will exit the loop and this function.
    # The `finally` block below stops both motors on every exit path.
    try:
        while 1:
            # Stop running if auto mode is disabled.
            if not memcache.get(constants.AUTO_MODE.value):
                logging.debug('auto mode disabled.')
                break

            # Read scale values (weight/unit/stable)
            scale.update()

            # The read can come back without a usable weight, which leaves the previous
            # reading in place. Acting on it would mean deciding twice on one reading, so
            # skip the pass. A few in a row is normal traffic; only a sustained silence
            # means the serial link is actually gone.
            # getattr, because scales.py is a file people customise and a scale class
            # without the flag should behave as it did before the flag existed.
            if not getattr(scale, 'is_fresh', True):
                if stale_since is None:
                    stale_since = time.time()
                elif time.time() - stale_since >= STALE_READ_TIMEOUT:
                    logging.warning(
                        'No usable scale reading for %ss, stopping. Check the serial link.',
                        STALE_READ_TIMEOUT)
                    break
                continue
            stale_since = None

            # Stop running if scale's unit no longer matches target unit.
            if scale.unit != target_unit:
                logging.debug('Target unit does not match scale unit.')
                break

            # Stop running if pan removed.
            if scale.weight < 0:
                logging.debug('Pan removed.')
                break

            feed_rate.add(scale.weight)
            remainder_weight = target_weight - scale.weight
            # Powder that is already in the air or that the scale hasn't caught up with
            # yet. Every decision below is made against what the charge is about to
            # weigh, not what the scale is reporting right now.
            in_flight_weight = feed_rate.rate() * settings.lookahead_time
            projected_remainder = remainder_weight - in_flight_weight
            logging.debug(
                'remainder_weight: %r, in_flight_weight: %r, projected_remainder: %r',
                remainder_weight,
                in_flight_weight,
                projected_remainder)

            pidtune_logger.info(
                '%s, %s, %s',
                datetime.datetime.now().timestamp(),
                trickler_motor1.speed,
                scale.weight / target_weight)

            # Trickling complete. Stop both motors here, before anything else in this
            # iteration can command them again, so no more powder goes into a pan that
            # has already reached the target weight.
            if remainder_weight <= settings.cutoff_weight:
                trickler_motor1.off()
                trickler_motor2.off()
                logging.debug('Trickling complete, motors turned off and PID reset.')
                outcome = 'complete'
                break

            # Close enough to hand over to the pulse feeder, which finishes the charge
            # against settled readings and then ends it.
            if projected_remainder <= settings.pulse_trickle_weight:
                trickler_motor1.off()
                trickler_motor2.off()
                outcome = pulse_phase(
                    memcache, constants, feeder, scale, target_weight, target_unit)
                break

            # Enough powder is already on its way to finish the charge, even though the
            # scale hasn't reported it yet. Stop feeding and let the reading catch up
            # instead of piling more on top of it. If the charge lands short, the next
            # pass picks the trickling back up.
            if projected_remainder <= settings.cutoff_weight:
                trickler_motor1.off()
                trickler_motor2.off()
                logging.debug('Projected weight has reached target, waiting for the scale.')
                time.sleep(settings.pulse_off_time)
                continue

            # PID controller requires float value instead of decimal.Decimal
            pid.update(float(scale.weight / target_weight) * 100)
            trickler_motor1.update(pid.output)
            # The second trickler only runs while there's still a meaningful amount of
            # powder left to throw. It feeds too fast to be used near the target.
            if projected_remainder <= settings.fine_trickle_weight:
                trickler_motor2.off()
            else:
                trickler_motor2.update(pid.output)
            logging.debug('trickler_motor1.speed: %r, trickler_motor2.speed: %r, pid.output: %r', trickler_motor1.speed, trickler_motor2.speed, pid.output)
            logging.info(
                'remainder: %s %s scale: %s %s motor1: %s motor2: %s',
                remainder_weight,
                target_unit,
                scale.weight,
                scale.unit,
                trickler_motor1.speed,
                trickler_motor2.speed)
    finally:
        # Clean up tasks.
        trickler_motor1.off()
        trickler_motor2.off()
        # Clear PID values.
        pid.clear()
        # Record here rather than at each exit, so a charge that was abandoned -- auto
        # mode switched off, pan lifted, a fault -- is visible in the history instead of
        # silently missing. `outcome` is only set where the charge actually finished.
        record_charge(settings, target_weight, scale.weight, target_unit,
                      outcome or 'aborted', feeder.pulses, time.time() - started,
                      feeder.rate)
    logging.info('Trickling process stopped.')


def main(config, memcache, args, pidtune_logger):
    """Main trickler function. This runs everything."""
    constants = enum.Enum('memcache_vars', config['memcache_vars'])

    # Set up the PID controller.
    pid = PID.PID(
        float(config['PID']['Kp']),
        float(config['PID']['Ki']),
        float(config['PID']['Kd']))
    logging.debug('pid: %r', pid)

    # Set up the trickler motor controller.
    trickler_motor1 = motors.TricklerMotor(1, config, memcache=memcache)
    logging.debug('trickler_motor1: %r', trickler_motor1)
    trickler_motor2 = motors.TricklerMotor(2, config, memcache=memcache)
    logging.debug('trickler_motor2: %r', trickler_motor2)
    servo_motor = motors.ServoMotor(config, memcache=memcache)
    logging.debug('servo_motor: %r', servo_motor)

    # Set up the scale controller.
    scale_cls = scales.SCALES[config['scale']['model']]
    # Wait until the scale is ready.
    while 1:
        try:
            scale = scale_cls(config, memcache=memcache)
        except scales.ScaleNotReady:
            logging.info('Scale not ready, trying again...')
            time.sleep(10)
        else:
            logging.debug('scale: %r', scale)
            break

    # Seed memcache, without disturbing anything already set. A restart used to stamp
    # these defaults over whatever the user had entered, so any crash silently threw away
    # the target weight and switched auto mode off -- which hid the crash itself.
    # Values given explicitly on the command line still win.
    seed_memcache(memcache, {
        constants.AUTO_MODE.value: args.auto_mode or False,
        constants.TARGET_WEIGHT.value: args.target_weight or decimal.Decimal('0.0'),
        constants.TARGET_UNIT.value: scale.unit_map.get(args.target_unit, 'GN'),
    }, overwrite={
        constants.AUTO_MODE.value: bool(args.auto_mode),
        constants.TARGET_WEIGHT.value: bool(args.target_weight),
        constants.TARGET_UNIT.value: False,
    })

    # Outer-most control loop for the whole trickler system.
    last_status = None
    while 1:
        # Update settings from memcache.
        auto_mode = memcache.get(constants.AUTO_MODE.value)
        target_weight = memcache.get(constants.TARGET_WEIGHT.value)
        target_unit = memcache.get(constants.TARGET_UNIT.value)
        # Use percentages for PID control to avoid complexity w/ different units of weight.
        pid.SetPoint = 100.0
        scale.update()

        # Set scale to match target unit.
        if target_unit != scale.unit:
            logging.info('scale.unit: %r, target_unit: %r', scale.unit, target_unit)
            scale.change_unit()

        # Only log when something actually changes. Logging every pass filled the
        # journal with tens of identical lines a second and buried real errors.
        status = (target_weight, target_unit, scale.weight, scale.unit, auto_mode)
        if status != last_status:
            logging.info(
                'target: %s %s scale: %s %s auto_mode: %s',
                target_weight,
                target_unit,
                scale.weight,
                scale.unit,
                auto_mode)
            last_status = status

        # Powder pan in place, scale stable, ready to trickle.
        if (scale.weight >= 0 and
                scale.weight < target_weight and
                scale.unit == target_unit and
                scale.is_stable and
                auto_mode):
            # One bad charge should cost a charge, not the daemon. Dying here takes the
            # service down with it, and the restart wipes the log context along with the
            # settings, which makes the original error very hard to find.
            try:
                # Stops the servo from dumping powder twice if the scale weight dips below the target weight
                if ((target_weight - scale.weight) / target_weight) >= 0.5:
                    # Wait a second to dump powder and start trickling.
                    time.sleep(1)
                    logging.info('Starting powder dump...')
                    servo_motor.run_servo()
                    time.sleep(1.5)
                    servo_motor.set_initial_angle()
                    # Required since the larger powder drop hitting the cup may overshoot the weight until settling
                    time.sleep(1)
                    # Long enough for the servo to have got back to its initial angle, so
                    # stop driving it. Holding a software-timed PWM signal on an idle
                    # servo makes it buzz and hunt; the measure holds position itself.
                    servo_motor.off()
                    logging.info('Completed powder dump.')
                # Run trickler loop.
                trickler_loop(config, memcache, constants, pid, trickler_motor1, trickler_motor2, scale, target_weight, target_unit, pidtune_logger)
            except Exception:
                logging.exception('Charge failed. Motors stopped; the trickler is still running.')
                trickler_motor1.off()
                trickler_motor2.off()
                # Don't spin on a fault that repeats every pass.
                time.sleep(1)


if __name__ == '__main__':
    import argparse
    import configparser

    # Default argument values.
    DEFAULTS = dict(
        verbose = False,
    )

    parser = argparse.ArgumentParser(description='Run OpenTrickler.')
    parser.add_argument('config_file')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--auto_mode', action='store_true')
    parser.add_argument('--pid_tune', action='store_true')
    parser.add_argument('--target_weight', type=decimal.Decimal, default=0)
    parser.add_argument('--target_unit', choices=('g', 'GN'), default='GN')
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

    # Setup memcache.
    memcache_client = helpers.get_mc_client()

    # Set up a separate logger for PID tuning with it's own format.
    pidtune_logger = logging.getLogger('pid_tune')
    pid_handler = logging.StreamHandler()
    pid_handler.setFormatter(logging.Formatter('%(message)s'))

    # Configure the log level based on if the tuner feature should be active.
    pidtune_logger.setLevel(logging.ERROR)
    if args.pid_tune or config['PID'].getboolean('pid_tuner_mode'):
        pidtune_logger.setLevel(logging.INFO)

    # Run the main trickler program.
    main(config, memcache_client, args, pidtune_logger)
