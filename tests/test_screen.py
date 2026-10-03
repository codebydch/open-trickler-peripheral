"""The Mini PiTFT screen: what it shows, and keeping in step with everything else.

screen.py imports `board` and `digitalio` at module scope, so it can normally only be
imported on a Pi. Those two are stubbed here, and the buttons go through gpiozero's mock
pin factory -- which is enough, because MiniPiTFTApp already takes the display, the pins,
the font, the colours, the config and the memcache client as arguments. No production
code was rearranged to make this testable.

Skipped rather than failed where Pillow or the DejaVu font is missing, so a checkout
without the screen's dependencies still runs the rest of the suite.
"""
import configparser
import decimal
import sys
import types
import unittest

from tests import CONFIG_PATH, quiet_logging


D = decimal.Decimal

FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def stub_hardware_modules():
    """Puts the Pi-only modules screen.py imports into sys.modules."""
    for name in ('board', 'digitalio'):
        module = types.ModuleType(name)
        module.SPI = lambda: None
        module.CE0 = module.D25 = module.D22 = object()
        module.DigitalInOut = lambda pin: types.SimpleNamespace(
            switch_to_output=lambda: None, value=True)
        sys.modules.setdefault(name, module)
    package = types.ModuleType('adafruit_rgb_display')
    package.__path__ = []
    st7789 = types.ModuleType('adafruit_rgb_display.st7789')
    st7789.ST7789 = object
    sys.modules.setdefault('adafruit_rgb_display', package)
    sys.modules.setdefault('adafruit_rgb_display.st7789', st7789)


try:
    import os

    import gpiozero
    from gpiozero.pins.mock import MockFactory
    from PIL import ImageFont

    if not os.path.exists(FONT_PATH):
        raise ImportError('the DejaVu font the screen uses is not installed')
    ImageFont.truetype(FONT_PATH, 12)
    stub_hardware_modules()
    gpiozero.Device.pin_factory = MockFactory()
    import screen
    SKIP = None
except ImportError as exc:  # pragma: no cover - depends on what is installed
    SKIP = str(exc)

quiet_logging()


class FakeDisplay:
    """Stands in for the ST7789, keeping every frame it is given."""

    width, height = 135, 240

    def __init__(self):
        self.images = []

    def image(self, image):
        self.images.append(image)

    @property
    def frame(self):
        return self.images[-1]


class FakeMemcache(dict):

    def get(self, key, default=None):
        return dict.get(self, key, default)

    def set(self, key, value):
        self[key] = value


@unittest.skipIf(SKIP, 'screen dependencies not available: %s' % SKIP)
class ScreenTestCase(unittest.TestCase):

    def setUp(self):
        self.config = configparser.ConfigParser()
        self.config.optionxform = str
        self.config.read(CONFIG_PATH)
        self.memcache = FakeMemcache()
        self.display = FakeDisplay()
        self.app = screen.MiniPiTFTApp(
            self.display, 23, 24, FONT_PATH,
            {'WHITE': (255, 255, 255), 'BLACK': (0, 0, 0),
             'RED': (255, 0, 0), 'GREEN': (0, 255, 0)},
            self.config, self.memcache, D('45.00'), False)
        self.keys = self.app.constants
        # gpiozero reserves a pin per Button, and the next test builds its own pair.
        self.addCleanup(self.app.button2.close)
        self.addCleanup(self.app.button1.close)

    def set(self, key, value):
        self.memcache.set(key.value, value)

    def top_band_is_red(self):
        """The jam band fills the top of the panel; the normal screen is black there.

        Sampled near the corner, clear of the white lettering centred in the band.
        """
        self.app.update_display()
        return self.display.frame.getpixel((2, 2)) == (255, 0, 0)


