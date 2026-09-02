"""
Daemon: holds Recorder, manages toggle, transcription, injection, overlay, tray, hotkey.
Communication: Unix socket ~/.cache/wisprflow/daemon.sock  + SIGUSR1
Hotkey: tries pynput global hotkey (default left ctrl+left shift). On Wayland this often fails; user should bind `wisprflow toggle` via GNOME Settings.
"""
import os
import signal
import socket
import threading
import time
import json
from pathlib import Path
import subprocess

from .config import load_config, DEFAULT_IDLE_SILENCE_MS, DEFAULT_MAX_RECORD_SECONDS
from .recorder import Recorder
from .overlay import Overlay, HAS_GTK
from .transcriber import transcribe
from .injector import inject_text

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    HAS_GTK_DAEMON = True
except Exception:
    HAS_GTK_DAEMON = False

CACHE_DIR = Path.home() / ".cache" / "wisprflow"
SOCK_PATH = CACHE_DIR / "daemon.sock"
PID_PATH = CACHE_DIR / "daemon.pid"


class Daemon:
    def __init__(self):
        self.cfg = load_config()
        self.recorder = Recorder(
            sample_rate=self.cfg.get("sample_rate", 16000),
            channels=self.cfg.get("channels", 1),
            max_seconds=self.cfg.get("max_record_seconds", DEFAULT_MAX_RECORD_SECONDS),
            vad_enabled=self.cfg.get("vad_enabled", True),
            vad_silence_ms=self.cfg.get("vad_silence_ms", DEFAULT_IDLE_SILENCE_MS),
            vad_threshold=self.cfg.get("vad_threshold", 500),
            vad_initial_grace_ms=self.cfg.get("vad_initial_grace_ms", 800),
            vad_callback=self._on_vad_silence,
        )
        self._rec_gen = 0
        # Pill is persistent and clickable - pass toggle callback + hotkey reload callback
        self.overlay = Overlay(enabled=self.cfg.get("overlay_enabled", True), toggle_callback=self.toggle, hotkey_reload_callback=self.reload_hotkey)
        # also ensure fallback case handled
        try:
            self.overlay.set_hotkey_reload_callback(self.reload_hotkey)
        except Exception:
            pass
        self._lock = threading.Lock()
        self._recording = False
        self._sock = None
        self._sock_thread = None
        self._hotkey_listener = None
        self._stop_event = threading.Event()

    def is_recording(self):
        return self._recording

    def _on_vad_silence(self):
        # Long-idle watchdog: sustained silence means recording was abandoned.
        with self._lock:
            if self._recording:
                # If no speech at all, don't waste OpenRouter call — just discard
                if self.recorder.vad_enabled and not getattr(self.recorder, '_vad_has_speech', True):
                    print("[wispr] idle timeout with no speech -> discarding (no OpenRouter call)")
                    self._recording = False
                    try:
                        # stop and discard wav
                        wav = self.recorder.stop()
                        if wav and __import__('os').path.exists(wav):
                            try:
                                __import__('os').unlink(wav)
                            except Exception:
                                pass
                    except Exception:
                        try:
                            self.recorder.cancel()
                        except Exception:
                            pass
                    self.overlay.show_idle()
                    return
                silence_s = self.cfg.get("vad_silence_ms", DEFAULT_IDLE_SILENCE_MS) / 1000
                print(f"[wispr] idle timeout after {silence_s:g}s of silence -> auto-stopping")
                self._stop_recording_locked()

    def toggle(self):
        with self._lock:
            if self._recording:
                # stop
                self._stop_recording_locked()
            else:
                self._start_recording_locked()

    def _start_recording_locked(self):
        # reload config in case key changed
        self.cfg = load_config()
        # Refresh the long-idle watchdog settings for this recording.
        try:
            self.recorder.set_vad_config(
                enabled=self.cfg.get("vad_enabled", True),
                silence_ms=self.cfg.get("vad_silence_ms", DEFAULT_IDLE_SILENCE_MS),
                threshold=self.cfg.get("vad_threshold", 500),
                initial_grace_ms=self.cfg.get("vad_initial_grace_ms", 800),
                callback=self._on_vad_silence,
            )
        except Exception:
            pass
        try:
            self.recorder.start()
            self._recording = True
            self._rec_gen += 1
            cur_gen = self._rec_gen
            self.overlay.show_listening(seconds=0.0)
            if self.cfg.get("sound_enabled"):
                self._play_sound("start")
            # An optional fixed cap remains available, but zero disables it so
            # continuous speech is never cut off by default.
            max_s = self.cfg.get("max_record_seconds", DEFAULT_MAX_RECORD_SECONDS)
            if max_s and max_s > 0:
                def _auto_stop(gen):
                    time.sleep(max_s)
                    with self._lock:
                        if self._recording and gen == self._rec_gen:
                            print("[wispr] max duration reached, auto-stopping")
                            self._stop_recording_locked()
                threading.Thread(target=_auto_stop, args=(cur_gen,), daemon=True).start()
            silence_ms = self.cfg.get("vad_silence_ms", DEFAULT_IDLE_SILENCE_MS)
            print(f"[wispr] recording started (idle watchdog={'on' if self.cfg.get('vad_enabled', True) else 'off'} silence={silence_ms / 1000:g}s, max={'off' if not max_s else f'{max_s}s'})")
        except Exception as e:
            self._recording = False
            print(f"[wispr] failed to start recording: {e}")
            self.overlay.show_error(str(e)[:80])

    def _stop_recording_locked(self):
        self._recording = False
        self.overlay.show_transcribing()
        if self.cfg.get("sound_enabled"):
            self._play_sound("stop")
        wav_path = None
        try:
            wav_path = self.recorder.stop()
        except Exception as e:
            self.overlay.show_error(f"Mic error: {e}")
            print(f"[wispr] recorder stop error: {e}")
            return

        if wav_path is None:
            print("[wispr] recording too short, ignoring")
            self.overlay.show_error("Too short — try again")
            # Back to idle pill after error
            return

        # transcribe in background so we don't block Gtk
        def _transcribe_and_inject(path):
            try:
                print(f"[wispr] transcribing {path} with {self.cfg.get('model')}")
                text = transcribe(path, self.cfg)
                print(f"[wispr] -> {text!r}")
                if not text or not text.strip():
                    self.overlay.show_error("No speech detected")
                    return
                # small delay to let overlay hide? but we want instant paste
                # Give focused window time to regain focus after overlay
                time.sleep(0.15)
                ok = inject_text(text)
                if ok:
                    self.overlay.show_done(text)
                    if self.cfg.get("sound_enabled"):
                        self._play_sound("done")
                else:
                    self.overlay.show_error("Transcribed but paste failed")
                    # still copy to clipboard already, so notify
                    print("[wispr] paste failed but text in clipboard:", text)
            except Exception as e:
                msg = str(e)
                print(f"[wispr] transcription error: {e}")
                is_network = "Network error" in msg or "NameResolutionError" in msg or "Temporary failure" in msg or "Max retries" in msg or "Failed to resolve" in msg
                # Friendlier pill message for transient network/DNS vs auth
                if is_network:
                    self.overlay.show_error("No internet — queued, will retry")
                    # Save to pending queue for retry
                    try:
                        pending_dir = CACHE_DIR / "pending"
                        pending_dir.mkdir(parents=True, exist_ok=True)
                        # save with timestamp
                        import shutil, datetime
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        dest = pending_dir / f"wispr_{ts}_{os.path.basename(path)}"
                        if os.path.exists(path):
                            shutil.move(path, dest)
                            print(f"[wispr] queued pending {dest} (network failure)")
                            # notify
                            try:
                                import subprocess as _sp, shutil as _sh
                                if _sh.which("notify-send"):
                                    _sp.Popen(["notify-send","-u","critical","-t","5000","Wispr — queued offline", f"{dest.name} saved, run `wisprflow retry-pending` when online"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                            except Exception:
                                pass
                            path = None  # don't delete in finally
                    except Exception as qe:
                        print(f"[wispr] queue failed: {qe}")
                elif "401" in msg or "auth failed" in msg.lower():
                    self.overlay.show_error("OpenRouter auth failed — check key")
                elif "402" in msg or "credits" in msg.lower():
                    self.overlay.show_error("OpenRouter credits exhausted")
                else:
                    self.overlay.show_error(msg[:70])
            finally:
                try:
                    if path and path and os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass

        threading.Thread(target=_transcribe_and_inject, args=(wav_path,), daemon=True).start()

    def _play_sound(self, kind):
        # try paplay / aplay with freedesktop sounds, else no-op
        try:
            if kind == "start":
                # short tick
                subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif kind == "stop":
                subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif kind == "done":
                subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # fallback: beep via console? ignore

    # -- socket server --
    def _ensure_cache(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            PID_PATH.write_text(str(os.getpid()))
        except Exception:
            pass

    def _start_socket(self):
        self._ensure_cache()
        # remove stale sock
        try:
            if SOCK_PATH.exists():
                # try connect to see if alive
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    s.connect(str(SOCK_PATH))
                    s.close()
                    print(f"[wispr] daemon already running at {SOCK_PATH}")
                    return False
                except Exception:
                    try:
                        SOCK_PATH.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(SOCK_PATH))
            self._sock.listen(5)
            os.chmod(SOCK_PATH, 0o600)
        except Exception as e:
            print(f"[wispr] socket bind failed: {e}")
            return False

        def _loop():
            while not self._stop_event.is_set():
                try:
                    self._sock.settimeout(0.5)
                    try:
                        conn, _ = self._sock.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        try:
                            data = conn.recv(1024).decode("utf-8", errors="ignore").strip()
                        except Exception:
                            data = ""
                        # any data or empty -> toggle; allow "toggle","status","stop","is_recording"
                        cmd = data.lower() if data else "toggle"
                        if cmd in ("toggle", "t", ""):
                            # toggle is thread-safe; overlay handles Gtk thread via idle_add internally
                            try:
                                self.toggle()
                            except Exception as e:
                                print(f"[wispr] toggle error: {e}")
                            try:
                                conn.sendall(b"ok toggled\n")
                            except Exception:
                                pass
                        elif cmd == "status":
                            st = "recording" if self._recording else "idle"
                            try:
                                conn.sendall(f"{st}\n".encode())
                            except Exception:
                                pass
                        elif cmd == "stop":
                            self.stop()
                            try:
                                conn.sendall(b"ok stopped\n")
                            except Exception:
                                pass
                        elif cmd == "is_recording":
                            try:
                                conn.sendall((b"1\n" if self._recording else b"0\n"))
                            except Exception:
                                pass
                        elif cmd in ("reload_hotkey", "reload", "hotkey_reload"):
                            ok = self.reload_hotkey()
                            try:
                                conn.sendall((b"ok reloaded\n" if ok else b"failed\n"))
                            except Exception:
                                pass
                        elif cmd.startswith("hotkey "):
                            # allow echo-like "hotkey f9" to set
                            try:
                                new_hk = cmd.split(None, 1)[1].strip().lower()
                                from .config import save_config
                                save_config({"hotkey": new_hk})
                                ok = self.reload_hotkey(new_hk)
                                conn.sendall((f"ok hotkey {new_hk}\n".encode() if ok else b"failed\n"))
                            except Exception as e:
                                try:
                                    conn.sendall(f"error {e}\n".encode())
                                except Exception:
                                    pass
                        else:
                            try:
                                conn.sendall(b"unknown\n")
                            except Exception:
                                pass
                except Exception as e:
                    if not self._stop_event.is_set():
                        print(f"[wispr] socket loop error: {e}")
            print("[wispr] socket loop exit")

        self._sock_thread = threading.Thread(target=_loop, daemon=True)
        self._sock_thread.start()
        return True

    def _setup_signals(self):
        def _handle_sigusr1(signum, frame):
            print("[wispr] SIGUSR1 -> toggle")
            threading.Thread(target=self.toggle, daemon=True).start()
        def _handle_sigterm(signum, frame):
            print("[wispr] SIGTERM -> exit")
            self.stop()
            # Also stop hotkey listeners
            try:
                for lis in getattr(self, '_hotkey_listeners', []):
                    try:
                        lis.stop()
                    except Exception:
                        pass
                if hasattr(self, '_hotkey_listener') and self._hotkey_listener:
                    try:
                        self._hotkey_listener.stop()
                    except Exception:
                        pass
            except Exception:
                pass
            # exit Gtk main if running
            if HAS_GTK_DAEMON:
                try:
                    Gtk.main_quit()
                except Exception:
                    pass
            os._exit(0)
        try:
            signal.signal(signal.SIGUSR1, _handle_sigusr1)
            signal.signal(signal.SIGTERM, _handle_sigterm)
            signal.signal(signal.SIGINT, _handle_sigterm)
        except Exception as e:
            print(f"[wispr] signal setup failed: {e}")

    def _setup_hotkey(self):
        # Try pynput global hotkey; may fail on Wayland or without X
        hk = (self.cfg.get("hotkey") or "shift+z").lower().strip()
        # Map "f9" -> <f9>, "ctrl+shift+space" etc? pynput uses <f9> syntax
        # Simple: if hk is like "f9" or "f8", wrap in <>
        # If hk contains "+", split
        try:
            from pynput import keyboard
        except Exception as e:
            print(f"[wispr] pynput not available, hotkey disabled (use GNOME shortcut): {e}")
            return

        # Build pynput hotkey string
        # pynput GlobalHotKeys expects strings like '<f9>' or '<ctrl>+<shift>+<space>'
        def normalize(hotkey_str: str) -> str:
            # Handle "left shift + z" with spaces: normalize before split
            s = hotkey_str.lower().strip()
            # Map left/right shift variants to canonical "shift" for GlobalHotKeys
            # (GlobalHotKeys uses <shift> for either; left-specific is handled via Listener below)
            for variant in ("left_shift", "left shift", "lshift", "shift_l", "leftshift"):
                s = s.replace(variant, "shift")
            for variant in ("right_shift", "right shift", "rshift", "shift_r", "rightshift"):
                s = s.replace(variant, "shift")
            parts = [p.strip() for p in s.replace(" ", "").split("+") if p.strip()]
            norm = []
            for p in parts:
                low = p.lower()
                if low in ("ctrl", "control", "ctrl_l", "ctrl_r"):
                    norm.append("<ctrl>")
                elif low in ("shift", "shift_l", "shift_r"):
                    norm.append("<shift>")
                elif low in ("alt", "mod1", "alt_l", "alt_r"):
                    norm.append("<alt>")
                elif low in ("super", "win", "meta", "cmd"):
                    norm.append("<cmd>")
                elif low.startswith("f") and low[1:].isdigit():
                    norm.append(f"<{low}>")
                elif low == "space":
                    norm.append("<space>")
                elif len(low) == 1:
                    norm.append(low)
                else:
                    norm.append(f"<{low}>")
            return "+".join(norm)

        hk_norm = normalize(hk)
        print(f"[wispr] trying global hotkey {hk!r} -> {hk_norm!r}")

        # Try GlobalHotKeys first (works on X11), fallback to raw Listener (also X11 but different grab)
        # Both are tried; either can trigger toggle. This fixes F9 on some GNOME setups where GlobalHotKeys is blocked.
        self._hotkey_listeners = []
        self._last_hotkey_time = 0
        self._hotkey_lock = threading.Lock()
        # Shared activate with debounce (ignore multiple listeners firing for same physical press)
        def on_activate(source="hotkey"):
            import time as _time
            now = _time.monotonic()
            with self._hotkey_lock:
                # debounce 700ms — GlobalHotKeys + Listener both fire for same X event, and pynput sometimes double-fires (shift+z needs extra margin)
                if now - self._last_hotkey_time < 0.7:
                    print(f"[wispr] hotkey {hk_norm} debounced ({source})")
                    return
                self._last_hotkey_time = now
            print(f"[wispr] hotkey {hk_norm} pressed via {source} -> toggle")
            threading.Thread(target=self.toggle, daemon=True).start()

        # Decide listener strategy: single F-keys → Listener only (more reliable, avoids double-fire); combos → GlobalHotKeys
        # Special case: left shift+z should use a left-shift-aware Listener to honor "left shift + z" and avoid right-shift false triggers, plus GlobalHotKeys as fallback
        target_key = None
        is_shift_z = False  # deprecated: was shift+z
        # left ctrl + left shift (either order) - handle all left/right variants, with/without underscores/spaces
        hk_clean = hk.strip().lower().replace(" ", "").replace("_", "")
        is_ctrl_shift = hk_clean in ("ctrl+shift", "shift+ctrl", "ctrlshift", "shiftctrl",
                                     "leftctrl+leftshift", "leftshift+leftctrl",
                                     "ctrl_l+shift_l", "shift_l+ctrl_l",
                                     "lctrl+lshift", "lshift+lctrl") or hk_clean.count("ctrl") and hk_clean.count("shift") and "+" in hk_clean
        try:
            from pynput import keyboard as kb2
            hk_simple = hk.strip().lower()
            if hk_simple.startswith("f") and hk_simple[1:].isdigit():
                try:
                    target_key = getattr(kb2.Key, hk_simple)
                except Exception:
                    target_key = None
            elif len(hk_simple) == 1:
                target_key = hk_simple
        except Exception:
            target_key = None

        if target_key is not None and not is_shift_z:
            # Single key → use Listener only (avoids GlobalHotKeys+Listener double-fire)
            try:
                from pynput import keyboard as kb2b
                def _on_press(key):
                    try:
                        if key == target_key:
                            print(f"[wispr] hotkey {hk_norm} pressed via Listener -> toggle")
                            on_activate("Listener")
                        elif isinstance(target_key, str) and hasattr(key, 'char') and key.char == target_key:
                            print(f"[wispr] hotkey {hk_norm} pressed via Listener -> toggle")
                            on_activate("Listener")
                    except Exception:
                        pass
                lis = kb2b.Listener(on_press=_on_press)
                lis.start()
                self._hotkey_listener = lis
                self._hotkey_listeners.append(lis)
                print(f"[wispr] hotkey listener active (Listener): {hk_norm}")
            except Exception as e:
                print(f"[wispr] Listener failed: {e}")
        elif is_ctrl_shift:
            # Left Ctrl+Left Shift: GlobalHotKeys does NOT work reliably for ctrl+shift (two modifiers)
            # Use Listener only that tracks both held. Avoids F9 race; debounce handles double events.
            try:
                from pynput import keyboard as kb2b
                pressed = set()
                # Track previous combo state to avoid duplicate triggers from duplicate X events
                prev_both = [False]
                def _on_press_left(key):
                    try:
                        # Check previous state before updating
                        was_both = prev_both[0]
                        # track modifiers (generic shift/ctrl, left and right)
                        if key in (kb2b.Key.shift, kb2b.Key.shift_l):
                            pressed.add("shift")
                            pressed.add("shift_l")
                        elif key == kb2b.Key.shift_r:
                            pressed.add("shift_r")
                        if key in (kb2b.Key.ctrl, kb2b.Key.ctrl_l):
                            pressed.add("ctrl")
                            pressed.add("ctrl_l")
                        elif key == kb2b.Key.ctrl_r:
                            pressed.add("ctrl_r")
                        # Determine current both state
                        has_shift = ("shift" in pressed or "shift_l" in pressed)
                        has_ctrl = ("ctrl" in pressed or "ctrl_l" in pressed)
                        only_right_shift = "shift_r" in pressed and "shift" not in pressed and "shift_l" not in pressed
                        only_right_ctrl = "ctrl_r" in pressed and "ctrl" not in pressed and "ctrl_l" not in pressed
                        is_both = has_shift and has_ctrl and not (only_right_shift or only_right_ctrl)
                        # Only trigger on transition from not-both to both (prevents duplicate triggers from duplicate X events)
                        if is_both and not was_both:
                            if key in (kb2b.Key.shift, kb2b.Key.shift_l, kb2b.Key.shift_r, kb2b.Key.ctrl, kb2b.Key.ctrl_l, kb2b.Key.ctrl_r):
                                print(f"[wispr] hotkey {hk_norm} pressed via Listener (left ctrl+shift) -> toggle")
                                on_activate("Listener-left-ctrl+shift")
                        prev_both[0] = is_both
                    except Exception as e:
                        print(f"[wispr] Listener ctrl+shift press error: {e}")
                def _on_release_left(key):
                    try:
                        if key in (kb2b.Key.shift, kb2b.Key.shift_l):
                            pressed.discard("shift")
                            pressed.discard("shift_l")
                        elif key == kb2b.Key.shift_r:
                            pressed.discard("shift_r")
                        if key in (kb2b.Key.ctrl, kb2b.Key.ctrl_l):
                            pressed.discard("ctrl")
                            pressed.discard("ctrl_l")
                        elif key == kb2b.Key.ctrl_r:
                            pressed.discard("ctrl_r")
                        # Update prev_both after release
                        has_shift = ("shift" in pressed or "shift_l" in pressed)
                        has_ctrl = ("ctrl" in pressed or "ctrl_l" in pressed)
                        only_right_shift = "shift_r" in pressed and "shift" not in pressed and "shift_l" not in pressed
                        only_right_ctrl = "ctrl_r" in pressed and "ctrl" not in pressed and "ctrl_l" not in pressed
                        prev_both[0] = has_shift and has_ctrl and not (only_right_shift or only_right_ctrl)
                    except Exception:
                        pass
                lis2 = kb2b.Listener(on_press=_on_press_left, on_release=_on_release_left)
                lis2.start()
                self._hotkey_listener = lis2
                self._hotkey_listeners.append(lis2)
                print(f"[wispr] hotkey listener active (Listener-left-ctrl+shift): {hk_norm}")
            except Exception as e:
                print(f"[wispr] Listener left-ctrl+shift failed: {e}")
        else:
            # Combo → GlobalHotKeys
            try:
                from pynput.keyboard import GlobalHotKeys
                listener = GlobalHotKeys({hk_norm: lambda: on_activate("GlobalHotKeys")})
                listener.start()
                self._hotkey_listener = listener
                self._hotkey_listeners.append(listener)
                print(f"[wispr] hotkey listener active (GlobalHotKeys): {hk_norm}")
            except Exception as e:
                print(f"[wispr] GlobalHotKeys failed: {e}")
            # Also try Listener for combos if possible? keep simple

        if not self._hotkey_listeners:
            print("[wispr] hotkey listen failed (expected on Wayland):")
            print("[wispr] -> bind `wisprflow toggle` to a GNOME custom shortcut instead.")
            print("[wispr]    Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts → +")
            print(f"[wispr]    Name: Wispr Toggle  Command: {str((__import__('pathlib').Path.home() / '.local/bin/wisprflow'))} toggle  Shortcut: {hk.upper() if hk.lower().startswith('f') else hk}")
        else:
            print(f"[wispr] hotkey ready — press {hk.upper() if hk.lower().startswith('f') else hk} to toggle (also `wisprflow toggle` works)")
            # Also ensure GNOME shortcut hint is always printed if user is on X11 but GlobalHotKeys might be blocked
            if len(self._hotkey_listeners) == 1:
                print(f"[wispr] Tip: if {hk} doesn't fire, bind `wisprflow toggle` via GNOME Settings → Keyboard → Custom Shortcuts")

    def reload_hotkey(self, new_hotkey=None):
        """Hot-reload hotkey from config or from pill recorder without daemon restart."""
        try:
            if new_hotkey is not None:
                # already saved via config, but ensure normalized
                new_hotkey = str(new_hotkey).strip().lower()
                print(f"[wispr] reload_hotkey requested -> {new_hotkey!r}")
            else:
                # reload from file
                self.cfg = load_config()
                new_hotkey = (self.cfg.get("hotkey") or "f9").strip().lower()
                print(f"[wispr] reload_hotkey from config -> {new_hotkey!r}")
            # stop old listeners
            old = list(getattr(self, '_hotkey_listeners', []) or [])
            if hasattr(self, '_hotkey_listener') and self._hotkey_listener:
                try:
                    if self._hotkey_listener not in old:
                        old.append(self._hotkey_listener)
                except Exception:
                    pass
            for lis in old:
                try:
                    lis.stop()
                except Exception:
                    pass
            self._hotkey_listeners = []
            self._hotkey_listener = None
            # reload cfg and re-setup
            self.cfg = load_config()
            # if new_hotkey provided, ensure config file already updated; if not, use provided
            if new_hotkey and self.cfg.get("hotkey") != new_hotkey:
                # fallback sync - config should have been saved by caller
                self.cfg["hotkey"] = new_hotkey
            self._setup_hotkey()
            # update overlay & tray UI to reflect new hotkey
            try:
                # update overlay pill sub-label on Gtk thread
                if HAS_GTK_DAEMON:
                    from gi.repository import GLib as _GLib
                    try:
                        _GLib.idle_add(lambda: (self.overlay._update_idle_ui(), False)[1] if hasattr(self.overlay, '_update_idle_ui') else False)
                        # also refresh overlay show_idle to ensure display
                        if hasattr(self.overlay, '_state') and self.overlay._state == "idle":
                            _GLib.idle_add(lambda: (self.overlay.show_idle(), False)[1])
                    except Exception:
                        pass
            except Exception:
                pass
            # update tray label if exists
            try:
                hk_pretty = new_hotkey.upper() if new_hotkey.lower().startswith("f") else "+".join(p.capitalize() for p in new_hotkey.split("+"))
                if hasattr(self, '_tray') and self._tray:
                    # tray menu first item is toggle hint
                    print(f"[wispr] tray hotkey updated to {hk_pretty}")
            except Exception:
                pass
            # also handle socket command "reload_hotkey" for external callers
            return True
        except Exception as e:
            print(f"[wispr] reload_hotkey failed: {e}")
            import traceback; traceback.print_exc()
            return False

    def _setup_tray(self):
        if not HAS_GTK_DAEMON:
            return
        try:
            # Try Ayatana AppIndicator, fallback to Gtk.StatusIcon
            self._tray = None
            try:
                import gi
                gi.require_version("AyatanaAppIndicator3", "0.1")
                from gi.repository import AyatanaAppIndicator3 as AppIndicator
                ind = AppIndicator.Indicator.new(
                    "wisprflow",
                    "audio-input-microphone",
                    AppIndicator.IndicatorCategory.APPLICATION_STATUS
                )
                ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
                ind.set_title("WisprFlow")
                menu = Gtk.Menu()

                # dynamic hotkey label
                try:
                    hk_cur = (self.cfg.get("hotkey") or "ctrl+shift").strip()
                    hk_pretty_cur = hk_cur.upper() if hk_cur.lower().startswith("f") else "+".join(p.capitalize() for p in hk_cur.split("+"))
                except Exception:
                    hk_pretty_cur = "Ctrl+Shift"
                item_toggle = Gtk.MenuItem(label=f"Click pill or press {hk_pretty_cur} to toggle")
                item_toggle.connect("activate", lambda _: threading.Thread(target=self.toggle, daemon=True).start())
                menu.append(item_toggle)
                self._tray_toggle_item = item_toggle

                item_hotkey = Gtk.MenuItem(label=f"Change hotkey…  (now {hk_pretty_cur})")
                def _tray_change_hotkey(_):
                    # show pill recorder on Gtk thread
                    try:
                        GLib.idle_add(lambda: (self.overlay._show_hotkey_recorder(), False)[1])
                    except Exception:
                        pass
                item_hotkey.connect("activate", _tray_change_hotkey)
                menu.append(item_hotkey)
                self._tray_hotkey_item = item_hotkey

                item_show = Gtk.MenuItem(label="Show pill")
                item_show.connect("activate", lambda _: self.overlay.show_idle())
                menu.append(item_show)

                item_status = Gtk.MenuItem(label="Status: Idle — Click pill to talk")
                def update_status_label():
                    # Use pill state for tray
                    state = getattr(self.overlay, '_state', 'idle')
                    item_status.set_label(f"Status: {state} — {self.cfg.get('model')}")
                    # also refresh hotkey labels live
                    try:
                        hk_live = (load_config().get("hotkey") or "ctrl+shift").strip()
                        hk_pretty_live = hk_live.upper() if hk_live.lower().startswith("f") else "+".join(p.capitalize() for p in hk_live.split("+"))
                        if hasattr(self, '_tray_toggle_item'):
                            self._tray_toggle_item.set_label(f"Click pill or press {hk_pretty_live} to toggle")
                        if hasattr(self, '_tray_hotkey_item'):
                            self._tray_hotkey_item.set_label(f"Change hotkey…  (now {hk_pretty_live})")
                    except Exception:
                        pass
                    return True
                GLib.timeout_add(1000, update_status_label)
                menu.append(item_status)

                item_cfg = Gtk.MenuItem(label="Config: ~/.config/wisprflow/config.json")
                item_cfg.connect("activate", lambda _: subprocess.Popen(["xdg-open", str(Path.home() / ".config/wisprflow/config.json")]))
                menu.append(item_cfg)

                sep = Gtk.SeparatorMenuItem()
                menu.append(sep)

                item_quit = Gtk.MenuItem(label="Quit")
                item_quit.connect("activate", lambda _: (self.stop(), Gtk.main_quit()))
                menu.append(item_quit)

                menu.show_all()
                ind.set_menu(menu)
                self._tray = ind
                print("[wispr] tray: AyatanaAppIndicator active")
            except Exception as e:
                print(f"[wispr] AppIndicator not available: {e}, trying StatusIcon")
                # fallback StatusIcon (deprecated but works on X11)
                try:
                    icon = Gtk.StatusIcon.new_from_icon_name("audio-input-microphone")
                    icon.set_tooltip_text("WisprFlow — Left Ctrl+Shift to toggle")
                    icon.set_visible(True)
                    def on_activate(icon):
                        GLib.idle_add(lambda: (self.toggle(), False)[1])
                    def on_popup(icon, button, time):
                        m = Gtk.Menu()
                        i1 = Gtk.MenuItem(label="Toggle")
                        i1.connect("activate", lambda _: GLib.idle_add(lambda: (self.toggle(), False)[1]))
                        m.append(i1)
                        i2 = Gtk.MenuItem(label="Quit")
                        i2.connect("activate", lambda _: (self.stop(), Gtk.main_quit()))
                        m.append(i2)
                        m.show_all()
                        m.popup(None, None, None, None, button, time)
                    icon.connect("activate", on_activate)
                    icon.connect("popup-menu", on_popup)
                    self._tray = icon
                except Exception as e2:
                    print(f"[wispr] StatusIcon failed: {e2}")
        except Exception as e:
            print(f"[wispr] tray setup failed: {e}")

    def run(self, no_tray=False):
        print("[wispr] starting daemon…")
        self._ensure_cache()
        self._setup_signals()
        ok = self._start_socket()
        if not ok:
            print("[wispr] socket start failed — maybe daemon already running")
            # still continue? but avoid double hotkey
        # hotkey and tray need Gtk main loop context? start after
        # For Gtk daemon, we need to init in main thread before Gtk.main
        if HAS_GTK_DAEMON:
            # schedule hotkey/tray after Gtk init via idle_add once main starts
            def _init_ui():
                self._setup_tray()
                self._setup_hotkey()
                # show initial notify
                if self.cfg.get("overlay_enabled"):
                    # Show persistent pill immediately (click to toggle)
                    try:
                        self.overlay.show_idle()
                    except Exception:
                        pass
                    try:
                        import shutil, subprocess
                        if shutil.which("notify-send"):
                            subprocess.Popen(["notify-send", "-u", "low", "-t", "1800", "WisprFlow ready", "Click the pill to talk — Left Ctrl+Shift also works"],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                return False
            GLib.idle_add(_init_ui)
            # block
            try:
                Gtk.main()
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        else:
            # headless loop without Gtk
            self._setup_hotkey()
            print("[wispr] running headless (no Gtk). Press Ctrl+C to quit. Use `wisprflow toggle` to control.")
            try:
                while not self._stop_event.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.stop()

    def stop(self):
        print("[wispr] stopping daemon")
        self._stop_event.set()
        with self._lock:
            if self._recording:
                try:
                    self.recorder.cancel()
                except Exception:
                    pass
                self._recording = False
        try:
            if self._hotkey_listener:
                self._hotkey_listener.stop()
        except Exception:
            pass
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        try:
            if SOCK_PATH.exists():
                SOCK_PATH.unlink()
        except Exception:
            pass
        try:
            if PID_PATH.exists():
                PID_PATH.unlink()
        except Exception:
            pass
        self.overlay.stop()
        # Don't quit Gtk here if called from signal handler outside main thread


def run_daemon():
    d = Daemon()
    d.run()
