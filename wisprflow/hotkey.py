"""Platform-specific global hotkey helpers."""
import os
import threading
import time


def _x11_session() -> bool:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type:
        return session_type == "x11"
    return bool(os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))


def parse_x11_hotkey(hotkey: str):
    """Return ``(keysym_name, modifier_mask)`` for a grabbable shortcut.

    X11 passive grabs require one non-modifier key. Modifier-only shortcuts
    continue to use the existing listener implementation.
    """
    from Xlib import X

    modifier_aliases = {
        "ctrl": ("ctrl", X.ControlMask),
        "control": ("ctrl", X.ControlMask),
        "ctrll": ("ctrl", X.ControlMask),
        "ctrlr": ("ctrl", X.ControlMask),
        "lctrl": ("ctrl", X.ControlMask),
        "rctrl": ("ctrl", X.ControlMask),
        "shift": ("shift", X.ShiftMask),
        "shiftl": ("shift", X.ShiftMask),
        "shiftr": ("shift", X.ShiftMask),
        "lshift": ("shift", X.ShiftMask),
        "rshift": ("shift", X.ShiftMask),
        "alt": ("alt", X.Mod1Mask),
        "altl": ("alt", X.Mod1Mask),
        "altr": ("alt", X.Mod1Mask),
        "lalt": ("alt", X.Mod1Mask),
        "ralt": ("alt", X.Mod1Mask),
        "super": ("super", X.Mod4Mask),
        "win": ("super", X.Mod4Mask),
        "meta": ("super", X.Mod4Mask),
        "cmd": ("super", X.Mod4Mask),
    }
    key_aliases = {
        "space": "space",
        "enter": "Return",
        "return": "Return",
        "tab": "Tab",
        "esc": "Escape",
        "escape": "Escape",
    }

    modifiers = 0
    modifier_names = set()
    key_names = []
    for raw_part in hotkey.strip().lower().split("+"):
        part = raw_part.strip()
        compact = part.replace(" ", "").replace("_", "").replace("-", "")
        if not compact:
            continue
        modifier = modifier_aliases.get(compact)
        if modifier:
            name, mask = modifier
            modifier_names.add(name)
            modifiers |= mask
            continue
        if len(compact) == 1:
            key_names.append(compact)
        elif compact.startswith("f") and compact[1:].isdigit():
            key_names.append(compact.upper())
        elif compact in key_aliases:
            key_names.append(key_aliases[compact])
        else:
            return None

    if len(key_names) != 1:
        return None
    return key_names[0], modifiers


class X11GrabHotkey:
    """Consume a global X11 shortcut using a passive root-window grab."""

    def __init__(self, hotkey: str, callback):
        self.hotkey = hotkey
        self.callback = callback
        self._display = None
        self._root = None
        self._keycode = None
        self._modifier_mask = 0
        self._grab_masks = []
        self._stop_event = threading.Event()
        self._thread = None
        self._pressed = False

    def start(self) -> bool:
        if not _x11_session():
            return False
        parsed = parse_x11_hotkey(self.hotkey)
        if parsed is None:
            return False

        try:
            from Xlib import X, XK, display

            keysym_name, self._modifier_mask = parsed
            self._display = display.Display()
            self._root = self._display.screen().root
            keysym = XK.string_to_keysym(keysym_name)
            self._keycode = self._display.keysym_to_keycode(keysym)
            if not keysym or not self._keycode:
                self._cleanup()
                return False

            # Caps Lock and Num Lock must not disable the shortcut.
            ignored_masks = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
            self._grab_masks = [self._modifier_mask | mask for mask in ignored_masks]
            for mask in self._grab_masks:
                self._root.grab_key(
                    self._keycode,
                    mask,
                    False,
                    X.GrabModeAsync,
                    X.GrabModeAsync,
                )
            self._display.sync()
        except Exception:
            self._cleanup()
            return False

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        from Xlib import X

        relevant_modifiers = X.ControlMask | X.ShiftMask | X.Mod1Mask | X.Mod4Mask
        try:
            while not self._stop_event.is_set():
                while self._display.pending_events():
                    event = self._display.next_event()
                    if event.detail != self._keycode:
                        continue
                    if event.type == X.KeyRelease:
                        self._pressed = False
                    elif (
                        event.type == X.KeyPress
                        and not self._pressed
                        and event.state & relevant_modifiers == self._modifier_mask
                    ):
                        self._pressed = True
                        self.callback()
                time.sleep(0.01)
        finally:
            self._cleanup()

    def _cleanup(self):
        display_obj = self._display
        root = self._root
        keycode = self._keycode
        grab_masks = list(self._grab_masks)
        self._display = None
        self._root = None
        self._grab_masks = []
        if display_obj is None:
            return
        if root is not None and keycode:
            for mask in grab_masks:
                try:
                    root.ungrab_key(keycode, mask)
                except Exception:
                    pass
            try:
                display_obj.sync()
            except Exception:
                pass
        try:
            display_obj.close()
        except Exception:
            pass

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
