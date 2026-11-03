"""Key combinations: one spelling in the config, and each desktop's own spelling of it.

The config keeps GTK's accelerator syntax in a fixed order - "<Control><Shift>1" - so
that the same combination always compares equal however it was typed or captured. GTK's
own accelerator_name() writes Ctrl as "<Primary>" and Shift+1 as "exclam" on some
paths, which is why this module, not GTK, is what normalises. The key is named as
libxkbcommon names it, which is how every desktop reads one (see key_name), and read
back here (key_value) rather than by GTK, which knows only its own spelling.

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

# A key that types a character but has no name of its own: U0561, or U+0561 as GDK
# writes it (and as the config held it before 4.0).
_UNICODE_KEY = re.compile(r"U\+?([0-9A-Fa-f]{4,8})")

# The XF86 keys, which GDK names without their XF86.
_XF86 = range(0x10080000, 0x10090000)


def key_name(keyval):
    """The name of key value `keyval` that the desktops read, or None if it has none.

    Every desktop reads a key's name with libxkbcommon - Hyprland, Sway, the portals,
    GNOME's and Cinnamon's window managers - and GDK's name for a key is the same but
    for two kinds of key. One that types a character with no keysym name of its own,
    an Armenian, Georgian or Sinhala letter, GDK calls U+0561, which nothing else reads;
    libxkbcommon calls it U0561. And GDK drops the XF86 from an XF86 key's name: the
    calculator key was written "Calculator", which libxkbcommon does not know. A key
    GDK can only give a number for, "0x100810f4", has no name here: GNOME and Cinnamon
    would read the number as a key code, and so as another key.
    """
    name = Gdk.keyval_name(keyval) if keyval else None
    if not name or name.startswith("0x"):
        return None
    if name.startswith("U+"):
        return "U" + name[2:]
    if keyval in _XF86 and not name.startswith("XF86"):
        return "XF86" + name
    return name


def key_value(name):
    """The key value - the X keysym - that a key's name stands for, or 0 if none.

    Read here, and not by GDK, which knows only the names in its own table: U0561 and
    U+0561 it read as no key at all, so a shortcut on such a key was dropped as invalid
    on every desktop, the button showed it as Ctrl+Alt+0xffffff, and Settings opened
    again with it gone. The U names are read as libxkbcommon reads them.
    """
    unicode_key = _UNICODE_KEY.fullmatch(name or "")
    if unicode_key:
        code = int(unicode_key.group(1), 16)
        if code < 0x20 or 0x7e < code < 0xa0 or code > 0x10ffff:
            return 0
        return code if code < 0x100 else 0x01000000 | code
    found = Gdk.keyval_from_name(name) if name else 0
    return 0 if found in (0, Gdk.KEY_VoidSymbol) else found


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
    name = key_name(Gdk.keyval_to_lower(keyval)) if keyval else None
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
    return _join(_mods_of(state), key_name(Gdk.keyval_to_lower(keyval)))


def canonical(accel):
    """`accel` in the config's own spelling, or None if it is empty or not valid.

    GTK reads the modifiers, as it always has, and key_value the key, which GTK cannot
    read when it is U0561. A key in GDK's spelling from before, U+0561 or Calculator,
    comes back in the one the desktops read."""
    if not accel:
        return None
    mods, close, key = accel.rpartition(">")
    found = key_value(key)
    if not found:
        return None
    state = 0
    if close:
        stand_in, state = Gtk.accelerator_parse(mods + close + "a")
        if not stand_in:
            return None                     # modifiers GTK does not know
    return from_keys(found, state)


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
    value = key_value(key)
    shown = Gtk.accelerator_get_label(value, 0) if value else key
    return "+".join([_LABEL[m] for m in MODIFIERS if m in mods] + [shown or key])


def shifted(key, keymap=None):
    """The symbol Shift makes of `key` on this keyboard's layout - "parenleft" for "9"
    on a US layout, "parenright" on a German one - or None for a key Shift only
    capitalises (a letter), or that the layout does not have."""
    value = key_value(key)
    if not value or Gdk.keyval_to_upper(value) != Gdk.keyval_to_lower(value):
        return None
    if keymap is None:
        display = Gdk.Display.get_default()
        if display is None:
            return None
        keymap = Gdk.Keymap.get_for_display(display)
    found, entries = keymap.get_entries_for_keyval(value)
    if not found or not entries:
        return None
    entry = entries[0]
    ok, symbol, *_rest = keymap.translate_keyboard_state(
        entry.keycode, Gdk.ModifierType.SHIFT_MASK, entry.group)
    if not ok or symbol in (0, value):
        return None
    return key_name(symbol)


def portal_trigger(accel, keymap=None, desktop="kde"):
    """The GlobalShortcuts portal's preferred_trigger: "CTRL+SHIFT+k", "LOGO+F5".

    On KDE, Shift with a key it does not just capitalise is sent as the symbol it makes,
    with no SHIFT: "CTRL+parenleft" for Ctrl+Shift+9 on a US layout. KDE matches a key
    press by the symbol it produced, so a stored Ctrl+Shift+9 never fired - a real
    Ctrl+Shift+9 arrives as Ctrl+( - while Ctrl+( does (Plasma 6.7 on Wayland). Worked
    out from this keyboard's own layout, which is what the desktop will see.

    GNOME is the other way round: sent Ctrl+(, it stored <Control>parenleft, and a real
    Ctrl+Shift+9 never matched it; sent CTRL+SHIFT+9, it stored <Shift><Control>9, which
    did (GNOME 50, Nov 2026). So every desktop but KDE is sent the keys as written."""
    mods, key = parts(accel)
    if "shift" in mods and desktop == "kde":
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
