"""Key combinations: one spelling in the config, and each desktop's own spelling of it.

The config keeps GTK's accelerator syntax in a fixed order - "<Control><Shift>1" - so
that the same combination always compares equal however it was typed or captured. GTK's
own accelerator_name() writes Ctrl as "<Primary>" and Shift+1 as "exclam" on some
paths, which is why this module, not GTK, is what normalises.

From that one spelling each mechanism gets its own (see hotkeys.py, keybindings.py):
an X keysym and modifier mask, the GlobalShortcuts portal's "CTRL+SHIFT+1", GNOME's and
Cinnamon's gsettings (GTK syntax again), Hyprland's "CTRL SHIFT, 1" and Sway's
"Ctrl+Shift+1".

A combination must hold Ctrl, Alt or Super, or be a function key: a global shortcut on
a bare letter would take that letter away from every program. And Ctrl+Alt+F1 to F12
are the display server's own - they switch to a text console - so they never arrive.
"""

import re

from . import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, Gtk

# Canonical order, and how each is spelled where it goes.
MODIFIERS = ("control", "alt", "shift", "super")
_GTK = {"control": "<Control>", "alt": "<Alt>", "shift": "<Shift>", "super": "<Super>"}
_LABEL = {"control": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super"}
_PORTAL = {"control": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "LOGO"}
_HYPRLAND = {"control": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "SUPER"}
_SWAY = {"control": "Ctrl", "alt": "Mod1", "shift": "Shift", "super": "Mod4"}

_FUNCTION_KEY = re.compile(r"F([1-9]|[12][0-9]|3[0-5])")


def _mods_of(state):
    found = set()
    if state & Gdk.ModifierType.CONTROL_MASK:
        found.add("control")
    if state & Gdk.ModifierType.MOD1_MASK:
        found.add("alt")
    if state & Gdk.ModifierType.SHIFT_MASK:
        found.add("shift")
    if state & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.MOD4_MASK):
        found.add("super")
    return found


def _join(mods, key):
    return "".join(_GTK[m] for m in MODIFIERS if m in mods) + key


def refused(keyval, state):
    """Why `keyval` held with `state` cannot be a global shortcut, or None if it can."""
    name = Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) if keyval else None
    if not name:
        return "That key has no name this app can bind."
    mods = _mods_of(state)
    function = _FUNCTION_KEY.fullmatch(name)
    if not (mods & {"control", "alt", "super"}) and not function:
        return ("That would take the key from every program: hold Ctrl, Alt or Super "
                "with it, or use a function key.")
    if {"control", "alt"} <= mods and function and int(function.group(1)) <= 12:
        return ("Ctrl+Alt+F1 to F12 switch to a text console, so they never reach any "
                "program. Choose another.")
    return None


def from_keys(keyval, state):
    """The canonical spelling of `keyval` held with `state`, or None when it is not a
    usable global shortcut (see refused). `keyval` should be the key's unshifted symbol
    - "1", not "exclam" - which is what the capture button asks the keymap for."""
    if refused(keyval, state):
        return None
    return _join(_mods_of(state), Gdk.keyval_name(Gdk.keyval_to_lower(keyval)))


def canonical(accel):
    """`accel` in the config's own spelling, or None if it is empty or not valid."""
    if not accel:
        return None
    keyval, state = Gtk.accelerator_parse(accel)
    return from_keys(keyval, state)


def parts(accel):
    """(modifiers, key name) of a canonical spelling: ({"control", "shift"}, "1")."""
    mods = set()
    rest = accel
    while rest.startswith("<"):
        tag, rest = rest[1:].split(">", 1)
        mods.add({"control": "control", "alt": "alt", "shift": "shift",
                  "super": "super"}[tag.lower()])
    return mods, rest


def label(accel):
    """For people: "Ctrl+Shift+1", "Super+F5"."""
    mods, key = parts(accel)
    keyval = Gdk.keyval_from_name(key)
    shown = Gtk.accelerator_get_label(keyval, 0) if keyval else key
    return "+".join([_LABEL[m] for m in MODIFIERS if m in mods] + [shown or key])


def shifted(key, keymap=None):
    """The symbol Shift makes of `key` on this keyboard's layout - "parenleft" for "9"
    on a US layout, "parenright" on a German one - or None for a key Shift only
    capitalises (a letter), or that the layout does not have."""
    keyval = Gdk.keyval_from_name(key)
    if not keyval or Gdk.keyval_to_upper(keyval) != Gdk.keyval_to_lower(keyval):
        return None
    if keymap is None:
        display = Gdk.Display.get_default()
        if display is None:
            return None
        keymap = Gdk.Keymap.get_for_display(display)
    found, entries = keymap.get_entries_for_keyval(keyval)
    if not found or not entries:
        return None
    entry = entries[0]
    ok, symbol, *_rest = keymap.translate_keyboard_state(
        entry.keycode, Gdk.ModifierType.SHIFT_MASK, entry.group)
    if not ok or symbol in (0, keyval):
        return None
    return Gdk.keyval_name(symbol)


def portal_trigger(accel, keymap=None):
    """The GlobalShortcuts portal's preferred_trigger: "CTRL+SHIFT+k", "LOGO+F5".

    Shift with a key it does not just capitalise is sent as the symbol it makes, with
    no SHIFT: "CTRL+parenleft" for Ctrl+Shift+9 on a US layout. KDE matches a key press
    by the symbol it produced, so a stored Ctrl+Shift+9 never fired - a real Ctrl+Shift+9
    arrives as Ctrl+( - while Ctrl+( does (Plasma 6.7 on Wayland). Worked out from this
    keyboard's own layout, which is what the desktop will see."""
    mods, key = parts(accel)
    if "shift" in mods:
        symbol = shifted(key, keymap)
        if symbol:
            mods, key = mods - {"shift"}, symbol
    return "+".join([_PORTAL[m] for m in MODIFIERS if m in mods] + [key])


def hyprland(accel):
    """(modifiers, key) for a Hyprland bind line: ("CTRL SHIFT", "1")."""
    mods, key = parts(accel)
    return " ".join(_HYPRLAND[m] for m in MODIFIERS if m in mods), key


def sway(accel):
    """Sway's bindsym: "Ctrl+Shift+1"."""
    mods, key = parts(accel)
    return "+".join([_SWAY[m] for m in MODIFIERS if m in mods] + [key])
