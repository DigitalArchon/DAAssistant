"""Settings > Shortcuts: a key for each action, captured by pressing it.

GTK 3 has no shortcut-capture widget, so ShortcutButton is one: click it, press the
combination, and it is read from the key event - its unshifted symbol, so Ctrl+Shift+1
is "1" and not "!" - and kept in shortcuts.py's canonical spelling. Escape cancels,
Backspace clears. Nothing is bound until Settings is saved; how it is bound then
depends on the session (hotkeys.py), which the page says at the top.

A key that is already one of the app's shortcuts never reaches the button as a key
event: on X11 the app's own grab takes it, on Wayland the desktop does. So while a
button waits, the page asks the running HotkeyManager to hand such a press to it
instead of running the action (HotkeyManager.capture).
"""

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, GLib, Gtk

from .. import hotkeys, keybindings, shortcuts

PROMPT = "Press keys…"

# Keys pressed on the way to a combination, never one themselves. Told apart by their
# key values, because GdkEventKey.is_modifier cannot be read from Python: it is a
# one-bit field, which the compiler packs into the byte after `group` and the typelib
# places four bytes later, so PyGObject reads 0 for every key - and Ctrl, then Shift,
# was captured as Ctrl+Shift_L before the key itself was pressed.
MODIFIER_KEYS = frozenset((
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_Super_L,
    Gdk.KEY_Super_R, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R, Gdk.KEY_Caps_Lock,
    Gdk.KEY_Shift_Lock, Gdk.KEY_Num_Lock, Gdk.KEY_ISO_Level3_Shift,
    Gdk.KEY_ISO_Level5_Shift, Gdk.KEY_Mode_switch, Gdk.KEY_ISO_Next_Group,
    Gdk.KEY_ISO_Prev_Group, Gdk.KEY_ISO_First_Group, Gdk.KEY_ISO_Last_Group))


class ShortcutButton(Gtk.Button):
    """Shows a key combination; click, then press one to change it.

    on_change(accel) with the canonical spelling, or "" when cleared. on_waiting(bool)
    as it starts and stops waiting for a key."""

    # How long focus may stay away before the wait for a key ends: see _on_focus_out.
    FOCUS_GRACE_MS = 300

    def __init__(self, accel, on_change, on_hint=None, on_waiting=None):
        super().__init__()
        self.accel = accel or ""
        self.on_change = on_change
        self.on_hint = on_hint or (lambda _text: None)
        self.on_waiting = on_waiting or (lambda _waiting: None)
        self.capturing = False
        self._leaving = None                # the timeout ending a wait focus has left
        self.set_size_request(170, -1)
        self.connect("clicked", lambda _w: self.start_capture())
        self.connect("key-press-event", self._on_key)
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("focus-in-event", lambda *_a: self._stay())
        self.connect("destroy", lambda _w: self._stay())
        self._sync()

    def _sync(self):
        self.set_label(PROMPT if self.capturing else
                       shortcuts.label(self.accel) if self.accel else "Disabled")

    def set_accel(self, accel):
        self.accel = accel or ""
        self._sync()

    def start_capture(self):
        self._stay()
        self.capturing = True
        self.grab_focus()
        self._sync()
        self.on_hint("Press the combination - Ctrl, Alt or Super with a key, or a "
                     "function key. Escape cancels, Backspace clears.")
        self.on_waiting(True)

    def stop_capture(self):
        self._stay()
        if self.capturing:
            self.capturing = False
            self._sync()
            self.on_hint("")
            self.on_waiting(False)
        return False

    def take(self, accel):
        """End the wait with `accel`, as though it had been pressed here."""
        self.stop_capture()
        self.set_accel(accel)
        self.on_change(accel)

    def _on_focus_out(self, *_args):
        """Stop waiting for a key once focus has stayed away for a moment.

        Not at once. On X11, pressing a key some program has grabbed - one of this
        app's own shortcuts, say - moves the keyboard to that program while the key is
        down, which GTK reports as losing focus, and it comes back when the key is let
        go. That press is the very one to capture, and the grab's report of it arrives
        just after the focus has gone. So the wait ends only after a moment, and at low
        priority: behind the report, should one be waiting to be dispatched.
        """
        if self.capturing and self._leaving is None:
            self._leaving = GLib.timeout_add(self.FOCUS_GRACE_MS, self._focus_gone,
                                             priority=GLib.PRIORITY_LOW)
        return False

    def _focus_gone(self):
        self._leaving = None
        self.stop_capture()
        return False

    def _stay(self):
        """Keep waiting: focus came back, or the wait ended some other way."""
        if self._leaving is not None:
            GLib.source_remove(self._leaving)
            self._leaving = None
        return False

    def _base_keyval(self, event):
        """The key's symbol with no modifiers held: "1" for Shift+1, not "!"."""
        keymap = Gdk.Keymap.get_for_display(self.get_display())
        found = keymap.translate_keyboard_state(event.hardware_keycode,
                                                Gdk.ModifierType(0), event.group)
        return found[1] if found and found[0] else event.keyval

    def _on_key(self, _widget, event):
        if not self.capturing:
            return False
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval in MODIFIER_KEYS:
            return True                     # wait for the key itself
        if not state and event.keyval == Gdk.KEY_Escape:
            self.stop_capture()
            return True
        if not state and event.keyval == Gdk.KEY_BackSpace:
            self.take("")
            return True
        keyval = self._base_keyval(event)
        accel = shortcuts.from_keys(keyval, state)
        if accel is None:
            self.on_hint(shortcuts.refused(keyval, state))
            return True
        self.take(accel)
        return True


