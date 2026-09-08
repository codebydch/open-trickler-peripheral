"""Stand-ins for the hardware, so the control logic can be exercised anywhere.

Nothing here stubs out the trickler's own classes. The scale classes, the motor class and
the control loop under test are the real ones -- only the serial port and the GPIO pin are
replaced. That is deliberate: the one failure that took the machine down was a mismatch
between `main.py` and `scales.py`, which a test using a stubbed scale object cannot see.
"""
import configparser
import decimal
import enum
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
    """

    def __init__(self, start_weight, fine_rate=0.3, coarse_rate=0.6,
                 lag=2, resolution='0.02', frame=and_frame, min_pwm=25.0,
                 stable_samples=3, kernel='0.02', seed=1):
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
        # When muted the scale stops sending, as if the serial link had dropped.
        self.mute = False
        self.port = FakeSerial()
        self.port.on_timeout = lambda: self.tick(0.1)
        self._emit()

    def _grain_weight(self):
        """One grain of powder. Not all the same: stick powder is cut, not milled, so
        grains vary by a quarter either way. A simulator where every grain weighs exactly
        one scale division lets a charge land dead on target every time, which flatters
        the code and teaches nothing."""
        return self.kernel * D(str(round(self._random.uniform(0.75, 1.25), 4)))

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
        for motor in (self.motor1, self.motor2):
            self.true_weight += self._deliver(round(motor.flow(dt), 7))
        self._history.append(self.true_weight)
        self.elapsed += dt
        self._emit()

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
    """Enough of a pymemcache client for the daemons, backed by a plain dict."""

    def __bool__(self):
        # A real client is always truthy; an empty dict would not be.
        return True

    def get(self, key, default=None):
        return dict.get(self, key, default)

    def set(self, key, value):
        self[key] = value

    def set_multi(self, mapping):
        self.update(mapping)

    def delete(self, key):
        self.pop(key, None)


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
