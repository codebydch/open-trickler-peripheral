"""Stand-ins for the hardware, so the control logic can be exercised anywhere.

Nothing here stubs out the trickler's own classes. The scale classes, the motor class and
the control loop under test are the real ones -- only the serial port and the GPIO pin are
replaced. That is deliberate: the one failure that took the machine down was a mismatch
between `main.py` and `scales.py`, which a test using a stubbed scale object cannot see.
"""
import configparser
import decimal
import enum
import math
import os
import random

import motors

from tests import CONFIG_PATH


D = decimal.Decimal


def load_config(history_path=None, profiles=None, active_profile=None,
                **trickler_overrides):
    """Reads the shipped config, with [trickler] overrides and an isolated history.

    Charge history is off unless `history_path` is given: the shipped config points at
    /var/lib/opentrickler, and a test suite has no business writing there.
    `profiles` is a mapping of name -> {setting: value}, written as [profile:Name].
    """
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(CONFIG_PATH)
    for key, value in trickler_overrides.items():
        config['trickler'][key] = str(value)

    config['history']['enabled'] = 'True' if history_path else 'False'
    if history_path:
        config['history']['path'] = str(history_path)
        # The shipped example names /var/lib/opentrickler/pulses.csv outright, so point
        # the pulse file at the same scratch directory as the charges.
        config['history']['pulses_path'] = os.path.join(
            os.path.dirname(str(history_path)), 'pulses.csv')
        config['history']['learned_path'] = os.path.join(
            os.path.dirname(str(history_path)), 'learned.json')

    for name, settings in (profiles or {}).items():
        section = 'profile:%s' % name
        config.add_section(section)
        for key, value in settings.items():
            config[section][key] = str(value)
    if active_profile is not None:
        config['profiles']['active'] = active_profile
    return config


def constants_for(config):
    """Builds the memcache_vars enum the same way the daemons do."""
    return enum.Enum('memcache_vars', config['memcache_vars'])


# --- Frame builders -------------------------------------------------------------------
# Byte-exact frames for each supported scale, matching the field offsets its parser uses.

def and_frame(weight, unit='GN', status='ST'):
    """An A&D frame, e.g. b'ST,+00045.02 GN\\r\\n'."""
    return ('%s,%+09.2f %s\r\n' % (status, weight, unit)).encode()


def creedmoor_frame(weight, unit='GN', status=None):
    """A Creedmoor frame, e.g. b'+0045.02 GN\\r\\n'.

    This scale sends no status field; stability is inferred from repeated readings.
    """
    del status
    return ('%+08.2f %s\r\n' % (weight, unit)).encode()


def ussolid_frame(weight, unit='gn', status=None):
    """A U.S. Solid frame, e.g. b'+  45.020gn\\r\\n'.

    This scale sends no status field; stability is inferred from repeated readings.
    """
    del status
    return ('%+09.3f%s\r\n' % (weight, unit)).encode()


class FakeSerial:
    """A pyserial port that hands back pre-scripted bytes.

    `readline()` returns whatever is buffered when no terminator has arrived, which is how
    a real port behaves when it times out mid-frame.
    """

    def __init__(self, chunks=()):
        self._pending = bytearray()
        self.written = []
        self.closed = False
        # Called when readline() finds no complete frame, standing in for the port's read
        # timeout. Without it a simulated clock never advances while the scale is silent.
        self.on_timeout = None
        for chunk in chunks:
            self.feed(chunk)

    def feed(self, data):
        """Queues bytes as though the scale had just sent them."""
        self._pending.extend(data if isinstance(data, (bytes, bytearray)) else data.encode())

    @property
    def in_waiting(self):
        return len(self._pending)

    def read(self, size=1):
        chunk = bytes(self._pending[:size])
        del self._pending[:size]
        return chunk

    def readline(self):
        end = self._pending.find(b'\n')
        if end == -1 and self.on_timeout is not None:
            self.on_timeout()
            end = self._pending.find(b'\n')
        if end == -1:
            # Timed out with only a partial frame available.
            chunk = bytes(self._pending)
            self._pending.clear()
            return chunk
        chunk = bytes(self._pending[:end + 1])
        del self._pending[:end + 1]
        return chunk

    def reset_input_buffer(self):
        self._pending.clear()

    def write(self, data):
        self.written.append(data)

    def close(self):
        self.closed = True


# --- A simulated machine --------------------------------------------------------------

