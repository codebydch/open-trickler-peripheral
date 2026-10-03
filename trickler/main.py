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

# How strongly the rate measured over the recent pulse window pulls the stored feed rate
# toward it, once a rate has been measured at that speed. Low enough to ride out one odd
# window, high enough to follow a different powder within a few pulses. Note the window
# is rebuilt empty for every charge, so the first update of a charge is one pulse's
# worth of evidence blended at this weight -- which is how one four-grain burst on a
# short pulse moved a stored 0.302 gn/s to 0.665. Bounding that is Phase 2 work.
PULSE_RATE_LEARN = 0.4

# Floor for the learned feed rate, so a run of pulses that delivered nothing can't drive
# it to zero and break the on-time calculation.
MIN_PULSE_RATE = 0.02

# Give up on a charge after this many pulses in a row deliver nothing measurable. That
# means an empty hopper or a jammed tube, not something more trickling will fix.
MAX_EMPTY_PULSES = 8

# The first pulse at a speed nothing has been measured at runs for this many of the
# shortest pulses the machine can place, and is also held to this fraction of what the
# guessed rate says would fill the gap. See PulseFeeder._probe_time.
FAST_PROBE_PULSES = 4
FAST_PROBE_SAFETY = 4
# How far a probe may distrust the rate it was sized from, when probe after probe comes
# back with nothing measurable. Bounded so a probe can never spoil a charge.
MAX_FAST_PROBE = 8

# How many recent pulses the pulse feeder measures its feed rate over. Long enough that
# whole grains landing at random average out, short enough to follow a hopper that is
# emptying. Not the `rate_window` setting: that one counts scale readings and belongs to
# FeedRateEstimator, which runs the continuous phases.
PULSE_RATE_WINDOW = 6

# How many scale divisions from target the feeder stops sizing pulses and just places one
# grain at a time. Aiming below this is arithmetic about doses the machine cannot deliver.
# Chosen in the simulator (tests/fakes.py), not on the bench: across 45 simulated charges
# spanning a 60x range of seed rates, at 5 divisions nothing landed more than one grain
# over, while 3 and 8 both produced charges that did.
FINAL_GRAINS = 5

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
    'pulse_dead_time',
    'pulse_off_time',
    'pulse_pwm',
    'pulse_fast_pwm',
    'pulse_fast_until',
    'settle_min_time',
    'stall_pwm',
    'pulse_rate',
    'pulse_aim',
    'settle_timeout',
    'cutoff_weight',
    'rate_window',
    'lookahead_time',
    'stall_drop_weight',
    'max_dump_attempts',
    'dump_retry_pause',
    'profile',
    'history_path',
    'history_max_rows',
    'pulses_path',
    'pulses_max_rows',
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


