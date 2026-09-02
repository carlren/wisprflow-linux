# WisprFlow Linux — Code Review & Bug Audit
**Project:** `/home/carlren/wisprflow-linux`  (v0.2.0)  
**Date:** 2026-08-16  
**Reviewer:** Muse Spark (DeepSeek Harness)  
**Scope:** Full static review of `wisprflow/*`, `install.sh`, `systemd/wisprflow.service`, tests, docs. *No code was modified in this pass — findings only.*

---

## 1. Executive Summary

The project is a competent push-to-talk STT shim: hotkey → `sounddevice` → temp WAV → OpenRouter `/audio/transcriptions` (base64 JSON) → clipboard + synthetic `Ctrl+V`. Overlay pill and tray provide UX. The codebase is ~3.2k LOC and mostly works on X11.

**Risk posture:** 2 **Critical** (duplicate paste + hardcoded systemd env), 8 **High** (auto-stop race, clipboard/injection triple-fire, temp-file leak, socket hijack, gsettings quoting, signal `os._exit`, audio frame race, perms), 10+ Medium/Low. No blocking security CVE, but UX bugs will bite every user (double/triple pasted text, hotkey not firing on Wayland).

> Recommendation: fix Critical/High before next harness cut; the rest can be filed as follow-ups.

---

## 2. Architecture

```
daemon.py (Gtk main + socket + signal + hotkey)
  ├─ recorder.py  (sounddevice / pyaudio / arecord fallback → /tmp/wispr_*.wav)
  ├─ transcriber.py (requests POST base64 JSON → {text})
  ├─ injector.py (wl-copy/xclip + wtype/xdotool/ydotool/pynput → paste)
  ├─ overlay.py (Gtk pill, drag, right-click, hotkey recorder)
  ├─ config.py (~/.config/wisprflow/config.json, 600)
  └─ cli.py (toggle/status/diagnose/test-mic/install-hotkey/pending)
```

Daemon IPC: `~/.cache/wisprflow/daemon.sock` (0600) + `SIGUSR1` fallback + `pgrep`.

---

## 3. Critical — Must Fix Before Release

### C1. Triple-paste in `injector.py` — all backends fire sequentially
**File:** `wisprflow/injector.py` — `_simulate_paste()` and `inject_text()`

For X11, code does:

    for keys in ["ctrl+v","ctrl+shift+v","Shift+Insert"]:
        r=_run(["xdotool","key","--clearmodifiers",keys])
        success|= (r.returncode==0)

Wayland `wtype` and `ydotool` loops do the same — three pastes are *sent* even after first succeeds. `inject_text` then calls `_simulate_paste()` twice + `_type_direct` if still "not pasted".

**Impact:** Every transcription is pasted 2–3 times in most terminals/editors.
**Fix:** Try one variant per session type and stop on first success; make `inject_text` paste-once.

### C2. Hard-coded systemd env breaks all non-uid 1000 / :1 users
**File:** `systemd/wisprflow.service`

    Environment=DISPLAY=:1
    Environment=XDG_SESSION_TYPE=x11
    Environment=XAUTHORITY=/run/user/1000/gdm/Xauthority

**Impact:** On any machine where user != 1000, display :0, or Wayland, the user service starts headless with wrong env — tray/overlay never shows.
**Fix:** Remove hard-coded env; rely on `systemd --user import-environment` or leave unset.

---

## 4. High — Strongly Recommended

### H1. Auto-stop timer kills *next* recording (daemon)
**File:** `wisprflow/daemon.py` — `_start_recording_locked` auto-stop thread

    def _auto_stop():
        time.sleep(max_s)
        with self._lock:
            if self._recording: self._stop_recording_locked()

If user stops at 10s and starts new recording at 20s, first timer fires at 120s and stops second recording early.
**Fix:** Generation counter: `self._rec_gen +=1; cur=self._rec_gen` and check `if cur==self._rec_gen`.

### H2. Frame list data-race (recorder)
**File:** `wisprflow/recorder.py`

- `self._frames` appended in collector thread and read in main thread without lock.
- `total_samples = sum(len(f) for f in frames)` is O(n^2).
- `self._recording` bool read in audio callback without barrier.

**Fix:** Guard `_frames` with lock; use running counter; use `threading.Event`.

### H3. Socket & PID hijack / TOCTOU
**File:** `wisprflow/daemon.py` & `wisprflow/cli.py`

- `CACHE_DIR` may be 0755; socket predictable, no `SO_PEERCRED` check.
- `cli._try_toggle_via_signal` kills PID from file without validating cmdline; fallback `pgrep -f` kills any matching PID.
- Socket leak: `cli._send_sock` only closes on success.

**Fix:** `close()` in `finally`; validate PID via `/proc/PID/cmdline`; avoid broad pgrep; restrict cache dir to 0700.

### H4. `os._exit(0)` in signal handler bypasses cleanup
**File:** `wisprflow/daemon.py` — `_handle_sigterm`

`os._exit` kills without `atexit`/`finally` or flushing transcription threads; leaves `.sock` orphaned.
**Fix:** Remove `os._exit`; let main loop exit naturally via `Gtk.main_quit`.

### H5. `gsettings` quoting injects literal quotes — GNOME shortcut never works auto
**File:** `wisprflow/cli.py` — `cmd_install_hotkey`

    subprocess.run(["gsettings","set", ..., "name", "'Wispr Toggle'"])

Wrapping in single quotes stores quotes literally; GNOME tries to run `'python…'` and fails.
**Fix:** Pass values without extra quotes: `"Wispr Toggle"`.

### H6. Temp WAV leak on crash/cancel
**File:** `wisprflow/recorder.py`

- arecord creates `/tmp/wispr_*.wav`; SIGKILL leaves files.
- `cancel()` vs `_stop_arecord` race; pending dir uncapped.

