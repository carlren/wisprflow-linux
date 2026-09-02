"""
Audio recorder for wisprflow.
Tries: sounddevice -> pyaudio -> arecord/parecord fallback.
Records 16kHz mono WAV and returns path.
"""
import os
import queue
import subprocess
import tempfile
import threading
import wave
import shutil
from pathlib import Path
import time

import numpy as np

# Optional webrtcvad — more reliable than energy if installed
try:
    import webrtcvad
    _HAS_WEBRTCVAD = True
except Exception:
    _HAS_WEBRTCVAD = False
    webrtcvad = None

def _rms_int16(frame: np.ndarray) -> float:
    try:
        flat = frame.reshape(-1).astype(np.float32)
        return float(np.sqrt(np.mean(flat * flat)))
    except Exception:
        return 0.0

DEFAULT_SR = 16000
DEFAULT_CH = 1

class Recorder:
    def __init__(self, sample_rate=16000, channels=1, max_seconds=0,
                 vad_enabled=True, vad_silence_ms=180000, vad_threshold=500, vad_initial_grace_ms=800,
                 vad_callback=None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_seconds = max_seconds
        self._recording = False
        self._frames = []
        self._stream = None
        self._thread = None
        self._arecord_proc = None
        self._tmp_path = None
        self._lock = threading.Lock()
        self._backend = None  # "sounddevice" | "pyaudio" | "arecord"
        # VAD is a long-idle watchdog, not sentence endpointing. Speech resets
        # the timer; only sustained silence invokes the callback.
        self.vad_enabled = vad_enabled
        self.vad_silence_ms = vad_silence_ms
        self.vad_threshold = vad_threshold
        self.vad_initial_grace_ms = vad_initial_grace_ms
        self.vad_callback = vad_callback
        self._vad_last_voice = None
        self._vad_start_time = None
        self._vad_triggered = False
        self._vad_has_speech = False
        self._vad_webrtc = None
        if _HAS_WEBRTCVAD and vad_enabled and sample_rate in (8000, 16000, 32000, 48000) and channels == 1:
            try:
                self._vad_webrtc = webrtcvad.Vad(2)
            except Exception:
                self._vad_webrtc = None

    def set_vad_config(self, enabled=True, silence_ms=180000, threshold=500, initial_grace_ms=800, callback=None):
        self.vad_enabled = enabled
        self.vad_silence_ms = silence_ms
        self.vad_threshold = threshold
        self.vad_initial_grace_ms = initial_grace_ms
        if callback is not None:
            self.vad_callback = callback

    def _idle_silence_reached(self, now: float, recorded_duration: float) -> bool:
        """Return whether a recording has been silent long enough to abandon.

        This deliberately ignores normal conversational pauses. The last-voice
        timestamp is refreshed whenever speech is detected, so the timeout is
        based on continuous inactivity rather than the end of a sentence.
        """
        if not self.vad_enabled or self._vad_triggered:
            return False
        if self._vad_start_time is None or self._vad_last_voice is None:
            return False
        elapsed_since_start_ms = (now - self._vad_start_time) * 1000
        elapsed_since_voice_ms = (now - self._vad_last_voice) * 1000
        return (
            recorded_duration >= 0.5
            and elapsed_since_start_ms >= self.vad_initial_grace_ms
            and elapsed_since_voice_ms >= self.vad_silence_ms
        )

    def _is_speech_webrtc(self, frame: np.ndarray) -> bool:
        if self._vad_webrtc is None:
            return False
        try:
            chunk_ms = 20
            chunk_samples = int(self.sample_rate * chunk_ms / 1000)
            flat = frame.reshape(-1)
            for i in range(0, len(flat), chunk_samples):
                chunk = flat[i:i+chunk_samples]
                if len(chunk) < chunk_samples:
                    break
                b = chunk.astype(np.int16).tobytes()
                if self._vad_webrtc.is_speech(b, self.sample_rate):
                    return True
            return False
        except Exception:
            return False

    def _is_speech(self, frame: np.ndarray) -> bool:
        if self._vad_webrtc is not None:
            try:
                if self._is_speech_webrtc(frame):
                    return True
            except Exception:
                pass
        return _rms_int16(frame) > self.vad_threshold

    def is_recording(self) -> bool:
        return self._recording

    def start(self):
        with self._lock:
            if self._recording:
                return False
            self._frames = []
            self._recording = True
            self._vad_triggered = False
            self._vad_has_speech = False
            self._vad_start_time = time.monotonic()
            self._vad_last_voice = self._vad_start_time

        # try sounddevice first
        if self._try_sounddevice():
            return True
        if self._try_pyaudio():
            return True
        if self._try_arecord():
            return True
        with self._lock:
            self._recording = False
        raise RuntimeError("No audio backend available. Install python3-sounddevice or portaudio19-dev, or ensure arecord/parecord exists. Run `wisprflow diagnose`.")

    def stop(self) -> str | None:
        """Stop recording and return path to WAV file, or None if too short."""
        with self._lock:
            if not self._recording:
                return None
            self._recording = False

        wav_path = None
        try:
            if self._backend == "sounddevice":
                wav_path = self._stop_sounddevice()
            elif self._backend == "pyaudio":
                wav_path = self._stop_pyaudio()
            elif self._backend == "arecord":
                wav_path = self._stop_arecord()
        finally:
            self._backend = None
            self._vad_triggered = False
        return wav_path

    def cancel(self):
        with self._lock:
            self._recording = False
        try:
            if self._backend == "sounddevice" and self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
            if self._backend == "pyaudio" and self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    pass
            if self._backend == "arecord" and self._arecord_proc is not None:
                try:
                    self._arecord_proc.terminate()
                except Exception:
                    pass
        finally:
            self._stream = None
            self._arecord_proc = None
            self._frames = []
            self._backend = None
            self._vad_triggered = False
            if self._tmp_path and os.path.exists(self._tmp_path):
                try:
                    os.unlink(self._tmp_path)
                except Exception:
                    pass

    # --- sounddevice backend ---
    def _try_sounddevice(self) -> bool:
        try:
            import sounddevice as sd
            import soundfile  # noqa: F401 ensure available
        except Exception as e:
            return False
        try:
            import sounddevice as sd
            q = queue.Queue()

            def callback(indata, frames, time, status):
                if self._recording:
                    q.put(indata.copy())

            # pick default input device
            try:
                dev = sd.default.device[0]
                if dev is None or dev < 0:
                    # query devices, pick first with inputs
                    for i, d in enumerate(sd.query_devices()):
                        if d["max_input_channels"] > 0:
                            dev = i
                            break
            except Exception:
                dev = None

            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=callback,
                device=dev if dev is not None and dev >= 0 else None,
            )
            stream.start()
            self._stream = stream
            self._sd_queue = q

            def collector():
                while self._recording:
                    try:
                        data = q.get(timeout=0.2)
                        is_speech = False
                        if self.vad_enabled:
                            try:
                                is_speech = self._is_speech(data)
                            except Exception:
                                is_speech = False
                            now = time.monotonic()
                            if is_speech:
                                self._vad_last_voice = now
                                self._vad_has_speech = True
                            total_samples = sum(len(f) for f in self._frames) + len(data)
                            duration = total_samples / self.sample_rate
                            if self._idle_silence_reached(now, duration):
                                self._vad_triggered = True
                                cb = self.vad_callback
                                if cb:
                                    try:
                                        threading.Thread(target=cb, daemon=True).start()
                                    except Exception:
                                        pass
                        self._frames.append(data)
                    except queue.Empty:
                        if self.vad_enabled and not self._vad_triggered:
                            now = time.monotonic()
                            total_samples = sum(len(f) for f in self._frames)
                            duration = total_samples / self.sample_rate if self.sample_rate else 0
                            if self._idle_silence_reached(now, duration):
                                self._vad_triggered = True
                                cb = self.vad_callback
                                if cb:
                                    try:
                                        threading.Thread(target=cb, daemon=True).start()
                                    except Exception:
                                        pass
                        continue
                    # auto-stop at max_seconds (hard limit)
                    total_samples = sum(len(f) for f in self._frames)
                    if self.max_seconds > 0 and total_samples / self.sample_rate >= self.max_seconds:
                        break

            t = threading.Thread(target=collector, daemon=True)
            t.start()
            self._thread = t
            self._backend = "sounddevice"
            return True
        except Exception as e:
            # cleanup
            try:
                if self._stream:
                    self._stream.stop()
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
            return False

    def _stop_sounddevice(self):
        try:
            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
            if self._thread:
                self._thread.join(timeout=0.5)
        finally:
            self._stream = None
            self._thread = None

        if not self._frames:
            return None
        audio = np.concatenate(self._frames, axis=0) if len(self._frames) > 1 else self._frames[0]
        # audio is int16
        duration = len(audio) / self.sample_rate
        if duration < 0.3:
            return None
        # write wav
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="wispr_")
        os.close(fd)
        import soundfile as sf
        sf.write(path, audio, self.sample_rate, subtype="PCM_16")
        return path

    # --- pyaudio backend ---
    def _try_pyaudio(self) -> bool:
        try:
            import pyaudio
        except Exception:
            return False
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            # find default input device
            dev_idx = None
            try:
                dev_idx = pa.get_default_input_device_info()["index"]
            except Exception:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    if info["maxInputChannels"] > 0:
                        dev_idx = i
                        break
            if dev_idx is None:
                pa.terminate()
                return False

            frames = []
            p = pa
            # VAD state for pyaudio
            last_voice_pa = [time.monotonic()]
            has_speech_pa = [False]
            vad_triggered_pa = [False]
            start_time_pa = time.monotonic()
            # sync with recorder VAD state
            self._vad_start_time = start_time_pa
            self._vad_last_voice = last_voice_pa[0]

            def callback(in_data, frame_count, time_info, status):
                if self._recording:
                    if self.vad_enabled and not vad_triggered_pa[0]:
                        try:
                            arr = np.frombuffer(in_data, dtype=np.int16)
                            if self.channels > 1:
                                arr = arr.reshape(-1, self.channels)
                            is_speech = self._is_speech(arr)
                            now = time.monotonic()
                            if is_speech:
                                last_voice_pa[0] = now
                                self._vad_last_voice = now
                                has_speech_pa[0] = True
                                self._vad_has_speech = True
                            total_bytes = sum(len(f) for f in frames)
                            duration_est = total_bytes / (2 * self.channels * self.sample_rate) if self.sample_rate else 0
                            if self._idle_silence_reached(now, duration_est):
                                vad_triggered_pa[0] = True
                                self._vad_triggered = True
                                cb = self.vad_callback
                                if cb:
                                    try:
                                        threading.Thread(target=cb, daemon=True).start()
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    frames.append(in_data)
                return (None, pyaudio.paContinue)

            stream = p.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=dev_idx,
                stream_callback=callback,
                frames_per_buffer=1024,
            )
            stream.start_stream()
            self._stream = stream
            self._pa = pa
            self._pa_frames = frames
            self._backend = "pyaudio"
            return True
        except Exception:
            try:
                pa.terminate()
            except Exception:
                pass
            return False

    def _stop_pyaudio(self):
        import pyaudio
        try:
            if self._stream:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    pass
            pa = getattr(self, "_pa", None)
            frames = getattr(self, "_pa_frames", [])
            if pa:
                try:
                    pa.terminate()
                except Exception:
                    pass
            if not frames:
                return None
            data = b"".join(frames)
            duration = len(data) / (2 * self.channels * self.sample_rate)
            if duration < 0.3:
                return None
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="wispr_")
            os.close(fd)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(data)
            return path
        finally:
            self._stream = None
            self._pa = None
            self._pa_frames = []

    # --- arecord / parecord fallback ---
    def _try_arecord(self) -> bool:
        arecord = shutil.which("arecord")
        parecord = shutil.which("parecord")
        # prefer arecord (alsa), fallback parecord (pulse/pipewire)
        if arecord:
            cmd = [arecord, "-f", "S16_LE", "-r", str(self.sample_rate), "-c", str(self.channels), "-t", "wav"]
            # use default device; -q quiet
            # we'll write to temp file directly and then stop
            # but for toggle we need streaming; arecord can write to stdout? use -t wav and file
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="wispr_")
            os.close(fd)
            # arecord will write wav header; we just run it and kill on stop
            try:
                proc = subprocess.Popen(cmd + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._arecord_proc = proc
                self._tmp_path = path
                self._arecord_cmd = "arecord"
                self._backend = "arecord"
                return True
            except Exception:
                return False
        elif parecord:
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="wispr_")
            os.close(fd)
            # parecord --channels=1 --rate=16000 --format=s16le --file-format=wav
            cmd = [parecord, f"--channels={self.channels}", f"--rate={self.sample_rate}", "--format=s16le", "--file-format=wav", path]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._arecord_proc = proc
                self._tmp_path = path
                self._arecord_cmd = "parecord"
                self._backend = "arecord"
                return True
            except Exception:
                return False
        else:
            return False

    def _stop_arecord(self):
        proc = self._arecord_proc
        path = self._tmp_path
        self._arecord_proc = None
        self._tmp_path = None
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
            except Exception:
                pass
        if path and os.path.exists(path):
            # check duration via file size
            try:
                sz = os.path.getsize(path)
                # wav header 44 bytes, 2 bytes per sample mono 16k
                data_bytes = max(0, sz - 44)
                duration = data_bytes / (2 * self.channels * self.sample_rate) if self.sample_rate else 0
                if duration < 0.3:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
                    return None
                return path
            except Exception:
                return path
        return None