def settled_weight(scale, timeout, min_wait=0.0):
    """Reads the scale until it reports a stable weight, or `timeout` seconds pass.

    `min_wait` is the time that must pass before a stable reading is believed. It
    matters more than it looks: for the first moment after powder is fed, it is still in
    the air, so the pan is undisturbed and the scale happily reports the *old* weight as
    stable. Trusting that reads the feed as having delivered nothing, and the one after
    it as having delivered double.
    """
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        scale.update()
        if time.time() - start < min_wait:
            continue
        if getattr(scale, 'is_fresh', True) and scale.is_stable:
            break
    return scale.weight


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
        # Grains (or grams) delivered per second of motor on-time, at each of the two
        # pulse speeds. Both are measured rather than assumed: a vibratory feeder's
        # throughput against drive is not reliably linear, and the whole point of
        # learning the slow rate applies just as much to the fast one.
        self._rate = float(settings.pulse_rate)
        # The configured pulse_rate is a starting guess, not a measurement. Until a pulse
        # has actually been weighed at a speed, pulses there are probes rather than aimed
        # doses -- see _probe_time().
        self._rate_measured = False
        self._fast_rate = None
        self._rate_key = None
        self._fast_rate_key = None
        if memcache is not None and constants is not None:
            # Scoped to the profile, so switching powder switches the estimate instead of
            # blending two powders into an average that fits neither.
            self._rate_key = learned_rate_key(constants, settings.profile)
            learned = memcache.get(self._rate_key)
            if learned:
                self._rate = max(float(learned), MIN_PULSE_RATE)
                self._rate_measured = True
            self._fast_rate_key = learned_rate_key(
                constants, settings.profile, fast=True)
            learned_fast = memcache.get(self._fast_rate_key)
            if learned_fast:
                self._fast_rate = max(float(learned_fast), MIN_PULSE_RATE)
        self.empty_pulses = 0
        self.pulses = 0
        # One dict per pulse fired, written to the pulse file when the charge ends. This
        # is the record the feed rate and spin-up are fitted from; the log line in
        # pulse_phase is for watching, this is for measuring.
        self.records = []
        # How far the guessed rate is discounted when sizing a probe. Doubles every time
        # a probe delivers too little to measure, because that is evidence the guess is
        # too high -- and a probe sized from a rate several times too high is far too
        # short to ever deliver anything.
        self._probe_scale = {False: 1.0, True: 1.0}
        # Recent pulses at each speed, as (on_time, dose). The rate is measured over the
        # whole window rather than pulse by pulse: powder arrives as whole grains, so an
        # individual dose is 0, one grain, or three, and none of those is the feed rate.
        # Summing the window lets the zeros and the clumps cancel, which is the only
        # unbiased way to measure a granular process.
        self._window = {False: collections.deque(maxlen=PULSE_RATE_WINDOW),
                        True: collections.deque(maxlen=PULSE_RATE_WINDOW)}

    @property
    def settings(self):
        """The trickler settings this feeder was built with."""
        return self._settings

    @property
    def rate(self):
        """Weight delivered per second of motor on-time, as currently learned."""
        return self._rate

    @property
    def fast_rate(self):
        """Weight per second of motor on-time at the fast pulse speed.

        Until a pulse has actually been measured at that speed there is nothing to go on,
        so the slow rate is scaled by drive above the stall point as a first guess. It
        only has to be close enough for one pulse: the measured result replaces it.
        """
        if self._fast_rate is not None:
            return self._fast_rate
        stall = self._settings.stall_pwm
        slow = max(self._settings.pulse_pwm, stall)
        fast = max(self._settings.pulse_fast_pwm, stall)
        if slow <= stall:
            return self._rate
        return self._rate * ((fast - stall) / (slow - stall))

    def _moving_time(self, on_time):
        """The part of a pulse that actually moved powder, after the motor spun up."""
        return on_time - self._settings.pulse_dead_time

    @property
    def resolution(self):
        """The smallest weight change this scale can report.

        Also, in practice, about the weight of one grain of stick powder -- the two are
        both near 0.02 gn and every dose this machine has ever measured was a multiple of
        it. Nothing finer can be placed *or* seen, so it is the floor under every
        judgement the feeder makes.
        """
        try:
            return decimal.Decimal(getattr(self._scale, 'resolution', 0))
        except (TypeError, ValueError, decimal.InvalidOperation):
            # A scale class that doesn't report one behaves as it did before this.
            return decimal.Decimal(0)

    @property
    def min_dose(self):
        """Smallest amount of powder a single pulse can deliver.

        Never less than one division: the machine cannot place half a grain, and letting
        this go below what the scale can resolve is what had it firing pulse after pulse
        at a target it could not measure.
        """
        computed = decimal.Decimal(str(
            self._rate * max(self._moving_time(self._settings.pulse_min_on_time), 0.0)))
        return max(computed, self.resolution)

    def done(self, remainder):
        """True once another pulse would miss the target by more than stopping short does.

        `min_dose` only earns a say here once a pulse has actually been weighed. It is
        rate x the shortest pulse, so a seed rate several times too high says the machine
        cannot place less than several grains, and the charge is declared finished before
        it has fired anything: seeded at 3 gn/s, a pan 0.04 gn short was called complete
        on the strength of a number nothing had measured. A guess may not widen the
        finish line -- it can only be narrowed to what the scale can resolve.

        With the shipped settings the `min_dose` term never decides anything: the shortest
        pulse moves 0.03 s x 0.217 gn/s, well under a division, so `min_dose` is pinned to
        the division and half of it is below `cutoff_weight`. In practice this rule is
        `remainder <= cutoff_weight`. The term only matters on a machine whose shortest
        pulse drops more than two divisions.
        """
        floor = max(self._settings.cutoff_weight, self.resolution / 2)
        if not self._rate_measured:
            return remainder <= floor
        return remainder <= max(self.min_dose / 2, floor)

    def settled_weight(self, min_wait=0.0):
        """The settled scale reading, using this feeder's configured settle times."""
        return settled_weight(self._scale, self._settings.settle_timeout, min_wait)

    def feed(self, remainder):
        """Fires one pulse aimed at part of `remainder`, returning what it actually delivered.

        Pulses far from the target are fired at `pulse_fast_pwm`, close ones at the fine
        `pulse_pwm`. Every pulse costs the same wait to weigh it whatever its size, so
        the way to a quicker charge is fewer, larger pulses -- and the way *not* to do it
        is a faster motor throughout, which would coarsen the smallest dose the machine
        can place and with it the accuracy.
        """
        fast = self._use_fast_speed(remainder)
        if not self._measured(fast):
            # Nothing weighed at this speed yet, so there is no rate to size anything
            # from -- not an aimed dose and not a single grain either.
            on_time = self._probe_time(remainder, fast)
        elif self._in_the_last_grains(remainder):
            # Close enough that aiming is meaningless: asking for 0.028 gn when powder
            # arrives in 0.02 gn grains gets you 0, 0.02 or 0.06 whatever the pulse
            # length. Place one grain and weigh the result.
            on_time = self._one_grain_time()
        else:
            # Aim short of what's left so a mis-estimate lands under the target rather
            # than over it. The next pulse closes whatever remains.
            if fast:
                rate = self.fast_rate
                # A fast pulse aims only at the gap down to where fine pulsing takes
                # over, never at the target itself: its job is to get the charge close
                # cheaply, and the fine pulses place the last of it.
                wanted = float(remainder - self._settings.pulse_fast_until)
            else:
                rate = self._rate
                wanted = float(remainder)
            wanted *= self._settings.pulse_aim
            on_time = (wanted / rate + self._settings.pulse_dead_time
                       if rate > 0 else self._settings.pulse_on_time)
        on_time = min(max(on_time, self._settings.pulse_min_on_time), self._settings.pulse_on_time)

        before = self._scale.weight
        self.pulses += 1
        self._motor.set_speed(self._pulse_speed(fast))
        time.sleep(on_time)
        self._motor.off()
        # Let the powder land before asking the scale what happened.
        time.sleep(self._settings.pulse_off_time)
        dose = self.settled_weight(self._settings.settle_min_time) - before
        self._learn(on_time, dose, fast)
        self.records.append({
            'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
            'pwm': round(self._pulse_speed(fast) * 100, 1),
            'on_time': round(on_time, 4),
            'moving_time': round(self._moving_time(on_time), 4),
            'remainder': remainder,
            'dose': dose,
            # The rate at the speed just used, after this pulse was folded in.
            'rate': round(self.fast_rate if fast else self._rate, 4),
        })
        return on_time, dose

    def _measured(self, fast):
        """Whether a pulse has ever been weighed at this speed."""
        return self._fast_rate is not None if fast else self._rate_measured

    def _probe_time(self, remainder, fast):
        """How long the first pulse at a speed should run, before anything has been
        measured there.

        Sizing a pulse needs a rate, and the only rate available for a speed nothing has
        been measured at is a guess scaled off the fine one, which can be several times
        out. So this pulse is a measurement rather than an aimed dose, bounded two ways:

        - in absolute terms, a few times the shortest pulse the machine can place. Not
          the shortest: most of a minimum pulse is the motor spinning up, so a dose
          measured from one reads far lower than the real feed rate, and a rate learned
          that low makes the *next* pulse several times too long.
        - against the gap it is aiming into, at a fraction of what the guessed rate says
          would fill it, so that a guess which is badly low still cannot overshoot.

        One measured pulse replaces the guess, and the result is kept per powder profile,
        so this is paid once rather than every charge.
        """
        settings = self._settings
        dead = settings.pulse_dead_time
        scale = self._probe_scale[bool(fast)]
        absolute = dead + settings.pulse_min_on_time * FAST_PROBE_PULSES * scale
        gap = float(remainder - settings.pulse_fast_until) if fast else float(remainder)
        # Discount the guess by however many times a probe has already come back
        # unmeasurable. Both bounds lengthen together: raising only the absolute one
        # leaves this bound binding at the same short pulse forever, which is how a slow
        # trickler fired eight probes that each delivered nothing.
        guess = (self.fast_rate if fast else self._rate) / scale
        against_gap = (dead + gap / (guess * FAST_PROBE_SAFETY)) if guess > 0 else absolute
        # Never shorter than the spin-up plus one minimum pulse: a probe that delivers
        # nothing measures nothing.
        floor = dead + settings.pulse_min_on_time
        return max(min(absolute, against_gap, settings.pulse_on_time), floor)

    def _one_grain_time(self):
        """How long to run to deliver about one grain, at the rate measured so far.

        Not `pulse_min_on_time`: the shortest pulse the machine can fire is not the same
        as the shortest pulse that moves powder, and on a slow trickler it delivers
        nothing at all -- which is the crawl of thirty fruitless pulses seen on the bench.
        Sizing it from the measured rate adapts to the machine instead of assuming one.
        """
        if self._rate <= 0:
            return self._settings.pulse_min_on_time
        return float(self.resolution) / self._rate + self._settings.pulse_dead_time

    def _in_the_last_grains(self, remainder):
        """True once what is left is only a few grains of powder."""
        return self.resolution > 0 and remainder <= self.resolution * FINAL_GRAINS

    def _use_fast_speed(self, remainder):
        """True while there is enough left to be worth a coarse pulse."""
        return (self._settings.pulse_fast_pwm > self._settings.pulse_pwm and
                remainder > self._settings.pulse_fast_until)

    def _pulse_speed(self, fast=False):
        """Pulse speed as a 0-1 PWM value, never below the point where the motor stalls."""
        pwm = self._settings.pulse_fast_pwm if fast else self._settings.pulse_pwm
        return min(max(pwm, self._settings.stall_pwm), 100.0) / 100

    def _learn(self, on_time, dose, fast=False):
        """Folds one measured pulse into the feed-rate estimate for the speed it used.

        Measured over a window of recent pulses rather than one at a time. Powder lands as
        whole grains, so a single dose is 0, one grain, or occasionally three -- none of
        which is the feed rate. Worse, judging each pulse alone and discarding the ones
        that read zero keeps only the hits and throws away the misses, which overestimated
        the rate by three times on the bench and made every pulse sized from it too long.
        Summing weight and time across the window lets the zeros and the clumps cancel.
        """
        if dose < 0:
            # Pan knocked, or the scale drifted down. Nothing to learn, but it still
            # counts as a pulse that got us no closer -- otherwise a run of them loops
            # here forever.
            logging.debug('Ignoring negative pulse dose: %r', dose)
            self.empty_pulses += 1
            return

        moving = self._moving_time(on_time)
        window = self._window[bool(fast)]
        if moving > 0:
            window.append((moving, float(dose)))

        delivered = sum(pulse_dose for _, pulse_dose in window)
        elapsed = sum(pulse_time for pulse_time, _ in window)
        # A pulse only counts as unproductive if the whole window has delivered nothing.
        # A few grainless pulses in a row is ordinary -- powder teeters on the lip and
        # falls on a later pulse -- and treating each one as evidence of an empty hopper
        # abandoned three charges in four on the bench, seconds before they finished.
        self.empty_pulses = 0 if delivered > 0 else self.empty_pulses + 1

        if delivered < float(self.resolution) or elapsed <= 0:
            logging.debug('Window has delivered %r over %rs, less than the scale can '
                          'resolve; not learning from it yet.', delivered, elapsed)
            if not self._measured(fast):
                # The probe was too short to read, so the rate it was sized from is too
                # high. Distrust it further and probe for longer, rather than firing the
                # same fruitless pulse over and over.
                self._probe_scale[bool(fast)] = min(
                    self._probe_scale[bool(fast)] * 2, MAX_FAST_PROBE)
            return

        observed = delivered / elapsed
        # Each speed has its own estimate. Folding a fast pulse into the fine rate would
        # make the pulses that finish the charge far too long.
        if not self._measured(fast):
            # The first real measurement at a speed replaces its guess outright. Easing
            # towards it from a guess that could be several times out would leave the
            # next pulse sized on a number nothing has ever measured.
            updated = max(observed, MIN_PULSE_RATE)
            self._probe_scale[bool(fast)] = 1.0
        else:
            current = self.fast_rate if fast else self._rate
            updated = max(current + (observed - current) * PULSE_RATE_LEARN, MIN_PULSE_RATE)
        if fast:
            self._fast_rate = updated
            key = self._fast_rate_key
        else:
            self._rate = updated
            self._rate_measured = True
            key = self._rate_key
        if self._memcache is not None and key is not None:
            self._memcache.set(key, updated)