STALL_PWM = 0.20        # below this the vibratory motor moves no powder
# Seconds of running before powder actually starts moving. Measured on the bench, not
# guessed: 120 pulses at 0.2 s averaged 0.0178 gn and 32 at 0.4 s averaged 0.0613 -- 3.4x
# the powder for twice the pulse, which solves to a 0.118 s spin-up. It was 0.02 here for
# a long time, and a simulator that starts feeding almost instantly cannot show the thing
# that dominates a short pulse on the real machine.
SPIN_UP = 0.12
# How far the heaviest grain runs over the nominal one, as a multiplier. Stick powder is
# cut, not milled, so grains vary either side of the average by about a quarter.
GRAIN_SPREAD = D('1.25')


class VibratoryMotor:
    """A trickler motor: nothing below the stall point, and a spin-up delay.

    Exposes the same surface `main.py` drives, and borrows the real `update()` so the
    clamping logic under test is the shipped one.
    """

    def __init__(self, rate_at_25, min_pwm=25.0, max_pwm=100.0):
        self.min_pwm = min_pwm
        self.max_pwm = max_pwm
        self.speed = 0.0
        self.commands = []
        self._run_time = 0.0
        # Pin the whole speed/rate curve from one measured point.
        self._slope = rate_at_25 / (0.25 - STALL_PWM)

    # The clamping under test is the shipped one.
    update = motors.TricklerMotor.update

    def set_speed(self, speed):
        if 0 <= speed <= 1:
            if speed == 0:
                self._run_time = 0.0
            self.speed = speed
            self.commands.append(speed)

    def off(self):
        self.set_speed(0)

    def flow(self, dt):
        """Weight delivered over `dt` seconds at the current speed."""
        if self.speed <= STALL_PWM:
            self._run_time = 0.0
            return 0.0
        moving = max(0.0, min(dt, self._run_time + dt - SPIN_UP))
        self._run_time += dt
        return self._slope * (self.speed - STALL_PWM) * moving


