"""
Floating pill overlay for wisprflow - persistent, clickable, real-time status.
States: idle (click to talk) / listening (timer) / transcribing / done / error
Click pill to toggle. Draggable. Always visible when enabled.
"""
import time
import threading
import subprocess
import shutil
from pathlib import Path
import json

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk, Gdk, GLib, Pango
    HAS_GTK = True
except Exception:
    HAS_GTK = False

POSITION_FILE = Path.home() / ".config" / "wisprflow" / "pill_position.json"


def _configure_text_rendering(settings):
    """Use grayscale AA on the pill's RGBA surface.

    LCD subpixel rendering assumes an opaque background. On a transparent,
    rounded window it produces visible red/blue fringes around glyphs.
    """
    for name, value in (
        ("gtk-xft-antialias", 1),
        ("gtk-xft-hinting", 1),
        ("gtk-xft-hintstyle", "hintslight"),
        ("gtk-xft-rgba", "none"),
    ):
        try:
            settings.set_property(name, value)
        except Exception:
            pass

class OverlayFallback:
    def __init__(self, enabled=True, toggle_cb=None, hotkey_reload_callback=None):
        self.enabled = enabled
        self._hotkey_cb = hotkey_reload_callback
    def set_toggle_callback(self, cb): pass
    def set_hotkey_reload_callback(self, cb): self._hotkey_cb = cb
    def _show_hotkey_recorder(self): _notify("Wispr", "Hotkey recorder needs GUI — use `wisprflow config --hotkey ctrl+shift`", "critical")
    def show_idle(self): 
        if self.enabled:
            _notify("Wispr — Ready", "Click pill or press Ctrl+Shift to talk")
    def show_listening(self, seconds=0): 
        if self.enabled:
            _notify("Wispr — Listening…", "Click again to stop")
    def show_transcribing(self): 
        if self.enabled:
            _notify("Wispr — Transcribing…", "Sending to OpenRouter")
    def show_done(self, text=""):
        if self.enabled:
            snippet = text[:80] + ("…" if len(text) > 80 else "")
            _notify("Wispr — Done", snippet)
    def show_error(self, msg): _notify("Wispr — Error", msg, "critical")
    def hide(self): pass
    def stop(self): pass

def _notify(title, body, urgency="normal"):
    try:
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-u", urgency, "-t", "2200", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _load_position():
    try:
        if POSITION_FILE.exists():
            d = json.loads(POSITION_FILE.read_text())
            return d.get("x"), d.get("y")
    except Exception:
        pass
    return None, None

def _save_position(x, y):
    try:
        POSITION_FILE.parent.mkdir(parents=True, exist_ok=True)
        POSITION_FILE.write_text(json.dumps({"x": x, "y": y}))
    except Exception:
        pass

