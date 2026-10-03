"""The simulator's contract with the bench.

`SimulatedMachine(tube=True)` is fitted to the pulse record of 2026-10-03 (`pulses.csv`
on the reference machine, nine charges, 30/45 and 30/30, settle 0.3 and 0.6). Each case
below is one regime from that record, with the statistics the record showed. If one of
these fails, the model has drifted from the machine, and anything proved on it -- the
calibration routine, an endgame policy -- is proved on something else.

Numbers are from the record, not the model: 45% pulses after continuous running never
dropped nothing and averaged ~3 grains with one in three dropping four or more; 30%
pulses in a run dropped nothing a third of the time; a 30 ms fine pulse right after fast
pulses still dropped a grain or three; and after a run of fine pulses, a quarter-second
pulse dropped nothing six times in ten. About two in five grains of a short pulse were
still in the air when the scale was read 0.3 s later.
"""
import statistics
import unittest

from tests import fakes


def prime(machine, speed, seconds):
    """Runs trickler 1 continuously, then lets everything land."""
    machine.motor1.set_speed(speed)
    for _ in range(int(seconds / 0.05)):
        machine.tick(0.05)
    machine.motor1.off()
    machine.settle(1.0)


def pulses(machine, speed, on_time, count, settle_read):
    """Fires pulses the way PulseFeeder does and reads the dose `settle_read` later.

    Returns (doses, tails) in grains: the dose is what had landed at the read, the tail
    what landed in the second after it.
    """
    doses, tails = [], []
    motor = machine.motor1
    for _ in range(count):
        before = machine.true_weight
        motor.set_speed(speed)
        elapsed = 0.0
        while elapsed < on_time - 1e-9:
            machine.tick(0.05)
            elapsed += 0.05
        motor.off()
        for _ in range(int(round((0.1 + settle_read) / 0.05))):
            machine.tick(0.05)
        read = machine.true_weight
        for _ in range(20):
            machine.tick(0.05)
        doses.append(round(float(read - before) / 0.02))
        tails.append(round(float(machine.true_weight - read) / 0.02))
    return doses, tails


def summary(doses):
    return (sum(d <= 0 for d in doses) / len(doses),
            statistics.mean(doses),
            sum(d >= 4 for d in doses) / len(doses),
            max(doses))


SEEDS = range(12)


