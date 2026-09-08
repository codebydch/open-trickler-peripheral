"""The control loop, end to end against a simulated machine.

The scale here is a real `ANDScale` reading real frames off a fake serial port. That is
the point of this module: the one failure that took the machine down in the field was
`main.py` calling an attribute `scales.py` did not have, which no amount of testing
against a stubbed scale object would have found.
"""
import decimal
import logging
import time
import unittest
from unittest import mock

import main
import scales
import PID

from tests import fakes


D = decimal.Decimal


def run_charge(machine, target, config=None, memcache=None, pid=None):
    """Runs one charge to completion against `machine`, on simulated time."""
    config = config or fakes.load_config()
    memcache = memcache if memcache is not None else fakes.FakeMemcache({'auto_mode': True})
    with mock.patch.object(scales.serial, 'Serial', return_value=machine.port):
        scale = scales.ANDScale(config)
    quiet = logging.getLogger('pid_tune')

    with mock.patch.object(time, 'sleep', machine.virtual_sleep()), \
         mock.patch.object(time, 'time', machine.virtual_clock()):
        # Build the PID inside the patched clock. It stamps last_time on construction,
        # and a controller holding a timestamp from a different clock computes a negative
        # delta and then never updates its output again.
        pid = pid or PID.PID(*(float(config['PID'][k]) for k in ('Kp', 'Ki', 'Kd')))
        pid.SetPoint = 100.0
        main.trickler_loop(
            config, memcache, fakes.constants_for(config), pid,
            machine.motor1, machine.motor2, scale, target,
            scales.ANDScale.Units.GRAINS, quiet)
    machine.settle(0.5)
    return scale


class ChargeAccuracyTest(unittest.TestCase):
    """What the whole exercise was for.

    The bound is one grain, not zero. Powder arrives in whole grains of about 0.02 gn,
    which is also one division of the scale, so a charge can land on the target or one
    grain either side of it and there is no third option. Asking for better is asking the
    machine to split a grain of Varget.
    """

    def test_lands_within_one_grain(self):
        for start in ('43.80', '44.50', '44.90'):
            with self.subTest(start=start):
                machine = fakes.SimulatedMachine(start)
                run_charge(machine, D('45.00'))
                error = machine.true_weight - D('45.00')
                self.assertLessEqual(abs(error), machine.kernel,
                                     'landed %s off target' % error)

    def test_never_overshoots_badly_across_trickler_speeds(self):
        """A faster trickler must not blow past the target."""
        for rate in (0.15, 0.30, 0.60):
            with self.subTest(rate=rate):
                machine = fakes.SimulatedMachine('43.80', fine_rate=rate, coarse_rate=rate * 2)
                run_charge(machine, D('45.00'))
                error = machine.true_weight - D('45.00')
                self.assertLess(abs(error), D('0.05'), 'landed %s off target' % error)

    def test_a_wrong_seed_rate_is_learned_away(self):
        """The feeder measures what it delivers, so the configured starting rate should
        barely matter."""
        errors = []
        for seed in (0.05, 0.3, 3.0):
            machine = fakes.SimulatedMachine('44.50')
            run_charge(machine, D('45.00'), config=fakes.load_config(pulse_rate=seed))
            errors.append(machine.true_weight - D('45.00'))
        for error in errors:
            self.assertLessEqual(abs(error), D('0.02'),
                                 'seed changed the outcome: %s' % errors)


