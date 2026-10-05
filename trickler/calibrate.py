#!/usr/bin/env python3
"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral

Per-powder calibration: measure how this powder feeds, then recommend settings.

The user presses Calibrate on the web page; the daemon runs trickler 1 on its own -- the
two tricklers are the same hardware, and the powder measure never runs, since the user
manages how much powder is in the container. The routine:

  prime      run until powder is arriving steadily, so the tube is loaded the way it is
             before every real final approach (an empty tube takes ~10 s to fill);
  stall      walk the drive down until grains stop, to find this powder's stall point;
  sweep      pulse at a grid of speeds and lengths, weighing every pulse the way a charge
             would, pausing for the container to be emptied before it would overflow;
  fit        turn the record into per-cell dose distributions and per-speed rates;
  recommend  run the shipped PulseFeeder on simulated time against those doses, for every
             candidate setting set, and pick the fastest that lands heavy no more than one
             charge in four (the owner's rule: speed first, a heavy grain is lifted out by
             hand).

Nothing is applied here. The result waits in memcache and learned.json for the user to
apply, edit or save as a profile from the page.
"""
import collections
import datetime
import logging
import random
import threading
import time

import helpers
import main
import powder_model


# The idle loop calls step() once a pass; each step does at most this much running or
# one pulse, so the pan check and commands are polled between steps.
STEP_SECONDS = 1.0
# Priming: the tube is loaded once the pan has gained for this many windows in a row.
PRIME_WINDOWS = 3
# A full 1 s window with no gain at a speed, this many times, means the motor has stalled.
STALL_WINDOWS = 3
# The settle-and-read after a pulse, for the status page's time estimate only.
READ_SECONDS = 0.15
# Second reading after every pulse, a second on, to measure what landed after the settle
# read: the tail. On the bench (2026-10-04) the tails were as large as the doses -- 0.048
# gn against 0.025 at 45% -- and measuring them on every third pulse only, with the rest
# recorded as nothing, under-read the delivery at every speed and made every candidate
# simulate slow. It costs a second a pulse; the numbers are the point of the routine.
TAIL_SECONDS = 1.0

RECOMMEND_THREADED = True


CalibrationSettings = collections.namedtuple('CalibrationSettings', (
    'speeds',           # pulse speeds (PWM %) to sweep
    'durations',        # pulse lengths (s) to sweep
    'pulses_per_cell',
    'capacity_margin',  # grains kept clear of the container limit
    'prime_timeout',    # seconds before "no powder arriving"
    'stall_step',       # PWM % per step of the stall search
    'stall_max_steps',
    'first_pass',       # simulated charges per candidate, then per finalist
    'second_pass',
))


def calibration_settings(config):
    """The [calibration] section, with the defaults the reference machine was swept at."""
    section = config['calibration'] if config.has_section('calibration') else {}

    def numbers(key, default):
        raw = section.get(key, default) if hasattr(section, 'get') else default
        return tuple(float(x) for x in str(raw).split(',') if x.strip())

    def number(key, default, cast=float):
        return cast(section.get(key, default)) if hasattr(section, 'get') else cast(default)

    return CalibrationSettings(
        speeds=numbers('speeds', '25,30,45'),
        durations=numbers('durations', '0.15,0.25,0.40'),
        pulses_per_cell=number('pulses_per_cell', 10, int),
        capacity_margin=number('capacity_margin', 0.5),
        prime_timeout=number('prime_timeout', 20.0),
        stall_step=number('stall_step', 2.0),
        stall_max_steps=number('stall_max_steps', 12, int),
        first_pass=number('first_pass', 60, int),
        second_pass=number('second_pass', 300, int))


DEFAULT_STATUS_KEY = helpers.DEFAULT_CALIBRATION_STATUS_KEY


def status_key(constants):
    """The memcache key the routine reports under; defaulted like command_key()."""
    return helpers.calibration_status_key(constants)


class Calibration:
    """One calibration run, advanced a step at a time by the idle loop."""

    def __init__(self, config, memcache, constants, hw, request, clock=None, sleep=None):
        self._memcache = memcache
        self._constants = constants
        self._hw = hw
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self.cal = calibration_settings(config)
        if request.get('pulses_per_cell'):
            self.cal = self.cal._replace(pulses_per_cell=int(request['pulses_per_cell']))
        target_unit = memcache.get(constants.TARGET_UNIT.value)
        settings = main.trickler_settings(config, memcache, constants, hw.scale, target_unit)
        self.settings = settings._replace(profile=request.get('profile', settings.profile))
        self.capacity = float(request.get('capacity', 200))
        self.target_unit = target_unit
        self._rng = random.Random(request.get('seed', 1))

        self.phase = 'prime'
        self.prompt = None
        self.message = 'Priming the tube.'
        self.error = None
        self.finished = False
        self.progress = 0.0
        self.started = self._clock()

        self.records = []            # every pulse, as pulses.csv rows
        self.cells = collections.defaultdict(list)  # (speed, duration) -> [(dose, tail)]
        self.continuous_rate = None
        self.stall_pwm = None
        self.results = None

        self._prime_started = None
        self._prime_gains = 0
        self._prime_first = None
        self._stall_speed = min(self.cal.speeds)
        self._stall_quiet = 0
        self._stall_steps = 0
        self._stall_last_window = None
        self._reprime_left = 0.0
        self._block_speed = None
        self._plan = self._make_plan()
        self._index = 0
        self._unflushed = 0
        self._resume_phase = None
        self._thread = None
        self._stop = False
        self._recommendation = None

        # Calibration and charging don't mix: switch auto mode off and say so.
        memcache.set(constants.AUTO_MODE.value, False)
        self.publish()

    # --- the plan ---------------------------------------------------------------------

    def _make_plan(self):
        """Every pulse of the sweep, speed by speed, lengths shuffled within a speed so
        tube state is not confounded with length."""
        plan = []
        for speed in sorted(self.cal.speeds):
            cells = [(speed, duration) for duration in self.cal.durations
                     for _ in range(self.cal.pulses_per_cell)]
            self._rng.shuffle(cells)
            plan.extend(cells)
        return plan

    # --- status ------------------------------------------------------------------------

    def publish(self):
        self._memcache.set(status_key(self._constants), {
            'phase': self.phase,
            'progress': round(self.progress, 3),
            'message': self.message,
            'prompt': self.prompt,
            'pulses_done': self._index,
            'pulses_total': len(self._plan),
            'profile': self.settings.profile,
            'capacity': self.capacity,
            'stall_pwm': self.stall_pwm,
            'continuous_rate': None if self.continuous_rate is None else round(self.continuous_rate, 4),
            'results': self.results,
            'error': self.error,
            'finished': self.finished,
            'started': datetime.datetime.fromtimestamp(self.started).isoformat(timespec='seconds')
            if self._clock is time.time else None,
        })

    # --- commands ----------------------------------------------------------------------

    def resume(self):
        """The user has emptied the container (or put the pan back) and pressed Continue.

        A paused sweep is preceded by a few seconds of running, so the tube is loaded the
        way it was; the earlier phases just carry on where they were.
        """
        if self.phase != 'paused':
            return
        self.prompt = None
        if self._resume_phase == 'sweep':
            self._reprime_left = 3.0
            self.phase = 'reprime'
            self.message = 'Loading the tube again before carrying on.'
        else:
            self.phase = self._resume_phase or 'prime'
            self.message = 'Carrying on.'
        self.publish()

    def abort(self, reason='Aborted.'):
        self._stop = True
        self._motor_off()
        self.phase = 'aborted'
        self.message = reason
        self.finished = True
        self.publish()

    # --- stepping ----------------------------------------------------------------------

    def step(self):
        """Advances the routine by one pulse or one second. Never raises."""
        if self.finished:
            return
        try:
            # Read the scale before deciding anything. While paused nothing else reads it,
            # and a stale reading from before the container was emptied would pause the
            # routine again the moment it continued.
            self._hw.scale.update()
            running = self.phase in ('prime', 'stall', 'reprime', 'sweep')
            weight = float(self._hw.scale.weight)
            if running and weight < 0:
                self._pause('pan_missing', 'The container is off the scale. Put it back and press Continue.')
            elif running and weight + self.cal.capacity_margin >= self.capacity:
                # Checked before every step, not just before pulses: priming and the
                # stall search run continuously and fill the container too.
                self._pause('empty_container',
                            'The container is at %.1f gn of %.0f. Empty it, put it back and press Continue.'
                            % (weight, self.capacity))
            elif self.phase == 'prime':
                self._prime()
            elif self.phase == 'stall':
                self._stall()
            elif self.phase == 'reprime':
                self._reprime()
            elif self.phase == 'sweep':
                self._sweep()
            elif self.phase == 'fit':
                self._fit()
            elif self.phase == 'recommending':
                self._poll_recommendation()
            elif self.phase == 'paused':
                pass
        except Exception as exc:
            logging.exception('Calibration failed.')
            self._motor_off()
            self.error = '%s: %s' % (type(exc).__name__, exc)
            self.phase = 'failed'
            self.message = 'Calibration failed: %s' % exc
            self.finished = True
        self.publish()

    def _motor_off(self):
        self._hw.trickler_motor1.off()
        self._hw.trickler_motor2.off()

    def _run_window(self, seconds):
        """Keeps the scale read for `seconds`; returns (gain, weight) over the window."""
        start_weight = self._hw.scale.weight
        start = self._clock()
        while self._clock() - start < seconds:
            self._hw.scale.update()
        weight = self._hw.scale.weight
        return weight - start_weight, weight

    def _pause(self, prompt, message):
        self._motor_off()
        self._resume_phase = self.phase if self.phase != 'paused' else self._resume_phase
        self.phase = 'paused'
        self.prompt = prompt
        self.message = message

    # --- phases ------------------------------------------------------------------------

    def _prime(self):
        speed = max(self.cal.speeds) / 100
        if self._prime_started is None:
            self._prime_started = self._clock()
            self._prime_first = (self._clock(), self._hw.scale.weight)
        self._hw.trickler_motor1.set_speed(speed)
        gain, weight = self._run_window(STEP_SECONDS)
        self._prime_gains = self._prime_gains + 1 if gain >= self._hw.scale.resolution else 0
        elapsed = self._clock() - self._prime_started
        self.message = 'Priming the tube (%.0f s).' % elapsed
        if self._prime_gains >= PRIME_WINDOWS:
            first_time, first_weight = self._prime_first
            # Powder per second of running, over the whole prime: the continuous rate the
            # charge-time estimate uses.
            total = float(weight - first_weight)
            self.continuous_rate = max(total / max(elapsed, 1e-6), 0.001)
            self._motor_off()
            main.settled_weight(self._hw.scale, self.settings.settle_timeout,
                                self.settings.settle_min_time, clock=self._clock)
            self.phase = 'stall'
            self.message = 'Finding the stall speed.'
        elif elapsed >= self.cal.prime_timeout:
            self._motor_off()
            self.error = 'No powder arrived in %.0f s of running. Is the hopper empty or the tube blocked?' % elapsed
            self.phase = 'failed'
            self.message = self.error
            self.finished = True

    def _stall(self):
        """Steps the drive down from the lowest sweep speed until a window gains nothing."""
        if self._stall_speed <= 0:
            self._finish_stall(0.0)
            return
        self._hw.trickler_motor1.set_speed(self._stall_speed / 100)
        gain, _ = self._run_window(STEP_SECONDS)
        self._stall_quiet = self._stall_quiet + 1 if gain < self._hw.scale.resolution else 0
        self.message = 'Finding the stall speed: %.0f%%.' % self._stall_speed
        if self._stall_quiet >= STALL_WINDOWS:
            # Nothing moved for STALL_WINDOWS seconds: this speed is below the stall point.
            self._finish_stall(self._stall_speed + self.cal.stall_step)
        elif self._stall_quiet == 0 and self._stall_last_window != self._stall_speed:
            # Powder is still moving at this speed; try the next one down.
            self._stall_last_window = self._stall_speed
            self._stall_steps += 1
            if self._stall_steps >= self.cal.stall_max_steps:
                self._finish_stall(self._stall_speed)
            else:
                self._stall_speed -= self.cal.stall_step
                self._stall_quiet = 0
        elif self._stall_quiet == 0:
            # Gained again after a quiet window at the same speed: keep watching.
            pass

    def _finish_stall(self, stall_pwm):
        self._motor_off()
        self.stall_pwm = round(max(0.0, stall_pwm), 1)
        self._reprime_left = 3.0
        self.phase = 'reprime'
        self.message = 'Stall at %.0f%%. Loading the tube for the sweep.' % self.stall_pwm

    def _reprime(self):
        self._hw.trickler_motor1.set_speed(max(self.cal.speeds) / 100)
        self._run_window(min(STEP_SECONDS, self._reprime_left))
        self._reprime_left -= STEP_SECONDS
        if self._reprime_left <= 0:
            self._motor_off()
            main.settled_weight(self._hw.scale, self.settings.settle_timeout,
                                self.settings.settle_min_time, clock=self._clock)
            self.phase = 'sweep'
            self.message = 'Sweeping pulse speeds and lengths.'

    def _sweep(self):
        if self._index >= len(self._plan):
            self._motor_off()
            self._flush()
            self.phase = 'fit'
            self.message = 'Fitting the record.'
            return
        scale = self._hw.scale
        speed, duration = self._plan[self._index]
        if speed != self._block_speed:
            # Each speed's pulses start from a loaded tube, as a charge's do after the
            # continuous phase; the previous speed's pulses may have drawn it down.
            self._block_speed = speed
            if self._index > 0:
                self._reprime_left = 3.0
                self.phase = 'reprime'
                self.message = 'Loading the tube before the %.0f%% pulses.' % speed
                return
        before = scale.weight
        pulse_speed = min(max(speed, self.settings.stall_pwm), 100.0) / 100
        self._hw.trickler_motor1.set_speed(pulse_speed)
        self._sleep(duration)
        self._hw.trickler_motor1.off()
        self._sleep(self.settings.pulse_off_time)
        dose = main.settled_weight(scale, self.settings.settle_timeout,
                                   self.settings.settle_min_time, clock=self._clock) - before
        if dose < 0:
            # Pan knocked or the scale wandered down: not a dose. Count the pulse, learn
            # nothing from it.
            dose = type(dose)('0')
        self._run_window(TAIL_SECONDS)
        tail = scale.weight - before - dose
        if tail < 0:
            tail = type(tail)('0')
        self.cells[(speed, duration)].append((float(dose), float(tail)))
        self.records.append({
            'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
            'pwm': speed,
            'on_time': duration,
            'moving_time': round(duration - self.settings.pulse_dead_time, 4),
            'remainder': '',
            'dose': dose,
            'rate': '',
            'tail': tail,
        })
        self._unflushed += 1
        self._index += 1
        self.progress = self._index / len(self._plan)
        self.message = 'Sweeping: pulse %d of %d (%.0f%%, %.2f s).' % (
            self._index, len(self._plan), speed, duration)
        if self._unflushed >= self.cal.pulses_per_cell:
            self._flush()

    def _flush(self):
        """Writes the pulses recorded so far to pulses.csv, a cell's worth at a time."""
        if not self._unflushed or not self.settings.pulses_path:
            self._unflushed = 0
            return
        rows = self.records[len(self.records) - self._unflushed:]
        unit = getattr(self.target_unit, 'name', self.target_unit)
        try:
            helpers.append_pulses(self.settings.pulses_path, [
                dict(row, profile=self.settings.profile, unit=unit, source='calibration')
                for row in rows], self.settings.pulses_max_rows)
        except Exception:
            logging.warning('Could not record calibration pulses to %s',
                            self.settings.pulses_path, exc_info=True)
        self._unflushed = 0

    def _fit(self):
        """Per-cell distributions, per-speed rates, then the recommendation."""
        powder = powder_model.EmpiricalPowder(self.cells, rng=self._rng)
        summary = powder.cell_summary(self._hw.scale.resolution)
        rates, dead_times = {}, {}
        for speed in sorted(self.cal.speeds):
            # A pulse delivered its dose and its tail: the tail landed after the settle
            # read, but the pulse put it in the air. The fits count both.
            rows = [{'pwm': speed, 'on_time': duration, 'dose': dose + tail}
                    for (s, duration), values in self.cells.items() if s == speed
                    for dose, tail in values]
            # The spin-up comes from the two-point solve across pulse lengths, when there
            # are enough pulses for the two means to be a line and not two lumps.
            fit = helpers.pulse_fit(rows, min_per_bucket=max(10, self.cal.pulses_per_cell))
            dead_time = fit['dead_time'] if fit['rate'] else self.settings.pulse_dead_time
            dead_time = min(max(dead_time, 0.0), 0.3)
            dead_times[speed] = dead_time
            # The rate is what the feeder itself learns: everything delivered over all
            # the time the motor was moving, which lumps cannot invert the way a
            # difference of two means can.
            moving = sum(max(duration - dead_time, 0.01) * len(values)
                         for (s, duration), values in self.cells.items() if s == speed)
            delivered = sum(dose + tail for (s, _d), values in self.cells.items() if s == speed
                            for dose, tail in values)
            rates[speed] = max(delivered / max(moving, 1e-6), main.MIN_PULSE_RATE)
        self.results = {
            'profile': self.settings.profile,
            'capacity': self.capacity,
            'stall_pwm': self.stall_pwm,
            'continuous_rate': self.continuous_rate,
            'cells': [{'speed': speed, 'on_time': duration, **stats}
                      for (speed, duration), stats in summary.items()],
            'rates': {str(speed): rate for speed, rate in rates.items()},
            'dead_times': {str(speed): dead for speed, dead in dead_times.items()},
            'pulses': len(self.records),
            'recommendation': None,
        }
        self._powder, self._rates = powder, rates
        self.phase = 'recommending'
        self.message = 'Simulating charges to pick settings.'
        self.progress = 0.0

        def work():
            try:
                self._recommendation = powder_model.recommend(
                    powder, self.settings, rates, self.continuous_rate,
                    self.cal.speeds, self.cal.durations,
                    first_pass=self.cal.first_pass, second_pass=self.cal.second_pass,
                    progress=self._set_progress, stop=lambda: self._stop)
            except Exception as exc:  # reported by _poll_recommendation
                logging.exception('Recommendation failed.')
                self._recommendation = exc
        if RECOMMEND_THREADED:
            self._thread = threading.Thread(target=work, name='calibration-recommend', daemon=True)
            self._thread.start()
        else:
            work()

    def _set_progress(self, fraction):
        self.progress = fraction

    def _poll_recommendation(self):
        if self._thread is not None and self._thread.is_alive():
            self.message = 'Simulating charges to pick settings (%.0f%%).' % (self.progress * 100)
            return
        recommendation = self._recommendation
        if isinstance(recommendation, Exception):
            raise recommendation
        if recommendation is not None and recommendation.get('recommended'):
            recommendation['recommended']['settings']['stall_pwm'] = self.stall_pwm
            recommendation['recommended']['settings']['pulse_rate'] = round(
                self._rates.get(recommendation['recommended']['settings']['pulse_pwm'],
                                float(self.settings.pulse_rate)), 4)
            recommendation['recommended']['settings']['pulse_dead_time'] = round(float(
                self.results['dead_times'].get(str(recommendation['recommended']['settings']['pulse_pwm']),
                                               self.settings.pulse_dead_time)), 4)
        self.results['recommendation'] = recommendation
        self._store()
        self.phase = 'done'
        self.progress = 1.0
        self.message = 'Done. Review the recommendation on the page.'
        self.finished = True

    def _store(self):
        """Keeps the results with the profile's learned state, and seeds its rates."""
        path = self.settings.learned_path
        if not path:
            return
        try:
            data = helpers.read_json(path)
            entry = data.get(self.settings.profile, {})
            if not isinstance(entry, dict):
                entry = {}
            entry['calibration'] = dict(self.results, updated=datetime.datetime.now().isoformat(timespec='seconds'))
            fine = self._rates.get(self.settings.pulse_pwm)
            fast = self._rates.get(self.settings.pulse_fast_pwm)
            if fine:
                entry['rate'] = fine
            if fast and self.settings.pulse_fast_pwm > self.settings.pulse_pwm:
                entry['fast_rate'] = fast
            data[self.settings.profile] = entry
            helpers.write_json(path, data)
        except OSError:
            logging.warning('Could not store the calibration in %s', path, exc_info=True)
