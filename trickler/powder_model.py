#!/usr/bin/env python3
"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral

A powder as the calibration sweep measured it, and charges simulated against it.

The sweep records, for each (pulse speed, pulse length) cell, what every pulse delivered
by the time the scale was read and what landed in the second after. That is the whole
model: no rate, no spin-up, just the doses this powder actually gave. A simulated charge
runs the real PulseFeeder on a virtual clock against a pan that hands out those doses,
so what is scored is the shipped final-approach code with candidate settings, on this
powder's own lumps.
"""
import collections
import decimal
import enum
import itertools
import random

import main


D = decimal.Decimal

# A simulated scale frame, and so the virtual clock's tick when the feeder reads it.
FRAME_SECONDS = 0.05
# Reading the result of a pulse costs about this on top of the settle wait: the frame
# that carries the stable flag, and the loop around it.
READ_SECONDS = 0.15
# How long after the read the recorded tail has landed (the sweep measured it at 1 s).
TAIL_SECONDS = 1.0
# A charge that has not finished in this many pulses is counted as a failure, not run on.
MAX_SIMULATED_PULSES = 40
# How much continuous trickling precedes the final approach on a typical charge: the
# measure drops ~52.5 gn for a 55 gn target on the reference machine. Only the difference
# between candidates matters -- where they hand over -- so a fixed distance serves.
TRICKLE_DISTANCE = D('2.5')


class Units(enum.Enum):
    """The units a scale class declares; the simulation only ever runs in grains, but
    trickler_settings() looks the other one up to decide on a conversion."""
    GRAINS = 0
    GRAMS = 1


class EmpiricalPowder:
    """What the sweep measured: (dose, tail) pairs per (speed %, pulse length) cell."""

    def __init__(self, cells, rng=None):
        """`cells` maps (speed_pct, on_time) to a list of (dose, tail) in weight units."""
        self.cells = {key: list(values) for key, values in cells.items() if values}
        if not self.cells:
            raise ValueError('a powder needs at least one measured cell')
        self._rng = rng or random.Random(1)

    def nearest(self, speed_pct, on_time):
        """The measured cell closest to a pulse: by speed first, then by length."""
        return min(self.cells, key=lambda key: (abs(key[0] - speed_pct), abs(key[1] - on_time)))

    def sample(self, speed_pct, on_time):
        """One (dose, tail) drawn from the nearest cell."""
        return self._rng.choice(self.cells[self.nearest(speed_pct, on_time)])

    def cell_summary(self, resolution=D('0.02')):
        """Per cell: pulses, share that delivered nothing, mean dose by the settle read,
        mean delivered in all (dose plus tail), share of four grains or more, the most.

        Nothing, bursts and the most are judged on what the pulse delivered in all: a
        grain that landed after the read still landed, and it is what decides heavy.
        """
        summary = {}
        for key, values in sorted(self.cells.items()):
            doses = [D(str(dose)) for dose, _ in values]
            tails = [D(str(tail)) for _, tail in values]
            totals = [dose + tail for dose, tail in zip(doses, tails)]
            summary[key] = {
                'pulses': len(doses),
                'zeros': sum(1 for t in totals if t <= 0) / len(totals),
                'mean_dose': float(sum(doses) / len(doses)),
                'mean_total': float(sum(totals) / len(totals)),
                'bursts': sum(1 for t in totals if t >= resolution * 4) / len(totals),
                'max_dose': float(max(totals)),
                'mean_tail': float(sum(tails) / len(tails)),
            }
        return summary


class VirtualClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SimulatedPan:
    """The scale surface PulseFeeder and settled_weight use, on a virtual clock.

    Powder lands on a schedule: the dose of a pulse just after the pulse-off pause, the
    tail a second later. Reading a frame advances the clock -- which is also what stops
    settled_weight() spinning on a clock nothing else moves.
    """

    Units = Units

    def __init__(self, clock, start_weight, resolution=D('0.02')):
        self._clock = clock
        self.resolution = resolution
        self.unit = Units.GRAINS
        self.is_fresh = True
        self.is_stable = True
        self._true = D(str(start_weight))
        self._landing = []          # (time, weight)

    def land_later(self, delay, weight):
        if weight:
            self._landing.append((self._clock() + delay, D(str(weight))))

    def _settle(self):
        now = self._clock()
        still_up = []
        for when, weight in self._landing:
            if when <= now:
                self._true += weight
            else:
                still_up.append((when, weight))
        self._landing = still_up
        # The scale calls itself stable once the last thing to land has had a frame or two.
        self.is_stable = not any(when - now < FRAME_SECONDS * 2 for when, _ in self._landing)

    @property
    def weight(self):
        self._settle()
        return (self._true / self.resolution).quantize(D('1'), rounding=decimal.ROUND_HALF_UP) * self.resolution

    @property
    def outstanding(self):
        """Powder in the air that has not landed yet."""
        return sum((weight for _, weight in self._landing), D('0'))

    def update(self):
        self._clock.advance(FRAME_SECONDS)
        self._settle()


class SimulatedMotor:
    """Trickler 1 as the feeder drives it; a pulse's dose is drawn when it switches off."""

    def __init__(self, clock, pan, powder, pulse_off_time):
        self._clock = clock
        self._pan = pan
        self._powder = powder
        self._pulse_off_time = pulse_off_time
        self.speed = 0.0
        self._on_since = None
        self.pulses = []            # (speed_pct, on_time, dose, tail)

    def set_speed(self, speed):
        if speed > 0 and self._on_since is None:
            self._on_since = self._clock()
        self.speed = speed

    def off(self):
        if self._on_since is not None:
            on_time = self._clock() - self._on_since
            speed_pct = round(self.speed * 100, 1)
            dose, tail = self._powder.sample(speed_pct, on_time)
            self.pulses.append((speed_pct, on_time, dose, tail))
            # The recorded dose is what had landed at the settle read; the tail came a
            # second later. Land them where the feeder will see them the same way.
            self._pan.land_later(self._pulse_off_time + FRAME_SECONDS, dose)
            self._pan.land_later(self._pulse_off_time + TAIL_SECONDS, tail)
            self._on_since = None
        self.speed = 0.0


