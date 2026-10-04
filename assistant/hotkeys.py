"""Global hotkeys.

Two mechanisms, because no single one works everywhere:

  Portal  - org.freedesktop.portal.GlobalShortcuts. KDE Plasma 6 and Hyprland provide a
            backend; Cinnamon and GNOME currently do not (probed, not assumed).
  Static  - bindings installed into the desktop's own keyboard settings by
            tools/install-hotkeys.sh, which invoke `run-assistant --action=NAME`. Works
            on every desktop including Wayland, which is why it is the documented path.

The X11 grabber below is for X11 sessions. X11 reports lock modifiers in the event
state, so a single grab for Ctrl+Shift+1 does not match when NumLock is on - every hotkey
would be silently dead with NumLock enabled. So it grabs the cross product of the lock
modifiers, waits on the connection fd rather than a busy loop, and ungrabs on shutdown.
"""

import threading

from gi.repository import GLib

ACTIONS = [
    ("translate_text", "Translate clipboard text", "<Control><Shift>1"),
    ("ocr_translate", "Translate image selection", "<Control><Shift>2"),
    ("query_text", "Query clipboard text", "<Control><Shift>3"),
    ("query_image", "Query image selection", "<Control><Shift>4"),
    ("ocr_only", "OCR only", "<Control><Shift>5"),
    ("look_up_word", "Look up word", "<Control><Shift>6"),
    ("search_encyclopedia", "Search Encyclopedia", "<Control><Shift>7"),
    ("ask_encyclopedia", "Ask Encyclopedia", "<Control><Shift>8"),
]


# Only for those with the encyclopedia DVD: see config.encyclopedia_enabled.
ENCYCLOPEDIA_ACTIONS = ("search_encyclopedia", "ask_encyclopedia")


def action_names():
    return [name for name, _, _ in ACTIONS]


def enabled_actions(config):
    """ACTIONS less the encyclopedia's while it is turned off, so that its keys are not
    taken from other programs by someone without the DVD."""
    on = bool(getattr(config, "encyclopedia_enabled", False))
    return [action for action in ACTIONS if on or action[0] not in ENCYCLOPEDIA_ACTIONS]


class X11Hotkeys:
    """python-xlib based grabber. X11 sessions only."""

    def __init__(self, dispatch, actions=ACTIONS):
        self.dispatch = dispatch
        self.actions = [name for name, _, _ in actions]
        self.display = None
        self.root = None
        self.running = False
        self.bindings = {}
        self._source_id = None

    @staticmethod
    def available():
        try:
            from Xlib import display  # noqa: F401
        except ImportError:
            return False
        return True

    def start(self):
        from Xlib import X, XK, display, error

        self.display = display.Display()
        self.root = self.display.screen().root

        # X11 puts the state of the lock modifiers into event.state, so a single grab
        # only matches when every lock happens to be off. Grab all 8 combinations.
        lock_masks = [0, X.LockMask, X.Mod2Mask, X.Mod5Mask,
                      X.LockMask | X.Mod2Mask, X.LockMask | X.Mod5Mask,
                      X.Mod2Mask | X.Mod5Mask,
                      X.LockMask | X.Mod2Mask | X.Mod5Mask]

        keysyms = {
            "translate_text": XK.XK_1, "ocr_translate": XK.XK_2,
            "query_text": XK.XK_3, "query_image": XK.XK_4, "ocr_only": XK.XK_5,
            "look_up_word": XK.XK_6, "search_encyclopedia": XK.XK_7,
            "ask_encyclopedia": XK.XK_8,
        }
        keysyms = {name: keysym for name, keysym in keysyms.items()
                   if name in self.actions}
        base = X.ControlMask | X.ShiftMask
        significant = X.ControlMask | X.ShiftMask | X.Mod1Mask | X.Mod4Mask

        self.display.set_error_handler(lambda *a: None)   # ignore BadAccess per-key
        grabbed = []
        for action, keysym in keysyms.items():
            keycode = self.display.keysym_to_keycode(keysym)
            if not keycode:
                continue
            for lock in lock_masks:
                try:
                    self.root.grab_key(keycode, base | lock, True,
                                       X.GrabModeAsync, X.GrabModeAsync)
                except error.XError:
                    continue
            self.bindings[keycode] = action
            grabbed.append(action)
        self.display.sync()

        self.running = True
        # Wake on the X connection fd rather than polling.
        self._source_id = GLib.io_add_watch(
            self.display.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_readable)
        return grabbed

    def _on_readable(self, _fd, _condition):
        from Xlib import X
        if not self.running:
            return False
        try:
            while self.display.pending_events():
                event = self.display.next_event()
                if event.type != X.KeyPress:
                    continue
                action = self.bindings.get(event.detail)
                # Compare only the modifiers we care about, masking the lock bits out.
                mods = event.state & (X.ControlMask | X.ShiftMask |
                                      X.Mod1Mask | X.Mod4Mask)
                if action and mods == (X.ControlMask | X.ShiftMask):
                    GLib.idle_add(self.dispatch, action)
        except Exception as e:
            print(f"[hotkeys] X11 event error: {e}")
        return True

    def stop(self):
        self.running = False
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
            self._source_id = None
        if self.display is not None:
            try:
                from Xlib import X
                for keycode in self.bindings:
                    self.root.ungrab_key(keycode, X.AnyModifier)
                self.display.sync()
                self.display.close()
            except Exception:
                pass
            self.display = None