**Fix:** Use `CACHE_DIR` temp, cleanup on start, cap pending at 20 files / 50 MB.

### H7. Hotkey normalize bug — `is_ctrl_shift` boolean mix
**File:** `wisprflow/daemon.py` line ~290

    is_ctrl_shift = hk_clean in ("ctrl+shift",...) or hk_clean.count("ctrl") and hk_clean.count("shift") and "+" in hk_clean

Second clause evaluates to True/False, so any hotkey containing substrings ctrl+shift+"+" mis-classified.
**Fix:** Explicit set compare: `set(hk_clean.split("+"))=={"ctrl","shift"}`.

### H8. Config file write is world-readable window
**File:** `wisprflow/config.py` — `save_config`

    with open(CONFIG_PATH,"w") as f: json.dump(...)
    os.chmod(CONFIG_PATH, 0o600)

With umask 022, file is 0644 between open and chmod — API key visible briefly.
**Fix:** Write to tmp with `os.open(...,0o600)` + `fsync` then `os.replace`.

---

## 5. Medium — Should Fix / Design Debt

### M1. `wtype` direct type injection flaw
**File:** `wisprflow/injector.py` — `_type_direct`

`wtype text[:2000]` treats leading `-` as option. E.g., transcribed `"-rm rf"` fails.
**Fix:** `_run(["wtype","--", text[:2000]])`.

### M2. Clipboard Popen may orphan wl-copy
**File:** `wisprflow/injector.py` — `_set_clipboard`

`communicate(timeout=3)` may raise `TimeoutExpired` without killing process.
**Fix:** `try/except TimeoutExpired: p.kill(); p.wait()`.

### M3. Transcriber loads whole WAV + base64 with no size cap
120s x 16k mono = 3.8 MB -> base64 5.1 MB + json 10 MB. No validation before POST.
**Fix:** Enforce max size; stream multipart for >10 MB.

### M4. Wayland/X11 heuristic fragile
**File:** `wisprflow/injector.py` — `_is_wayland`/`_is_x11`

XWayland has both DISPLAY and WAYLAND_DISPLAY → picks Wayland incorrectly.
**Fix:** Prefer `XDG_SESSION_TYPE` if set.

### M5. Daemon reload_hotkey may leave no listener
Stops old listeners then calls `_setup_hotkey`; if that fails, daemon ends with zero hotkey.
**Fix:** Keep old listeners until new setup succeeds.

### M6. Overlay pill multi-monitor placement deprecated API
**File:** `wisprflow/overlay.py` — `Gdk.Screen.get_width()` deprecated, ignores scale/monitor.
**Fix:** Use `Gdk.Display.get_monitor_at_window()` + workarea.

### M7. Tray status updater never removed
`GLib.timeout_add(1000, update_status_label)` never removed on stop.
**Fix:** Store timeout id and remove in `stop`.

### M8. API key via CLI args leaks to ps/history
`wisprflow config --api-key sk-or-...` exposes key in `ps aux` and shell history.
**Fix:** Support `--api-key-from-stdin` or `getpass` prompt.

### M9. Test coverage only overlay idle (2 tests)
No tests for recorder/transcriber/injector/daemon.
**Fix:** Add mocked unit tests.

---

## 6. Low / Nits

- L1. `recorder.py` imports `sounddevice` twice, defines `DEFAULT_SR/CH` unused.
- L2. `daemon.py` imports `subprocess` both top-level and inside function.
- L3. `install.sh` `pulseaudio-utils || pipewire-pulse || true` — `||` applies to apt command, not package list.
- L4. `install.sh` appends `export PATH` to `~/.bashrc` on every run → duplicates.
- L5. `overlay.py` CSS provider added per instance without removal.
- L6. `cli.diagnose` uses `["which","tool"]` rather than `shutil.which`.
- L7. `config.DEFAULT_HOTKEY="ctrl+shift"` vs README default `F9` — mismatch.
- L8. `pending` retry keeps files on non-network errors forever.
- L9. `injector._get_clipboard` tries `wl-paste` even on X11.
- L10. Logging is bare `print`; no levels/rotation.

---

## 7. Cross-Harness / Portability Notes

1. **No git repo** — not a git repository; CI expecting git SHA will fail.
2. **Hard-coded user paths** — `wisprflow.desktop` `Exec=/home/carlren/.local/bin/wisprflow daemon` hard-codes `carlren`; should be `Exec=wisprflow daemon` or `%h/.local/bin/wisprflow`.
3. **Python deps via `--break-system-packages`** — may pollute system pip; prefer pipx/venv.
4. **DSH sandbox** — daemon writes to `~/.cache/wisprflow` outside workspace-write; okay locally but document.

---

## 8. Recommended Fix Order

1. Immediate: C1 (paste), C2 (systemd), H5 (gsettings), H1 (auto-stop)
2. Next iteration: H2, H3, H4, H8, M4, M1
3. Polish: M8 (key input), M9 (tests), L3-L4 (install.sh)

---

## 9. Validation Checklist (for after fixes)

- [ ] `wisprflow daemon` + `toggle` pastes *once* on X11 and Wayland.
- [ ] `systemctl --user status wisprflow` shows correct env on fresh user (not 1000).
- [ ] `wisprflow install-hotkey --hotkey f9` creates gsettings entry that actually runs.
- [ ] Hold toggle 125s → auto-stop, then immediate re-record → not killed by old timer.
- [ ] Pull network during transcribe → pending queued, `retry-pending` replays.
- [ ] `ls -l ~/.config/wisprflow/config.json` is 600 with no 644 window.
- [ ] `python -m pytest tests/` passes with mocked audio.

---

*End of review — no code was changed. Next step: approve fixes or request deeper audit of a specific module.*