ChargeResult = collections.namedtuple('ChargeResult', ('error', 'seconds', 'pulses', 'finished'))


class ChargeSimulator:
    """Runs the real PulseFeeder, on simulated time, against an EmpiricalPowder.

    `rates` maps speed % to the feed rate the sweep fitted there; the feeder's model is
    seeded with them so no pulse is a probe, as on a machine that has calibrated.
    """

    def __init__(self, powder, settings, rates, continuous_rate, rng=None,
                 trickle_distance=TRICKLE_DISTANCE):
        self.powder = powder
        self.settings = settings
        self.rates = rates
        self.continuous_rate = float(continuous_rate) if continuous_rate else None
        self.trickle_distance = D(str(trickle_distance))
        self._rng = rng or random.Random(2)

    def _seeded_model(self):
        model = main.FeedModel(self.settings._replace(learned_path=''))
        model.rate = max(self.rates.get(self.settings.pulse_pwm, model.rate), main.MIN_PULSE_RATE)
        model.measured = True
        if self.settings.pulse_fast_pwm > self.settings.pulse_pwm:
            model.fast_rate = max(self.rates.get(self.settings.pulse_fast_pwm, model.rate),
                                  main.MIN_PULSE_RATE)
        return model

    def run(self, target=D('55.00')):
        """One charge from the handover to the end of the final approach."""
        settings = self.settings
        # The continuous phase hands over a little past pulse_trickle_weight -- powder in
        # flight -- by a varying amount: 0.28-0.52 gn for a setting of 0.5 on the bench.
        handover = settings.pulse_trickle_weight * D(str(round(self._rng.uniform(0.6, 1.05), 3)))
        clock = VirtualClock()
        pan = SimulatedPan(clock, target - handover, resolution=D('0.02'))
        motor = SimulatedMotor(clock, pan, self.powder, settings.pulse_off_time)
        feeder = main.PulseFeeder(motor, pan, settings, model=self._seeded_model(),
                                  clock=clock, sleep=clock.advance)

        started = clock()
        weight = feeder.settled_weight(settings.settle_min_time)
        finished = False
        while feeder.pulses < MAX_SIMULATED_PULSES:
            remainder = target - weight
            if feeder.done(remainder):
                finished = True
                break
            feeder.feed(remainder)
            weight = pan.weight
            clock.advance(READ_SECONDS)
        pulse_seconds = clock() - started
        continuous_seconds = 0.0
        if self.continuous_rate:
            distance = max(D('0'), self.trickle_distance - settings.pulse_trickle_weight)
            # Both tricklers run in the continuous phase, so about twice the primed rate.
            continuous_seconds = float(distance) / (2 * self.continuous_rate)
        landed = pan.weight + pan.outstanding
        return ChargeResult(float(landed - target), pulse_seconds + continuous_seconds,
                            feeder.pulses, finished)


