"""
Text injector: paste text at cursor — frictionless terminal/GUI support.
Strategy:
1. Set clipboard + primary via best tool (wl-copy / xclip / xsel / Gtk) — one write, keep for manual paste
2. Detect focused window (X11 via xdotool/xprop, Wayland via gdbus/swaymsg/hyprctl) -> auto choose paste mode
   - paste_mode config: "auto" (default, detect) | "gui" | "terminal" | "primary"
   - gui      -> Ctrl+V (clipboard)
   - terminal -> Ctrl+Shift+V (clipboard, terminal binding) with Shift+Insert fallback only if needed
   - primary  -> Shift+Insert (primary selection)
3. Simulate *single* paste via best tool (wtype / xdotool / ydotool / pynput) — no triple-fire
4. Fallback to direct typing only for short text if paste didn't execute
Keeps clipboard for manual paste (TUI) for 30s+; no duplicate pastes.
"""
import os
import shutil
import subprocess
import time
import threading
import re

TERMINAL_CLASSES = {
    "gnome-terminal", "org.gnome.terminal", "konsole", "xterm", "alacritty",
    "kitty", "wezterm", "foot", "terminator", "tilix", "x-terminal-emulator",
    "com.gex.terminal", "io.elementary.terminal", "xfce4-terminal",
    "mate-terminal", "urxvt", "st", "hyper", "deepin-terminal",
}

def _is_wayland() -> bool:
    t = os.environ.get("XDG_SESSION_TYPE")
    if t:
        return t == "wayland"
    return bool(os.environ.get("WAYLAND_DISPLAY"))

def _is_x11() -> bool:
    t = os.environ.get("XDG_SESSION_TYPE")
    if t:
        return t == "x11"
    return bool(os.environ.get("DISPLAY") and not _is_wayland())

def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, **kw)
    except Exception:
        return None

# ---- Focused window detection ----

def _get_active_window_class_x11() -> str | None:
    if shutil.which("xdotool"):
        for args in [
            ["xdotool", "getactivewindow", "getwindowclassname"],
            ["xdotool", "getactivewindow", "getwindowname"],
        ]:
            r = _run(args)
            if r and r.returncode == 0:
                out = r.stdout.decode("utf-8", errors="ignore").strip()
                if out:
                    return out
        if shutil.which("xprop"):
            try:
                r = _run(["xdotool", "getactivewindow"])
                if r and r.returncode == 0:
                    wid = r.stdout.decode().strip()
                    r2 = _run(["xprop", "-id", wid, "WM_CLASS"])
                    if r2 and r2.returncode == 0:
                        out = r2.stdout.decode()
                        m = re.search(r'"([^"]+)"', out)
                        if m:
                            return m.group(1)
            except Exception:
                pass
    return None

def _get_active_window_class_wayland() -> str | None:
    if shutil.which("gdbus"):
        try:
            r = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                      "--object-path", "/org/gnome/Shell",
                      "--method", "org.gnome.Shell.Eval",
                      "global.display.focus_window ? global.display.focus_window.wm_class : 'null'"])
            if r and r.returncode == 0:
                out = r.stdout.decode("utf-8", errors="ignore")
                m = re.search(r'"([^"]+)"', out)
                if m:
                    val = m.group(1)
                    if val.lower() != "null":
                        return val
        except Exception:
            pass
    if shutil.which("swaymsg"):
        try:
            r = _run(["swaymsg", "-t", "get_tree"])
            if r and r.returncode == 0:
                import json
                tree = json.loads(r.stdout.decode("utf-8", errors="ignore"))
                def find_focused(node):
                    if node.get("focused"):
                        return node
                    for n in node.get("nodes", []) + node.get("floating_nodes", []):
                        res = find_focused(n)
                        if res:
                            return res
                    return None
                f = find_focused(tree)
                if f:
                    app = f.get("app_id") or f.get("window_properties", {}).get("class") or f.get("name")
                    if app:
                        return str(app)
        except Exception:
            pass
    if shutil.which("hyprctl"):
        try:
            r = _run(["hyprctl", "activewindow", "-j"])
            if r and r.returncode == 0:
                import json
                j = json.loads(r.stdout.decode("utf-8", errors="ignore"))
                cls = j.get("class") or j.get("initialClass") or j.get("title")
                if cls:
                    return str(cls)
        except Exception:
            pass
    return None

