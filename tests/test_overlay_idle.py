import unittest

from wisprflow.overlay import Overlay, _configure_text_rendering


class FakeGtkSettings:
    def __init__(self):
        self.values = {}

    def set_property(self, name, value):
        self.values[name] = value


class OverlayRenderingTest(unittest.TestCase):
    def test_deferred_window_creation_runs_once(self):
        overlay = Overlay.__new__(Overlay)
        calls = []
        overlay._ensure_window = lambda: calls.append("called") or True

        self.assertFalse(overlay._deferred_ensure_window())
        self.assertEqual(calls, ["called"])

    def test_rgba_window_disables_lcd_subpixel_antialiasing(self):
        settings = FakeGtkSettings()
        _configure_text_rendering(settings)
        self.assertEqual(settings.values["gtk-xft-rgba"], "none")
        self.assertEqual(settings.values["gtk-xft-antialias"], 1)
        self.assertEqual(settings.values["gtk-xft-hintstyle"], "hintslight")

if __name__ == "__main__":
    unittest.main()