def learned_rate_key(constants, profile, fast=False):
    """The memcache key holding a learned feed rate for one powder profile.

    One key per pulse speed, since the two are measured separately.
    """
    base = (constants.TRICKLER_FAST_PULSE_RATE.value if fast
            else constants.TRICKLER_PULSE_RATE.value)
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


def record_pulses(settings, records, target_unit):
    """Writes one charge's pulses to the pulse file, in a single write.

    Once per charge rather than once per pulse: the file is rewritten whole, and an SD
    card does not want a dozen rewrites a charge. Never raises, for the same reason
    record_charge() doesn't.
    """
    if not settings.pulses_path or not records:
        return
    unit = getattr(target_unit, 'name', target_unit)
    try:
        helpers.append_pulses(settings.pulses_path, [
            dict(record, profile=settings.profile, unit=unit) for record in records
        ], settings.pulses_max_rows)
    except Exception:
        logging.warning('Could not record the pulses to %s', settings.pulses_path,
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
    history = helpers.history_files(config)
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
        pulse_dead_time=float(section['pulse_dead_time']),
        pulse_off_time=float(section['pulse_off_time']),
        pulse_pwm=float(section['pulse_pwm']),
        pulse_fast_pwm=float(section['pulse_fast_pwm']),
        pulse_fast_until=decimal.Decimal(section['pulse_fast_until']) * factor,
        settle_min_time=float(section['settle_min_time']),
        stall_pwm=float(section['stall_pwm']),
        # The seed rate is in grains per second; convert it the same way as the weights.
        pulse_rate=decimal.Decimal(section['pulse_rate']) * factor,
        pulse_aim=float(section['pulse_aim']),
        settle_timeout=float(section['settle_timeout']),
        cutoff_weight=decimal.Decimal(section['cutoff_weight']) * factor,
        rate_window=int(section['rate_window']),
        lookahead_time=decimal.Decimal(section['lookahead_time']),
        stall_drop_weight=decimal.Decimal(section['stall_drop_weight']) * factor,
        max_dump_attempts=int(float(section['max_dump_attempts'])),
        dump_retry_pause=float(section['dump_retry_pause']),
        profile=profile,
        history_path=history.charges,
        history_max_rows=history.max_rows,
        pulses_path=history.pulses,
        pulses_max_rows=history.pulses_max_rows)