class ShortcutsPage(Gtk.Box):
    """The Shortcuts tab. values() is {"hotkeys": {...}} for the dialog to save.

    `manager` is the running HotkeyManager, when there is one: it says how keys are
    bound in this session, and what went wrong with the ones bound now."""

    def __init__(self, config, manager=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("start", "end", "top", "bottom"):
            getattr(self, f"set_margin_{side}")(14)
        self.config = config
        self.manager = manager
        chosen = getattr(config, "hotkeys", None) or {}
        self.staged = {name: shortcuts.canonical(chosen.get(name)) or ""
                       for name in hotkeys.action_names()}
        self.buttons, self.notes = {}, {}
        self.waiting = None                 # the action whose button waits for a key
        self.connect("destroy", lambda _w: self._waiting(None, False))

        mode = manager.mode if manager is not None else hotkeys.choose(config)[0]
        self.mode = mode
        how = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
        how.set_text(manager.describe() if manager is not None
                     else "Shortcuts are bound when you press Save.")
        self.pack_start(how, False, False, 0)
        if manager is not None and manager.error:
            error = Gtk.Label(xalign=0, wrap=True, max_width_chars=80,
                              label=f"Could not save them there: {manager.error}")
            error.get_style_context().add_class("error-banner")
            self.pack_start(error, False, False, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=10)
        self.pack_start(grid, False, False, 0)
        encyclopedia = bool(getattr(config, "encyclopedia_enabled", False))
        # The user's own actions only once they are defined, under their own names: a
        # new one gets its row when Settings is opened again after saving it.
        named, defined = hotkeys.labels(config), hotkeys.custom_defined(config)
        shown = [(name, named[name], key) for name, _label, key in hotkeys.ACTIONS
                 if name not in hotkeys.CUSTOM_ACTIONS or name in defined]
        for row, (name, label, _suggested) in enumerate(shown):
            title = Gtk.Label(label=label, xalign=0)
            grid.attach(title, 0, row, 1, 1)
            button = ShortcutButton(self.staged[name],
                                    lambda accel, n=name: self._changed(n, accel),
                                    self._hint,
                                    lambda waiting, n=name: self._waiting(n, waiting))
            self.buttons[name] = button
            grid.attach(button, 1, row, 1, 1)
            clear = Gtk.Button.new_from_icon_name("edit-clear-symbolic",
                                                  Gtk.IconSize.BUTTON)
            clear.set_relief(Gtk.ReliefStyle.NONE)
            clear.set_tooltip_text("No shortcut")
            clear.connect("clicked", lambda _w, n=name: self._set(n, ""))
            grid.attach(clear, 2, row, 1, 1)
            note = Gtk.Label(xalign=0, wrap=True, max_width_chars=40)
            note.get_style_context().add_class("dim-label")
            self.notes[name] = note
            grid.attach(note, 3, row, 1, 1)
            if name in hotkeys.ENCYCLOPEDIA_ACTIONS and not encyclopedia:
                title.set_sensitive(False)
                note.set_text("Bound only while the encyclopedia is on")
        self._show_problems()

        self.hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
        self.hint.get_style_context().add_class("dim-label")
        self.pack_start(self.hint, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        suggested = Gtk.Button.new_with_label("Use suggested keys")
        suggested.set_tooltip_text(", ".join(
            f"{label}: {shortcuts.label(key)}" for _n, label, key in hotkeys.ACTIONS
            if key))
        suggested.connect("clicked", lambda _w: self.use_suggested())
        row.pack_start(suggested, False, False, 0)
        clear_all = Gtk.Button.new_with_label("Clear all")
        clear_all.connect("clicked", lambda _w: self.clear_all())
        row.pack_start(clear_all, False, False, 0)
        if mode == "portal" and manager is not None and manager.impl is not None \
                and manager.impl.can_configure():
            change = Gtk.Button.new_with_label("Change in system settings…")
            change.set_tooltip_text("Open your desktop's own dialog for these shortcuts")
            change.connect("clicked", lambda _w: manager.impl.configure())
            row.pack_end(change, False, False, 0)
        self.pack_start(row, False, False, 0)

        # By hand: the commands, kept in step with the keys above.
        self.commands = None
        if mode == "manual":
            frame = Gtk.ScrolledWindow()
            frame.set_min_content_height(120)
            frame.get_style_context().add_class("preview-frame")
            self.commands = Gtk.TextView(editable=False, monospace=True)
            for side in ("start", "end", "top", "bottom"):
                getattr(self.commands, f"set_margin_{side}")(8)
            frame.add(self.commands)
            self.pack_start(frame, True, True, 0)
            copy = Gtk.Button.new_with_label("Copy")
            copy.set_halign(Gtk.Align.START)
            copy.connect("clicked", lambda _w: Gtk.Clipboard.get(
                Gdk.SELECTION_CLIPBOARD).set_text(self.command_text(), -1))
            self.pack_start(copy, False, False, 0)
            self._sync_commands()

    def _hint(self, text):
        self.hint.set_text(text)

    def _waiting(self, name, waiting):
        """`name`'s button started or stopped waiting for a key; None: the page is gone.
        One waits at a time, and while it does, the manager hands it a press of a key
        that is already a shortcut (_take_bound)."""
        if waiting:
            for other, button in self.buttons.items():
                if other != name and button.capturing:
                    button.stop_capture()
            self.waiting = name
            if self.manager is not None:
                self.manager.capture = self._take_bound
        elif name is None or name == self.waiting:
            self.waiting = None
            if self.manager is not None and self.manager.capture == self._take_bound:
                self.manager.capture = None

    def _take_bound(self, action, accel):
        """HotkeyManager.capture: `action`'s key, `accel`, was pressed while a button
        waits. The button takes the key, and whichever action holds it here lets it go,
        as when a key is given to a second action by pressing it (_changed). True: taken,
        and the action is not run."""
        button = self.buttons.get(self.waiting)
        if button is None or not button.capturing:
            return False
        button.take(accel)
        return True

    def _show_problems(self):
        if self.manager is None:
            return
        for name, note in self.notes.items():
            problem = self.manager.problems.get(name)
            if problem and self.staged.get(name) == self.manager.wanted.get(name):
                note.set_text(problem.capitalize())
            elif self.manager.mode == "portal" and name in self.manager.wanted and \
                    self.manager.impl is not None and self.manager.impl.triggers.get(name):
                note.set_text(f"Your desktop: {self.manager.impl.triggers[name]}")

    def _changed(self, name, accel):
        """A key captured for `name`. One key, one action: another holding it lets go."""
        for other, key in self.staged.items():
            if accel and other != name and key == accel:
                self._set(other, "")
                self.notes[other].set_text(
                    f"{shortcuts.label(accel)} went to "
                    f"{hotkeys.labels(self.config).get(name, name)}")
        self.staged[name] = accel
        self.notes[name].set_text("")
        self._sync_commands()

    def _set(self, name, accel):
        self.staged[name] = accel
        if name in self.buttons:
            self.buttons[name].set_accel(accel)
            self.notes[name].set_text("")
        self._sync_commands()

    def use_suggested(self):
        for name, _label, key in hotkeys.ACTIONS:
            if key:
                self._set(name, key)

    def clear_all(self):
        for name in self.staged:
            self._set(name, "")

    def command_text(self):
        lines = []
        named = hotkeys.labels(self.config)
        for name, _label, _ in hotkeys.ACTIONS:
            label = named.get(name, name)
            key = self.staged.get(name)
            if key and (name not in hotkeys.CUSTOM_ACTIONS
                        or name in hotkeys.custom_defined(self.config)):
                lines.append(f"{shortcuts.label(key):<16} {keybindings.command(name)}"
                             f"    # {label}")
        return "\n".join(lines) or "(no shortcuts chosen)"

    def _sync_commands(self):
        if self.commands is not None:
            self.commands.get_buffer().set_text(self.command_text())

    def values(self):
        return {"hotkeys": {name: key for name, key in self.staged.items() if key}}
