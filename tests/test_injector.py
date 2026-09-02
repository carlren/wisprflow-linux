import subprocess
import unittest
from unittest.mock import patch

from wisprflow import injector


def completed(stdout=b"", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=b"")


class ActiveWindowDetectionTest(unittest.TestCase):
    @patch("wisprflow.injector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
    @patch("wisprflow.injector._run")
    def test_gnome_terminal_uses_wm_class_before_title(self, run, _which):
        run.side_effect = [
            completed(b"81788938\n"),
            completed(b'WM_CLASS(STRING) = "gnome-terminal-server", "Gnome-terminal"\n'),
        ]

        window_class = injector._get_active_window_class_x11()
        self.assertEqual(window_class, "gnome-terminal-server Gnome-terminal")
        with patch("wisprflow.injector._get_active_window_class", return_value=window_class):
            self.assertTrue(injector._is_terminal_focused())

    @patch("wisprflow.injector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
    @patch("wisprflow.injector._run")
    def test_window_title_is_only_a_fallback(self, run, _which):
        run.side_effect = [
            completed(b"42\n"),
            completed(b"WM_CLASS: not found.\n", returncode=1),
            completed(b"Project shell\n"),
        ]

        self.assertEqual(injector._get_active_window_class_x11(), "Project shell")


if __name__ == "__main__":
    unittest.main()