class Tube:
    """The lip of a trickler tube, where grains wait to fall: what makes pulses lumpy.

    Fitted to the bench record of 2026-10-03, which the old continuous model could not
    produce: identical 0.4 s pulses at 30% dropping 0, 0, 0, 0, 0.02 and then 0.08; a
    nine-grain pulse at 45%; a 30 ms fine pulse dropping three grains right after fast
    pulses, and nothing at all a few pulses later.

    Running the motor moves grains from the hopper onto a ramp, and from the ramp onto
    the lip, at drive-dependent rates. Grains leave the lip in *events* -- a Poisson
    process while moving, rate rising with drive -- and each event takes a clump, which
    is what a burst is. The jolt of the motor starting shakes a clump loose on its own
    with some probability, which is why a pulse too short to move much still delivers
    when the lip is loaded. An empty lip delivers nothing however long the pulse, which
    is the run of zeros.

    Grains that have left land `landing_delay()` later: most inside a quarter of a
    second, some the better part of a second. That is the tail that a 0.3 s settle
    credits to the next pulse.
    """

    # The defaults were fitted to the 2026-10-03 record by tests/test_simulator.py's
    # regimes: a grid over these values, scored against the table there.
    def __init__(self, rng, ramp_rate=40.0, lip_rate=220.0, lip_capacity=20.0,
                 event_rate=16.0, burst_chance=0.06, kick=1.0, tail_share=0.5,
                 lip_exponent=2.0, tail_max=1.2):
        self._rng = rng
        self.tail_share = tail_share    # share of grains that take the slow way down
        self.tail_max = tail_max        # the slowest of them lands this long after leaving
        self.lip_exponent = lip_exponent  # how steeply lip feed falls off below full drive
        self.ramp = 0.0         # grains on the ramp, fed from the hopper while running
        self.lip = 0.0          # grains at the lip, ready to fall
        self.ramp_rate = ramp_rate      # grains/s hopper -> ramp at full drive above stall
        # Ramp -> lip, at full drive. Falls off faster than the drive does (drive^1.5).
        # At 45% the lip is fed faster than discharge empties it, so continuous running
        # leaves it full and the next pulses burst; at 30% it is fed slower than 0.4 s
        # pulses empty it, so a run of them starves it -- the run of zeros on the bench.
        self.lip_rate = lip_rate
        self.lip_capacity = lip_capacity
        self.event_rate = event_rate    # discharge events/s at full drive above stall
        self.burst_chance = burst_chance  # an event that takes a clump instead of a grain
        # Chance the motor starting shakes a clump loose. Scales with the square of how
        # full the lip is: a loaded lip (right after fast pulses) nearly always sheds on
        # the jolt, a half-empty one rarely -- which is how a 30 ms fine pulse delivers
        # three grains after fast pulses and nothing after a run of fine ones.
        self.kick = kick

    @staticmethod
    def _drive(speed):
        """Fraction of full drive above the stall point, 0 at or below it."""
        return max(0.0, (speed - STALL_PWM) / (1.0 - STALL_PWM))

    def _clump(self):
        """How many grains one discharge event takes: one, or now and then a clump."""
        size = 1
        if self._rng.random() < self.burst_chance:
            size += self._rng.randint(2, 5)
        return min(size, int(self.lip))

    def run(self, dt, speed, starting):
        """Moves powder for `dt` seconds at `speed` (0-1); returns whole grains released.

        `starting` is True on the step the motor was switched on.
        """
        drive = self._drive(speed)
        if drive <= 0:
            return 0
        self.ramp += self.ramp_rate * drive * dt
        onto_lip = min(self.ramp, self.lip_rate * drive ** self.lip_exponent * dt,
                       max(0.0, self.lip_capacity - self.lip))
        self.ramp -= onto_lip
        self.lip += onto_lip

        released = 0
        events = 0
        # Poisson count of discharge events in dt.
        expected = self.event_rate * drive * dt
        threshold = math.exp(-expected)
        product = self._rng.random()
        while product > threshold:
            events += 1
            product *= self._rng.random()
        for _ in range(events):
            if self.lip < 1:
                break
            grains = self._clump()
            self.lip -= grains
            released += grains
        # The jolt of the motor starting shakes loose whatever is teetering: from a loaded
        # lip a clump of several grains, from a half-empty one a grain or nothing.
        fullness = min(1.0, self.lip / self.lip_capacity)
        if starting and self.lip >= 1 and self._rng.random() < self.kick * fullness ** 2:
            grains = min(int(self.lip), 1 + int(self._rng.random() * (1 + 3 * fullness)))
            self.lip -= grains
            released += grains
        return released

    def landing_delay(self):
        """Seconds from leaving the lip to landing in the pan."""
        if self._rng.random() >= self.tail_share:
            return self._rng.uniform(0.05, 0.3)
        return self._rng.uniform(0.3, self.tail_max)