Prediction = collections.namedtuple('Prediction', ('seconds', 'heavy', 'light', 'unfinished', 'pulses'))


def predict(simulator, charges, resolution=0.02):
    """Runs `charges` simulated charges and summarises them."""
    results = [simulator.run() for _ in range(charges)]
    n = len(results)
    return Prediction(
        seconds=sum(r.seconds for r in results) / n,
        heavy=sum(1 for r in results if r.error >= resolution - 1e-9) / n,
        light=sum(1 for r in results if r.error <= -resolution + 1e-9) / n,
        unfinished=sum(1 for r in results if not r.finished) / n,
        pulses=sum(r.pulses for r in results) / n)


# The final-approach settings the recommendation may move, and the values it tries.
TRICKLE_WEIGHTS = ('0.3', '0.5', '0.8')
AIMS = (0.7, 0.85)
FAST_UNTILS = ('0.10', '0.16')


def candidates(base, speeds, durations):
    """Every setting set the recommendation considers, as TricklerSettings."""
    speeds = sorted(float(s) for s in speeds)
    durations = sorted(float(d) for d in durations)
    for fine, fast in itertools.combinations_with_replacement(speeds, 2):
        untils = FAST_UNTILS if fast > fine else (str(base.pulse_fast_until),)
        for on_time, trickle, aim, until in itertools.product(durations, TRICKLE_WEIGHTS, AIMS, untils):
            yield base._replace(
                pulse_pwm=fine, pulse_fast_pwm=fast, pulse_on_time=on_time,
                pulse_trickle_weight=D(trickle), pulse_aim=aim, pulse_fast_until=D(until))


def single_changes(base, speeds, durations):
    """Every variant of `base` that moves exactly one setting to another grid value.

    The owner's rule at the bench is one change per run, three charges, the display noted
    after each -- so the variants that can actually be tried next are the ones that move
    one thing. Yields (setting, from, to, settings).
    """
    speeds = sorted(float(s) for s in speeds)
    durations = sorted(float(d) for d in durations)
    fine, fast = float(base.pulse_pwm), float(base.pulse_fast_pwm)
    choices = (
        ('pulse_pwm', [s for s in speeds if s <= fast]),
        ('pulse_fast_pwm', [s for s in speeds if s >= fine]),
        ('pulse_on_time', durations),
        ('pulse_trickle_weight', [D(t) for t in TRICKLE_WEIGHTS]),
        ('pulse_aim', list(AIMS)),
        ('pulse_fast_until', [D(u) for u in FAST_UNTILS] if fast > fine else []),
    )
    for name, values in choices:
        current = getattr(base, name)
        for value in values:
            if float(value) == float(current):
                continue
            yield name, float(current), float(value), base._replace(**{name: value})