def dump_powder(servo_motor, scale, settings):
    """Runs the powder measure once, and reports how much powder actually landed.

    Returns (dropped, attempts). The servo is released whatever happens: it holds its
    GPIO line while it is being driven, and a line still held after an error locks the
    servo setup page out until this service restarts.

    A jammed measure drops *nothing*. A kernel of powder caught in the drum stops the
    handle dead, and the servo is not strong enough to shear it, so there is no partial
    drop to reason about -- which is why the check is a fixed floor rather than something
    learned from previous charges. When it happens, back the arm off to unload the
    handle, wait, and push again: that motion is also the one most likely to shift the
    kernel. `stall_drop_weight` of 0 turns the whole check off.
    """
    attempts = 0
    dropped = decimal.Decimal('0')
    before = settled_weight(scale, settings.settle_timeout, settings.settle_min_time)

    while attempts < max(1, settings.max_dump_attempts):
        if attempts:
            logging.warning(
                'The measure dropped nothing (%s). Working it again, attempt %s of %s.',
                dropped, attempts + 1, settings.max_dump_attempts)
            time.sleep(settings.dump_retry_pause)
        attempts += 1
        try:
            servo_motor.run_servo()
            time.sleep(1.5)
            servo_motor.set_initial_angle()
            # The drop hitting the pan overshoots until it settles, so let it.
            time.sleep(1)
        finally:
            servo_motor.off()

        dropped = settled_weight(
            scale, settings.settle_timeout, settings.settle_min_time) - before
        if settings.stall_drop_weight <= 0 or dropped >= settings.stall_drop_weight:
            break

    return dropped, attempts