class SimulatedMachine:
    """Motors feeding a pan on a scale that lags, quantises, and streams frames.

    The scale side is driven through a real `SerialScale` subclass over `self.port`, so
    tests exercise the actual framing and parsing rather than a stubbed weight attribute.

    Powder arrives as whole kernels, not as a smooth fluid, because that is what the
    machine does. Every dose ever measured on the bench was a multiple of 0.02 gn --
    0.00, 0.02, 0.04, 0.06, never 0.01 or 0.03 -- since one grain of stick powder and one
    division of the scale are both about that. Modelling it as a fluid is what hid four
    separate defects: a rate estimate biased three times high, charges abandoned as
    "hopper empty" when a couple of pulses happened to drop no grain, aiming for doses
    that cannot exist, and a stopping rule finer than the machine can see.

    Set `kernel` to 0 for the old continuous behaviour.

    `tube=True` replaces the grain-by-grain model with a `Tube` per motor -- lumpy,
    bursty, with a landing tail -- fitted to the bench record; `flicker=True` makes a
    still pan's stable reading wander a division now and then, as the real scale's does.
    Both are off by default so the older tests keep testing what they tested.
    """

    def __init__(self, start_weight, fine_rate=0.3, coarse_rate=0.6,
                 lag=2, resolution='0.02', frame=and_frame, min_pwm=25.0,
                 stable_samples=3, kernel='0.02', seed=1, tube=False, flicker=False):
        self.true_weight = D(str(start_weight))
        self.resolution = D(resolution)
        self.kernel = D(str(kernel))
        self.elapsed = 0.0
        self.lag = lag
        self.stable_samples = stable_samples
        self._frame = frame
        self._history = [self.true_weight]
        self._recent_reported = []
        self.motor1 = VibratoryMotor(fine_rate, min_pwm)
        self.motor2 = VibratoryMotor(coarse_rate, min_pwm)
        # Powder the motors have moved but that has not yet fallen as a whole grain.
        self._pending = D('0')
        # Which grain falls when is chance, but a repeatable one: a test that fails
        # should fail the same way next time.
        self._random = random.Random(seed)
        self._next_grain = self._grain_weight()
        # The lumpy model: a tube per motor, grains in the air waiting to land as
        # (landing_time, weight), and whether each motor was running last tick.
        # `tube` may be True for the fitted defaults, or a dict of Tube() arguments.
        tube_args = tube if isinstance(tube, dict) else {}
        self.tubes = ({self.motor1: Tube(self._random, **tube_args),
                       self.motor2: Tube(self._random, **tube_args)} if tube else None)
        self._airborne = []
        self._was_running = {self.motor1: False, self.motor2: False}
        # A still pan's stable reading steps a division up or down every few seconds.
        self.flicker = flicker
        self._flicker_offset = D('0')
        self._next_flicker = 0.0
        # When muted the scale stops sending, as if the serial link had dropped.
        self.mute = False
        self.port = FakeSerial()
        self.port.on_timeout = lambda: self.tick(0.1)
        self._emit()

    @property
    def largest_grain(self):
        """The heaviest single grain this machine can drop.

        What a charge can overshoot by: the feeder stops as soon as the reading reaches
        the target, so the last grain to land is the last thing that can push it over.
        """
        return self.kernel * GRAIN_SPREAD

    def _grain_weight(self):
        """One grain of powder. Not all the same: stick powder is cut, not milled, so
        grains vary by a quarter either way. A simulator where every grain weighs exactly
        one scale division lets a charge land dead on target every time, which flatters
        the code and teaches nothing."""
        return self.kernel * D(str(
            round(self._random.uniform(float(2 - GRAIN_SPREAD), float(GRAIN_SPREAD)), 4)))

    def _deliver(self, weight):
        """Turns continuous flow into whole grains landing in the pan.

        Powder the motor has moved accumulates, and grains fall off the lip one at a
        time as enough of it arrives. The remainder sits on the ramp, which is why a
        pulse can deliver nothing and the next one two grains.
        """
        if self.kernel <= 0:
            return D(str(weight))
        self._pending += D(str(weight))
        delivered = D('0')
        while self._pending >= self._next_grain:
            # A grain teetering on the lip waits for a later pulse.
            if self._random.random() < 0.15:
                break
            self._pending -= self._next_grain
            delivered += self._next_grain
            self._next_grain = self._grain_weight()
        return delivered

    def _reported(self):
        """What the scale would display now: `lag` samples behind, rounded to a division."""
        raw = self._history[-1 - self.lag] if len(self._history) > self.lag else self._history[0]
        if self.flicker and self.elapsed >= self._next_flicker:
            self._flicker_offset = (D('0') if self._flicker_offset else
                                    self.resolution * self._random.choice((-1, 1)))
            self._next_flicker = self.elapsed + self._random.uniform(2.0, 10.0)
        raw += self._flicker_offset
        return (raw / self.resolution).quantize(D('1'), rounding=decimal.ROUND_HALF_UP) * self.resolution

    @property
    def is_settled(self):
        """True once the displayed weight has held still, as a real scale judges it."""
        return (len(self._recent_reported) >= self.stable_samples
                and len(set(self._recent_reported[-self.stable_samples:])) == 1)

    def _emit(self):
        if self.mute:
            return
        reported = self._reported()
        self._recent_reported.append(reported)
        status = 'ST' if self.is_settled else 'US'
        self.port.feed(self._frame(float(reported), status=status))

    def tick(self, dt):
        """Advances the simulation, landing powder and emitting a fresh frame."""
        if self.tubes is None:
            for motor in (self.motor1, self.motor2):
                self.true_weight += self._deliver(round(motor.flow(dt), 7))
        else:
            self._tick_tubes(dt)
        self._history.append(self.true_weight)
        self.elapsed += dt
        self._emit()

    def _tick_tubes(self, dt):
        """The lumpy model: each motor's tube releases grains, which land a little later."""
        for motor in (self.motor1, self.motor2):
            running = motor.speed > STALL_PWM
            starting = running and not self._was_running[motor]
            self._was_running[motor] = running
            if not running:
                motor._run_time = 0.0
                continue
            # Only the part of the tick after the motor has spun up moves powder; the
            # jolt of starting is passed on its own, since it sheds grains by itself.
            moving = max(0.0, min(dt, motor._run_time + dt - SPIN_UP))
            motor._run_time += dt
            grains = self.tubes[motor].run(moving, motor.speed, starting)
            for _ in range(grains):
                self._airborne.append((self.elapsed + self.tubes[motor].landing_delay(),
                                       self._grain_weight()))
        landing_now = self.elapsed + dt
        still_up = []
        for land_at, weight in self._airborne:
            if land_at <= landing_now:
                self.true_weight += weight
            else:
                still_up.append((land_at, weight))
        self._airborne = still_up

    def settle(self, seconds=1.0):
        """Runs time forward with the motors off, so in-flight powder lands."""
        for _ in range(int(seconds / 0.1)):
            self.tick(0.1)

    def virtual_clock(self):
        """A `time.time` replacement tied to simulated, not wall, time."""
        return lambda: 1000.0 + self.elapsed

    def virtual_sleep(self):
        """A `time.sleep` replacement that advances the simulation instead of blocking."""
        return self.tick


