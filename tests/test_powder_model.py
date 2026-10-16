"""Charges simulated against a powder as the calibration sweep measured it."""
import decimal
import random
import unittest

import main
import powder_model
import scales

from tests import fakes


D = decimal.Decimal


def settings(**overrides):
    config = fakes.load_config(**overrides)
    scale = powder_model.SimulatedPan(powder_model.VirtualClock(), '0')
    return main.trickler_settings(config, None, None, scale, powder_model.Units.GRAINS)


def steady_powder(grains_by_speed, speeds=(30.0, 45.0), durations=(0.15, 0.25, 0.4)):
    """A powder whose every pulse at a speed drops the same number of grains."""
    cells = {(speed, duration): [(0.02 * grains_by_speed[speed], 0.0)] * 10
             for speed in speeds for duration in durations}
    return powder_model.EmpiricalPowder(cells, rng=random.Random(1))


class EmpiricalPowderTest(unittest.TestCase):

    def test_the_nearest_cell_is_by_speed_then_length(self):
        powder = steady_powder({30.0: 1, 45.0: 3})
        self.assertEqual(powder.nearest(31, 0.16), (30.0, 0.15))
        self.assertEqual(powder.nearest(44, 0.9), (45.0, 0.4))

    def test_samples_come_from_the_record(self):
        cells = {(30.0, 0.2): [(0.00, 0.0), (0.02, 0.02), (0.08, 0.0)]}
        powder = powder_model.EmpiricalPowder(cells, rng=random.Random(3))
        drawn = {powder.sample(30, 0.2) for _ in range(50)}
        self.assertEqual(drawn, set(cells[(30.0, 0.2)]))

    def test_the_summary_is_the_bench_table(self):
        cells = {(30.0, 0.4): [(0.0, 0.0), (0.0, 0.02), (0.02, 0.0), (0.08, 0.0)]}
        summary = powder_model.EmpiricalPowder(cells).cell_summary()[(30.0, 0.4)]
        self.assertEqual(summary['pulses'], 4)
        self.assertAlmostEqual(summary['zeros'], 0.25, msg='the one with only a tail delivered')
        self.assertAlmostEqual(summary['bursts'], 0.25)
        self.assertAlmostEqual(summary['mean_dose'], 0.025)
        self.assertAlmostEqual(summary['mean_total'], 0.03)

    def test_an_empty_record_is_refused(self):
        with self.assertRaises(ValueError):
            powder_model.EmpiricalPowder({(30.0, 0.2): []})


class SimulatedPanTest(unittest.TestCase):

    def test_powder_lands_on_schedule_and_the_reading_is_quantised(self):
        clock = powder_model.VirtualClock()
        pan = powder_model.SimulatedPan(clock, '44.50')
        pan.land_later(0.5, '0.03')
        self.assertEqual(pan.weight, D('44.50'))
        clock.advance(0.6)
        self.assertEqual(pan.weight, D('44.54'), 'rounded to a division, half up')
        self.assertEqual(pan.outstanding, D('0'))

    def test_reading_a_frame_moves_the_clock(self):
        """settled_weight() loops on the clock; a pan that never moved it would hang it."""
        clock = powder_model.VirtualClock()
        pan = powder_model.SimulatedPan(clock, '44.50')
        before = clock()
        weight = main.settled_weight(pan, timeout=1.0, min_wait=0.3, clock=clock)
        self.assertEqual(weight, D('44.50'))
        self.assertGreaterEqual(clock() - before, 0.3)
        self.assertLess(clock() - before, 0.5)

    def test_the_scale_is_unstable_just_before_something_lands(self):
        clock = powder_model.VirtualClock()
        pan = powder_model.SimulatedPan(clock, '44.50')
        pan.land_later(0.08, '0.02')
        pan.weight
        self.assertFalse(pan.is_stable)
        clock.advance(0.2)
        pan.weight
        self.assertTrue(pan.is_stable)


