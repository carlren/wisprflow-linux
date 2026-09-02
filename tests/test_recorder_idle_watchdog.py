import unittest

from wisprflow.config import DEFAULT_CONFIG
from wisprflow.recorder import Recorder


class RecorderIdleWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.recorder = Recorder(
            vad_enabled=True,
            vad_silence_ms=180_000,
            vad_initial_grace_ms=800,
        )
        self.recorder._vad_start_time = 0.0
        self.recorder._vad_last_voice = 0.0
        self.recorder._vad_triggered = False

    def test_default_is_three_minute_idle_timeout_without_fixed_cap(self):
        self.assertEqual(DEFAULT_CONFIG["vad_silence_ms"], 180_000)
        self.assertEqual(DEFAULT_CONFIG["max_record_seconds"], 0)

    def test_normal_pause_does_not_stop_recording(self):
        self.assertFalse(self.recorder._idle_silence_reached(30.0, 30.0))
        self.assertFalse(self.recorder._idle_silence_reached(179.999, 179.999))

    def test_three_minutes_of_continuous_silence_stops_recording(self):
        self.assertTrue(self.recorder._idle_silence_reached(180.0, 180.0))

    def test_new_speech_resets_the_idle_timeout(self):
        self.recorder._vad_last_voice = 170.0
        self.assertFalse(self.recorder._idle_silence_reached(180.0, 180.0))
        self.assertTrue(self.recorder._idle_silence_reached(350.0, 350.0))

    def test_disabled_watchdog_never_stops_recording(self):
        self.recorder.vad_enabled = False
        self.assertFalse(self.recorder._idle_silence_reached(10_000.0, 10_000.0))


if __name__ == "__main__":
    unittest.main()
