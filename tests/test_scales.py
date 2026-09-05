"""Scale framing and parsing, against the real classes over a fake serial port."""
import decimal
import unittest
from unittest import mock

import scales

from tests import fakes


D = decimal.Decimal


def make_scale(cls, port):
    """Builds a real scale class around a fake port."""
    with mock.patch.object(scales.serial, 'Serial', return_value=port):
        return cls(fakes.load_config())


class ParsingTest(unittest.TestCase):
    """Each brand reads back the frames it is documented to emit."""

    def test_and_scale(self):
        port = fakes.FakeSerial([fakes.and_frame(45.02)])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertEqual(scale.weight, D('45.02'))
        self.assertEqual(scale.unit, scales.ANDScale.Units.GRAINS)
        self.assertTrue(scale.is_stable)
        self.assertTrue(scale.is_fresh)

    def test_and_scale_unstable(self):
        port = fakes.FakeSerial([fakes.and_frame(45.02, status='US')])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertFalse(scale.is_stable)
        self.assertTrue(scale.is_fresh, 'an unstable reading is still a real reading')

    def test_creedmoor_scale(self):
        port = fakes.FakeSerial([fakes.creedmoor_frame(45.02)])
        scale = make_scale(scales.CreedmoorScale, port)
        scale.update()
        self.assertEqual(scale.weight, D('45.02'))
        self.assertEqual(scale.unit, scales.CreedmoorScale.Units.GRAINS)

    def test_ussolid_scale(self):
        port = fakes.FakeSerial([fakes.ussolid_frame(45.020)])
        scale = make_scale(scales.USSolidScale, port)
        scale.update()
        self.assertEqual(scale.weight, D('45.020'))
        self.assertEqual(scale.unit, scales.USSolidScale.Units.GRAINS)

    def test_negative_weight(self):
        """A pan lifted off reads negative, which the control loop keys on."""
        port = fakes.FakeSerial([fakes.and_frame(-12.34)])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertEqual(scale.weight, D('-12.34'))


class FreshnessTest(unittest.TestCase):
    """`is_fresh` has to mean "this update produced a new weight", or the loop
    double-counts a reading it has already acted on."""

    def test_partial_frame_is_not_fresh(self):
        port = fakes.FakeSerial([b'ST,+000'])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertFalse(scale.is_fresh)
        self.assertEqual(scale.weight, D('0.00'), 'a partial frame must not change the weight')

    def test_partial_frame_completes_on_the_next_read(self):
        """The tail of a split frame is kept and joined to what arrives next."""
        port = fakes.FakeSerial([b'ST,+00045.'])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertFalse(scale.is_fresh)
        port.feed(b'02 GN\r\n')
        scale.update()
        self.assertTrue(scale.is_fresh)
        self.assertEqual(scale.weight, D('45.02'))

    def test_garbage_leaves_the_weight_alone(self):
        port = fakes.FakeSerial([fakes.and_frame(45.02)])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertEqual(scale.weight, D('45.02'))

        port.feed(b'\xff\xfe nonsense \r\n')
        scale.update()
        self.assertFalse(scale.is_fresh)
        self.assertEqual(scale.weight, D('45.02'))

    def test_a_later_bad_frame_supersedes_an_earlier_good_one(self):
        """Newest wins, even when the newest is unreadable -- the loop then waits for the
        next frame rather than acting on one it may already have seen."""
        port = fakes.FakeSerial([fakes.and_frame(45.02), b'\xff\xfe nonsense \r\n'])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertFalse(scale.is_fresh)

    def test_nothing_waiting_is_not_fresh(self):
        scale = make_scale(scales.ANDScale, fakes.FakeSerial())
        scale.update()
        self.assertFalse(scale.is_fresh)


class LatestFrameTest(unittest.TestCase):
    """The loop wants the newest reading, not the oldest queued one."""

    def test_uses_the_most_recent_of_several_queued_frames(self):
        port = fakes.FakeSerial([
            fakes.and_frame(10.00),
            fakes.and_frame(20.00),
            fakes.and_frame(30.00),
        ])
        scale = make_scale(scales.ANDScale, port)
        scale.update()
        self.assertEqual(scale.weight, D('30.00'), 'stale queued frames must be skipped')

    def test_runaway_buffer_is_discarded(self):
        """A wrong baud rate produces bytes that never contain a terminator."""
        scale = make_scale(scales.ANDScale, fakes.FakeSerial())
        scale._buffer = b'x' * (scales.MAX_BUFFER_BYTES + 1)
        scale.update()
        self.assertEqual(scale._buffer, b'')


class StabilityTest(unittest.TestCase):
    """Scales without a stability flag infer it from repeated readings."""

    def test_creedmoor_becomes_stable_after_repeats(self):
        port = fakes.FakeSerial()
        scale = make_scale(scales.CreedmoorScale, port)
        for _ in range(scale._readings.maxlen):
            port.feed(fakes.creedmoor_frame(45.02))
            scale.update()
        self.assertTrue(scale.is_stable)

        port.feed(fakes.creedmoor_frame(45.04))
        scale.update()
        self.assertFalse(scale.is_stable)


class ClassLevelMapTest(unittest.TestCase):
    """The unit and resolution maps, which are read off the class as well as the instance.

    These were written as @classmethod stacked on @property. Python 3.9 and 3.10 allowed
    that combination, 3.11 deprecated it, and 3.13 removed it -- so on Raspberry Pi OS
    Trixie the trickler died at startup with

        TypeError: 'method' object is not subscriptable

    while every version before it worked. That is the shape of bug these tests cannot
    catch by exercising behaviour, because on an older interpreter the broken form behaves
    correctly. So the decorator itself is what is asserted.
    """

    SCALE_CLASSES = (scales.ANDScale, scales.CreedmoorScale, scales.USSolidScale)

    def test_no_classmethod_stacked_on_property(self):
        for cls in (scales.SerialScale,) + self.SCALE_CLASSES:
            for name, attribute in vars(cls).items():
                with self.subTest(cls=cls.__name__, attribute=name):
                    self.assertFalse(
                        isinstance(attribute, classmethod) and
                        isinstance(attribute.__func__, property),
                        'chaining @classmethod and @property was removed in Python 3.13; '
                        'use the classproperty descriptor instead')

    def test_maps_are_subscriptable_from_the_class(self):
        """What SerialScale.__init__ does on the very first line of a charge."""
        for cls in self.SCALE_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertIsInstance(cls.resolution_map[cls.Units.GRAINS], decimal.Decimal)
                self.assertIn(cls.Units.GRAINS, cls.unit_map.values())

    def test_maps_are_subscriptable_from_an_instance(self):
        for cls in self.SCALE_CLASSES:
            with self.subTest(cls=cls.__name__):
                scale = make_scale(cls, fakes.FakeSerial([]))
                self.assertEqual(scale.resolution, scale.resolution_map[scale.unit])

    def test_reverse_unit_map_inverts_the_unit_map(self):
        for cls in self.SCALE_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertEqual(
                    cls.reverse_unit_map,
                    {unit: text for text, unit in cls.unit_map.items()})

    def test_each_scale_declares_a_grain_resolution(self):
        """The pulse feeder refuses to learn from a dose below this, so a wrong value
        here quietly stops the machine learning its feed rate."""
        for cls in self.SCALE_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertGreater(cls.resolution_map[cls.Units.GRAINS], 0)


if __name__ == '__main__':
    unittest.main()