class SyncTest(ScreenTestCase):
    """The screen is not the only thing writing these values."""

    def test_a_target_weight_set_elsewhere_is_picked_up(self):
        self.set(self.keys.TARGET_WEIGHT, D('42.30'))
        self.assertTrue(self.app.refresh())
        self.assertEqual(self.app.target_weight, D('42.30'))

    def test_auto_mode_switched_off_by_the_trickler_is_picked_up(self):
        """This is what a jam does, and the screen used to keep showing green."""
        self.set(self.keys.AUTO_MODE, True)
        self.app.refresh()
        self.set(self.keys.AUTO_MODE, False)
        self.assertTrue(self.app.refresh())
        self.assertFalse(self.app.auto_mode)

    def test_one_press_turns_auto_mode_back_on_after_the_trickler_stood_down(self):
        """The bug behind this change: the screen toggled from its own stale value, so
        it took two presses to undo something the trickler had done."""
        self.set(self.keys.AUTO_MODE, True)
        self.app.refresh()
        self.set(self.keys.AUTO_MODE, False)   # the trickler gives up on a jam
        self.app.refresh()
        self.app.toggle_auto_mode()            # one both-button press
        self.assertTrue(self.app.auto_mode)
        self.assertTrue(self.memcache.get(self.keys.AUTO_MODE.value))

    def test_nothing_changed_means_no_redraw(self):
        """A blind redraw would push a full frame over SPI several times a second."""
        self.assertFalse(self.app.refresh())

    def test_our_own_button_presses_do_not_look_like_external_changes(self):
        self.app.increment_digit()
        self.assertFalse(self.app.refresh())

    def test_values_are_put_back_after_memcached_restarts(self):
        self.memcache.clear()
        self.assertFalse(self.app.refresh())
        self.assertEqual(self.memcache.get(self.keys.TARGET_WEIGHT.value), D('45.00'))
        self.assertIs(self.memcache.get(self.keys.AUTO_MODE.value), False)

    def test_an_unusable_weight_is_ignored_rather_than_crashing_the_loop(self):
        self.set(self.keys.TARGET_WEIGHT, 'not a weight')
        self.assertFalse(self.app.refresh())
        self.assertEqual(self.app.target_weight, D('45.00'))


class JamBandTest(ScreenTestCase):

    def test_no_band_when_all_is_well(self):
        self.assertFalse(self.top_band_is_red())

    def test_the_band_appears_when_the_measure_jams(self):
        self.set(self.keys.DUMP_ERROR, 'The powder measure dropped nothing...')
        self.assertTrue(self.app.refresh())
        self.assertTrue(self.top_band_is_red())

    def test_the_band_clears_when_the_trickler_clears_the_error(self):
        self.set(self.keys.DUMP_ERROR, 'jammed')
        self.app.refresh()
        self.set(self.keys.DUMP_ERROR, '')
        self.assertTrue(self.app.refresh())
        self.assertFalse(self.top_band_is_red())

    def test_turning_auto_mode_on_from_the_screen_clears_the_jam(self):
        """Same rule as the control panel: switching auto mode on says it is cleared."""
        self.set(self.keys.DUMP_ERROR, 'jammed')
        self.app.refresh()
        self.app.toggle_auto_mode()
        self.assertTrue(self.app.auto_mode)
        self.assertEqual(self.memcache.get(self.keys.DUMP_ERROR.value), '')
        self.assertFalse(self.top_band_is_red())

    def test_the_band_does_not_cover_the_target_weight(self):
        """The weight is drawn at y=60; the band has to stop above it."""
        self.set(self.keys.DUMP_ERROR, 'jammed')
        self.app.refresh()
        self.app.update_display()
        frame = self.display.frame
        row = [frame.getpixel((x, 58)) for x in range(frame.width)]
        self.assertNotIn((255, 0, 0), row)


class DigitEditorTest(ScreenTestCase):
    """Unchanged behaviour, pinned so the sync work above cannot disturb it."""

    def test_incrementing_a_digit_wraps_at_ten(self):
        self.app.target_weight = D('49.00')
        self.app.digit_index = 1
        self.app.increment_digit()
        self.assertEqual(self.app.target_weight, D('40.00'))

    def test_every_press_writes_through_to_memcache(self):
        self.app.increment_digit()
        self.assertEqual(self.memcache.get(self.keys.TARGET_WEIGHT.value),
                         self.app.target_weight)

    def test_the_cursor_skips_the_decimal_point(self):
        seen = []
        for _ in range(5):
            seen.append(self.app.digit_index)
            self.app.move_to_next_digit()
        self.assertEqual(seen, [0, 1, 3, 4, 0])


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(SKIP, 'screen dependencies not available: %s' % SKIP)
class ScreenConfigTest(unittest.TestCase):
    """The pins and font come from the ini. They used to come from hard-coded defaults
    that happened to match the shipped ini, so an edited ini was silently ignored."""

    @staticmethod
    def config(**sections):
        config = configparser.ConfigParser()
        for name, values in sections.items():
            config.add_section(name)
            for key, value in values.items():
                config[name][key] = value
        return config

    def test_the_ini_is_read(self):
        settings = screen.screen_config(self.config(
            buttons={'button1_gpio': '5', 'button2_gpio': '6'},
            screen={'font_path': '/fonts/other.ttf'}))
        self.assertEqual(settings, (5, 6, '/fonts/other.ttf'))

    def test_missing_sections_fall_back_to_the_mini_pitft_wiring(self):
        settings = screen.screen_config(self.config())
        self.assertEqual(settings, (23, 24, FONT_PATH))

    def test_the_shipped_config_matches_the_defaults(self):
        """Which is why nobody noticed the file was never read."""
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        self.assertEqual(screen.screen_config(config), (23, 24, FONT_PATH))