def _get_active_window_class() -> str | None:
    cls = None
    if not _is_wayland():
        cls = _get_active_window_class_x11()
        if cls:
            return cls
    cls = _get_active_window_class_wayland()
    if cls:
        return cls
    if _is_wayland():
        cls = _get_active_window_class_x11()
    return cls

def _is_terminal_focused() -> bool | None:
    cls = _get_active_window_class()
    if not cls:
        return None
    low = cls.lower()
    for term in TERMINAL_CLASSES:
        if term in low:
            return True
    if "terminal" in low and "chrome" not in low:
        return True
    return False

def _get_config_paste_mode() -> str:
    try:
        from .config import load_config
        cfg = load_config()
        mode = str(cfg.get("paste_mode", "auto") or "auto").strip().lower()
        if mode in ("auto", "gui", "terminal", "primary", "type"):
            return mode
        return "auto"
    except Exception:
        return "auto"

def _decide_paste_mode() -> str:
    mode = _get_config_paste_mode()
    if mode != "auto":
        return mode
    term = _is_terminal_focused()
    if term is True:
        return "terminal"
    if term is False:
        return "gui"
    return "gui"

# ---- Clipboard helpers ----

def _get_clipboard() -> str | None:
    if shutil.which("wl-paste") and _is_wayland():
        r = _run(["wl-paste", "-n"])
        if r and r.returncode == 0:
            return r.stdout.decode("utf-8", errors="ignore")
    if shutil.which("xclip"):
        r = _run(["xclip", "-o", "-selection", "clipboard"])
        if r and r.returncode == 0:
            return r.stdout.decode("utf-8", errors="ignore")
    if shutil.which("xsel"):
        r = _run(["xsel", "-b", "-o"])
        if r and r.returncode == 0:
            return r.stdout.decode("utf-8", errors="ignore")
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, Gdk
        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        text = clip.wait_for_text()
        return text
    except Exception:
        return None