class ExitPathTest(unittest.TestCase):
    """Whatever ends a charge, the motors must stop."""

    def _assert_motors_off(self, machine):
        self.assertEqual(machine.motor1.speed, 0.0, 'motor 1 left running')
        self.assertEqual(machine.motor2.speed, 0.0, 'motor 2 left running')

    def test_motors_stop_on_completion(self):
        machine = fakes.SimulatedMachine('44.50')
        run_charge(machine, D('45.00'))
        self._assert_motors_off(machine)

    def test_motors_stop_when_auto_mode_is_switched_off(self):
        machine = fakes.SimulatedMachine('43.00')
        run_charge(machine, D('45.00'), memcache=fakes.FakeMemcache({'auto_mode': False}))
        self._assert_motors_off(machine)

    def test_motors_stop_when_the_pan_is_removed(self):
        machine = fakes.SimulatedMachine('-1.00')
        run_charge(machine, D('45.00'))
        self._assert_motors_off(machine)

    def test_motors_stop_when_the_loop_raises(self):
        """The `finally` has to hold even for a failure nobody predicted."""
        machine = fakes.SimulatedMachine('44.50')
        with mock.patch.object(main, 'pulse_phase', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                run_charge(machine, D('45.00'))
        self._assert_motors_off(machine)


class LegacyANDScale(scales.ANDScale):
    """An A&D scale as it was before the is_fresh flag existed.

    `scales.py` is a file people customise, and the deployed copy is not always the one
    the control loop was written against -- exactly the mismatch that took the machine
    down. Accessing `is_fresh` on this raises, as it did then.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.is_fresh

    def update(self):
        super().update()
        del self.is_fresh


class StaleReadingTest(unittest.TestCase):
    """Reads that produce no weight are routine, not a reason to abandon a charge."""

    def test_a_silent_scale_eventually_stops_the_charge(self):
        machine = fakes.SimulatedMachine('44.50')
        machine.mute = True
        machine.port.reset_input_buffer()
        with self.assertLogs(level='WARNING') as logs:
            run_charge(machine, D('45.00'))
        self.assertTrue(any('serial link' in line for line in logs.output), logs.output)
        self.assertGreaterEqual(
            machine.elapsed, main.STALE_READ_TIMEOUT,
            'gave up before the timeout; a few unreadable frames is normal traffic')

    def test_a_scale_without_is_fresh_still_completes_a_charge(self):
        """The regression that cost an evening on the bench."""
        machine = fakes.SimulatedMachine('44.50')
        config = fakes.load_config()
        with mock.patch.object(scales.serial, 'Serial', return_value=machine.port):
            scale = LegacyANDScale(config)
        self.assertFalse(hasattr(scale, 'is_fresh'), 'test scale should lack the flag')

        with mock.patch.object(time, 'sleep', machine.virtual_sleep()), \
             mock.patch.object(time, 'time', machine.virtual_clock()):
            pid = PID.PID(*(float(config['PID'][k]) for k in ('Kp', 'Ki', 'Kd')))
            pid.SetPoint = 100.0
            main.trickler_loop(
                config, fakes.FakeMemcache({'auto_mode': True}),
                fakes.constants_for(config), pid,
                machine.motor1, machine.motor2, scale, D('45.00'),
                scales.ANDScale.Units.GRAINS, logging.getLogger('pid_tune'))

        machine.settle(0.5)
        self.assertEqual(machine.motor1.speed, 0.0)
        self.assertLess(abs(machine.true_weight - D('45.00')), D('0.05'))


class SeedMemcacheTest(unittest.TestCase):
    """Startup must not throw away settings the user already entered."""

    def test_absent_keys_are_seeded(self):
        memcache = fakes.FakeMemcache()
        main.seed_memcache(memcache, {'target_weight': D('0.0'), 'auto_mode': False})
        self.assertEqual(memcache['target_weight'], D('0.0'))

    def test_existing_values_are_kept(self):
        memcache = fakes.FakeMemcache({'target_weight': D('45.0'), 'auto_mode': True})
        main.seed_memcache(memcache, {'target_weight': D('0.0'), 'auto_mode': False})
        self.assertEqual(memcache['target_weight'], D('45.0'))
        self.assertTrue(memcache['auto_mode'])

    def test_explicit_values_win(self):
        memcache = fakes.FakeMemcache({'target_weight': D('45.0')})
        main.seed_memcache(memcache, {'target_weight': D('12.5')},
                           overwrite={'target_weight': True})
        self.assertEqual(memcache['target_weight'], D('12.5'))


if __name__ == '__main__':
    unittest.main()


class GranularDeliveryTest(unittest.TestCase):
    """Powder arrives as whole grains, and the feeder has to reason in whole grains.

    Every dose ever measured on the machine was a multiple of 0.02 gn -- 0.00, 0.02,
    0.04, 0.06, never 0.01 or 0.03 -- because one grain of stick powder and one division
    of the scale are both about that. Treating delivery as a smooth fluid, which the
    simulator used to do, hid every defect in this class.
    """

    def feeder(self, scale=None, **overrides):
        config = fakes.load_config(**overrides)
        if scale is None:
            scale = mock.Mock()
            scale.Units = scales.ANDScale.Units
            scale.resolution = D('0.02')
        settings = main.trickler_settings(
            config, None, None, scale, scales.ANDScale.Units.GRAINS)
        return main.PulseFeeder(mock.Mock(), scale, settings)

    def feed_window(self, feeder, doses, on_time=0.2):
        """Pushes a sequence of measured doses through the learner."""
        feeder._rate_measured = True
        for dose in doses:
            feeder._learn(on_time, D(str(dose)))

    def test_the_rate_converges_on_what_was_really_delivered(self):
        """Judging each pulse alone and dropping the ones that read zero keeps the hits
        and discards the misses. On the bench that read 0.111 gn/s when the truth was
        nearer 0.036 -- and every pulse sized from it ran three times too long.

        Two grains per six pulses of 0.18 s moving time is 0.037 gn/s. Counting only the
        two pulses that delivered would say 0.111, three times too fast.
        """
        feeder = self.feeder()
        for _ in range(5):
            self.feed_window(feeder, [0, 0.02, 0, 0, 0.02, 0])
        self.assertAlmostEqual(feeder.rate, 0.037, delta=0.005)

    def test_the_rate_never_reads_like_the_hits_alone(self):
        """Even one window in, it must not be anywhere near the biased figure."""
        feeder = self.feeder()
        self.feed_window(feeder, [0, 0.02, 0, 0, 0.02, 0])
        self.assertLess(feeder.rate, 0.111,
                        'this is the biased estimate the bench was suffering from')

    def test_a_clump_does_not_send_the_estimate_flying(self):
        """One pulse dropping three grains is ordinary; it is not a threefold rate."""
        feeder = self.feeder()
        self.feed_window(feeder, [0.02, 0.02, 0.02, 0.02, 0.02, 0.06])
        self.assertLess(feeder.rate, 0.15)

    def test_grainless_pulses_do_not_look_like_an_empty_hopper(self):
        """The bug that abandoned three charges in four: a couple of pulses without a
        grain is powder teetering on the lip, not an empty tube."""
        feeder = self.feeder()
        self.feed_window(feeder, [0.02, 0, 0, 0, 0.02, 0, 0])
        self.assertLess(feeder.empty_pulses, main.MAX_EMPTY_PULSES)

    def test_a_genuinely_empty_hopper_still_gives_up(self):
        feeder = self.feeder()
        self.feed_window(feeder, [0] * (main.MAX_EMPTY_PULSES + 2))
        self.assertGreaterEqual(feeder.empty_pulses, main.MAX_EMPTY_PULSES)

    def test_the_bench_sequence_does_not_abandon_the_charge(self):
        """Replayed from the log of 2026-09-07 22:14, where the feeder gave up at 0.04 gn
        to go and the very next pulse after restarting finished the charge."""
        feeder = self.feeder()
        self.feed_window(
            feeder,
            [0, 0.02, 0, 0.02, 0, 0, 0.02, 0, 0, 0, 0, 0, 0.02, 0, 0.02, 0, 0, 0, 0, 0])
        self.assertLess(feeder.empty_pulses, main.MAX_EMPTY_PULSES,
                        'this is the sequence that used to be read as an empty hopper')

    def test_nothing_finer_than_one_grain_can_be_placed(self):
        """min_dose was reporting 0.0144 gn -- less than a grain of Varget."""
        feeder = self.feeder(pulse_rate=0.05, pulse_min_on_time=0.03)
        self.assertGreaterEqual(feeder.min_dose, D('0.02'))

    def test_the_charge_stops_within_one_grain(self):
        """Chasing half a division is chasing something the scale cannot show, which is
        what fired eight fruitless pulses in a row."""
        feeder = self.feeder(cutoff_weight=0.001)
        self.assertTrue(feeder.done(D('0.01')))

    def test_the_endgame_places_one_grain_at_a_time(self):
        """Aiming for 0.7 x 0.04 = 0.028 gn is arithmetic about a dose that cannot
        exist. Below a few grains the feeder should just place one and look."""
        feeder = self.feeder()
        feeder._rate_measured = True
        feeder._rate = 0.2
        # One grain at 0.2 gn/s is 0.1 s of movement, plus the spin-up.
        self.assertAlmostEqual(feeder._one_grain_time(), 0.12, places=3)
        self.assertTrue(feeder._in_the_last_grains(D('0.08')))
        self.assertFalse(feeder._in_the_last_grains(D('0.30')))

    def test_the_shortest_pulse_is_not_used_as_the_endgame_pulse(self):
        """On a slow trickler the shortest pulse the machine can fire delivers nothing at
        all -- thirty of them in a row was the 77-second crawl on the bench."""
        feeder = self.feeder(pulse_min_on_time=0.03)
        feeder._rate_measured = True
        feeder._rate = 0.05
        self.assertGreater(feeder._one_grain_time(),
                           feeder.settings.pulse_min_on_time * 3)


class PulseSpeedTest(unittest.TestCase):
    """Two pulse speeds: coarse while there is a way to go, fine to finish.

    Every pulse costs the same wait to weigh it whatever it delivered, so a quicker
    charge means fewer, larger pulses. Simply running the motor faster throughout would
    not do -- that coarsens the smallest dose the machine can place, which is what sets
    the accuracy -- so the last pulses drop back to the fine speed.
    """

    def feeder(self, memcache=None, **overrides):
        config = fakes.load_config(**overrides)
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.resolution = D('0.02')
        constants = fakes.constants_for(config) if memcache is not None else None
        settings = main.trickler_settings(
            config, memcache, constants, scale, scales.ANDScale.Units.GRAINS)
        return main.PulseFeeder(mock.Mock(), scale, settings, memcache, constants)

    def test_far_from_target_uses_the_fast_speed(self):
        feeder = self.feeder()
        self.assertTrue(feeder._use_fast_speed(D('0.40')))

    def test_the_last_pulses_use_the_fine_speed(self):
        feeder = self.feeder()
        self.assertFalse(feeder._use_fast_speed(D('0.05')))

    def test_the_two_speeds_are_the_configured_ones(self):
        feeder = self.feeder(pulse_pwm=25, pulse_fast_pwm=45)
        self.assertAlmostEqual(feeder._pulse_speed(fast=False), 0.25)
        self.assertAlmostEqual(feeder._pulse_speed(fast=True), 0.45)

    def test_neither_speed_is_ever_below_the_stall_point(self):
        feeder = self.feeder(pulse_pwm=5, pulse_fast_pwm=8, stall_pwm=20)
        self.assertAlmostEqual(feeder._pulse_speed(fast=False), 0.20)
        self.assertAlmostEqual(feeder._pulse_speed(fast=True), 0.20)

    def test_matching_the_speeds_turns_the_feature_off(self):
        """The way back to single-speed pulsing, from the tuning page."""
        feeder = self.feeder(pulse_fast_pwm=25, pulse_pwm=25)
        self.assertFalse(feeder._use_fast_speed(D('5.00')))

    def test_each_speed_learns_its_own_rate(self):
        """Folding a fast pulse into the fine rate would make the pulses that finish the
        charge several times too long."""
        feeder = self.feeder()
        fine_before = feeder.rate
        feeder._learn(0.20, D('0.30'), fast=True)
        self.assertEqual(feeder.rate, fine_before, 'the fine rate must not move')
        self.assertGreater(feeder.fast_rate, fine_before)

    def test_the_fast_rate_is_guessed_before_it_is_measured(self):
        """Only to size the very first probe: 45% drive against a 20% stall is five
        times the powder of 25%, which is a starting point, not a measurement."""
        feeder = self.feeder(pulse_rate=0.3, pulse_pwm=25, pulse_fast_pwm=45, stall_pwm=20)
        self.assertAlmostEqual(feeder.fast_rate, 1.5)

    def test_the_first_measurement_replaces_the_guess_outright(self):
        """Easing towards it would leave the next pulse sized on a number nothing has
        measured."""
        feeder = self.feeder()
        feeder._learn(0.22, D('0.10'), fast=True)   # 0.10 gn in 0.2 s of movement
        self.assertAlmostEqual(feeder.fast_rate, 0.5, places=6)

    def test_a_speed_that_has_never_been_measured_is_probed_not_aimed(self):
        """A rate nothing has measured is a guess, and at the fast speed with a long
        pulse cap a guess that is badly low would dump the whole remainder at once."""
        feeder = self.feeder()
        probe = feeder._probe_time(D('1.50'), fast=True)
        aimed = 1.4 * feeder.settings.pulse_aim / feeder.fast_rate
        self.assertLess(probe, aimed)

    def test_a_probe_still_clears_the_motor_spin_up(self):
        """A pulse shorter than the spin-up delivers nothing, and measures nothing."""
        feeder = self.feeder()
        for remainder in ('0.15', '1.50', '20.00'):
            with self.subTest(remainder=remainder):
                probe = feeder._probe_time(D(remainder), fast=True)
                self.assertGreater(probe, feeder.settings.pulse_dead_time)

    def test_the_rates_are_kept_separately_per_profile(self):
        config = fakes.load_config()
        constants = fakes.constants_for(config)
        fine = main.learned_rate_key(constants, 'Varget')
        fast = main.learned_rate_key(constants, 'Varget', fast=True)
        self.assertNotEqual(fine, fast)
        self.assertIn('Varget', fast)

    def test_both_rates_survive_to_the_next_charge(self):
        memcache = fakes.FakeMemcache()
        feeder = self.feeder(memcache=memcache)
        feeder._learn(0.22, D('0.10'), fast=True)
        feeder._learn(0.22, D('0.05'), fast=False)
        again = self.feeder(memcache=memcache)
        self.assertAlmostEqual(again.fast_rate, feeder.fast_rate)
        self.assertAlmostEqual(again.rate, feeder.rate)


class SpinUpTest(unittest.TestCase):
    """A pulse only moves powder once the motor is up to speed.

    Without accounting for it, a rate measured from a short pulse reads low -- most of
    that pulse was spin-up -- and the pulse sized from that rate comes out too long. It
    is what made the first fast pulses overshoot while this was being built.
    """

    def feeder(self, **overrides):
        config = fakes.load_config(**overrides)
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.resolution = D('0.02')
        settings = main.trickler_settings(
            config, None, None, scale, scales.ANDScale.Units.GRAINS)
        return main.PulseFeeder(mock.Mock(), scale, settings)

    def test_the_rate_is_measured_over_the_moving_part_of_a_pulse(self):
        feeder = self.feeder(pulse_dead_time=0.02)
        feeder._learn(0.12, D('0.10'), fast=True)
        # 0.10 gn over 0.10 s of movement, not over the 0.12 s the motor was powered.
        self.assertAlmostEqual(feeder.fast_rate, 1.0, places=6)

    def test_short_and_long_pulses_measure_the_same_rate(self):
        """The property that matters: a probe and a full pulse have to agree, or the
        pulse sized from the probe is wrong."""
        short, long_ = self.feeder(), self.feeder()
        short._learn(0.07, D('0.05'), fast=True)    # 0.05 s moving at 1.0 gn/s
        long_._learn(0.52, D('0.50'), fast=True)    # 0.50 s moving at 1.0 gn/s
        self.assertAlmostEqual(short.fast_rate, long_.fast_rate, places=6)

    def test_a_pulse_that_is_all_spin_up_teaches_nothing(self):
        feeder = self.feeder(pulse_dead_time=0.05)
        before = feeder.rate
        feeder._learn(0.04, D('0.03'))
        self.assertEqual(feeder.rate, before)

    def test_the_finest_dose_accounts_for_the_spin_up(self):
        """min_dose sets where a charge stops, so it has to be the weight a shortest
        pulse really delivers."""
        feeder = self.feeder(pulse_min_on_time=0.05, pulse_dead_time=0.02, pulse_rate=1.0)
        self.assertAlmostEqual(float(feeder.min_dose), 0.03, places=6)


class PulseLearningTest(unittest.TestCase):
    """What the feeder is allowed to learn from."""

    def feeder(self, resolution='0.02', rate='0.30'):
        config = fakes.load_config(pulse_rate=rate)
        scale = mock.Mock()
        scale.Units = scales.ANDScale.Units
        scale.resolution = D(resolution)
        settings = main.trickler_settings(
            config, None, None, scale, scales.ANDScale.Units.GRAINS)
        return main.PulseFeeder(mock.Mock(), scale, settings)

    def test_a_measurable_dose_corrects_the_rate(self):
        feeder = self.feeder()
        feeder._learn(0.2, D('0.10'))
        self.assertGreater(feeder.rate, 0.30, 'a fat dose should raise the estimate')

    def test_a_sub_resolution_dose_teaches_nothing(self):
        """A dose below one scale division reads as zero whether it was zero or most of a
        division. Learning from it drags the estimate toward nothing and the feeder then
        over-pulses; the short pulses at the end of every charge are all like this."""
        feeder = self.feeder()
        before = feeder.rate
        feeder._learn(0.05, D('0.00'))
        self.assertEqual(feeder.rate, before)

    def test_a_sub_resolution_dose_still_counts_as_unproductive(self):
        """Otherwise a jam would never trip the give-up counter."""
        feeder = self.feeder()
        for _ in range(3):
            feeder._learn(0.05, D('0.00'))
        self.assertEqual(feeder.empty_pulses, 3)

    def test_a_negative_dose_counts_but_does_not_corrupt_the_rate(self):
        feeder = self.feeder()
        before = feeder.rate
        feeder._learn(0.2, D('-0.04'))
        self.assertEqual(feeder.rate, before)
        self.assertEqual(feeder.empty_pulses, 1)