class DoseRegimeTest(unittest.TestCase):

    def collect(self, regime):
        doses, tails = [], []
        for seed in SEEDS:
            d, t = regime(seed)
            doses += d
            tails += t
        return doses, tails

    def assert_regime(self, doses, zeros, mean, bursts=None, at_least=None):
        p0, avg, p4, biggest = summary(doses)
        self.assertAlmostEqual(p0, zeros, delta=0.12,
                               msg='zero-dose rate %.0f%% vs bench %.0f%%' % (p0 * 100, zeros * 100))
        self.assertAlmostEqual(avg, mean, delta=0.7,
                               msg='mean %.2f grains vs bench %.1f' % (avg, mean))
        if bursts is not None:
            self.assertAlmostEqual(p4, bursts, delta=0.12,
                                   msg='four-grain rate %.0f%% vs bench %.0f%%' % (p4 * 100, bursts * 100))
        if at_least is not None:
            self.assertGreaterEqual(biggest, at_least, 'the record had a %d-grain pulse' % at_least)

    def test_fast_pulses_after_continuous_running(self):
        """45%, 0.4 s, read at 0.3 s: 30 pulses on the bench, none empty, mean ~3.2,
        a third of them four grains or more, one of nine."""
        def regime(seed):
            machine = fakes.SimulatedMachine('40.00', tube=True, seed=seed)
            prime(machine, 0.45, 8)
            return pulses(machine, 0.45, 0.4, 20, settle_read=0.3)
        doses, _ = self.collect(regime)
        self.assert_regime(doses, zeros=0.02, mean=3.2, bursts=0.35, at_least=8)

    def test_fine_speed_pulses_in_a_run(self):
        """30%, 0.4 s, read at 0.6 s: 23 pulses on the bench, a third empty, mean ~1.7,
        bursts to four grains."""
        def regime(seed):
            machine = fakes.SimulatedMachine('40.00', tube=True, seed=seed + 100)
            prime(machine, 0.45, 8)
            d, t = pulses(machine, 0.30, 0.4, 12, settle_read=0.6)
            return d[2:], t[2:]
        doses, _ = self.collect(regime)
        self.assert_regime(doses, zeros=0.35, mean=1.7, bursts=0.15, at_least=4)

    def test_a_short_fine_pulse_right_after_fast_pulses_still_delivers(self):
        """30%, 0.15 s (30 ms of movement), read at 0.3 s, straight after six 45%
        pulses: the loaded lip sheds on the jolt. Bench: a quarter empty, mean ~1.3."""
        doses, tails = self.collect(self._after_fast)
        self.assert_regime(doses, zeros=0.25, mean=1.3)

    def test_the_landing_tail_of_a_short_pulse(self):
        """About two grains in five of such a pulse were still in the air at the 0.3 s
        read, and landed in the second after it."""
        doses, tails = self.collect(self._after_fast)
        tail = sum(tails) / (sum(tails) + sum(doses))
        self.assertAlmostEqual(tail, 0.45, delta=0.15, msg='tail %.0f%%' % (tail * 100))

    @staticmethod
    def _after_fast(seed):
        machine = fakes.SimulatedMachine('40.00', tube=True, seed=seed + 200)
        prime(machine, 0.45, 8)
        pulses(machine, 0.45, 0.4, 6, settle_read=0.3)
        return pulses(machine, 0.30, 0.15, 3, settle_read=0.3)

    def test_one_grain_pulses_after_a_run_of_fine_pulses_mostly_deliver_nothing(self):
        """30%, 0.25 s, read at 0.6 s, after six 0.4 s pulses at 30%: the lip has been
        drawn down. Bench: 0, 0, 0, 0, 1, 1 and 1, 0, 1, 1 -- six in ten empty."""
        def regime(seed):
            machine = fakes.SimulatedMachine('40.00', tube=True, seed=seed + 300)
            prime(machine, 0.45, 8)
            pulses(machine, 0.30, 0.4, 6, settle_read=0.6)
            return pulses(machine, 0.30, 0.25, 6, settle_read=0.6)
        doses, _ = self.collect(regime)
        self.assert_regime(doses, zeros=0.60, mean=0.5)

    def test_identical_pulses_can_drop_nothing_and_then_four_grains(self):
        """The sequence the old model could not produce: 0, 0, 0, 0 then a burst."""
        seen_run_then_burst = False
        for seed in range(30):
            machine = fakes.SimulatedMachine('40.00', tube=True, seed=seed + 400)
            prime(machine, 0.45, 8)
            doses, _ = pulses(machine, 0.30, 0.4, 16, settle_read=0.6)
            for i in range(len(doses) - 3):
                if doses[i] == doses[i + 1] == doses[i + 2] == 0 and max(doses[i + 3:]) >= 4:
                    seen_run_then_burst = True
        self.assertTrue(seen_run_then_burst)


class IdlePanTest(unittest.TestCase):

    def test_a_still_pan_flickers_a_division_now_and_then(self):
        """The real scale's stable reading on an untouched pan steps 54.98 <-> 55.00
        every few seconds, both flagged stable."""
        machine = fakes.SimulatedMachine('55.00', tube=True, flicker=True, seed=7)
        readings = []
        for _ in range(600):            # a minute
            machine.tick(0.1)
            readings.append(machine._reported())
        changes = sum(1 for a, b in zip(readings, readings[1:]) if a != b)
        self.assertGreaterEqual(changes, 4)
        self.assertLessEqual(changes, 60)
        self.assertTrue(all(abs(r - fakes.D('55.00')) <= fakes.D('0.02') for r in readings))

    def test_without_flicker_a_still_pan_reads_the_same_every_time(self):
        machine = fakes.SimulatedMachine('55.00', tube=True, seed=7)
        readings = {machine._reported() for _ in range(300) if not machine.tick(0.1)}
        self.assertEqual(readings, {fakes.D('55.00')})


class OldModelUntouchedTest(unittest.TestCase):

    def test_the_default_machine_is_the_grain_by_grain_one(self):
        machine = fakes.SimulatedMachine('40.00')
        self.assertIsNone(machine.tubes)
        self.assertFalse(machine.flicker)


if __name__ == '__main__':
    unittest.main()
