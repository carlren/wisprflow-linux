"""
CLI for wisprflow
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import signal
import shutil
import tempfile
from pathlib import Path

from .config import load_config, save_config, redact_key, CONFIG_PATH, DEFAULT_MODEL
from .daemon import SOCK_PATH, PID_PATH, CACHE_DIR

def _send_sock(cmd: str, timeout=1.0) -> str | None:
    if not SOCK_PATH.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(SOCK_PATH))
        s.sendall((cmd + "\n").encode())
        s.settimeout(timeout)
        data = s.recv(4096).decode("utf-8", errors="ignore").strip()
        s.close()
        return data
    except Exception:
        return None

def _try_toggle_via_sock() -> bool:
    resp = _send_sock("toggle", timeout=1.0)
    return resp is not None and "ok" in resp.lower()

def _try_toggle_via_signal() -> bool:
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, signal.SIGUSR1)
            return True
        except Exception:
            return False
    # try pgrep
    try:
        out = subprocess.check_output(["pgrep", "-f", "wisprflow.*daemon"], text=True)
        for line in out.strip().splitlines():
            try:
                os.kill(int(line.strip()), signal.SIGUSR1)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False

def cmd_toggle(args):
    if _try_toggle_via_sock():
        print("Toggled (via socket).")
        return 0
    if _try_toggle_via_signal():
        print("Toggled (via SIGUSR1).")
        return 0
    print("No running daemon found. Starting one-shot toggle? Use `wisprflow daemon &` first.", file=sys.stderr)
    print("Hint: run `wisprflow daemon` in another terminal, then `wisprflow toggle` again.", file=sys.stderr)
    # Optionally auto-start daemon in background?
    # But better to tell user
    return 1

def cmd_daemon(args):
    from .daemon import run_daemon
    # check already running
    if SOCK_PATH.exists() and _send_sock("status", timeout=0.5) is not None:
        print("Daemon already running.", file=sys.stderr)
        return 1
    run_daemon()
    return 0

def cmd_start(args):
    # systemd --user start, fallback to nohup
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "start", "wisprflow"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode == 0:
            print("Started via systemctl --user")
            subprocess.run(["systemctl", "--user", "status", "wisprflow", "--no-pager"], stdout=sys.stdout, stderr=sys.stderr)
            return 0
        else:
            print(r.stderr.decode()[:500], file=sys.stderr)
    # fallback: nohup
    print("systemctl not available or failed, starting with nohup...")
    log = Path.home() / ".cache" / "wisprflow" / "daemon.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as f:
        proc = subprocess.Popen([sys.executable, "-m", "wisprflow", "daemon"], stdout=f, stderr=f, start_new_session=True)
    print(f"Daemon pid {proc.pid}, log {log}")
    time.sleep(0.6)
    print(_send_sock("status") or "check `wisprflow status`")
    return 0

def cmd_stop(args):
    stopped = False
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "stop", "wisprflow"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode == 0:
            print("Stopped via systemctl --user")
            stopped = True
    # try socket stop
    resp = _send_sock("stop")
    if resp:
        print("Stopped via socket")
        stopped = True
    # try signal
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to {pid}")
            stopped = True
            time.sleep(0.5)
        except Exception as e:
            print(f"kill failed: {e}", file=sys.stderr)
    else:
        try:
            out = subprocess.check_output(["pgrep", "-f", "wisprflow.*daemon"], text=True)
            for line in out.strip().splitlines():
                os.kill(int(line.strip()), signal.SIGTERM)
                print(f"Killed {line.strip()}")
                stopped = True
        except Exception:
            pass
    if not stopped:
        print("No daemon found.", file=sys.stderr)
        return 1
    return 0

def cmd_status(args):
    cfg = load_config()
    print(f"Config: {CONFIG_PATH}  exists={CONFIG_PATH.exists()}")
    print(f"  model: {cfg.get('model')}")
    print(f"  hotkey: {cfg.get('hotkey')}")
    print(f"  api_key: {redact_key(cfg.get('api_key',''))}")
    print(f"  language: {cfg.get('language')}")
    print(f"  overlay: {cfg.get('overlay_enabled')}")
    print(f"  XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')}  WAYLAND={bool(os.environ.get('WAYLAND_DISPLAY'))}  DISPLAY={os.environ.get('DISPLAY')}")
    print(f"Daemon sock: {SOCK_PATH} exists={SOCK_PATH.exists()}")
    print(f"Daemon pid: {PID_PATH} exists={PID_PATH.exists()}  pid={PID_PATH.read_text().strip() if PID_PATH.exists() else '—'}")
    st = _send_sock("status", timeout=0.7)
    if st:
        print(f"Daemon status: {st}")
    else:
        print("Daemon status: not running (no socket)")
        # check systemd
        if shutil.which("systemctl"):
            r = subprocess.run(["systemctl", "--user", "is-active", "wisprflow"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            print(f"systemd is-active: {r.stdout.decode().strip()} / {r.stderr.decode().strip()}")
    return 0

def cmd_config(args):
    if args.show:
        cfg = load_config()
        # redact
        out = dict(cfg)
        out["api_key"] = redact_key(out.get("api_key",""))
        print(json.dumps(out, indent=2))
        print(f"\nConfig file: {CONFIG_PATH}")
        return 0
    updates = {}
    if args.api_key is not None:
        updates["api_key"] = args.api_key.strip()
    if args.model is not None:
        updates["model"] = args.model.strip()
    if args.language is not None:
        # allow "auto" to clear
        if args.language.lower() in ("auto", "none", "null", ""):
            updates["language"] = None
        else:
            updates["language"] = args.language.strip()
    if args.hotkey is not None:
        updates["hotkey"] = args.hotkey.strip().lower()
    if args.no_overlay:
        updates["overlay_enabled"] = False
    if args.overlay:
        updates["overlay_enabled"] = True
    if args.no_sound:
        updates["sound_enabled"] = False
    if args.sound:
        updates["sound_enabled"] = True
    if args.paste_mode is not None:
        mode = args.paste_mode.strip().lower()
        if mode not in ("auto", "gui", "terminal", "primary", "type"):
            print(f"Invalid paste-mode {mode!r}: choose auto/gui/terminal/primary", file=sys.stderr)
            return 1
        updates["paste_mode"] = mode
    if not updates:
        print("No config changes given. Use --help.", file=sys.stderr)
        return 1
    new_cfg = save_config(updates)
    print(f"Saved to {CONFIG_PATH}")
    # show redacted
    for k, v in updates.items():
        if k == "api_key":
            print(f"  {k}: {redact_key(v)}")
        else:
            print(f"  {k}: {v!r}")
    return 0

def cmd_diagnose(args):
    print("=== wisprflow diagnose ===")
    cfg = load_config()
    print(f"Python: {sys.version.split()[0]}  exe={sys.executable}")
    print(f"Config: {CONFIG_PATH} exists={CONFIG_PATH.exists()}")
    print(f"  api_key: {redact_key(cfg.get('api_key',''))}  model={cfg.get('model')}  lang={cfg.get('language')}  hotkey={cfg.get('hotkey')}")
    print(f"Env: XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')}  WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}  DISPLAY={os.environ.get('DISPLAY')}")
    print(f"Session audio: PipeWire/Pulse? trying pactl/pipewire")
    for cmd in [["which", "xdotool"], ["which", "wtype"], ["which", "wl-copy"], ["which", "wl-paste"], ["which", "xclip"], ["which", "xsel"], ["which", "ydotool"], ["which", "arecord"], ["which", "parecord"]]:
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
            print(f"  {' '.join(cmd)}: {r.stdout.strip() or '(not found)'}")
        except Exception as e:
            print(f"  {' '.join(cmd)}: error {e}")
    print("\nPython deps:")
    for mod in ["gi", "sounddevice", "soundfile", "pynput", "requests", "numpy"]:
        try:
            __import__(mod)
            print(f"  {mod}: OK")
        except Exception as e:
            print(f"  {mod}: missing ({e})")
    print("\nGtk:")
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        print("  Gtk 3.0: OK")
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3
            print("  AyatanaAppIndicator3: OK")
        except Exception as e:
            print(f"  AyatanaAppIndicator3: missing ({e}) -> fallback to StatusIcon")
    except Exception as e:
        print(f"  Gtk: missing ({e}) -> overlay will use notify-send only")

    print("\nMicrophones (arecord -l):")
    try:
        r = subprocess.run(["arecord", "-l"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3)
        print(r.stdout[:2000] if r.stdout else "(no output)")
    except Exception as e:
        print(f"  arecord not found or error: {e}")

    print("\nTrying sounddevice query:")
    try:
        import sounddevice as sd
        print(f"  sounddevice {sd.__version__}")
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                print(f"    [{i}] {dev['name']}  in={dev['max_input_channels']} sr={dev['default_samplerate']}")
    except Exception as e:
        print(f"  sounddevice query failed: {e}")

    print("\nDaemon:")
    print(f"  sock {SOCK_PATH} exists={SOCK_PATH.exists()}")
    st = _send_sock("status")
    print(f"  status via sock: {st or 'not running'}")
    # clipboard test
    print("\nClipboard test: trying to set 'wispr-diagnose'...")
    try:
        from .injector import _set_clipboard, _get_clipboard
        ok = _set_clipboard("wispr-diagnose-ok")
        print(f"  set: {ok}")
        time.sleep(0.2)
        got = _get_clipboard()
        print(f"  get: {got!r}")
    except Exception as e:
        print(f"  clipboard error: {e}")

    print("\nPaste simulation dry-run (will paste if you have a focused text field): skipped in diagnose.")
    print("Run `wisprflow test-mic` for 3-sec mic test, or `wisprflow daemon` to start.")
    return 0

def cmd_test_mic(args):
    secs = args.seconds or 3
    print(f"Recording {secs}s test… speak now")
    from .recorder import Recorder
    cfg = load_config()
    rec = Recorder(sample_rate=cfg.get("sample_rate", 16000), channels=1, max_seconds=secs+1)
    try:
        rec.start()
        for i in range(secs):
            print(f"  {i+1}/{secs}s", end="\r", flush=True)
            time.sleep(1)
        path = rec.stop()
        if not path:
            print("\nNo audio captured (too short or mic muted).")
            return 1
        print(f"\nSaved {path}  size={os.path.getsize(path)} bytes")
        # show wav info
        try:
            import soundfile as sf
            data, sr = sf.read(path)
            print(f"  soundfile: sr={sr} shape={getattr(data, 'shape', len(data))} dur={len(data)/sr:.2f}s")
        except Exception as e:
            print(f"  soundfile read failed: {e}")
        # optionally try transcription if key set
        if cfg.get("api_key"):
            print("Transcribing test with OpenRouter…")
            try:
                from .transcriber import transcribe
                text = transcribe(path, cfg)
                print(f"  -> {text!r}")
            except Exception as e:
                print(f"  transcribe failed: {e}")
        else:
            print("Skipping transcribe (no API key). Set with `wisprflow config --api-key ...`")
        # keep file for debug? delete
        try:
            os.unlink(path)
        except Exception:
            pass
        return 0
    except Exception as e:
        print(f"\nMic test failed: {e}", file=sys.stderr)
        try:
            rec.cancel()
        except Exception:
            pass
        return 1

def cmd_install_hotkey(args):
    # helper to create GNOME custom keybinding via gsettings
    hotkey = args.hotkey or load_config().get("hotkey", "f9")
    # Map F9 -> F9, but gsettings expects <Primary> etc; F keys are just "F9"
    gsettings_hotkey = hotkey
    # Try to add custom keybinding
    cmd = f"{sys.executable} -m wisprflow toggle"
    # also try ~/.local/bin/wisprflow if exists
    local_bin = Path.home() / ".local" / "bin" / "wisprflow"
    if local_bin.exists():
        cmd = str(local_bin) + " toggle"
    print(f"Attempting to register GNOME shortcut: {gsettings_hotkey!r} -> {cmd!r}")
    try:
        # read existing custom-keybindings
        r = subprocess.run(["gsettings", "get", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings"],
                           stdout=subprocess.PIPE, text=True)
        cur = r.stdout.strip()
        print(f"Current: {cur}")
        # For simplicity, print manual instructions and try dconf route for first slot
        # We will not overwrite user's existing bindings aggressively; instead instruct
        print("\nTo finish manually (GNOME Wayland safe):")
        print("  Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts → +")
        print(f"  Name: Wispr Toggle")
        print(f"  Command: {cmd}")
        print(f"  Shortcut: {hotkey.upper() if hotkey.lower().startswith('f') else hotkey}")
        # Try automated for custom0 if empty
        if cur in ("@as []", "[]", ""):
            path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
            subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"+path, "name", "'Wispr Toggle'"], check=False)
            subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"+path, "command", f"'{cmd}'"], check=False)
            subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"+path, "binding", f"'{gsettings_hotkey}'"], check=False)
            subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings", f"['{path}']"], check=False)
            print(f"\nAutomated: added {path} with binding {gsettings_hotkey}")
        else:
            print("\nCustom shortcuts already exist — please add one manually to avoid overwriting.")
    except Exception as e:
        print(f"gsettings failed: {e}", file=sys.stderr)
        print("Please add the shortcut manually via Settings.")
    return 0

def build_parser():
    p = argparse.ArgumentParser(prog="wisprflow", description="WisprFlow for Linux — push-to-talk STT via OpenRouter")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("daemon", help="run daemon (foreground)")
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("toggle", help="toggle recording (press hotkey)")
    sp.set_defaults(func=cmd_toggle)

    sp = sub.add_parser("start", help="start daemon (systemd or nohup)")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop daemon")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("status", help="show status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("config", help="configure")
    sp.add_argument("--api-key", dest="api_key", help="OpenRouter API key (sk-or-...)")
    sp.add_argument("--model", help="STT model, e.g. openai/gpt-4o-transcribe")
    sp.add_argument("--language", help="language code (en, fr… or 'auto')")
    sp.add_argument("--hotkey", help="hotkey like f9, ctrl+shift+space")
    sp.add_argument("--paste-mode", dest="paste_mode", help="paste mode: auto (detect terminal vs GUI), gui=Ctrl+V, terminal=Ctrl+Shift+V/Shift+Insert, primary=Shift+Insert")
    sp.add_argument("--show", action="store_true", help="show current config")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--overlay", action="store_true", help="enable overlay")
    grp.add_argument("--no-overlay", action="store_true", help="disable overlay")
    grp2 = sp.add_mutually_exclusive_group()
    grp2.add_argument("--sound", action="store_true", help="enable sounds")
    grp2.add_argument("--no-sound", action="store_true", help="disable sounds")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("diagnose", help="check system deps")
    sp.set_defaults(func=cmd_diagnose)

    sp = sub.add_parser("test-mic", help="record 3s and check mic")
    sp.add_argument("--seconds", type=int, default=3)
    sp.set_defaults(func=cmd_test_mic)

    sp = sub.add_parser("install-hotkey", help="helper to add GNOME shortcut")
    sp.add_argument("--hotkey", help="key, default from config")
    sp.set_defaults(func=cmd_install_hotkey)

    sp = sub.add_parser("listen-keys", help="debug: listen for hotkey presses (press F9 to test, Ctrl+C to exit)")
    sp.add_argument("--hotkey", help="hotkey to watch, default from config")
    sp.add_argument("--seconds", type=int, default=10, help="seconds to listen")
    def _listen_keys(args):
        from .config import load_config
        cfg = load_config()
        hk = (args.hotkey or cfg.get("hotkey") or "f9").lower()
        print(f"Listening for hotkey {hk!r} for {args.seconds}s — press it now (Ctrl+C to exit)...")
        print(f"Config hotkey: {cfg.get('hotkey')}  XDG_SESSION_TYPE={__import__('os').environ.get('XDG_SESSION_TYPE')} DISPLAY={__import__('os').environ.get('DISPLAY')}")
        try:
            from pynput import keyboard
            def normalize(s):
                parts = [p.strip() for p in s.replace(" ", "").split("+") if p.strip()]
                norm = []
                for p in parts:
                    low = p.lower()
                    if low in ("ctrl", "control"):
                        norm.append("<ctrl>")
                    elif low == "shift":
                        norm.append("<shift>")
                    elif low == "alt":
                        norm.append("<alt>")
                    elif low in ("super", "win", "meta"):
                        norm.append("<cmd>")
                    elif low.startswith("f") and low[1:].isdigit():
                        norm.append(f"<{low}>")
                    elif low == "space":
                        norm.append("<space>")
                    else:
                        norm.append(low)
                return "+".join(norm)
            print(f"Normalized: {normalize(hk)}")
            fired = [False]
            ghk = None
            try:
                from pynput.keyboard import GlobalHotKeys
                def on_activate():
                    print(f"*** GlobalHotKeys FIRED: {hk} ***")
                    fired[0] = True
                ghk = GlobalHotKeys({normalize(hk): on_activate})
                ghk.start()
                print("GlobalHotKeys listening...")
            except Exception as e:
                print(f"GlobalHotKeys failed: {e}")
            target = None
            if hk.startswith("f") and hk[1:].isdigit():
                try:
                    target = getattr(keyboard.Key, hk)
                except Exception:
                    target = None
            def on_press(key):
                print(f"on_press: {key}")
                if target and key == target:
                    print(f"*** Listener FIRED: {hk} ***")
                    fired[0] = True
                if key == keyboard.Key.f9:
                    print("*** Listener saw Key.f9 ***")
            lis = keyboard.Listener(on_press=on_press)
            lis.start()
            print("Listener listening...")
            import time
            for i in range(args.seconds):
                time.sleep(1)
                print(f"{args.seconds - i - 1}s remaining... fired={fired[0]}")
            if ghk:
                ghk.stop()
            lis.stop()
            print("Done listen-keys")
            if fired[0]:
                print("SUCCESS: hotkey detected! Daemon should also detect it.")
            else:
                print("No hotkey detected — try GNOME shortcut `wisprflow toggle` instead.")
                print("Run: wisprflow install-hotkey  and set via Settings → Keyboard → Custom Shortcuts")
        except Exception as e:
            print(f"listen-keys failed: {e}")
            import traceback; traceback.print_exc()
        return 0
    sp.set_defaults(func=_listen_keys)

    sp = sub.add_parser("retry-pending", help="retry queued audio when back online (failed OpenRouter calls)")
    def _retry_pending(args):
        pending = CACHE_DIR / "pending"
        if not pending.exists() or not any(pending.iterdir()):
            print(f"No pending files in {pending}")
            return 0
        from .config import load_config
        from .transcriber import transcribe
        from .injector import inject_text
        cfg = load_config()
        files = sorted(pending.glob("wispr_*.wav"))
        if not files:
            files = sorted(pending.glob("*.wav"))
        print(f"Retrying {len(files)} pending file(s) in {pending}")
        ok_cnt = 0
        fail_cnt = 0
        for wav in files:
            print(f"  {wav.name} ({wav.stat().st_size} bytes) -> {cfg.get('model')} ...", end=" ", flush=True)
            try:
                text = transcribe(str(wav), cfg)
                print(f"-> {text!r}")
                if text and text.strip():
                    from .injector import _set_clipboard
                    try:
                        _set_clipboard(text)
                    except Exception:
                        pass
                    # try inject
                    try:
                        inject_text(text)
                    except Exception:
                        pass
                    print(f"    pasted + kept in clipboard. Removing {wav.name}")
                    try:
                        wav.unlink()
                    except Exception:
                        pass
                    ok_cnt += 1
                else:
                    print("    empty result, keeping")
                    fail_cnt += 1
            except Exception as e:
                print(f"    failed: {e}")
                fail_cnt += 1
                # keep file for next retry if network error, else remove?
                if "Network error" in str(e) or "Failed to resolve" in str(e):
                    print("    network still down, keeping for next retry")
                else:
                    print("    non-network error, keeping file — check `journalctl --user -u wisprflow`")
        print(f"Done: {ok_cnt} succeeded, {fail_cnt} still pending. Pending dir: {pending}")
        if pending.exists():
            remain = list(pending.glob("*.wav"))
            if remain:
                print(f"Remaining: {len(remain)} file(s): {[p.name for p in remain]}")
            else:
                print("No pending files left.")
        return 0
    sp.set_defaults(func=_retry_pending)

    sp = sub.add_parser("pending", help="list pending queued audio (offline)")
    def _pending_list(args):
        pending = CACHE_DIR / "pending"
        if not pending.exists():
            print(f"No pending dir {pending}")
            return 0
        files = sorted(pending.glob("*.wav"))
        if not files:
            print(f"No pending files in {pending}")
            return 0
        print(f"Pending in {pending}:")
        for f in files:
            print(f"  {f.name}  {f.stat().st_size} bytes  {time.ctime(f.stat().st_mtime)}")
        return 0
    sp.set_defaults(func=_pending_list)

    return p

def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        # also show status hint
        print("\nHint: `wisprflow diagnose` to check setup, `wisprflow daemon` to run.")
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