class ChargeSimulatorTest(unittest.TestCase):

    def test_a_one_grain_powder_lands_a_grain_light_every_time(self):
        """The feeder stops a division short by design; a powder that always drops one
        grain lets it, and a two-speed charge gets there in a handful of pulses."""
        powder = steady_powder({30.0: 1, 45.0: 1})
        sim = powder_model.ChargeSimulator(powder, settings(pulse_pwm=30, pulse_fast_pwm=45),
                                           {30.0: 0.1, 45.0: 0.1}, continuous_rate=0.2)
        for _ in range(10):
            result = sim.run()
            self.assertTrue(result.finished)
            self.assertAlmostEqual(result.error, -0.02, places=6)
            self.assertLess(result.pulses, 30)
            self.assertGreater(result.seconds, 0)

    def test_a_bursting_powder_lands_heavy(self):
        powder = steady_powder({30.0: 5, 45.0: 5})
        sim = powder_model.ChargeSimulator(powder, settings(pulse_pwm=30, pulse_fast_pwm=45),
                                           {30.0: 0.3, 45.0: 0.3}, continuous_rate=0.2)
        prediction = powder_model.predict(sim, 20)
        self.assertGreater(prediction.heavy, 0.5)

    def test_the_real_feeder_is_what_runs(self):
        """Not a copy of its rules: the pulses the motor saw are the feeder's own sizes."""
        powder = steady_powder({30.0: 1, 45.0: 2})
        sim = powder_model.ChargeSimulator(powder, settings(pulse_pwm=30, pulse_fast_pwm=45),
                                           {30.0: 0.1, 45.0: 0.2}, continuous_rate=0.2)
        sim.run()
        # The last pulse is at the fine speed, in the last-grains regime, sized by
        # _one_grain_time: 0.02 / 0.1 + dead time, clamped to the floor and cap.
        clock = powder_model.VirtualClock()
        pan = powder_model.SimulatedPan(clock, '54.80')
        motor = powder_model.SimulatedMotor(clock, pan, powder, 0.1)
        feeder = main.PulseFeeder(motor, pan, settings(pulse_pwm=30, pulse_fast_pwm=45),
                                  model=sim._seeded_model(), clock=clock, sleep=clock.advance)
        feeder.feed(D('0.06'))
        speed_pct, on_time, _, _ = motor.pulses[0]
        self.assertEqual(speed_pct, 30.0)
        self.assertAlmostEqual(on_time, feeder._one_grain_time(), places=6)


class RecommendTest(unittest.TestCase):

    def recommend(self, powder, rates):
        return powder_model.recommend(
            powder, settings(pulse_pwm=30, pulse_fast_pwm=45), rates, continuous_rate=0.2,
            speeds=(30.0, 45.0), durations=(0.25,), first_pass=8, second_pass=16, finalists=6)

    def test_the_faster_of_two_safe_settings_wins(self):
        """45% drops three grains a pulse, 30% one; both finish light, so the quicker
        one -- more fast pulses, a later handover -- is recommended."""
        powder = steady_powder({30.0: 1, 45.0: 3})
        result = self.recommend(powder, {30.0: 0.1, 45.0: 0.3})
        recommended = result['recommended']
        self.assertTrue(recommended['meets_limit'])
        self.assertLessEqual(recommended['prediction']['seconds'],
                             result['current']['prediction']['seconds'] + 1e-9)
        self.assertEqual(result['evaluated'], 24)

    def test_a_setting_that_lands_heavy_too_often_is_not_recommended(self):
        """At 45% this powder drops five grains every pulse: any charge that finishes at
        45% lands heavy, so the fine speed must be 30 even though 45 is quicker."""
        powder = steady_powder({30.0: 1, 45.0: 5})
        result = self.recommend(powder, {30.0: 0.1, 45.0: 0.5})
        self.assertEqual(result['recommended']['settings']['pulse_pwm'], 30.0)
        self.assertTrue(result['recommended']['meets_limit'])

    def test_stop_ends_it_early_with_what_is_known(self):
        powder = steady_powder({30.0: 1, 45.0: 3})
        calls = []
        result = powder_model.recommend(
            powder, settings(), {30.0: 0.1, 45.0: 0.3}, 0.2, (30.0, 45.0), (0.25,),
            first_pass=4, second_pass=4, stop=lambda: len(calls) > 3,
            progress=lambda fraction: calls.append(fraction))
        self.assertIsNotNone(result)
        self.assertLess(result['evaluated'], 24)


if __name__ == '__main__':
    unittest.main()