class Overlay:
    """Persistent floating pill. Click to toggle."""
    def __init__(self, enabled=True, toggle_callback=None, hotkey_reload_callback=None):
        if HAS_GTK:
            try:
                _configure_text_rendering(Gtk.Settings.get_default())
            except Exception:
                pass
        self.enabled = enabled
        self._toggle_cb = toggle_callback
        self._hotkey_reload_cb = hotkey_reload_callback
        self._pending_hotkey = None
        self._hotkey_dialog = None
        self._window = None
        self._label = None
        self._sub = None
        self._dot = None
        self._dot_holder = None
        self._spinner = None
        self._icon = None
        self._state = "idle"
        self._start_time = None
        self._tick_id = None
        self._hide_done_id = None
        self._drag_x = 0
        self._drag_y = 0
        self._dragging = False
        if not HAS_GTK or not enabled:
            self._fallback = OverlayFallback(enabled=enabled)
        else:
            self._fallback = None
            # Defer window creation to the Gtk thread. GLib repeats idle
            # callbacks while they return True, so always use a one-shot wrapper.
            GLib.idle_add(self._deferred_ensure_window)

    def _deferred_ensure_window(self):
        self._ensure_window()
        return False

    def set_toggle_callback(self, cb):
        self._toggle_cb = cb

    def set_hotkey_reload_callback(self, cb):
        self._hotkey_reload_cb = cb

    def _ensure_window(self):
        if not HAS_GTK or not self.enabled:
            return False
        if self._window is not None:
            return True
        try:
            win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            win.set_decorated(False)
            win.set_app_paintable(False)
            win.set_keep_above(True)
            win.set_skip_taskbar_hint(True)
            win.set_skip_pager_hint(True)
            win.set_accept_focus(False)
            win.set_focus_on_map(False)
            win.set_type_hint(Gdk.WindowTypeHint.DOCK)
            win.set_resizable(False)
            win.set_title("WisprPill")
            win.set_opacity(1.0)
            # Make it dock-like: not in pager, keep above
            win.set_name("wispr-pill")

            # Paint only the outer window. Descendant boxes stay transparent;
            # painting every GtkBox produced overlapping, aliased rounded layers.
            css = b"""
            #wispr-pill {
                background-image: none;
                background-color: rgba(8, 8, 10, 0.98);
                border-radius: 24px;
                border: 1px solid rgba(255, 255, 255, 0.16);
            }
            #wispr-pill:hover {
                background-image: none;
                background-color: rgba(20, 20, 23, 0.98);
                border-color: rgba(255, 255, 255, 0.24);
            }
            #wispr-pill box, #wispr-pill eventbox {
                background-image: none;
                background-color: transparent;
            }
            #dot-idle { color: #8e8e93; font-size: 12px; }
            #dot-listening { color: #ff3b30; font-size: 12px; }
            #dot-transcribing { color: #0a84ff; font-size: 12px; }
            #dot-done { color: #30d158; font-size: 12px; }
            #dot-error { color: #ff453a; font-size: 12px; }
            #label {
                color: #f5f5f7;
                font-size: 13px;
                font-weight: 600;
                font-family: "Ubuntu Sans", "Noto Sans", sans-serif;
            }
            #sub {
                color: rgba(235,235,245,0.60);
                font-size: 11px;
                font-weight: 400;
                font-family: "Ubuntu Sans", "Noto Sans", sans-serif;
            }
            #mic {
                color: rgba(235,235,245,0.72);
                font-size: 13px;
            }
            """
            provider = Gtk.CssProvider()
            provider.load_from_data(css)
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            # For solid black pill, keep rounded corners but interior opaque
            # Use RGBA visual for anti-aliased rounded corners (corners transparent), interior #000 opaque
            try:
                visual = win.get_screen().get_rgba_visual()
                if visual:
                    win.set_visual(visual)
            except Exception:
                pass

            # EventBox to capture clicks and drag
            ebox = Gtk.EventBox()
            ebox.set_visible_window(False)
            ebox.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
            ebox.connect("button-press-event", self._on_button_press)
            ebox.connect("button-release-event", self._on_button_release)
            ebox.connect("motion-notify-event", self._on_motion)

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(14)
            box.set_margin_end(14)
            box.set_valign(Gtk.Align.CENTER)

            # Dot
            dot = Gtk.Label(label="●")
            dot.set_name("dot-idle")
            dot.set_size_request(10, 10)
            dot.set_xalign(0.5)
            dot.set_yalign(0.5)
            dot_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            dot_holder.set_valign(Gtk.Align.CENTER)
            dot_holder.add(dot)

            # Labels
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            lbl = Gtk.Label(label="Click to speak")
            lbl.set_name("label")
            lbl.set_halign(Gtk.Align.START)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(28)
            sub = Gtk.Label(label="Ctrl+Shift  •  Drag to move")
            sub.set_name("sub")
            sub.set_halign(Gtk.Align.START)
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.set_max_width_chars(32)
            vbox.add(lbl)
            vbox.add(sub)

            spinner = Gtk.Spinner()
            spinner.set_size_request(16, 16)

            box.add(dot_holder)
            box.add(vbox)
            box.add(spinner)

            ebox.add(box)
            win.add(ebox)

            # Tooltip
            win.set_tooltip_text("Click to start/stop  •  Drag to move  •  Right-click for menu")

            # Right-click menu
            ebox.connect("button-press-event", self._on_button_press_menu)

            win.connect("realize", self._on_realize)
            win.show_all()
            spinner.hide()
            # Start in idle
            self._window = win
            self._ebox = ebox
            self._label = lbl
            self._sub = sub
            self._dot = dot
            self._dot_holder = dot_holder
            self._spinner = spinner
            self._box = box
            self._state = "idle"
            self._update_idle_ui()
            # Place window
            self._place_window()
            return True
        except Exception as e:
            print(f"[wispr] pill create failed: {e}")
            import traceback; traceback.print_exc()
            self._fallback = OverlayFallback(enabled=self.enabled)
            return False

    def _on_realize(self, win):
        # After realize, place correctly
        self._place_window()
        # Make window click-through handling: ensure input shape
        try:
            win.get_window().set_cursor(Gdk.Cursor.new_from_name(win.get_display(), "pointer"))
        except Exception:
            pass

    def _place_window(self):
        if not self._window:
            return
        try:
            # Try saved position first
            sx, sy = _load_position()
            if sx is not None and sy is not None:
                # Validate saved position is on-screen (bottom area)
                screen = Gdk.Screen.get_default()
                if screen:
                    sw = screen.get_width()
                    sh = screen.get_height()
                    if 0 <= sx <= sw - 100 and 0 <= sy <= sh - 40:
                        self._window.move(int(sx), int(sy))
                        return
                    else:
                        # Invalid saved pos (e.g., top bar), ignore
                        try:
                            POSITION_FILE.unlink()
                        except Exception:
                            pass
            screen = Gdk.Screen.get_default()
            if screen is None:
                return
            # Use raw screen size for reliable bottom-center (avoid workarea top bar offset)
            w = screen.get_width()
            h = screen.get_height()
            # Get window size
            win_w, win_h = self._window.get_size()
            if win_w < 50:
                win_w = 284
            if win_h < 20:
                win_h = 92
            # Bottom-center, 72px above bottom (above dock/taskbar)
            x = (w - win_w) // 2
            y = h - win_h - 80
            # Clamp
            x = max(12, min(x, w - win_w - 12))
            y = max(12, min(y, h - win_h - 12))
            self._window.move(x, y)
        except Exception as e:
            print(f"[wispr] pill place failed: {e}")

    def _on_button_press(self, widget, event):
        if event.button == 1:
            # Start drag
            self._dragging = False
            win_x, win_y = self._window.get_position()
            self._drag_x = event.x_root - win_x
            self._drag_y = event.y_root - win_y
            # We'll detect drag vs click in motion/release
            return False
        return False

    def _on_motion(self, widget, event):
        if event.state & Gdk.ModifierType.BUTTON1_MASK:
            # Dragging
            if abs(event.x_root - self._drag_x - self._window.get_position()[0]) > 3 or abs(event.y_root - self._drag_y - self._window.get_position()[1]) > 3:
                self._dragging = True
            new_x = int(event.x_root - self._drag_x)
            new_y = int(event.y_root - self._drag_y)
            self._window.move(new_x, new_y)
            return True
        return False

    def _on_button_release(self, widget, event):
        if event.button == 1:
            if self._dragging:
                # End drag, save position
                self._dragging = False
                x, y = self._window.get_position()
                _save_position(x, y)
                return True
            else:
                # Click -> toggle
                if self._toggle_cb:
                    try:
                        # Run toggle in thread to not block Gtk
                        threading.Thread(target=self._toggle_cb, daemon=True).start()
                    except Exception as e:
                        print(f"[wispr] pill toggle failed: {e}")
                else:
                    # Fallback: try socket toggle
                    try:
                        import socket
                        from pathlib import Path
                        p = Path.home() / ".cache" / "wisprflow" / "daemon.sock"
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.settimeout(1)
                        s.connect(str(p))
                        s.sendall(b"toggle\n")
                        s.recv(1024)
                        s.close()
                    except Exception:
                        pass
                return True
        return False

    def _on_button_press_menu(self, widget, event):
        if event.button == 3:
            # Right-click menu: show options
            menu = Gtk.Menu()
            # Status
            mi_status = Gtk.MenuItem(label=f"Status: {self._state.title()} — {self._get_model_name()}")
            mi_status.set_sensitive(False)
            menu.append(mi_status)
            menu.append(Gtk.SeparatorMenuItem())
            # Change hotkey — record your own combo
            _, pretty = self._get_current_hotkey_display()
            mi_hotkey = Gtk.MenuItem(label=f"Change hotkey…  (now {pretty})")
            def do_hotkey(_):
                self._show_hotkey_recorder()
            mi_hotkey.connect("activate", do_hotkey)
            menu.append(mi_hotkey)
            menu.append(Gtk.SeparatorMenuItem())
            # Hide pill
            mi_hide = Gtk.MenuItem(label="Hide pill (show with tray)")
            def do_hide(_):
                self.hide()
            mi_hide.connect("activate", do_hide)
            menu.append(mi_hide)
            # Quit
            mi_quit = Gtk.MenuItem(label="Quit WisprFlow")
            def do_quit(_):
                try:
                    if self._toggle_cb:
                        # daemon will handle quit via tray; this just hides
                        pass
                    Gtk.main_quit()
                except Exception:
                    pass
                import os, signal
                try:
                    # Try to stop daemon via socket
                    import socket
                    from pathlib import Path
                    p = Path.home() / ".cache" / "wisprflow" / "daemon.sock"
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect(str(p))
                    s.sendall(b"stop\n")
                    s.close()
                except Exception:
                    pass
            mi_quit.connect("activate", do_quit)
            menu.append(mi_quit)
            menu.show_all()
            menu.popup(None, None, None, None, event.button, event.time)
            return True
        return False

    def _get_model_name(self):
        try:
            from .config import load_config
            m = load_config().get("model", "")
            # shorten openai/gpt-4o-transcribe -> gpt-4o
            if "/" in m:
                m = m.split("/")[-1]
            return m
        except Exception:
            return ""

    def _get_current_hotkey_display(self):
        try:
            from .config import load_config
            hk = (load_config().get("hotkey") or "ctrl+shift").strip()
            # pretty: ctrl+shift -> Ctrl+Shift, f9 -> F9
            parts = hk.split("+")
            pretty = "+".join(p.capitalize() if p.lower().startswith("f") else p.capitalize() for p in parts)
            # special: ctrl -> Ctrl
            pretty = pretty.replace("Ctrl", "Ctrl").replace("Shift", "Shift").replace("Alt", "Alt").replace("Super", "Super")
            return hk, pretty
        except Exception:
            return "ctrl+shift", "Ctrl+Shift"

    def _gtk_event_to_hotkey(self, event):
        """Convert Gdk key-press event to config hotkey string like 'ctrl+shift', 'f9', 'ctrl+alt+space'."""
        try:
            mods = []
            state = event.state
            if state & Gdk.ModifierType.CONTROL_MASK:
                mods.append("ctrl")
            if state & Gdk.ModifierType.SHIFT_MASK:
                mods.append("shift")
            if state & Gdk.ModifierType.MOD1_MASK:
                mods.append("alt")
            if state & Gdk.ModifierType.SUPER_MASK:
                mods.append("super")
            # also handle LOCK? ignore
            keyval = event.keyval
            keyname = Gdk.keyval_name(keyval)
            if not keyname:
                return None
            low = keyname.lower()
            # ignore pure modifier presses unless they complete a mod-only combo like ctrl+shift
            modifier_keys = {"control_l", "control_r", "ctrl_l", "ctrl_r", "shift_l", "shift_r", "alt_l", "alt_r", "super_l", "super_r", "meta_l", "meta_r", "caps_lock", "iso_level3_shift", "num_lock", "scroll_lock"}
            # map modifier keynames to their mod string
            mod_key_map = {
                "control_l": "ctrl", "control_r": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
                "shift_l": "shift", "shift_r": "shift",
                "alt_l": "alt", "alt_r": "alt",
                "super_l": "super", "super_r": "super", "meta_l": "super", "meta_r": "super",
            }
            if low in modifier_keys:
                # include the modifier being pressed itself
                cur_mods = list(mods)
                add = mod_key_map.get(low)
                if add and add not in cur_mods:
                    cur_mods.append(add)
                # mod-only combo: if at least 2 distinct mods are held, treat as hotkey like ctrl+shift
                uniq = []
                for m in ["ctrl", "shift", "alt", "super"]:
                    if m in cur_mods and m not in uniq:
                        uniq.append(m)
                if len(uniq) >= 2:
                    return "+".join(uniq)
                return None
            # normal key: map Gdk names to config names
            # Gdk returns "F9" -> "f9", "space" -> "space", "a" -> "a", "Return" -> "enter" etc
            mapping = {
                "return": "enter",
                "escape": "esc",
                "control_l": "ctrl",
                "control_r": "ctrl",
                "shift_l": "shift",
                "shift_r": "shift",
                "alt_l": "alt",
                "alt_r": "alt",
            }
            main = mapping.get(low, low)
            # For letters, Gdk may give uppercase when Shift held; normalize to lower and avoid duplicate shift
            # If main is a single letter and shift is in mods, keep shift as modifier but use lower letter
            if len(main) == 1:
                main = main.lower()
            # Build final: mods (without duplicate of main) + main
            # avoid duplicating if main already in mods (e.g., main==shift already handled)
            if main in mods:
                return "+".join(mods)
            # ensure mods are unique and ordered ctrl,shift,alt,super then main
            ordered = []
            for m in ["ctrl", "shift", "alt", "super"]:
                if m in mods:
                    ordered.append(m)
            # add any other mods not in ordered (unlikely)
            for m in mods:
                if m not in ordered:
                    ordered.append(m)
            ordered.append(main)
            return "+".join(ordered)
        except Exception:
            return None

    def _hotkey_pretty(self, hk):
        try:
            parts = hk.split("+")
            pretty = "+".join(p.upper() if p.lower().startswith("f") and p[1:].isdigit() else p.capitalize() for p in parts)
            return pretty
        except Exception:
            return hk

    def _show_hotkey_recorder(self):
        if not HAS_GTK or not self.enabled or self._window is None:
            _notify("Wispr", "Hotkey recorder needs GUI — use `wisprflow config --hotkey ctrl+shift`", "critical")
            return
        # avoid duplicate dialogs
        if self._hotkey_dialog is not None:
            try:
                self._hotkey_dialog.present()
            except Exception:
                pass
            return
        try:
            cur_hk, cur_pretty = self._get_current_hotkey_display()
            dialog = Gtk.Dialog(title="Record Hotkey", transient_for=self._window, flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT)
            dialog.set_default_size(420, 200)
            dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
            dialog.set_keep_above(True)
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            save_btn = dialog.add_button("Save", Gtk.ResponseType.OK)
            save_btn.set_sensitive(False)
            # Content
            content = dialog.get_content_area()
            content.set_spacing(10)
            content.set_margin_top(16)
            content.set_margin_bottom(12)
            content.set_margin_start(16)
            content.set_margin_end(16)
            # Header
            hdr = Gtk.Label(label=f"Current: {cur_pretty}")
            hdr.set_halign(Gtk.Align.START)
            hdr.get_style_context().add_class("dim-label")
            content.add(hdr)
            instr = Gtk.Label(label="Press your new shortcut\n(e.g. F9, Ctrl+Shift, Ctrl+Alt+Space)")
            instr.set_halign(Gtk.Align.CENTER)
            instr.set_justify(Gtk.Justification.CENTER)
            content.add(instr)
            # Live preview
            self._pending_hotkey = None
            preview = Gtk.Label(label="—  waiting for input  —")
            preview.set_name("hotkey-preview")
            # inline CSS for preview
            try:
                css = b"#hotkey-preview { font-size: 22px; font-weight: 800; color: #0a84ff; padding: 14px; border: 1px solid #2a2a2a; border-radius: 12px; background: #111; }"
                prov = Gtk.CssProvider()
                prov.load_from_data(css)
                Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            except Exception:
                pass
            content.add(preview)
            hint = Gtk.Label(label="Tip: for Ctrl+Shift just hold both keys together  •  Esc to cancel  •  Enter to save")
            hint.set_halign(Gtk.Align.CENTER)
            hint.get_style_context().add_class("dim-label")
            hint.set_line_wrap(True)
            content.add(hint)
            error_lbl = Gtk.Label(label="")
            error_lbl.set_halign(Gtk.Align.CENTER)
            error_lbl.set_name("error")
            content.add(error_lbl)
            dialog.show_all()
            self._hotkey_dialog = dialog

            def on_key_press(widget, event):
                # Esc cancels
                if event.keyval == Gdk.KEY_Escape:
                    dialog.response(Gtk.ResponseType.CANCEL)
                    return True
                if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
                    if self._pending_hotkey:
                        dialog.response(Gtk.ResponseType.OK)
                    return True
                hk = self._gtk_event_to_hotkey(event)
                if hk:
                    # normalize to lower
                    hk = hk.lower().strip()
                    # validate: at least one non-empty part, allow f-keys single, or mod combos
                    if hk:
                        self._pending_hotkey = hk
                        pretty = self._hotkey_pretty(hk)
                        preview.set_text(pretty)
                        save_btn.set_sensitive(True)
                        error_lbl.set_text("")
                        # auto flash
                        preview.set_opacity(0.6)
                        GLib.timeout_add(90, lambda: (preview.set_opacity(1.0), False)[1])
                    return True
                return False

            # Grab focus and capture keys
            dialog.connect("key-press-event", on_key_press)
            dialog.set_events(dialog.get_events() | Gdk.EventMask.KEY_PRESS_MASK)
            # Ensure dialog gets focus
            dialog.present()
            dialog.grab_focus()

            def on_response(d, resp):
                pending = self._pending_hotkey
                # cleanup
                self._hotkey_dialog = None
                d.destroy()
                if resp == Gtk.ResponseType.OK and pending:
                    # save
                    try:
                        from .config import save_config
                        save_config({"hotkey": pending})
                        pretty = self._hotkey_pretty(pending)
                        # update pill UI immediately on Gtk thread
                        def _after_save():
                            self._update_idle_ui()
                            # queue redraw
                            try:
                                if self._dot:
                                    self._dot.queue_draw()
                            except Exception:
                                pass
                            return False
                        GLib.idle_add(_after_save)
                        # notify daemon to reload hotkey
                        if self._hotkey_reload_cb:
                            try:
                                # run in thread to not block Gtk
                                threading.Thread(target=lambda: self._hotkey_reload_cb(pending), daemon=True).start()
                            except Exception as e:
                                print(f"[wispr] hotkey reload cb failed: {e}")
                        else:
                            # fallback: try socket? daemon will reload on next toggle anyway
                            pass
                        _notify("Wispr — Hotkey updated", f"New hotkey: {pretty} — press it to toggle")
                        print(f"[wispr] hotkey changed via pill to {pending!r} ({pretty})")
                    except Exception as e:
                        _notify("Wispr — Error", f"Failed to save hotkey: {e}", "critical")
                        print(f"[wispr] save hotkey failed: {e}")
                else:
                    self._pending_hotkey = None
                return False

            dialog.connect("response", on_response)
            # Handle window close
            dialog.connect("delete-event", lambda w, e: (setattr(self, '_hotkey_dialog', None), False)[1])
        except Exception as e:
            print(f"[wispr] hotkey recorder failed: {e}")
            import traceback; traceback.print_exc()
            _notify("Wispr — Error", str(e)[:80], "critical")
            self._hotkey_dialog = None

    def _update_idle_ui(self):
        if self._dot:
            self._dot.set_name("dot-idle")
        if self._label:
            self._label.set_text("Click to speak")
        if self._sub:
            _, pretty = self._get_current_hotkey_display()
            self._sub.set_text(f"{pretty}  •  Drag to move")
            # update tooltip dynamically
            try:
                if self._window:
                    self._window.set_tooltip_text(f"Click to start/stop  •  Drag to move  •  Hotkey: {pretty}  •  Right-click for menu")
            except Exception:
                pass
        if self._spinner:
            self._spinner.hide()
            self._spinner.stop()
        if self._dot_holder:
            self._dot_holder.show()
        # queue redraw for CSS change
        try:
            self._dot.queue_draw()
        except Exception:
            pass

    # Public API - thread-safe via GLib.idle_add
    def show_idle(self):
        if not self.enabled:
            return
        if not HAS_GTK:
            if self._fallback:
                self._fallback.show_idle()
            return
        def _do():
            if not self._ensure_window():
                return False
            self._state = "idle"
            if self._tick_id:
                try:
                    if self._tick_id:
                        GLib.source_remove(self._tick_id)
                except Exception:
                    pass
                self._tick_id = None
            if self._hide_done_id:
                try:
                    if self._hide_done_id:
                        GLib.source_remove(self._hide_done_id)
                except Exception:
                    pass
                self._hide_done_id = None
            self._update_idle_ui()
            self._window.show_all()
            if self._spinner:
                self._spinner.hide()
            self._dot_holder.show()
            # Ensure window is visible (in case it was hidden)
            self._window.show()
            return False
        GLib.idle_add(_do)

    def show_listening(self, seconds=0):
        if not self.enabled:
            return
        if not HAS_GTK:
            if self._fallback:
                self._fallback.show_listening(seconds)
            return
        def _do():
            if not self._ensure_window():
                return False
            self._state = "listening"
            self._start_time = time.time()
            if self._hide_done_id:
                try:
                    if self._hide_done_id:
                        GLib.source_remove(self._hide_done_id)
                except Exception:
                    pass
                self._hide_done_id = None
            if self._dot:
                self._dot.set_name("dot-listening")
            if self._label:
                self._label.set_text(f"Listening…  {seconds:.1f}s")
            if self._sub:
                self._sub.set_text("Click to stop  •  ESC to cancel")
                self._sub.show()
            if self._spinner:
                self._spinner.hide()
                self._spinner.stop()
            if self._dot_holder:
                self._dot_holder.show()
            self._window.show_all()
            if self._spinner:
                self._spinner.hide()
            self._dot_holder.show()
            try:
                self._dot.queue_draw()
            except Exception:
                pass
            if self._tick_id:
                GLib.source_remove(self._tick_id)
            self._tick_id = GLib.timeout_add(100, self._tick_listening)
            return False
        GLib.idle_add(_do)

    def _tick_listening(self):
        if self._state != "listening":
            return False
        if self._label and self._start_time:
            elapsed = time.time() - self._start_time
            self._label.set_text(f"Listening…  {elapsed:.1f}s")
        return True

    def show_transcribing(self):
        if not self.enabled:
            return
        if not HAS_GTK:
            if self._fallback:
                self._fallback.show_transcribing()
            return
        def _do():
            if not self._ensure_window():
                return False
            self._state = "transcribing"
            if self._tick_id:
                try:
                    if self._tick_id:
                        GLib.source_remove(self._tick_id)
                except Exception:
                    pass
                self._tick_id = None
            if self._dot:
                self._dot.set_name("dot-transcribing")
            if self._label:
                self._label.set_text("Transcribing…")
            if self._sub:
                self._sub.set_text("Sending to OpenRouter")
            if self._dot_holder:
                self._dot_holder.show()
            if self._spinner:
                self._spinner.show()
                self._spinner.start()
            self._window.show_all()
            try:
                self._dot.queue_draw()
            except Exception:
                pass
            return False
        GLib.idle_add(_do)

    def show_done(self, text=""):
        if not self.enabled:
            return
        if not HAS_GTK:
            if self._fallback:
                self._fallback.show_done(text)
            return
        def _do():
            if not self._ensure_window():
                return False
            self._state = "done"
            if self._tick_id:
                try:
                    if self._tick_id:
                        GLib.source_remove(self._tick_id)
                except Exception:
                    pass
                self._tick_id = None
            if self._dot:
                self._dot.set_name("dot-done")
            snippet = text[:36] + ("…" if len(text) > 36 else "")
            if self._label:
                self._label.set_text("Done ✓")
            if self._sub:
                self._sub.set_text(snippet if snippet else "Pasted at cursor")
            if self._dot_holder:
                self._dot_holder.show()
            if self._spinner:
                self._spinner.stop()
                self._spinner.hide()
            self._window.show_all()
            try:
                self._dot.queue_draw()
            except Exception:
                pass
            # Auto-return to idle after 1.8s (pill stays visible)
            if self._hide_done_id:
                try:
                    GLib.source_remove(self._hide_done_id)
                except Exception:
                    pass
            self._hide_done_id = GLib.timeout_add(1800, lambda: (self.show_idle(), False)[1])
            return False
        GLib.idle_add(_do)

    def show_error(self, msg):
        if not HAS_GTK:
            if self._fallback:
                self._fallback.show_error(msg)
            return
        def _do():
            if not self._ensure_window():
                _notify("Wispr — Error", msg, "critical")
                return False
            self._state = "error"
            if self._tick_id:
                try:
                    if self._tick_id:
                        GLib.source_remove(self._tick_id)
                except Exception:
                    pass
                self._tick_id = None
            if self._dot:
                self._dot.set_name("dot-error")
            if self._label:
                self._label.set_text("Error")
            if self._sub:
                self._sub.set_text(msg[:40])
            if self._spinner:
                self._spinner.stop()
                self._spinner.hide()
            if self._dot_holder:
                self._dot_holder.show()
            self._window.show_all()
            try:
                self._dot.queue_draw()
            except Exception:
                pass
            _notify("Wispr — Error", msg, "critical")
            if self._hide_done_id:
                try:
                    GLib.source_remove(self._hide_done_id)
                except Exception:
                    pass
            self._hide_done_id = GLib.timeout_add(3500, lambda: (self.show_idle(), False)[1])
            return False
        GLib.idle_add(_do)

    def hide(self):
        if not HAS_GTK or not self.enabled:
            return
        def _do():
            if self._window:
                self._window.hide()
            self._state = "hidden"
            if self._tick_id:
                try:
                    if self._tick_id:
                        GLib.source_remove(self._tick_id)
                except Exception:
                    pass
                self._tick_id = None
            return False
        GLib.idle_add(_do)

    def stop(self):
        # Don't hide pill on stop, just return to idle then hide if needed
        self.hide()
