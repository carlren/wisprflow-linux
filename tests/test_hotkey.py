import unittest

from Xlib import X

from wisprflow.hotkey import parse_x11_hotkey


class X11HotkeyParsingTest(unittest.TestCase):
    def test_alt_letter_can_be_consumed(self):
        self.assertEqual(parse_x11_hotkey("alt+z"), ("z", X.Mod1Mask))

    def test_lock_independent_modifier_combo(self):
        self.assertEqual(
            parse_x11_hotkey("ctrl+shift+f9"),
            ("F9", X.ControlMask | X.ShiftMask),
        )

    def test_modifier_only_shortcut_uses_fallback(self):
        self.assertIsNone(parse_x11_hotkey("ctrl+shift"))

    def test_unknown_key_uses_fallback(self):
        self.assertIsNone(parse_x11_hotkey("alt+not-a-real-key"))


if __name__ == "__main__":
    unittest.main()
