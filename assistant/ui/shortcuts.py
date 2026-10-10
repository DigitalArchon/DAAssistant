"""Settings > Shortcuts: a key for each action, captured by pressing it.

GTK 3 has no shortcut-capture widget, so ShortcutButton is one: click it, press the
combination, and it is read from the key event - its unshifted symbol, so Ctrl+Shift+1
is "1" and not "!" - and kept in shortcuts.py's canonical spelling. Escape cancels,
Backspace clears. Nothing is bound until Settings is saved; how it is bound then
depends on the session (hotkeys.py), which the page says at the top.
"""

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, Gtk

from .. import hotkeys, keybindings, shortcuts

PROMPT = "Press keys…"


class ShortcutButton(Gtk.Button):
    """Shows a key combination; click, then press one to change it.

    on_change(accel) with the canonical spelling, or "" when cleared."""

    def __init__(self, accel, on_change, on_hint=None):
        super().__init__()
        self.accel = accel or ""
        self.on_change = on_change
        self.on_hint = on_hint or (lambda _text: None)
        self.capturing = False
        self.set_size_request(170, -1)
        self.connect("clicked", lambda _w: self.start_capture())
        self.connect("key-press-event", self._on_key)
        self.connect("focus-out-event", lambda *_a: self.stop_capture())
        self._sync()

    def _sync(self):
        self.set_label(PROMPT if self.capturing else
                       shortcuts.label(self.accel) if self.accel else "Disabled")

    def set_accel(self, accel):
        self.accel = accel or ""
        self._sync()

    def start_capture(self):
        self.capturing = True
        self.grab_focus()
        self._sync()
        self.on_hint("Press the combination - Ctrl, Alt or Super with a key, or a "
                     "function key. Escape cancels, Backspace clears.")

    def stop_capture(self):
        if self.capturing:
            self.capturing = False
            self._sync()
            self.on_hint("")
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
        if event.is_modifier:
            return True                     # wait for the key itself
        if not state and event.keyval == Gdk.KEY_Escape:
            self.stop_capture()
            return True
        if not state and event.keyval == Gdk.KEY_BackSpace:
            self.capturing = False
            self.set_accel("")
            self.on_hint("")
            self.on_change("")
            return True
        keyval = self._base_keyval(event)
        accel = shortcuts.from_keys(keyval, state)
        if accel is None:
            self.on_hint(shortcuts.refused(keyval, state))
            return True
        self.capturing = False
        self.set_accel(accel)
        self.on_hint("")
        self.on_change(accel)
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
        for row, (name, label, _suggested) in enumerate(hotkeys.ACTIONS):
            title = Gtk.Label(label=label, xalign=0)
            grid.attach(title, 0, row, 1, 1)
            button = ShortcutButton(self.staged[name],
                                    lambda accel, n=name: self._changed(n, accel),
                                    self._hint)
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
            f"{label}: {shortcuts.label(key)}" for _n, label, key in hotkeys.ACTIONS))
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
                    f"{shortcuts.label(accel)} went to {hotkeys.LABELS[name]}")
        self.staged[name] = accel
        self.notes[name].set_text("")
        self._sync_commands()

    def _set(self, name, accel):
        self.staged[name] = accel
        self.buttons[name].set_accel(accel)
        self.notes[name].set_text("")
        self._sync_commands()

    def use_suggested(self):
        for name, _label, key in hotkeys.ACTIONS:
            self._set(name, key)

    def clear_all(self):
        for name in self.staged:
            self._set(name, "")

    def command_text(self):
        lines = []
        for name, label, _ in hotkeys.ACTIONS:
            key = self.staged.get(name)
            if key:
                lines.append(f"{shortcuts.label(key):<16} {keybindings.command(name)}"
                             f"    # {label}")
        return "\n".join(lines) or "(no shortcuts chosen)"

    def _sync_commands(self):
        if self.commands is not None:
            self.commands.get_buffer().set_text(self.command_text())

    def values(self):
        return {"hotkeys": {name: key for name, key in self.staged.items() if key}}
