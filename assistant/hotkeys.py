"""Global shortcuts: the keys the user chose in Settings > Shortcuts, by whatever means
this session allows.

config.hotkeys maps an action to a key combination (shortcuts.py), and an action not in
it has no shortcut. New installs start with none; an install from before shortcuts
could be chosen keeps the keys it had (migrate). Who listens for them depends on the
session, and is decided here (choose):

  x11      An X11 session - Cinnamon, MATE, XFCE, KDE or GNOME on Xorg. The app grabs
           the keys itself, and knows at once if another program holds one.
  portal   org.freedesktop.portal.GlobalShortcuts, on Wayland where a backend binds the
           keys itself: KDE Plasma 6, and GNOME from 48. The keys are suggestions; the
           desktop owns them, may ask the user to confirm them, and can change them in
           its own settings. What it actually bound is read back and shown.
  desktop  Wayland without a usable portal: the shortcuts are written into the
           desktop's own keybindings (keybindings.py) - GNOME's and Cinnamon's custom
           shortcuts, Hyprland's and Sway's config. Hyprland's portal is passed over
           on purpose: it binds nothing until the user writes `global` binds by hand.
  manual   Anything else: Settings > Shortcuts lists the commands to bind by hand.

What the app wrote into a desktop is recorded (config.hotkeys_installed), so that when
another mechanism takes over - an X11 session after a Wayland one, shortcuts switched
off - it is taken back out rather than left to fire as well.

The X11 grabber grabs every combination of the lock modifiers too: X11 reports NumLock
and CapsLock in the event state, so a single grab would leave every key dead with
NumLock on.
"""

from gi.repository import GLib

from . import APP_ID, keybindings, shortcuts