def dump_or_stop(servo_motor, scale, settings, memcache, constants, tricklers):
    """Dumps a charge, and stops the machine if the measure turns out to be jammed.

    Returns True if there is powder in the pan and the charge may go on.

    Stopping means auto mode off, both tricklers off, and the reason where a person will
    see it. Auto mode matters most: without it the next pass through the control loop
    would drive the servo straight back into the same jam, forever. The alternative --
    carrying on -- means handing an empty pan to the trickler loop, which would try to
    build the entire charge with the vibratory motors.
    """
    logging.info('Starting powder dump...')
    dropped, attempts = dump_powder(servo_motor, scale, settings)
    if settings.stall_drop_weight > 0 and dropped < settings.stall_drop_weight:
        message = (
            'The powder measure dropped nothing in %s attempt(s) -- it is probably '
            'jammed on a kernel. Clear it, then turn auto mode back on.' % attempts)
        logging.error(message)
        for motor in tricklers:
            motor.off()
        memcache.set(constants.DUMP_ERROR.value, message)
        memcache.set(constants.AUTO_MODE.value, False)
        return False

    memcache.set(constants.DUMP_ERROR.value, '')
    logging.info('Completed powder dump: %s in %s attempt(s).', dropped, attempts)
    return True


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
        # Every pulse, at INFO, including the ones that delivered nothing and taught
        # nothing: this line is the only view of the final approach anyone has, and it
        # used to record what the feeder learned rather than what it did -- so the pulses
        # worth looking at were exactly the ones missing from it. The rate is here too,
        # since a rate drifting away from reality is what a bad charge looks like before
        # it goes wrong.
        logging.info(
            'remainder: %s %s scale: %s %s pulsed %.3fs -> %s (rate %.3f/s)',
            remainder, target_unit, weight, scale.unit, on_time, dose, feeder.rate)


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
        record_pulses(settings, feeder.records, target_unit)
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
                settings = trickler_settings(config, memcache, constants, scale, target_unit)
                # Stops the servo from dumping powder twice if the scale weight dips below the target weight
                if ((target_weight - scale.weight) / target_weight) >= 0.5:
                    # Wait a second to dump powder and start trickling.
                    time.sleep(1)
                    if not dump_or_stop(servo_motor, scale, settings, memcache,
                                        constants,
                                        (trickler_motor1, trickler_motor2)):
                        continue
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
    # default=None so "not given" can be told from "given as false": with
    # store_true alone the flag is False when absent, and `args.verbose is not
    # None` was then always true, so the config file's verbose never applied.
    parser.add_argument('--verbose', action='store_true', default=None)
    parser.add_argument('--auto_mode', action='store_true')
    parser.add_argument('--pid_tune', action='store_true')
    parser.add_argument('--target_weight', type=decimal.Decimal, default=0)
    parser.add_argument('--target_unit', choices=('g', 'GN'), default='GN')
    args = parser.parse_args()

    config = helpers.load_config(args.config_file)

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