class PortalHotkeys:
    """org.freedesktop.portal.GlobalShortcuts.

    Present on KDE Plasma 6 and Hyprland. The user is prompted once to approve the
    shortcuts, then the compositor delivers Activated signals.
    """

    # Restarting reuses the process id, and the portal will not export a second session
    # at a path it is still holding the first one on.
    _sessions = 0

    def __init__(self, dispatch, actions=ACTIONS):
        self.dispatch = dispatch
        self.actions = list(actions)
        self.bus = None
        self.session_handle = None
        self._subs = []

    @staticmethod
    def available():
        from . import session
        return session.portal_has("GlobalShortcuts")

    def start(self):
        from gi.repository import Gio
        import os

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        unique = self.bus.get_unique_name()[1:].replace(".", "_")
        PortalHotkeys._sessions += 1
        token = f"daassistant{os.getpid()}_{PortalHotkeys._sessions}"
        request_path = f"/org/freedesktop/portal/desktop/request/{unique}/{token}"

        def on_created(_c, _s, _p, _i, _sig, params):
            code, results = params.unpack()
            if code != 0:
                print(f"[hotkeys] GlobalShortcuts session refused (code {code})")
                return
            self.session_handle = results.get("session_handle")
            self._bind()

        self._subs.append(self.bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
            "Response", request_path, None, Gio.DBusSignalFlags.NONE, on_created))

        self._subs.append(self.bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.GlobalShortcuts", "Activated", None, None,
            Gio.DBusSignalFlags.NONE, self._on_activated))

        options = {
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", token),
        }
        self.bus.call_sync(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.GlobalShortcuts", "CreateSession",
            GLib.Variant("(a{sv})", (options,)), GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE, -1, None)
        return True

    def _bind(self):
        from gi.repository import Gio
        shortcuts = [
            (name, {"description": GLib.Variant("s", label),
                    "preferred_trigger": GLib.Variant("s", trigger.replace("<Control>", "CTRL+")
                                                      .replace("<Shift>", "SHIFT+"))})
            for name, label, trigger in self.actions
        ]
        self.bus.call(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.GlobalShortcuts", "BindShortcuts",
            GLib.Variant("(oa(sa{sv})sa{sv})",
                         (self.session_handle, shortcuts, "", {})),
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None, None)

    def _on_activated(self, _c, _s, _p, _i, _sig, params):
        try:
            _session, shortcut_id, _ts, _opts = params.unpack()
        except ValueError:
            return
        if shortcut_id in (name for name, _, _ in self.actions):
            GLib.idle_add(self.dispatch, shortcut_id)

    def stop(self):
        from gi.repository import Gio
        if self.bus is not None:
            for sub in self._subs:
                self.bus.signal_unsubscribe(sub)
            self._subs.clear()
            if self.session_handle:
                # Otherwise the compositor keeps the shortcuts bound to a session
                # nobody is listening to.
                self.bus.call(
                    "org.freedesktop.portal.Desktop", self.session_handle,
                    "org.freedesktop.portal.Session", "Close", None, None,
                    Gio.DBusCallFlags.NONE, -1, None, None)
                self.session_handle = None


class HotkeyManager:
    """Chooses a mechanism based on config.hotkey_mode and what is actually available."""

    def __init__(self, config, dispatch):
        self.config = config
        self.dispatch = dispatch
        self.impl = None
        self.mode = "off"
        self.detail = ""

    def start(self):
        from . import session

        # start() runs again when the setting changes, so nothing may be left over from
        # last time: "off" used to go on reporting the x11 grabs it had just released.
        self.mode = "off"
        self.detail = ""

        wanted = (self.config.hotkey_mode or "auto").lower()
        if wanted == "off":
            self.detail = "disabled in settings"
            return
        if wanted == "static":
            # Bindings live in the desktop's keyboard settings and reach us over D-Bus;
            # grabbing here as well would double-fire every shortcut.
            self.mode = "static"
            self.detail = "desktop keybindings -> run-assistant --action=..."
            return

        if wanted in ("auto", "portal") and PortalHotkeys.available():
            try:
                self.impl = PortalHotkeys(self.dispatch, enabled_actions(self.config))
                self.impl.start()
                self.mode = "portal"
                self.detail = "org.freedesktop.portal.GlobalShortcuts"
                return
            except Exception as e:
                self.impl = None
                self.detail = f"portal failed: {e}"

        if wanted == "portal":
            self.mode = "off"
            self.detail = self.detail or "GlobalShortcuts portal unavailable"
            return

        if session.session_type() == "x11" and X11Hotkeys.available():
            try:
                self.impl = X11Hotkeys(self.dispatch, enabled_actions(self.config))
                grabbed = self.impl.start()
                self.mode = "x11"
                self.detail = f"X11 grabs ({len(grabbed)} actions, lock-modifier safe)"
                return
            except Exception as e:
                self.impl = None
                self.detail = f"X11 grab failed: {e}"

        self.mode = "off"
        if not self.detail:
            self.detail = ("no global-hotkey mechanism available; "
                           "run tools/install-hotkeys.sh")

    def stop(self):
        if self.impl is not None:
            self.impl.stop()
            self.impl = None