def recommend(powder, base, rates, continuous_rate, speeds, durations, heavy_limit=0.25,
              first_pass=60, second_pass=300, finalists=12, progress=None, stop=None, seed=3):
    """Picks the fastest candidate whose heavy rate stays under `heavy_limit`.

    Two passes: a quick look at every candidate, then a longer look at the best few, so
    the whole thing stays around a minute on a Pi Zero 2. `progress(fraction)` is called
    as it goes; `stop()` returning True ends it early with whatever is known.

    Returns a dict: `recommended` (settings dict + prediction), `current` (the base
    settings' prediction), `runners_up`, `single_changes` (each variant of the current
    settings that moves one value, best first, at the longer look), `reason` (why the
    best does not qualify, or None), and `evaluated` (how many candidates).
    """
    rng = random.Random(seed)
    scored = []
    pool = list(candidates(base, speeds, durations))
    variants = list(single_changes(base, speeds, durations))
    total = len(pool) + finalists + len(variants) + 1
    done = 0

    def evaluate(settings, charges):
        sim = ChargeSimulator(powder, settings, rates, continuous_rate, rng=rng)
        return predict(sim, charges)

    current = evaluate(base, second_pass)
    for settings in pool:
        if stop and stop():
            break
        scored.append((settings, evaluate(settings, first_pass)))
        done += 1
        if progress:
            progress(done / total)

    def qualifies(prediction):
        return prediction.unfinished == 0 and prediction.heavy <= heavy_limit

    def rank(item):
        """Finishes within the limit, fastest first; then finishes but heavy too often,
        least heavy first; a setting that does not finish every charge comes last
        whatever else it does. The first version ranked the rest by heavy rate alone,
        and the least heavy setting is the one that never delivers anything: on the
        bench it recommended 25% / 0.15 s, which its own prediction said would not
        finish 99.7% of charges, and the trickler took minutes to do nothing."""
        settings, prediction = item
        if qualifies(prediction):
            return (0, prediction.seconds, prediction.heavy)
        if prediction.unfinished == 0:
            return (1, prediction.heavy, prediction.seconds)
        return (2, prediction.unfinished, prediction.seconds)

    scored.sort(key=rank)
    refined = []
    for settings, _ in scored[:finalists]:
        if stop and stop():
            break
        refined.append((settings, evaluate(settings, second_pass)))
        done += 1
        if progress:
            progress(min(1.0, done / total))
    refined.sort(key=rank)
    if not refined:
        # Stopped before any finalist got its longer look: report the quick pass.
        refined = scored[:finalists]
    if not refined:
        return None

    def describe(settings, prediction):
        return {
            'settings': settings_to_dict(settings),
            'prediction': prediction._asdict(),
            'meets_limit': qualifies(prediction),
        }

    changes = []
    for name, before, after, settings in variants:
        if stop and stop():
            break
        prediction = evaluate(settings, second_pass)
        changes.append(dict(describe(settings, prediction),
                            change={'setting': name, 'from': before, 'to': after}))
        done += 1
        if progress:
            progress(min(1.0, done / total))
    changes.sort(key=lambda c: rank((None, Prediction(**c['prediction']))))

    best_settings, best_prediction = refined[0]
    reason = None
    if not qualifies(best_prediction):
        if best_prediction.unfinished > 0:
            reason = ('No setting in the grid finished every simulated charge; the best '
                      'left %.0f%% unfinished. Keep the current settings.'
                      % (best_prediction.unfinished * 100))
        else:
            reason = ('No setting in the grid kept the heavy rate under %.0f%%; the best '
                      'was %.0f%%. Keep the current settings, or widen the grid.'
                      % (heavy_limit * 100, best_prediction.heavy * 100))
    return {
        'heavy_limit': heavy_limit,
        'current': describe(base, current),
        'recommended': describe(best_settings, best_prediction),
        'reason': reason,
        'runners_up': [describe(s, p) for s, p in refined[1:4]],
        'single_changes': changes,
        'evaluated': len(scored),
    }


RECOMMENDED_KEYS = ('pulse_pwm', 'pulse_fast_pwm', 'pulse_fast_until', 'pulse_on_time',
                    'pulse_trickle_weight', 'pulse_aim')


def settings_to_dict(settings):
    """The settings the recommendation moves, as plain numbers for JSON and the page."""
    return {key: float(getattr(settings, key)) for key in RECOMMENDED_KEYS}