class FakeMemcache(dict):
    """Enough of a pymemcache client for the daemons, backed by a plain dict.

    It checks keys the way pymemcache does -- no whitespace or control characters, at
    most 250 bytes -- because a profile named "Hodgdon H1000" got through the suite and
    took the daemon down on the bench.
    """

    def __bool__(self):
        # A real client is always truthy; an empty dict would not be.
        return True

    @staticmethod
    def check_key(key):
        encoded = key.encode('utf-8') if isinstance(key, str) else key
        if not isinstance(encoded, bytes):
            raise TypeError('memcache keys are strings: %r' % (key,))
        if len(encoded) > 250:
            raise ValueError('Key is too long: %r' % key)
        if any(c < 33 or c == 127 for c in encoded):
            raise ValueError('Key contains whitespace: %r' % key)
        return key

    def get(self, key, default=None):
        return dict.get(self, self.check_key(key), default)

    def set(self, key, value):
        self[self.check_key(key)] = value

    def set_multi(self, mapping):
        for key, value in mapping.items():
            self.set(key, value)

    def delete(self, key):
        self.pop(self.check_key(key), None)


class FakeLgpio:
    """Stands in for the lgpio module, recording what the servo asked the hardware to do.

    The pulse widths passed to tx_servo are the whole point: the servo's accuracy is the
    microseconds it is sent, and the reason this project drives lgpio directly rather than
    through gpiozero is that gpiozero's backend rounds them to 200 us steps.
    """

    class error(Exception):
        """lgpio's own exception type, which is not an OSError."""

    def __init__(self, busy=False):
        # Set busy to have the line refuse to be claimed, as it does when another
        # process is already holding it.
        self.busy = busy
        self.open_chips = []
        self.claimed = []
        self.pulses = []
        self.freed = []
        self.closed = []
        self._next_handle = 100

    def gpiochip_open(self, chip):
        self._next_handle += 1
        self.open_chips.append((chip, self._next_handle))
        return self._next_handle

    def gpio_claim_output(self, handle, gpio):
        if self.busy:
            raise self.error('GPIO busy')
        self.claimed.append((handle, gpio))

    def tx_servo(self, handle, gpio, pulse_width, *args, **kwargs):
        self.pulses.append(pulse_width)

    def gpio_free(self, handle, gpio):
        self.freed.append((handle, gpio))

    def gpiochip_close(self, handle):
        self.closed.append(handle)

    @property
    def held_lines(self):
        """Lines claimed on handles that have not been closed."""
        return [(handle, gpio) for handle, gpio in self.claimed
                if handle not in self.closed]


class SimulatedMeasure:
    """A powder measure worked by the servo, which can jam.

    Stands in for motors.ServoMotor in the dump. A jam is all-or-nothing on this
    machine: a kernel caught in the drum stops the handle dead and the servo cannot
    shear it, so a jammed cycle delivers nothing at all rather than a short charge.

    `jams` is how many attempts jam before one works; None means it never clears.
    """

    def __init__(self, machine, drop=25.0, jams=0):
        self.machine = machine
        self.drop = D(str(drop))
        self.jams = jams
        self.cycles = 0
        self.released = 0

    def run_servo(self):
        self.cycles += 1
        if self.jams is None or self.cycles <= self.jams:
            return
        self.machine.true_weight += self.drop

    def set_initial_angle(self):
        pass

    def off(self):
        self.released += 1

    def stop(self):
        self.off()