class RankingTest(unittest.TestCase):
    """What the recommender may and may not pick."""

    def recommend(self, powder, rates, **kw):
        kw.setdefault('first_pass', 8)
        kw.setdefault('second_pass', 16)
        kw.setdefault('finalists', 6)
        return powder_model.recommend(
            powder, settings(pulse_pwm=30, pulse_fast_pwm=45), rates, continuous_rate=0.2,
            speeds=(30.0, 45.0), durations=(0.25,), **kw)

    def test_a_setting_that_never_delivers_is_never_recommended(self):
        """At 30% this powder drops nothing, so every charge at 30 is unfinished and never
        heavy; the first version ranked the rest by heavy rate and picked exactly that."""
        powder = steady_powder({30.0: 0, 45.0: 5})
        result = self.recommend(powder, {30.0: 0.01, 45.0: 0.5})
        recommended = result['recommended']
        self.assertEqual(recommended['prediction']['unfinished'], 0.0)
        self.assertEqual(recommended['settings']['pulse_pwm'], 45.0)
        self.assertFalse(recommended['meets_limit'], 'five grains a pulse lands heavy')
        self.assertIn('heavy', result['reason'])

    def test_nothing_finishing_says_so(self):
        powder = steady_powder({30.0: 0, 45.0: 0})
        result = self.recommend(powder, {30.0: 0.01, 45.0: 0.01})
        self.assertFalse(result['recommended']['meets_limit'])
        self.assertIn('unfinished', result['reason'])

    def test_a_qualifying_setting_has_no_reason_to_give(self):
        powder = steady_powder({30.0: 1, 45.0: 3})
        result = self.recommend(powder, {30.0: 0.1, 45.0: 0.3})
        self.assertTrue(result['recommended']['meets_limit'])
        self.assertIsNone(result['reason'])


class CellTotalsTest(unittest.TestCase):

    def test_the_summary_counts_the_tail_as_delivered(self):
        cells = {(30.0, 0.4): [(0.0, 0.0), (0.0, 0.02), (0.02, 0.0), (0.02, 0.06)]}
        summary = powder_model.EmpiricalPowder(cells).cell_summary()[(30.0, 0.4)]
        self.assertAlmostEqual(summary['zeros'], 0.25, msg='a tail is not nothing')
        self.assertAlmostEqual(summary['mean_dose'], 0.01)
        self.assertAlmostEqual(summary['mean_total'], 0.03)
        self.assertAlmostEqual(summary['bursts'], 0.25, msg='0.02 + 0.06 is four grains')
        self.assertAlmostEqual(summary['max_dose'], 0.08)


class SingleChangeTest(unittest.TestCase):
    """The variants of the current settings that move one value: the ones a bench run,
    one change at a time, can actually try next."""

    def test_each_variant_moves_exactly_one_value(self):
        base = settings(pulse_pwm=30, pulse_fast_pwm=45)
        variants = list(powder_model.single_changes(base, (25.0, 30.0, 45.0), (0.15, 0.25, 0.4)))
        self.assertTrue(variants)
        for name, before, after, changed in variants:
            moved = [key for key in powder_model.RECOMMENDED_KEYS
                     if float(getattr(changed, key)) != float(getattr(base, key))]
            self.assertEqual(moved, [name])
            self.assertNotEqual(before, after)

    def test_the_fine_speed_never_passes_the_fast_one(self):
        base = settings(pulse_pwm=30, pulse_fast_pwm=45)
        for name, _, after, changed in powder_model.single_changes(base, (25.0, 30.0, 45.0, 60.0), (0.4,)):
            self.assertLessEqual(float(changed.pulse_pwm), float(changed.pulse_fast_pwm), name)

    def test_one_speed_offers_no_fast_until(self):
        base = settings(pulse_pwm=30, pulse_fast_pwm=30)
        names = {name for name, *_ in powder_model.single_changes(base, (30.0, 45.0), (0.4,))}
        self.assertNotIn('pulse_fast_until', names)

    def test_the_recommendation_carries_them_best_first(self):
        powder = steady_powder({30.0: 1, 45.0: 3})
        result = powder_model.recommend(
            powder, settings(pulse_pwm=30, pulse_fast_pwm=45), {30.0: 0.1, 45.0: 0.3}, 0.2,
            (30.0, 45.0), (0.25, 0.4), first_pass=4, second_pass=8, finalists=3)
        changes = result['single_changes']
        self.assertTrue(changes)
        self.assertTrue(all({'setting', 'from', 'to'} <= set(c['change']) for c in changes))
        qualifying = [c['meets_limit'] for c in changes]
        self.assertEqual(qualifying, sorted(qualifying, reverse=True),
                         'the ones within the limit come first')
        fast = [c['prediction']['seconds'] for c in changes if c['meets_limit']]
        self.assertEqual(fast, sorted(fast), 'and among them, the quickest first')