def _popen_communicate(cmd, text: str, timeout=3) -> bool:
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            p.communicate(input=text.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass
            return False
        return p.returncode == 0
    except Exception:
        return False

def _set_clipboard(text: str) -> bool:
    ok = False
    if shutil.which("wl-copy"):
        if _popen_communicate(["wl-copy"], text, timeout=3):
            ok = True
        _popen_communicate(["wl-copy", "--primary"], text, timeout=2)
    if shutil.which("xclip"):
        if _popen_communicate(["xclip", "-i", "-selection", "clipboard"], text, timeout=3):
            ok = True
        _popen_communicate(["xclip", "-i", "-selection", "primary"], text, timeout=2)
    if shutil.which("xsel"):
        if _popen_communicate(["xsel", "-b", "-i"], text, timeout=3):
            ok = True
        _popen_communicate(["xsel", "-p", "-i"], text, timeout=2)
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, Gdk
        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clip.set_text(text, -1)
        clip.store()
        try:
            prim = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
            prim.set_text(text, -1)
            prim.store()
        except Exception:
            pass
        ok = True
    except Exception:
        pass
    return ok

# ---- Single-shot paste per backend ----

def _paste_via_wtype(mode: str) -> bool:
    if not shutil.which("wtype"):
        return False
    if mode == "terminal":
        for cmd in [
            ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"],
        ]:
            r = _run(cmd)
            if r and r.returncode == 0:
                return True
        r = _run(["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"])
        return bool(r and r.returncode == 0)
    elif mode == "primary":
        r = _run(["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"])
        return bool(r and r.returncode == 0)
    else:
        r = _run(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"])
        return bool(r and r.returncode == 0)

def _paste_via_xdotool(mode: str) -> bool:
    if not shutil.which("xdotool"):
        return False
    if mode == "terminal":
        r = _run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"])
        if r and r.returncode == 0:
            return True
        r2 = _run(["xdotool", "key", "--clearmodifiers", "Shift+Insert"])
        return bool(r2 and r2.returncode == 0)
    elif mode == "primary":
        r = _run(["xdotool", "key", "--clearmodifiers", "Shift+Insert"])
        return bool(r and r.returncode == 0)
    else:
        r = _run(["xdotool", "key", "--clearmodifiers", "ctrl+v"])
        return bool(r and r.returncode == 0)

def _paste_via_ydotool(mode: str) -> bool:
    if not shutil.which("ydotool"):
        return False
    if mode == "terminal":
        r = _run(["ydotool", "key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"])
        return bool(r and r.returncode == 0)
    elif mode == "primary":
        r = _run(["ydotool", "key", "42:1", "110:1", "110:0", "42:0"])
        return bool(r and r.returncode == 0)
    else:
        r = _run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"])
        return bool(r and r.returncode == 0)

def _paste_via_pynput(mode: str) -> bool:
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        if mode == "terminal":
            with kb.pressed(Key.ctrl):
                with kb.pressed(Key.shift):
                    kb.press("v")
                    kb.release("v")
            return True
        elif mode == "primary":
            with kb.pressed(Key.shift):
                kb.press(Key.insert)
                kb.release(Key.insert)
            return True
        else:
            with kb.pressed(Key.ctrl):
                kb.press("v")
                kb.release("v")
            return True
    except Exception:
        return False

def _simulate_paste() -> bool:
    mode = _decide_paste_mode()
    is_wl = _is_wayland()
    if is_wl and shutil.which("wtype"):
        if _paste_via_wtype(mode):
            return True
    if shutil.which("xdotool"):
        if _paste_via_xdotool(mode):
            return True
    if shutil.which("ydotool"):
        if _paste_via_ydotool(mode):
            return True
    if _paste_via_pynput(mode):
        return True
    return False

def _type_direct(text: str) -> bool:
    if shutil.which("wtype") and _is_wayland():
        try:
            r = _run(["wtype", "--", text[:2000]])
            if r and r.returncode == 0:
                return True
        except Exception:
            pass
    if shutil.which("xdotool"):
        try:
            r = _run(["xdotool", "type", "--clearmodifiers", "--", text[:2000]])
            if r and r.returncode == 0:
                return True
        except Exception:
            pass
    try:
        from pynput.keyboard import Controller
        kb = Controller()
        kb.type(text[:2000])
        return True
    except Exception:
        return False
    return False

def inject_text(text: str, restore_clipboard_delay=0.6, restore_old=False) -> bool:
    if not text:
        return False
    text = text.strip()
    if not text:
        return False
    try:
        from .config import load_config as _lc
        if not _lc().get("auto_paste", True):
            return _set_clipboard(text)
    except Exception:
        pass
    ok_clip = _set_clipboard(text)
    time.sleep(0.18)
    pasted = False
    if ok_clip:
        pasted = _simulate_paste()
    if not pasted and len(text) < 500:
        pasted = _type_direct(text)
    if restore_old:
        def _restore():
            time.sleep(restore_clipboard_delay if restore_clipboard_delay >= 5 else 30)
            try:
                cur = _get_clipboard()
                if cur == text:
                    pass
            except Exception:
                pass
        threading.Thread(target=_restore, daemon=True).start()
    if ok_clip and not pasted:
        try:
            if shutil.which("notify-send"):
                snippet = text[:60] + ("…" if len(text) > 60 else "")
                body = snippet
                mode = _get_config_paste_mode()
                if mode == "auto" and _is_terminal_focused() is None and _is_wayland():
                    body = snippet + "  (if terminal, set: wisprflow config --paste-mode terminal)"
                subprocess.Popen(["notify-send", "-u", "low", "-t", "2500", "Wispr: copied to clipboard", body],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return bool(ok_clip)