# (action, menu label, suggested key). The suggestions are what "Use suggested keys"
# fills in; none is bound until the user says so.
ACTIONS = [
    ("ask_anything", "Ask anything", "<Control><Shift>9"),
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

# What versions before Settings > Shortcuts bound, which an existing install keeps.
LEGACY_KEYS = {name: key for name, _, key in ACTIONS if name != "ask_anything"}

LABELS = {name: label for name, label, _ in ACTIONS}

# config.hotkey_mode: which mechanism, when "auto" is not what is wanted.
MECHANISMS = ("auto", "x11", "portal", "desktop", "off")


def action_names():
    return [name for name, _, _ in ACTIONS]


def enabled_actions(config):
    """ACTIONS less the encyclopedia's while it is turned off, so that its keys are not
    taken from other programs by someone without the DVD."""
    on = bool(getattr(config, "encyclopedia_enabled", False))
    return [action for action in ACTIONS if on or action[0] not in ENCYCLOPEDIA_ACTIONS]


def bindings(config):
    """{action: canonical key} for the shortcuts to bind now: chosen, valid, and for an
    action that is switched on. A key given to two actions goes to the first."""
    chosen = getattr(config, "hotkeys", None) or {}
    out, taken = {}, set()
    for name, _, _ in enabled_actions(config):
        key = shortcuts.canonical(chosen.get(name))
        if key and key not in taken:
            out[name] = key
            taken.add(key)
    return out


def choose(config):
    """(mechanism, why) for this session: see the module docstring."""
    from . import session
    wanted = (getattr(config, "hotkey_mode", None) or "auto").lower()
    if wanted not in MECHANISMS:
        wanted = "auto"                 # "static", from before: the app writes them now
    if wanted == "off":
        return "off", "switched off in Settings > Advanced"
    desktop = session.desktop()
    x11 = session.session_type() == "x11" and X11Hotkeys.available()
    portal = PortalHotkeys.available()
    if wanted == "x11":
        return ("x11", "X11 key grabs") if x11 else ("off", "not an X11 session")
    if wanted == "portal":
        return (("portal", "org.freedesktop.portal.GlobalShortcuts") if portal
                else ("off", "the GlobalShortcuts portal is not available"))
    if wanted == "auto":
        if x11:
            return "x11", "X11 key grabs"
        if portal and desktop != "hyprland":
            return "portal", "org.freedesktop.portal.GlobalShortcuts"
    found = keybindings.writer(desktop)
    if found is not None:
        return "desktop", found.describe()
    return "manual", f"no automatic setup for this desktop ({desktop})"


def migrate(config):
    """Give a config from before Settings > Shortcuts the keys it had working.

    Config.load leaves config.hotkeys None for such a file. "off" had none; "static"
    had the old script's bindings in the desktop, which the app now takes over; "auto"
    and "portal" had keys only where the app could listen itself - X11, or a portal
    that binds them. Anything else had none working, and is not given keys now that
    would suddenly be written into the desktop. True if the config was migrated.
    """
    if getattr(config, "hotkeys", None) is not None:
        return False
    from . import session
    old = (getattr(config, "hotkey_mode", None) or "auto").lower()
    keys = {}
    if old == "static":
        keys = dict(LEGACY_KEYS)
        config.hotkeys_installed = {"mechanism": "legacy"}
    elif old != "off":
        x11 = session.session_type() == "x11" and X11Hotkeys.available()
        portal = PortalHotkeys.available() and session.desktop() != "hyprland"
        if x11 or portal:
            keys = dict(LEGACY_KEYS)
    config.hotkeys = keys
    config.hotkey_mode = "auto"
    try:
        config.save()
    except OSError as e:
        print(f"[hotkeys] migrated settings not saved: {e}")
    return True


class X11Hotkeys:
    """python-xlib grabs on the root window. X11 sessions only."""

    def __init__(self, dispatch, wanted):
        self.dispatch = dispatch
        self.wanted = dict(wanted)          # {action: canonical key}
        self.display = None
        self.root = None
        self.running = False
        self.bindings = {}                  # (keycode, modifier mask) -> action
        self.failed = {}                    # action -> why it is not bound
        self._source_id = None

    @staticmethod
    def available():
        try:
            from Xlib import display  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def _mask(mods):
        from Xlib import X
        bits = {"control": X.ControlMask, "shift": X.ShiftMask, "alt": X.Mod1Mask,
                "super": X.Mod4Mask}
        mask = 0
        for mod in mods:
            mask |= bits[mod]
        return mask

    def start(self):
        from Xlib import X, XK, display, error

        self.display = display.Display()
        self.root = self.display.screen().root
        locks = [0, X.LockMask, X.Mod2Mask, X.Mod5Mask,
                 X.LockMask | X.Mod2Mask, X.LockMask | X.Mod5Mask,
                 X.Mod2Mask | X.Mod5Mask, X.LockMask | X.Mod2Mask | X.Mod5Mask]

        for action, accel in self.wanted.items():
            mods, key = shortcuts.parts(accel)
            keycode = self.display.keysym_to_keycode(XK.string_to_keysym(key))
            if not keycode:
                self.failed[action] = "no such key on this keyboard"
                continue
            mask = self._mask(mods)
            # BadAccess: another client holds the grab. Caught per key, so one taken
            # key is reported rather than silently dead, and the rest still bind.
            caught = error.CatchError(error.BadAccess)
            for lock in locks:
                self.root.grab_key(keycode, mask | lock, True, X.GrabModeAsync,
                                   X.GrabModeAsync, onerror=caught)
            self.display.sync()
            if caught.get_error():
                for lock in locks:
                    self.root.ungrab_key(keycode, mask | lock)
                self.display.sync()
                self.failed[action] = "taken by another program"
                continue
            self.bindings[(keycode, mask)] = action

        self.running = True
        # Wake on the X connection fd rather than polling.
        self._source_id = GLib.io_add_watch(
            self.display.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_readable)
        return list(self.bindings.values())

    def _on_readable(self, _fd, _condition):
        from Xlib import X
        if not self.running:
            return False
        significant = X.ControlMask | X.ShiftMask | X.Mod1Mask | X.Mod4Mask
        try:
            while self.display.pending_events():
                event = self.display.next_event()
                if event.type != X.KeyPress:
                    continue
                action = self.bindings.get((event.detail, event.state & significant))
                if action:
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
                for keycode, _mask in self.bindings:
                    self.root.ungrab_key(keycode, X.AnyModifier)
                self.display.sync()
                self.display.close()
            except Exception:
                pass
            self.display = None


class PortalHotkeys:
    """org.freedesktop.portal.GlobalShortcuts.

    The keys sent are preferred triggers only: the desktop decides, may ask the user,
    and keeps whatever the user later changes in its own settings. So what it reports
    back - in BindShortcuts' response, and ShortcutsChanged after - is what the menu and
    Settings show, in `triggers`.

    The portal must know which app this is. It names an unsandboxed app from its
    systemd scope, which only a launch from a .desktop file gives it; started any other
    way - autostart on some desktops, a terminal - it has none, and KDE refuses the
    session outright: "An app id is required" (Plasma 6.7, xdg-desktop-portal 1.22). So
    the app registers its id (org.freedesktop.host.portal.Registry), which the portal
    accepts only for an id with a .desktop file installed: keybindings.app_entry writes
    a hidden one. Registering has to come before the connection's first portal request,
    so the session gets a connection of its own rather than the one GTK and the
    screenshot portal share.
    """

    # Restarting reuses the process id, and the portal will not export a second session
    # at a path it is still holding the first one on.
    _sessions = 0

    def __init__(self, dispatch, wanted, on_change=None):
        self.dispatch = dispatch
        self.wanted = dict(wanted)
        self.on_change = on_change or (lambda: None)
        self.triggers = {}                  # action -> the desktop's description
        self.failed = {}
        self.bus = None
        self.session_handle = None
        self._subs = []

    @staticmethod
    def available():
        from . import session
        return session.portal_has("GlobalShortcuts")

    @staticmethod
    def version():
        from . import session
        return session.portal_version("GlobalShortcuts") or 1

    def _token(self):
        import os
        PortalHotkeys._sessions += 1
        return f"daassistant{os.getpid()}_{PortalHotkeys._sessions}"

    def _request_path(self, token):
        unique = self.bus.get_unique_name()[1:].replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{unique}/{token}"

    def _on_response(self, token, then):
        """Subscribe to the Response of the request made with `token`, once."""
        from gi.repository import Gio

        def handler(_c, _s, _p, _i, _sig, params):
            self.bus.signal_unsubscribe(sub)
            self._subs.remove(sub)
            code, results = params.unpack()
            then(code, results)

        sub = self.bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
            "Response", self._request_path(token), None, Gio.DBusSignalFlags.NONE,
            handler)
        self._subs.append(sub)

    def _connect(self):
        """A session bus connection of this session's own, registered as APP_ID."""
        from gi.repository import Gio
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        bus = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        try:
            keybindings.app_entry()
        except OSError as e:
            print(f"[hotkeys] no .desktop entry for the portal: {e}")
        try:
            bus.call_sync("org.freedesktop.portal.Desktop",
                          "/org/freedesktop/portal/desktop",
                          "org.freedesktop.host.portal.Registry", "Register",
                          GLib.Variant("(sa{sv})", (APP_ID, {})), None,
                          Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as e:
            # Known already by its scope, or a portal from before the call (1.19):
            # CreateSession says whether that is enough.
            print(f"[hotkeys] portal registration as {APP_ID}: {e.message}")
        return bus

    def start(self):
        from gi.repository import Gio

        self.bus = self._connect()
        token = self._token()

        def created(code, results):
            if code != 0:
                print(f"[hotkeys] GlobalShortcuts session refused (code {code})")
                self.failed = {action: "refused by the desktop" for action in self.wanted}
                self.on_change()
                return
            self.session_handle = results.get("session_handle")
            self._bind()

        self._on_response(token, created)
        for signal, handler in (("Activated", self._on_activated),
                                ("ShortcutsChanged", self._on_changed)):
            self._subs.append(self.bus.signal_subscribe(
                "org.freedesktop.portal.Desktop",
                "org.freedesktop.portal.GlobalShortcuts", signal, None, None,
                Gio.DBusSignalFlags.NONE, handler))

        options = {"handle_token": GLib.Variant("s", token),
                   "session_handle_token": GLib.Variant("s", token)}
        self.bus.call_sync(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.GlobalShortcuts", "CreateSession",
            GLib.Variant("(a{sv})", (options,)), GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE, -1, None)
        return True

    def _bind(self):
        from gi.repository import Gio
        listed = [(name, {"description": GLib.Variant("s", label),
                          "preferred_trigger": GLib.Variant(
                              "s", shortcuts.portal_trigger(self.wanted[name]))})
                  for name, label, _ in ACTIONS if name in self.wanted]
        token = self._token()

        def bound(code, results):
            if code != 0:
                self.failed = {action: "not confirmed in the desktop's dialog"
                               for action in self.wanted}
            self._take(results.get("shortcuts") or [])

        self._on_response(token, bound)
        self.bus.call(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.GlobalShortcuts", "BindShortcuts",
            GLib.Variant("(oa(sa{sv})sa{sv})", (
                self.session_handle, listed, "",
                {"handle_token": GLib.Variant("s", token)})),
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None, None)

    def _take(self, listed):
        """Note what the desktop says each shortcut is bound to."""
        for name, props in listed:
            if name in self.wanted:
                trigger = (props or {}).get("trigger_description") or ""
                self.triggers[name] = trigger
                if trigger:
                    self.failed.pop(name, None)
                else:
                    self.failed[name] = "not assigned by the desktop"
        self.on_change()

    def _on_changed(self, _c, _s, _p, _i, _sig, params):
        try:
            session_handle, listed = params.unpack()
        except ValueError:
            return
        if session_handle == self.session_handle:
            self._take(listed)

    def _on_activated(self, _c, _s, _p, _i, _sig, params):
        try:
            session_handle, shortcut_id, _ts, _opts = params.unpack()
        except ValueError:
            return
        # Only this session's, and only one still wanted: an earlier session the
        # desktop has not let go of yet must not fire a shortcut since taken off.
        if session_handle == self.session_handle and shortcut_id in self.wanted:
            GLib.idle_add(self.dispatch, shortcut_id)

    def can_configure(self):
        return self.session_handle is not None and self.version() >= 2

    def configure(self):
        """Open the desktop's own dialog for these shortcuts (portal version 2+)."""
        from gi.repository import Gio
        self.bus.call(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.GlobalShortcuts", "ConfigureShortcuts",
            GLib.Variant("(osa{sv})", (self.session_handle, "", {})),
            None, Gio.DBusCallFlags.NONE, -1, None, None)

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
            # Ours alone: the portal ends whatever is left on it when it goes.
            self.bus.flush_sync(None)
            self.bus.close(None, None, None)
            self.bus = None


class HotkeyManager:
    """Binds config.hotkeys by the mechanism choose() picks, and says how it went."""

    def __init__(self, config, dispatch, on_change=None, run=keybindings.run_host):
        self.config = config
        self.dispatch = dispatch
        self.on_change = on_change or (lambda: None)
        self.run = run
        self.impl = None
        self.mode = "off"
        self.detail = ""
        self.wanted = {}
        self.problems = {}                  # action -> why it is not bound as asked
        self.error = None                   # the desktop could not be written

    def start(self):
        from . import session

        # start() runs again whenever the shortcuts change, so nothing may be left
        # over from last time.
        self.impl, self.problems, self.error = None, {}, None
        self.mode, self.detail = choose(self.config)
        self.wanted = bindings(self.config) if self.mode != "off" else {}
        desktop = session.desktop()
        if desktop == "kde":
            try:
                if keybindings.remove_kde_legacy(self.run):
                    print("[hotkeys] removed the old install-hotkeys.sh launchers")
            except OSError as e:
                print(f"[hotkeys] old KDE launchers not removed: {e}")
        target = f"desktop:{desktop}" if self.mode == "desktop" else None
        self._tidy(target, desktop)

        if self.mode == "x11" and self.wanted:
            try:
                self.impl = X11Hotkeys(self.dispatch, self.wanted)
                self.impl.start()
                self.problems = dict(self.impl.failed)
            except Exception as e:
                self.impl = None
                self.mode, self.detail = "off", f"X11 grab failed: {e}"
        elif self.mode == "portal" and self.wanted:
            try:
                self.impl = PortalHotkeys(self.dispatch, self.wanted, self._portal_changed)
                self.impl.start()
            except Exception as e:
                self.impl = None
                self.mode, self.detail = "off", f"portal failed: {e}"
        elif self.mode == "desktop":
            self._install(target, desktop)
        self.on_change()

    def _record(self, value):
        self.config.hotkeys_installed = value
        try:
            self.config.save()
        except OSError as e:
            print(f"[hotkeys] could not record the desktop's shortcuts: {e}")

    def _tidy(self, target, desktop):
        """Take out what an earlier mechanism wrote into the desktop, if that is not
        what is about to be written now."""
        installed = getattr(self.config, "hotkeys_installed", None) or {}
        previous = installed.get("mechanism")
        if not previous or previous == target:
            return
        names = action_names()
        if previous == "legacy":
            # The old script's, written for whichever desktop this was.
            found = keybindings.writer(desktop, self.run)
        else:
            found = keybindings.writer(previous.split(":", 1)[1], self.run)
        try:
            if found is not None:
                found.remove(names)
        except OSError as e:
            print(f"[hotkeys] earlier shortcuts not removed: {e}")
            return
        self._record({})

    def _install(self, target, desktop):
        """Write the shortcuts into the desktop - only when they, or where the launcher
        is, changed since they were last written."""
        record = {"mechanism": target, "bindings": self.wanted,
                  "launcher": keybindings.launcher()}
        if (getattr(self.config, "hotkeys_installed", None) or {}) == record:
            return
        try:
            keybindings.writer(desktop, self.run).apply(self.wanted, LABELS,
                                                        action_names())
        except OSError as e:
            self.error = str(e)
            return
        self._record(record)

    def _portal_changed(self):
        """From the portal's signals, which arrive on the main loop."""
        if self.impl is not None:
            self.problems = dict(self.impl.failed)
        self.on_change()

    def trigger_label(self, action):
        """The key that runs `action` now, for the menu - or None."""
        if action not in self.wanted or action in self.problems:
            return None
        if self.mode == "portal":
            return (self.impl.triggers.get(action) or None) if self.impl else None
        if self.mode in ("x11", "desktop") and not self.error:
            return shortcuts.label(self.wanted[action])
        return None

    def describe(self):
        """A sentence for Settings > Shortcuts on how shortcuts work here."""
        if self.mode == "x11":
            return ("This is an X11 session: DA Assistant listens for these keys itself "
                    "while it runs, and says if another program already has one.")
        if self.mode == "portal":
            return ("Your desktop manages these shortcuts (the GlobalShortcuts portal). "
                    "The keys chosen here are suggestions: it may ask you to confirm "
                    "them, and once it has bound one, it keeps it until you change it "
                    "in its own shortcut settings.")
        if self.mode == "desktop":
            return (f"Saved into {self.detail}, each running DA Assistant, when you "
                    f"press Save. Nothing else there is changed"
                    + ("; a backup is kept." if self.detail.startswith("~") else "."))
        if self.mode == "manual":
            return ("This desktop cannot be set up automatically. Bind these commands to "
                    "keys in its keyboard settings:")
        return f"Shortcuts are off: {self.detail}."

    def stop(self):
        if self.impl is not None:
            self.impl.stop()
            self.impl = None
