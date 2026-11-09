"""Settings > Shortcuts: chosen keys, bound by whatever the session allows.

Nothing here reads or writes the real desktop. The desktop writers are driven through a
recording stand-in for the host's gsettings, kwriteconfig and hyprctl, config files are
temporary ones, and the session (desktop, X11 or Wayland, portal) is decided by each
test. The X11 grabs are real, on the test's own virtual display, with keys pressed
through XTEST.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gdk, Gio, GLib, Gtk

from assistant import hotkeys, keybindings, session, shortcuts
from assistant.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def config(self, on_disk=None):
        path = os.path.join(self.dir, "c.json")
        if on_disk is not None:
            with open(path, "w") as f:
                json.dump(on_disk, f)
        return Config(path)


class FakeHost:
    """Stands in for the host's programs: answers `gsettings get` with a list the user
    already has, and writes down everything it is asked to run instead of running it."""

    def __init__(self, listed="@as []"):
        self.listed = listed
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["gsettings", "get"]:
            return 0, self.listed + "\n"
        if args[:2] == ["gsettings", "set"] and args[3] in ("custom-list",
                                                           "custom-keybindings"):
            self.listed = args[4]
        if args[:2] == ["gsettings", "reset"] and args[3] == "custom-keybindings":
            self.listed = "@as []"                  # GNOME's default
        return 0, ""

    def sets(self, key):
        return [c for c in self.calls if c[:2] == ["gsettings", "set"] and c[3] == key]

    def writes(self):
        """The calls that change something: not the reads, which every start makes to
        look for keys the desktop has bound already (clashes)."""
        return [c for c in self.calls
                if c[:2] not in (["gsettings", "get"], ["gsettings", "list-recursively"],
                                 ["hyprctl", "binds"])]


def session_of(desktop="cinnamon", kind="wayland", portal=False, x11=True, answered=True):
    """Patches deciding the session for one test. `answered`: whether the portal has
    answered a probe yet - whether it is there or not is `portal`."""
    return [mock.patch.object(session, "desktop", return_value=desktop),
            mock.patch.object(session, "session_type", return_value=kind),
            mock.patch.object(session, "portal_answered", return_value=answered),
            mock.patch.object(hotkeys.PortalHotkeys, "available",
                              staticmethod(lambda: portal)),
            mock.patch.object(hotkeys.X11Hotkeys, "available",
                              staticmethod(lambda: x11))]


class Patched:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for patch in self.patches:
            patch.start()

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()


# ---- the spelling ----------------------------------------------------------------

class Spelling(unittest.TestCase):
    def test_one_spelling_however_it_was_written(self):
        for written in ("<Primary><Shift>1", "<Shift><Control>1", "<Ctrl><Shift>1"):
            self.assertEqual(shortcuts.canonical(written), "<Control><Shift>1")
        self.assertEqual(shortcuts.canonical("<Super><Alt>A"), "<Alt><Super>a")

    def test_a_shortcut_needs_ctrl_alt_or_super_unless_a_function_key(self):
        for refused in ("a", "<Shift>a", "1", "", None, "<Control>"):
            self.assertIsNone(shortcuts.canonical(refused), refused)
        self.assertEqual(shortcuts.canonical("F5"), "F5")
        self.assertEqual(shortcuts.canonical("<Shift>F12"), "<Shift>F12")

    def test_the_console_switching_keys_are_refused_with_a_reason(self):
        self.assertIsNone(shortcuts.canonical("<Control><Alt>F2"))
        keyval, state = Gtk.accelerator_parse("<Control><Alt>F2")
        self.assertIn("text console", shortcuts.refused(keyval, state))
        self.assertEqual(shortcuts.canonical("<Control><Alt>F13"), "<Control><Alt>F13")
        self.assertEqual(shortcuts.canonical("<Control><Shift>F2"), "<Control><Shift>F2")

    def test_each_desktops_own_spelling(self):
        accel = "<Control><Shift>1"
        self.assertEqual(shortcuts.label(accel), "Ctrl+Shift+1")
        self.assertEqual(shortcuts.label("<Super>F5"), "Super+F5")
        self.assertEqual(shortcuts.portal_trigger("<Control><Shift>k"), "CTRL+SHIFT+k")
        self.assertEqual(shortcuts.portal_trigger("<Control><Alt>3"), "CTRL+ALT+3")
        self.assertEqual(shortcuts.portal_trigger("<Super>space"), "LOGO+space")
        self.assertEqual(shortcuts.hyprland("<Control><Alt>t"), ("CTRL ALT", "t"))
        self.assertEqual(shortcuts.sway("<Alt><Super>t"), "Mod1+Mod4+t")

    def test_the_portal_is_sent_the_symbol_shift_makes_of_a_digit(self):
        # KDE matches Ctrl+Shift+9 as Ctrl+( (seen on Plasma 6.7): what the layout
        # makes of Shift+9, here the test display's US one.
        self.assertEqual(shortcuts.portal_trigger("<Control><Shift>9"), "CTRL+parenleft")
        self.assertEqual(shortcuts.portal_trigger("<Control><Shift>1"), "CTRL+exclam")
        self.assertEqual(shortcuts.portal_trigger("<Shift><Super>F5"), "SHIFT+LOGO+F5")

    def test_gnome_is_sent_the_keys_as_written(self):
        # GNOME 50 stored Ctrl+( as <Control>parenleft, which a real Ctrl+Shift+9 never
        # matched; CTRL+SHIFT+9 it stored as <Shift><Control>9, which fired.
        for desktop in ("gnome", "unknown"):
            self.assertEqual(shortcuts.portal_trigger("<Control><Shift>9", desktop=desktop),
                             "CTRL+SHIFT+9")
            self.assertEqual(shortcuts.portal_trigger("<Control><Shift>k", desktop=desktop),
                             "CTRL+SHIFT+k")
        self.assertEqual(shortcuts.portal_trigger("<Control><Shift>9", desktop="kde"),
                         "CTRL+parenleft")

    def test_a_gnome_shortcut_that_lost_its_shift_is_found(self):
        self.assertTrue(hotkeys.shift_lost("<Control><Shift>1", "Press <Control>exclam"))
        self.assertFalse(hotkeys.shift_lost("<Control><Shift>1", "Press <Shift><Control>1"))
        self.assertFalse(hotkeys.shift_lost("<Control><Shift>k", "Press <Control>k"))
        self.assertFalse(hotkeys.shift_lost("<Control>1", "Press <Control>1"))
        self.assertTrue(hotkeys.shift_lost("<Control><Alt><Shift>5",
                                           "Press <Alt><Control>percent"))

    def test_a_key_the_user_chose_in_gnome_is_not_a_lost_shift(self):
        # Changed in GNOME's own settings, perhaps through Change in system settings…:
        # it was said to be a key no press matches, kept out of the tray menu, and
        # blamed on 3.4.0 at every start.
        for said in ("Press <Control><Alt>t", "Press <Control>at", "Press <Super>F5",
                     "Press <Control>1", "Press <Shift><Control>2"):
            self.assertFalse(hotkeys.shift_lost("<Control><Shift>1", said), said)
        portal = hotkeys.PortalHotkeys(lambda name: None,
                                       {"translate_text": "<Control><Shift>1"})
        with mock.patch.object(session, "desktop", return_value="gnome"):
            portal._take([("translate_text",
                           {"trigger_description": "Press <Control><Alt>t"})])
        self.assertEqual(portal.failed, {})
        self.assertEqual(portal.triggers["translate_text"], "Ctrl+Alt+T")

    def test_gnomes_own_syntax_reads_as_ours(self):
        self.assertEqual(hotkeys.readable_trigger("Press <Control>parenleft"), "Ctrl+(")
        self.assertEqual(hotkeys.readable_trigger("Press <Shift><Control>1"), "Ctrl+Shift+1")
        self.assertEqual(hotkeys.readable_trigger("Shift+Ctrl+1"), "Shift+Ctrl+1")
        self.assertEqual(hotkeys.readable_trigger("Press <Hyper>x"), "<Hyper>x")
        self.assertEqual(hotkeys.readable_trigger(None), "")

    def test_what_gnome_reports_back_is_checked_for_it(self):
        portal = hotkeys.PortalHotkeys(lambda name: None,
                                       {"translate_text": "<Control><Shift>1",
                                        "ask_anything": "<Control><Shift>9"})
        listed = [("translate_text", {"trigger_description": "Press <Control>exclam"}),
                  ("ask_anything", {"trigger_description": "Shift+Ctrl+9"})]
        with mock.patch.object(session, "desktop", return_value="gnome"):
            portal._take(listed)
        self.assertIn("GNOME holds it as Ctrl+!,", portal.failed["translate_text"])
        self.assertEqual(portal.triggers["translate_text"], "Ctrl+!")
        self.assertNotIn("ask_anything", portal.failed)
        with mock.patch.object(session, "desktop", return_value="kde"):
            portal._take(listed)                # KDE's own way of naming it
        self.assertEqual(portal.failed, {})

    def test_another_layout_gives_its_own_symbol(self):
        class German:
            def get_entries_for_keyval(self, keyval):
                return True, [type("Key", (), {"keycode": 18, "group": 0})()]

            def translate_keyboard_state(self, keycode, state, group):
                return True, Gdk.KEY_parenright, 0, 1, 0

        self.assertEqual(shortcuts.portal_trigger("<Control><Shift>9", German()),
                         "CTRL+parenright")


class GnomeInAnyLanguage(unittest.TestCase):
    """GNOME's portal describes the key it holds for a shortcut in a sentence of the
    session's language, the key in GTK's syntax. These are xdg-desktop-portal-gnome 50's
    own translations of "Press %s" and "Press %s or %s". Only the English "Press " was
    taken off, so in any other language the key 3.4.0 left behind - Ctrl+( for
    Ctrl+Shift+9, which no key press matches - was never found: nothing was said, and
    the menu showed the key as working. The layout is a US one, with no display."""

    ONE = ("Press %s", "Drücken Sie %s", "Appuyer sur %s", "%s を押す", "按 %s",
           "%s 키를 누르십시오")
    TWO = ("Press %s or %s", "Drücken Sie %s oder %s", "Appuyer sur %s ou %s",
           "Pulse %s o %s", "%s または %s を押す", "按 %s 或 %s")

    def setUp(self):
        us = {"1": "exclam", "5": "percent", "9": "parenleft"}
        patcher = mock.patch.object(shortcuts, "shifted",
                                    lambda key, keymap=None: us.get(key))
        patcher.start()
        self.addCleanup(patcher.stop)

    def take(self, said, wanted="<Control><Shift>9"):
        """(key shown, problem) once GNOME has said `said` of ask_anything's key."""
        portal = hotkeys.PortalHotkeys(lambda name: None, {"ask_anything": wanted})
        with mock.patch.object(session, "desktop", return_value="gnome"):
            portal._take([("ask_anything", {"trigger_description": said})])
        return portal.triggers["ask_anything"], portal.failed.get("ask_anything")

    def test_the_key_3_4_0_left_is_found_in_every_language(self):
        for sentence in self.ONE:
            with self.subTest(sentence=sentence):
                shown, problem = self.take(sentence % "<Control>parenleft")
                # What the app's notice about such keys looks for.
                self.assertTrue((problem or "").startswith(
                    "GNOME holds it as Ctrl+(, which no key press matches"), problem)
                self.assertEqual(shown, "Ctrl+(")
                shown, problem = self.take(sentence % "<Control><Alt>percent",
                                           "<Control><Alt><Shift>5")
                self.assertTrue((problem or "").startswith("GNOME holds it as Ctrl+Alt+%"),
                                problem)

    def test_any_other_key_is_the_one_gnome_bound_in_every_language(self):
        for sentence in self.ONE:
            for held, wanted in (("<Shift><Control>9", "Ctrl+Shift+9"),
                                 ("<Control><Alt>t", "Ctrl+Alt+T"),
                                 ("<Super>F5", "Super+F5"), ("F5", "F5"),
                                 ("<Control>exclam", "Ctrl+!")):
                with self.subTest(sentence=sentence, held=held):
                    self.assertEqual(self.take(sentence % held), (wanted, None))

    def test_of_two_keys_gnome_holds_one_that_works_is_enough(self):
        for sentence in self.TWO:
            with self.subTest(sentence=sentence):
                self.assertEqual(
                    self.take(sentence % ("<Control>parenleft", "<Shift><Control>9")),
                    ("Ctrl+( or Ctrl+Shift+9", None))
                shown, problem = self.take(sentence % ("<Control>parenleft",
                                                       "<Control>parenleft"))
                self.assertTrue((problem or "").startswith(
                    "GNOME holds it as Ctrl+( or Ctrl+(,"), problem)

    def test_no_word_of_the_sentence_is_taken_for_a_key(self):
        # Spanish's "o", for "or", is also the name of the O key; a key with no modifier
        # is read only when it is a function key, the one kind the app binds alone.
        self.assertEqual(hotkeys.readable_trigger("Pulse F5 o F6"), "F5 or F6")
        self.assertEqual(hotkeys.readable_trigger("Pulse <Control>a o <Control>e"),
                         "Ctrl+A or Ctrl+E")
        # KDE's own descriptions, which have no key in GTK's syntax, stay as they are.
        for kde in ("Ctrl+(", "Strg+Umschalt+F5", "Meta+F5, Ctrl+F6"):
            self.assertEqual(hotkeys.readable_trigger(kde), kde)
        self.assertEqual(hotkeys.readable_trigger(""), "")


def xkb_keysym(name):
    """What libxkbcommon - every desktop's reader of key names - makes of `name`: 0 for
    a name it does not know, None where the library is not installed."""
    import ctypes
    try:
        xkb = ctypes.CDLL("libxkbcommon.so.0")
    except OSError:
        return None
    xkb.xkb_keysym_from_name.restype = ctypes.c_uint32
    xkb.xkb_keysym_from_name.argtypes = [ctypes.c_char_p, ctypes.c_int]
    return xkb.xkb_keysym_from_name(name.encode(), 0)


class KeysTheDesktopsRead(unittest.TestCase):
    """A key is written as libxkbcommon names it, which is how Hyprland, Sway, the
    portals and GNOME's and Cinnamon's window managers read one, and read back here. GDK
    calls an Armenian letter U+0561 and the calculator key Calculator, which none of them
    reads, and could not read U+0561 back itself, so such a shortcut was dropped."""

    CTRL, ALT = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.MOD1_MASK

    def test_a_letter_with_no_keysym_name_of_its_own(self):
        accel = shortcuts.from_keys(0x1000561, self.CTRL | self.ALT)     # Armenian ayb
        self.assertEqual(accel, "<Control><Alt>U0561")
        self.assertEqual(shortcuts.canonical(accel), accel)
        self.assertEqual(shortcuts.canonical("<Control><Alt>U+0561"), accel)    # GDK's
        self.assertEqual(shortcuts.label(accel), "Ctrl+Alt+Ա")
        self.assertEqual(shortcuts.hyprland(accel), ("CTRL ALT", "U0561"))
        self.assertEqual(shortcuts.sway(accel), "Ctrl+Mod1+U0561")
        self.assertEqual(shortcuts.portal_trigger(accel), "CTRL+ALT+U0561")
        self.assertEqual(hotkeys.X11Hotkeys.keysym("U0561"), 0x1000561)
        self.assertEqual(hotkeys.X11Hotkeys.keysym("U+0561"), 0x1000561)

    def test_an_xf86_key_keeps_its_xf86(self):
        accel = shortcuts.from_keys(0x1008ff1d, self.CTRL)
        self.assertEqual(accel, "<Control>XF86Calculator")
        self.assertEqual(shortcuts.canonical("<Control>Calculator"), accel)     # GDK's
        self.assertEqual(shortcuts.hyprland(accel), ("CTRL", "XF86Calculator"))
        self.assertEqual(shortcuts.sway(accel), "Ctrl+XF86Calculator")
        self.assertEqual(shortcuts.portal_trigger(accel), "CTRL+XF86Calculator")

    def test_a_key_gdk_can_only_number_is_refused(self):
        # XF86BrightnessAuto, which GDK has no name for: GNOME and Cinnamon would read
        # the number it gives instead as a key code.
        self.assertIsNone(shortcuts.from_keys(0x100810f4, self.CTRL))
        self.assertIn("no name", shortcuts.refused(0x100810f4, self.CTRL))

    def test_libxkbcommon_reads_every_name_written(self):
        if xkb_keysym("F5") is None:
            self.skipTest("libxkbcommon is not installed")
        for keyval in (0x1000561, 0x10010d0, 0x1000d85, 0x1008ff1d, 0x1008fe01, 0x6c6,
                       0x7e1, 0xfe52, 0x20ac, 0xffc2, 0x31, 0x61, 0x28, 0xff0d):
            name = shortcuts.key_name(keyval)
            self.assertEqual(xkb_keysym(name), keyval, name)
            self.assertEqual(shortcuts.key_value(name), keyval, name)
        for gdks in ("Calculator", "U+0561", "Switch_VT_1"):
            self.assertEqual(xkb_keysym(gdks), 0, gdks)


# ---- what is bound, and how ------------------------------------------------------

class WhatIsBound(TempDir):
    def test_new_installs_have_none(self):
        config = self.config()
        self.assertEqual(config.hotkeys, {})
        self.assertEqual(hotkeys.bindings(config), {})

    def test_invalid_and_duplicate_keys_and_the_encyclopedia_while_off(self):
        config = self.config()
        config.hotkeys = {"translate_text": "<Control><Shift>1", "ocr_only": "a",
                          "query_text": "<Primary><Shift>1",
                          "ask_encyclopedia": "<Control><Shift>8"}
        self.assertEqual(hotkeys.bindings(config),
                         {"translate_text": "<Control><Shift>1"})
        config.encyclopedia_enabled = True
        self.assertIn("ask_encyclopedia", hotkeys.bindings(config))

    def test_ask_anything_is_an_action(self):
        self.assertEqual(hotkeys.action_names()[0], "ask_anything")
        self.assertNotIn("ask_anything", hotkeys.LEGACY_KEYS)

    def test_a_key_in_gdks_spelling_is_bound_in_the_desktops(self):
        # As 3.4.0 wrote them: the first was dropped as invalid on every desktop.
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Alt>U+0561",
                          "query_text": "<Control>Calculator"}
        self.assertEqual(hotkeys.bindings(config),
                         {"ocr_only": "<Control><Alt>U0561",
                          "query_text": "<Control>XF86Calculator"})


class Mechanism(TempDir):
    def choose(self, mode="auto", **kw):
        config = self.config()
        config.hotkey_mode = mode
        with Patched(session_of(**kw)):
            return hotkeys.choose(config)[0]

    def test_auto(self):
        self.assertEqual(self.choose(desktop="cinnamon", kind="x11"), "x11")
        self.assertEqual(self.choose(desktop="kde", kind="x11", portal=True), "x11")
        self.assertEqual(self.choose(desktop="kde", portal=True), "portal")
        self.assertEqual(self.choose(desktop="gnome", portal=True), "portal")
        self.assertEqual(self.choose(desktop="gnome"), "desktop")
        self.assertEqual(self.choose(desktop="cinnamon"), "desktop")
        # Hyprland's portal binds nothing without hand-written `global` binds.
        self.assertEqual(self.choose(desktop="hyprland", portal=True), "desktop")
        self.assertEqual(self.choose(desktop="sway"), "desktop")
        self.assertEqual(self.choose(desktop="xfce"), "manual")
        self.assertEqual(self.choose(desktop="xfce", kind="x11", x11=False), "manual")

    def test_forced_and_off(self):
        self.assertEqual(self.choose("off", kind="x11"), "off")
        self.assertEqual(self.choose("desktop", desktop="gnome", kind="x11"), "desktop")
        self.assertEqual(self.choose("portal", desktop="hyprland", portal=True), "portal")
        self.assertEqual(self.choose("x11"), "off")
        self.assertEqual(self.choose("static", kind="x11"), "x11")     # from before


class Migration(TempDir):
    OLD = {"api_url": "https://nano-gpt.com/api/v1/chat/completions",
           "hotkey_mode": "auto", "encyclopedia_enabled": False}

    def migrate(self, mode, **kw):
        config = self.config(dict(self.OLD, hotkey_mode=mode))
        self.assertIsNone(config.hotkeys)
        with Patched(session_of(**kw)):
            self.assertTrue(hotkeys.migrate(config))
        reloaded = self.config()
        self.assertEqual(reloaded.hotkeys, config.hotkeys)
        self.assertEqual(reloaded.hotkey_mode, "auto")
        return reloaded

    def test_keys_that_worked_are_kept(self):
        self.assertEqual(self.migrate("auto", kind="x11").hotkeys, hotkeys.LEGACY_KEYS)
        self.assertEqual(self.migrate("portal", desktop="kde", portal=True).hotkeys,
                         hotkeys.LEGACY_KEYS)
        static = self.migrate("static", desktop="gnome")
        self.assertEqual(static.hotkeys, hotkeys.LEGACY_KEYS)
        self.assertEqual(static.hotkeys_installed, {"mechanism": "legacy"})

    def test_keys_that_never_worked_are_not_written_into_the_desktop_now(self):
        self.assertEqual(self.migrate("auto", desktop="cinnamon").hotkeys, {})
        self.assertEqual(self.migrate("auto", desktop="hyprland", portal=True).hotkeys,
                         {})
        self.assertEqual(self.migrate("off", kind="x11").hotkeys, {})

    def test_undecided_survives_a_save_until_migrated(self):
        config = self.config(dict(self.OLD))
        config.use_premium = True
        config.save()
        with open(os.path.join(self.dir, "c.json")) as f:
            self.assertNotIn("hotkeys", json.load(f))
        self.assertIsNone(config.hotkeys)
        self.assertIsNone(self.config().hotkeys)

    def test_a_decided_config_is_left_alone(self):
        config = self.config(dict(self.OLD, hotkeys={"ocr_only": "F5"}))
        with Patched(session_of(kind="x11")):
            self.assertFalse(hotkeys.migrate(config))
        self.assertEqual(config.hotkeys, {"ocr_only": "F5"})


# ---- the desktop's own keybindings -------------------------------------------------

LABELS = hotkeys.LABELS
NAMES = hotkeys.action_names()
CINNAMON_ITEM = "org.cinnamon.desktop.keybindings.custom-keybinding"


def gvariant_string(text):
    """What gsettings makes of `text` for a string key: it is parsed as GVariant text."""
    return GLib.Variant.parse(GLib.VariantType("s"), text, None, None).get_string()


def gnome_runs(commandline):
    """What GNOME's media keys run for a custom shortcut's `commandline`. They launch it
    through g_app_info_create_from_commandline, which makes it a desktop entry's Exec,
    expanded as GLib expands one: %% is a %, %c, %i and %k the app's name, icon and
    file, any other % code nothing, there being no files - and then split as the shell
    would."""
    out, i = [], 0
    while i < len(commandline):
        if commandline[i] == "%" and i + 1 < len(commandline):
            code = commandline[i + 1]
            out.append({"%": "%", "c": "'name'", "i": "--icon 'icon'", "k": "'file'"}
                       .get(code, ""))
            i += 2
            continue
        out.append(commandline[i])
        i += 1
    return GLib.shell_parse_argv("".join(out))[1]


CINNAMON_BASE = "/org/cinnamon/desktop/keybindings/custom-keybindings"


def cinnamon_name(action):
    """Ours in Cinnamon's list: custom-, and the CRC-32 of the name used before."""
    import zlib
    return f"custom-{zlib.crc32(f'da-assistant-{action}'.encode())}"


class FakeCinnamon(FakeHost):
    """gsettings, with Cinnamon watching: what is set is kept, and the shortcuts Cinnamon
    has bound are kept as Cinnamon keeps them (js/ui/keybindings.js, Cinnamon 6.6.9).
    It reads them again when custom-list changes, and only then: it lets go of those
    whose names have "custom" in them, and binds each one in the list again, in order -
    one whose keys are unchanged keeping the command it was bound with, and one whose
    keys another still holds not bound at all."""

    def __init__(self, listed="@as []", items=None, bound=None):
        super().__init__(listed)
        self.items = dict(items or {})      # item path -> {key: GVariant text}
        self.bound = dict(bound or {})      # name -> (keys, command)

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["gsettings", "get"]:
            if args[3] == "custom-list":
                return 0, self.listed + "\n"
            return 0, self.items.get(args[2], {}).get(args[3], "@as []") + "\n"
        if args[:2] == ["gsettings", "set"]:
            if args[3] == "custom-list":
                changed, self.listed = args[4] != self.listed, args[4]
                if changed:
                    self.rebuild()
            else:
                self.items.setdefault(args[2], {})[args[3]] = args[4]
        if args[:2] == ["gsettings", "reset-recursively"]:
            self.items.pop(args[2], None)
        return 0, ""

    def listing(self):
        return GLib.Variant.parse(None, self.listed, None, None).unpack()

    def rebuild(self):
        for name in [name for name in self.bound if "custom" in name]:
            del self.bound[name]
        for name in self.listing():
            if name == "__dummy__":
                continue
            item = self.items.get(f"{CINNAMON_ITEM}:{CINNAMON_BASE}/{name}/", {})
            keys = GLib.Variant.parse(None, item.get("binding", "@as []"), None,
                                      None).unpack()
            if name in self.bound:
                if self.bound[name][0] == keys:
                    continue
                del self.bound[name]
            held = {key for other, _command in self.bound.values() for key in other}
            if keys and not held & set(keys):
                self.bound[name] = (keys, gvariant_string(item.get("command", "''")))

    def ours(self):
        return {name: bound for name, bound in self.bound.items() if name != "custom0"}

    @classmethod
    def with_the_users(cls, *more, items=None, bound=None):
        """Cinnamon with a shortcut of the user's own, custom0, bound at login."""
        listed = ["custom0", *more]
        return cls(str(listed),
                   items={f"{CINNAMON_ITEM}:{CINNAMON_BASE}/custom0/":
                          {"binding": "['<Super>t']", "command": "'xterm'"},
                          **(items or {})},
                   bound={"custom0": (["<Super>t"], "xterm"), **(bound or {})})


class GSettingsWriter(unittest.TestCase):
    def test_cinnamon_adds_names_to_the_users_list(self):
        host = FakeHost("['custom0', '__dummy__']")
        writer = keybindings.GSettingsKeybindings("cinnamon", host)
        writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
        # And takes __dummy__ out again, as Cinnamon's own settings would.
        self.assertEqual(host.listed, f"['custom0', '{cinnamon_name('ocr_only')}']")
        path = f"{CINNAMON_ITEM}:{CINNAMON_BASE}/{cinnamon_name('ocr_only')}/"
        self.assertIn(["gsettings", "set", path, "binding", "['<Control><Shift>5']"],
                      host.calls)
        self.assertIn(["gsettings", "set", path, "name", "'DA Assistant: OCR only'"],
                      host.calls)
        command = gvariant_string(next(c for c in host.calls if c[3:4] == ["command"])[4])
        self.assertTrue(command.endswith(" --action=ocr_only"), command)
        self.assertIn("run-assistant", command)

    def test_gnome_lists_paths_and_stores_one_string(self):
        mine = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
        host = FakeHost(f"['{mine}']")
        writer = keybindings.GSettingsKeybindings("gnome", host)
        writer.apply({"query_image": "<Super>q"}, LABELS, NAMES)
        self.assertEqual(host.listed, f"['{mine}', '/org/gnome/settings-daemon/plugins/"
                         "media-keys/custom-keybindings/da-assistant-query_image/']")
        self.assertTrue(any(c[3:5] == ["binding", "'<Super>q'"] for c in host.calls))

    def test_a_command_quoted_for_the_shell_is_still_one_string_to_gsettings(self):
        # shlex quotes a path with a space, and a value that begins with a quote is one
        # gsettings will not take as a bare string: "expected end of input".
        host = FakeHost()
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/My Apps/DA.AppImage"}):
            keybindings.GSettingsKeybindings("gnome", host).apply(
                {"ocr_only": "F5"}, LABELS, NAMES)
            wanted = keybindings.command("ocr_only")
        sent = next(c for c in host.calls if c[3:4] == ["command"])[4]
        self.assertNotEqual(sent, wanted)
        self.assertEqual(gvariant_string(sent), wanted)

    def test_a_percent_in_the_launchers_path_reaches_gnome_as_it_is(self):
        # gsd-media-keys runs a custom shortcut through g_app_info_create_from_
        # commandline, which expands field codes: 50%off ran 50ff, pct%u ran pct and
        # 100%% ran 100%.
        for path in ("/home/u/50%off/DA.AppImage", "/home/u/pct%u/DA.AppImage",
                     "/home/u/100%%/DA.AppImage", "/home/u/My %c Apps/DA.AppImage"):
            for desktop, runs in (("gnome", gnome_runs),
                                  ("cinnamon", lambda line: GLib.shell_parse_argv(line)[1])):
                with self.subTest(path=path, desktop=desktop):
                    host = FakeHost()
                    with mock.patch.dict(os.environ, {"APPIMAGE": path}):
                        keybindings.GSettingsKeybindings(desktop, host).apply(
                            {"ocr_only": "F5"}, LABELS, NAMES)
                    sent = next(c for c in host.calls if c[3:4] == ["command"])[4]
                    self.assertEqual(runs(gvariant_string(sent)),
                                     [path, "--action=ocr_only"])

    def test_what_gsettings_said_is_in_the_error(self):
        def refuses(args):
            if args[:2] == ["gsettings", "set"]:
                return 1, ("expected end of input:\n  '/a b' --action=ocr_only\n"
                           "        ^\n")
            return 0, "@as []\n"

        with self.assertRaises(OSError) as raised:
            keybindings.GSettingsKeybindings("cinnamon", refuses).apply(
                {"ocr_only": "F5"}, LABELS, NAMES)
        self.assertIn("name failed: expected end of input: '/a b' --action=ocr_only",
                      str(raised.exception))
        self.assertNotIn("^", str(raised.exception))

    def test_twice_is_once_and_an_unbound_one_is_taken_out(self):
        base = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
        host = FakeHost(f"['{base}/da-assistant-ocr_only/', '{base}/custom0/', "
                        f"'{base}/da-assistant-query_text/']")
        writer = keybindings.GSettingsKeybindings("gnome", host)
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        self.assertEqual(host.listed, f"['{base}/custom0/', '{base}/da-assistant-ocr_only/']")
        self.assertTrue(any(c[1] == "reset-recursively" and "query_text" in c[2]
                            for c in host.calls))
        before = len(host.sets("custom-keybindings"))
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        self.assertEqual(len(host.sets("custom-keybindings")), before)  # unchanged: not set

    def test_remove_keeps_the_users_own_and_writes_nothing_if_none_are_ours(self):
        host = FakeHost(f"['custom0', '{cinnamon_name('ocr_only')}']")
        writer = keybindings.GSettingsKeybindings("cinnamon", host)
        writer.remove(NAMES)
        self.assertEqual(host.listed, "['custom0', '__dummy__']")
        self.assertFalse(writer.unfinished)
        host = FakeHost("@as []")
        keybindings.GSettingsKeybindings("cinnamon", host).remove(NAMES)
        self.assertEqual([c for c in host.calls if c[1] != "get"], [])

    def test_gnome_s_list_is_reset_when_nothing_is_left_in_it(self):
        # Measured on GNOME 50: the list was unset before anything was bound, and an
        # explicit empty list written over it after - the same value, but not dconf as
        # the user had it.
        host = FakeHost()
        writer = keybindings.GSettingsKeybindings("gnome", host)
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        writer.remove(NAMES)
        self.assertIn(["gsettings", "reset", "org.gnome.settings-daemon.plugins.media-keys",
                       "custom-keybindings"], host.calls)
        self.assertEqual(host.listed, "@as []")
        host = FakeHost("['/org/gnome/theirs/']")       # theirs stays, as it was
        writer = keybindings.GSettingsKeybindings("gnome", host)
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        writer.remove(NAMES)
        self.assertEqual(host.listed, "['/org/gnome/theirs/']")

    def test_quotes_in_the_users_list_survive(self):
        host = FakeHost("['it\\'s mine']")
        keybindings.GSettingsKeybindings("cinnamon", host).apply(
            {"ocr_only": "F5"}, LABELS, NAMES)
        self.assertEqual(host.listed,
                         f"['it\\'s mine', '{cinnamon_name('ocr_only')}', '__dummy__']")


class CinnamonTakesEveryChange(unittest.TestCase):
    """Cinnamon reads its shortcuts again only when custom-list changes, lets go of only
    those named "custom…", and keeps the command one was bound with while its keys stay
    the same. Ours were named da-assistant-…, and the list written only when what was in
    it changed: a changed key, a cleared one and a moved AppImage all waited for the next
    login."""

    def writer(self, host):
        return keybindings.GSettingsKeybindings("cinnamon", host)

    def test_a_changed_key_a_cleared_one_and_a_moved_launcher_reach_it_at_once(self):
        host = FakeCinnamon.with_the_users()
        name = cinnamon_name("ocr_only")
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/DA.AppImage"}):
            self.writer(host).apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
            self.assertEqual(host.ours(), {name: (["<Control><Shift>5"],
                                                  "/home/u/DA.AppImage --action=ocr_only")})
            self.writer(host).apply({"ocr_only": "<Control><Shift>6"}, LABELS, NAMES)
            self.assertEqual(host.ours()[name][0], ["<Control><Shift>6"])
        with mock.patch.dict(os.environ, {"APPIMAGE": "/opt/DA.AppImage"}):
            self.writer(host).apply({"ocr_only": "<Control><Shift>6"}, LABELS, NAMES)
            self.assertEqual(host.ours()[name][1], "/opt/DA.AppImage --action=ocr_only")
            self.writer(host).apply({}, LABELS, NAMES)
        self.assertEqual(host.ours(), {})
        self.assertEqual(host.bound["custom0"], (["<Super>t"], "xterm"))   # the user's

    def test_its_own_settings_can_read_every_name_in_the_list(self):
        # cinnamon-settings numbers a new shortcut from int(name.replace("custom", ""))
        # of every name but __dummy__: with da-assistant-… in the list, its Add custom
        # shortcut raised ValueError and added nothing. Its own are custom0 and on.
        host = FakeCinnamon("['custom0', 'custom1']")
        self.writer(host).apply({name: f"<Control><Shift>{n}"
                                 for n, name in enumerate(NAMES[:6])}, LABELS, NAMES)
        numbers = [int(entry.replace("custom", "")) for entry in host.listing()
                   if entry != "__dummy__"]
        self.assertEqual(numbers[:2], [0, 1])
        self.assertTrue(all(number < 0 for number in numbers[2:]), numbers)
        self.assertEqual(len(set(numbers)), len(numbers))

    def test_entries_from_before_are_let_go_of_and_then_taken_out(self):
        # As 3.4.0 left them: bound by Cinnamon at login under names no rebuild touches.
        old = "da-assistant-ocr_only"
        item = f"{CINNAMON_ITEM}:{CINNAMON_BASE}/{old}/"
        host = FakeCinnamon.with_the_users(
            old, items={item: {"binding": "['<Control><Shift>5']",
                               "command": "'/old/run-assistant --action=ocr_only'"}},
            bound={old: (["<Control><Shift>5"], "/old/run-assistant --action=ocr_only")})
        writer = self.writer(host)
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/DA.AppImage"}):
            writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
        new = cinnamon_name("ocr_only")
        self.assertEqual(host.ours(), {new: (["<Control><Shift>5"],
                                             "/home/u/DA.AppImage --action=ocr_only")})
        # Listed, empty and ahead of ours, for that rebuild; taken out at a later one.
        self.assertEqual(host.listing(), ["custom0", old, new, "__dummy__"])
        self.assertNotIn(item, host.items)
        self.assertTrue(writer.unfinished)
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/DA.AppImage"}):
            writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES, settled=True)
        self.assertEqual(host.listing(), ["custom0", new])
        self.assertFalse(writer.unfinished)
        self.assertEqual(set(host.bound), {"custom0", new})


def memory_gsettings(args):
    """The host's own gsettings, kept away from the desktop's settings: on the memory
    backend it opens no dconf database, and whatever it sets is gone when it exits."""
    if args[0] != "gsettings":
        raise AssertionError(f"only gsettings is run here, not {args[0]}")
    with mock.patch.dict(os.environ, {"GSETTINGS_BACKEND": "memory"}):
        env = keybindings.hostenv.host_env() or os.environ
        if env.get("GSETTINGS_BACKEND") != "memory":
            raise AssertionError("gsettings would reach the desktop's own settings")
        return keybindings.run_host(args)


class RealGSettings(unittest.TestCase):
    """What is written is what the host's own gsettings takes, run on its memory backend.

    Every gsettings is a process of its own, and the memory backend keeps nothing from
    one to the next, so a value cannot be read back with `gsettings get`. That gsettings
    accepted it, and that GVariant's parser gives back exactly the command, is the test.
    """

    PATHS = ("/home/u/My Apps/DA Assistant.AppImage",
             "/home/u/Jason's Apps/DA.AppImage",
             "/home/u/Téléchargements/DA.AppImage",
             "/home/u/$HOME/DA.AppImage")

    def writer(self, desktop):
        if not shutil.which("gsettings"):
            self.skipTest("gsettings is not installed")
        item = keybindings.GSettingsKeybindings.SCHEMAS[desktop][2]
        code, schemas = memory_gsettings(["gsettings", "list-relocatable-schemas"])
        if code != 0 or item not in schemas.split():
            self.skipTest(f"no {item} schema here")
        calls = []

        def run(args):
            calls.append(list(args))
            return memory_gsettings(args)

        return keybindings.GSettingsKeybindings(desktop, run), calls

    def test_gsettings_takes_every_value_and_reads_the_command_as_written(self):
        for desktop in ("gnome", "cinnamon"):
            for path in self.PATHS:
                with self.subTest(desktop=desktop, path=path):
                    writer, calls = self.writer(desktop)
                    with mock.patch.dict(os.environ, {"APPIMAGE": path}):
                        # Raises OSError if gsettings refused any of it.
                        writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
                        wanted = keybindings.command("ocr_only")
                    sent = {c[3]: c[4] for c in calls if c[1] == "set"}
                    self.assertEqual(gvariant_string(sent["command"]), wanted)
                    self.assertEqual(GLib.shell_parse_argv(wanted)[1][0], path)
                    self.assertEqual(gvariant_string(sent["name"]),
                                     "DA Assistant: OCR only")

    def test_the_bare_command_is_refused_and_the_error_says_why(self):
        writer, _calls = self.writer("gnome")
        with mock.patch.dict(os.environ, {"APPIMAGE": self.PATHS[0]}):
            bare = keybindings.command("ocr_only")
        with self.assertRaises(OSError) as raised:
            writer._set(f"{writer.item_schema}:{writer.base}/da-assistant-x/", "command",
                        bare)
        self.assertIn("expected end of input", str(raised.exception))


def hyprlang(line):
    """`line` as Hyprland's config language reads it: ## is a literal #, any other #
    begins a comment - inside shell quotes too - and what is left is trimmed."""
    out, i = [], 0
    while i < len(line):
        if line[i] == "#":
            if line[i + 1:i + 2] != "#":
                break
            i += 1
        out.append(line[i])
        i += 1
    return "".join(out).strip()


def hyprlang_variables(text, variables):
    """`text` with $name, for each variable the config defines, made its value - as
    Hyprland's config language does, in quotes or not, the longest name first."""
    for name in sorted(variables, key=len, reverse=True):
        text = text.replace("$" + name, variables[name])
    return text


def sway_variables(text, variables):
    """`text` after Sway's variable replacement, which it makes as it reads the config
    and again as a key runs its command: $$ is a $ left alone, and $name, for a
    variable set in the config, its value, in quotes or not."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("$$", i):
            out.append("$")
            i += 2
            continue
        name = next((name for name in sorted(variables, key=len, reverse=True)
                     if text.startswith(name, i)), None)
        out.append(variables[name] if name else text[i])
        i += len(name) if name else 1
    return "".join(out)


def sway_commands(command):
    """A bindsym's `command` as Sway runs it: split into several at every ; and , that
    is outside quotes, a backslash escaping the character after it."""
    found, start, quote, escaped = [], 0, None, False
    for i, char in enumerate(command):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in ";,":
            found.append(command[start:i])
            start = i + 1
    found.append(command[start:])
    return [part.strip() for part in found if part.strip()]


class ConfigFileWriter(TempDir):
    OLD_BLOCK = ("# --- DA Assistant hotkeys (generated by tools/install-hotkeys.sh) ---\n"
                 "bind = CTRL SHIFT, 5, exec, /old/run-assistant --action=ocr_only\n"
                 "# --- end DA Assistant hotkeys ---\n")

    def writer(self, text, desktop="hyprland"):
        path = os.path.join(self.dir, "hyprland.conf")
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o640)
        self.host = FakeHost()
        return keybindings.ConfigFileKeybindings(desktop, self.host, path), path

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_the_old_scripts_block_is_replaced_and_a_backup_kept(self):
        writer, path = self.writer("monitor=,preferred,auto,1\n\n" + self.OLD_BLOCK +
                                   "bind = SUPER, Q, exec, kitty\n")
        writer.apply({"query_image": "<Control><Alt>q"}, LABELS, NAMES)
        text = self.read(path)
        self.assertIn("monitor=,preferred,auto,1", text)
        self.assertIn("bind = SUPER, Q, exec, kitty", text)
        self.assertNotIn("/old/run-assistant", text)
        self.assertIn("bind = CTRL ALT, q, exec, ", text)
        self.assertIn("--action=query_image  # Query image selection", text)
        self.assertEqual(text.count("# --- DA Assistant hotkeys"), 1)
        self.assertIn("/old/run-assistant", self.read(path + ".da-assistant.bak"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertIn(["hyprctl", "reload"], self.host.calls)

    def test_nothing_changed_nothing_written(self):
        writer, path = self.writer("bind = SUPER, Q, exec, kitty\n")
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        os.unlink(path + ".da-assistant.bak")
        self.host.calls.clear()
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        self.assertFalse(os.path.exists(path + ".da-assistant.bak"))
        self.assertEqual(self.host.calls, [])

    def test_remove_strips_the_block(self):
        writer, path = self.writer("bind = SUPER, Q, exec, kitty\n\n" + self.OLD_BLOCK)
        writer.remove(NAMES)
        self.assertEqual(self.read(path), "bind = SUPER, Q, exec, kitty\n")

    def test_with_nothing_to_write_and_none_of_ours_there_the_file_is_left_alone(self):
        # A first start with no shortcuts chosen rewrote a config that did not end in
        # exactly one newline, overwrote its backup and reloaded the compositor.
        for text in ("bind = SUPER, Q, exec, kitty", "bind = SUPER, Q, exec, kitty\n\n\n",
                     "", "\n", "# mine\n\n"):
            for desktop in ("hyprland", "sway"):
                with self.subTest(text=text, desktop=desktop):
                    writer, path = self.writer(text, desktop)
                    writer.apply({}, LABELS, NAMES)
                    writer.remove(NAMES)
                    self.assertEqual(self.read(path), text)
                    self.assertFalse(os.path.exists(path + ".da-assistant.bak"))
                    self.assertEqual(self.host.calls, [])

    def test_a_block_goes_after_the_users_lines_and_comes_out_whole(self):
        for text in ("bind = SUPER, Q, exec, kitty", "bind = SUPER, Q, exec, kitty\n\n\n"):
            with self.subTest(text=text):
                writer, path = self.writer(text)
                writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
                self.assertTrue(self.read(path).startswith(
                    "bind = SUPER, Q, exec, kitty\n\n# --- DA Assistant hotkeys"))
                writer.remove(NAMES)
                self.assertEqual(self.read(path), "bind = SUPER, Q, exec, kitty\n")

    def test_a_hash_in_the_launchers_path_is_doubled_for_hyprland(self):
        writer, path = self.writer("")
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/C# Apps/DA#1.AppImage"}):
            writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
            wanted = keybindings.command("ocr_only")
        bind = next(line for line in self.read(path).splitlines()
                    if line.startswith("bind"))
        self.assertEqual(bind, "bind = CTRL SHIFT, 5, exec, "
                               "'/home/u/C## Apps/DA##1.AppImage' --action=ocr_only"
                               "  # OCR only")
        self.assertEqual(hyprlang(bind), f"bind = CTRL SHIFT, 5, exec, {wanted}")

    def test_sway(self):
        writer, path = self.writer("", desktop="sway")
        writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
        self.assertIn("bindsym Ctrl+Shift+5 exec ", self.read(path))
        self.assertIn(["swaymsg", "reload"], self.host.calls)

    def bind(self, path):
        return next(line for line in self.read(path).splitlines()
                    if line.startswith(("bind ", "bindsym ")))

    def test_an_actions_name_is_only_ever_a_comment(self):
        # A Gtk.Entry keeps a pasted line break: the second line of the name became a
        # line of the config, and Hyprland runs `exec =` lines at every reload. Sway
        # runs what follows the key as its command, and splits it into several at ;
        # and , - an action called "Explain; exit" ended the session at its key.
        named = dict(LABELS, custom_1="Explain\nexec = notify-send injected\r\x85 ; "
                                      "exit, now\\")
        for desktop in ("hyprland", "sway"):
            with self.subTest(desktop=desktop):
                writer, path = self.writer("", desktop=desktop)
                writer.apply({"custom_1": "<Control><Shift>7"}, named, NAMES)
                lines = self.read(path).splitlines()
                self.assertEqual(len(lines), 3 if desktop == "hyprland" else 4, lines)
                self.assertFalse([line for line in lines if "exec =" in line.split("#")[0]])
                bind = self.bind(path)
                if desktop == "hyprland":
                    self.assertTrue(bind.endswith(
                        "--action=custom_1  # Explain exec = notify-send injected ; exit, "
                        "now"), bind)
                    self.assertTrue(hyprlang(bind).endswith("--action=custom_1"))
                else:
                    self.assertEqual(lines[1], "# DA Assistant: Explain exec = notify-send "
                                               "injected ; exit, now")
                    command = bind.split(" ", 2)[2]
                    self.assertEqual(sway_commands(command), [command])
                    self.assertTrue(command.endswith("--action=custom_1"), command)

    PATHS = ("/home/u/Apps,old/DA.AppImage", "/home/u/R&D; tests/DA.AppImage",
             "/home/u/$mod/DA.AppImage", "/home/u/$mainMod $HOME$$/DA.AppImage",
             "/home/u/Jason's \"Apps\"/DA.AppImage", "/home/u/C# Apps/DA#1.AppImage",
             "/home/u/[x] {y} (z)/DA.AppImage", "/home/u/Apps/DA.AppImage")

    def test_any_path_reaches_the_shell_whole_from_sway(self):
        variables = {"$mod": "Mod4", "$mainMod": "Mod4", "$HOME": "/root"}
        for launcher in self.PATHS:
            with self.subTest(launcher=launcher):
                writer, path = self.writer("", desktop="sway")
                with mock.patch.dict(os.environ, {"APPIMAGE": launcher}):
                    writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
                read = sway_variables(self.bind(path), variables)         # the config
                commands = sway_commands(read.split(" ", 2)[2])            # the key
                self.assertEqual(len(commands), 1, commands)
                self.assertTrue(commands[0].startswith("exec "), commands)
                run = sway_variables(commands[0][len("exec "):], variables)
                self.assertEqual(GLib.shell_parse_argv(run)[1],
                                 [launcher, "--action=ocr_only"])

    def test_any_path_reaches_the_shell_whole_from_hyprland(self):
        variables = {"mod": "SUPER", "mainMod": "SUPER", "HOME": "/root"}
        for launcher in self.PATHS:
            with self.subTest(launcher=launcher):
                writer, path = self.writer("")
                with mock.patch.dict(os.environ, {"APPIMAGE": launcher}):
                    writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
                value = hyprlang_variables(hyprlang(self.bind(path)).split("=", 1)[1],
                                           variables)
                mods, key, dispatcher, run = (part.strip() for part in value.split(",", 3))
                self.assertEqual((mods, key, dispatcher), ("CTRL SHIFT", "5", "exec"))
                self.assertEqual(GLib.shell_parse_argv(run)[1],
                                 [launcher, "--action=ocr_only"])

    def test_a_path_no_line_of_the_config_can_hold_is_refused(self):
        for desktop in ("hyprland", "sway"):
            for launcher, what in (("/home/u/two\nlines/DA.AppImage", "line break"),
                                   ("/home/u/tab\there/DA.AppImage", "control character"),
                                   ("/home/u/back\\slash/DA.AppImage", "backslash")):
                with self.subTest(desktop=desktop, launcher=launcher):
                    writer, path = self.writer("bind = SUPER, Q, exec, kitty\n", desktop)
                    with mock.patch.dict(os.environ, {"APPIMAGE": launcher}), \
                            self.assertRaises(OSError) as raised:
                        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
                    self.assertIn(what, str(raised.exception))
                    self.assertIn("Nothing was changed", str(raised.exception))
                    self.assertEqual(self.read(path), "bind = SUPER, Q, exec, kitty\n")
                    self.assertEqual(self.host.calls, [])
        # Lua strings hold anything.
        lua = os.path.join(self.dir, "hyprland.lua")
        with open(lua, "w") as f:
            f.write("-- mine\n")
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/back\\slash\n/DA.AppImage"}):
            keybindings.ConfigFileKeybindings("hyprland", FakeHost(), lua).apply(
                {"ocr_only": "F5"}, LABELS, NAMES)
        with open(lua) as f:
            self.assertIn("--action=ocr_only", f.read())

    def test_a_path_never_ends_the_block_or_puts_a_line_outside_it(self):
        # Hyprland and Sway read a line as far as "\n", and a path with U+2028 in it is
        # one more they can run. The next rewrite read the line as str.splitlines does,
        # broken there, and an end marker in the path ended the block: in Sway's config
        # the rest of it was kept as a line of the user's own, a command of its own.
        for sep in ("\u2028", "\u2029"):
            for desktop in ("hyprland", "sway"):
                with self.subTest(sep=hex(ord(sep)), desktop=desktop):
                    writer, path = self.writer("bind = SUPER, Q, exec, kitty\n", desktop)
                    launcher = (f"/home/u/x{sep}{keybindings.END}{sep}"
                                f"exec_always notify-send injected/DA.AppImage")
                    with mock.patch.dict(os.environ, {"APPIMAGE": launcher}):
                        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
                        writer.apply({"ocr_only": "F6"}, LABELS, NAMES)
                        lines = self.read(path).split("\n")
                        self.assertFalse([line for line in lines
                                          if line.startswith("exec_always")], lines)
                        self.assertEqual(len([line for line in lines
                                              if line.startswith("bind")]), 2, lines)
                        writer.remove(NAMES)
                    self.assertEqual(self.read(path), "bind = SUPER, Q, exec, kitty\n")

    def test_a_missing_config_is_not_created(self):
        writer = keybindings.ConfigFileKeybindings(
            "hyprland", FakeHost(), os.path.join(self.dir, "none.conf"))
        with self.assertRaises(OSError):
            writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        writer.apply({}, LABELS, NAMES)                 # nothing to add: nothing to say
        self.assertFalse(os.path.exists(os.path.join(self.dir, "none.conf")))

    def test_an_end_marker_an_editor_has_touched_still_ends_the_block(self):
        for end in ("# --- end DA Assistant hotkeys --- \n",
                    "    # --- end DA Assistant hotkeys ---\n",
                    "\t# --- end DA Assistant hotkeys ---\t \n"):
            with self.subTest(end=end):
                writer, path = self.writer(self.OLD_BLOCK.replace(
                    "# --- end DA Assistant hotkeys ---\n", end)
                    + "bind = SUPER, Q, exec, kitty\n")
                writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
                text = self.read(path)
                self.assertIn("bind = SUPER, Q, exec, kitty\n", text)
                self.assertNotIn("/old/run-assistant", text)
                self.assertEqual(text.count("end DA Assistant hotkeys"), 1)

    def test_a_block_with_no_end_leaves_the_file_and_its_backup_alone(self):
        unended = ("monitor=,preferred,auto,1\n"
                   + self.OLD_BLOCK.replace("# --- end DA Assistant hotkeys ---\n", "")
                   + "bind = SUPER, Q, exec, kitty\n")
        for text in (unended, unended + self.OLD_BLOCK):
            with self.subTest(text=text):
                writer, path = self.writer(text)
                with open(path + ".da-assistant.bak", "w") as f:
                    f.write("the good copy\n")
                for step in (lambda: writer.apply({"ocr_only": "F5"}, LABELS, NAMES),
                             lambda: writer.remove(NAMES)):
                    with self.assertRaises(OSError) as raised:
                        step()
                    self.assertIn("line 2", str(raised.exception))
                    self.assertEqual(self.read(path), text)
                    self.assertEqual(self.read(path + ".da-assistant.bak"),
                                     "the good copy\n")
                self.assertEqual(self.host.calls, [])
                self.assertEqual(sorted(os.listdir(self.dir)),
                                 ["hyprland.conf", "hyprland.conf.da-assistant.bak"])

    def test_a_config_that_is_a_link_is_written_through_it(self):
        dotfiles = os.path.join(self.dir, "dotfiles")
        os.makedirs(dotfiles)
        real = os.path.join(dotfiles, "hyprland.conf")
        with open(real, "w") as f:
            f.write("bind = SUPER, Q, exec, kitty\n")
        link = os.path.join(self.dir, "hyprland.conf")
        os.symlink(real, link)
        writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost(), link)
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), real)
        self.assertIn("--action=ocr_only", self.read(real))
        self.assertEqual(self.read(link + ".da-assistant.bak"),
                         "bind = SUPER, Q, exec, kitty\n")
        self.assertFalse(os.path.islink(link + ".da-assistant.bak"))
        self.assertEqual(os.listdir(dotfiles), ["hyprland.conf"])     # no temporary left
        writer.remove(NAMES)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(self.read(real), "bind = SUPER, Q, exec, kitty\n")

    @unittest.skipIf(os.geteuid() == 0, "root writes read-only files all the same")
    def test_a_link_into_somewhere_read_only_says_so_and_writes_nothing(self):
        store = os.path.join(self.dir, "store")
        os.makedirs(store)
        real = os.path.join(store, "hyprland.conf")
        with open(real, "w") as f:
            f.write("bind = SUPER, Q, exec, kitty\n")
        os.chmod(real, 0o444)
        os.chmod(store, 0o555)
        self.addCleanup(os.chmod, store, 0o755)
        link = os.path.join(self.dir, "hyprland.conf")
        os.symlink(real, link)
        host = FakeHost()
        writer = keybindings.ConfigFileKeybindings("hyprland", host, link)
        with self.assertRaises(OSError) as raised:
            writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        self.assertIn(f"a link to {os.path.realpath(real)}, cannot be written",
                      str(raised.exception))
        self.assertEqual(self.read(real), "bind = SUPER, Q, exec, kitty\n")
        self.assertFalse(os.path.exists(link + ".da-assistant.bak"))
        self.assertEqual(host.calls, [])

    def test_bytes_that_are_not_utf8_come_through_unchanged(self):
        latin1 = b"# Configur\xe9 \xa9 2019\nbind = SUPER, Q, exec, kitty\n"
        writer, path = self.writer("")
        with open(path, "wb") as f:
            f.write(latin1)
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        with open(path, "rb") as f:
            written = f.read()
        self.assertTrue(written.startswith(latin1), written)
        self.assertIn(b"--action=ocr_only", written)
        writer.remove(NAMES)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), latin1)


class KdeLegacy(TempDir):
    def test_the_old_launchers_are_removed(self):
        apps = os.path.join(self.dir, "applications")
        os.makedirs(apps)
        for name in ("da-assistant-ocr_only.desktop", "someone-else.desktop"):
            open(os.path.join(apps, name), "w").close()
        host = FakeHost()
        self.assertTrue(keybindings.remove_kde_legacy(host, self.dir))
        self.assertEqual(os.listdir(apps), ["someone-else.desktop"])
        self.assertTrue(any("da-assistant-ocr_only.desktop" in c and "--delete" in c
                            for c in host.calls))
        self.assertFalse(keybindings.remove_kde_legacy(FakeHost(), self.dir))


class Launcher(unittest.TestCase):
    def test_bindings_name_the_appimage_not_its_mount(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/Apps/DA Assistant.AppImage"}):
            self.assertEqual(keybindings.command("ocr_only"),
                             "'/home/u/Apps/DA Assistant.AppImage' --action=ocr_only")
        with mock.patch.dict(os.environ):
            os.environ.pop("APPIMAGE", None)
            self.assertEqual(keybindings.launcher(), os.path.join(ROOT, "run-assistant"))


class PortalAppEntry(TempDir):
    """The portal accepts the app's id only with a .desktop entry for it (KDE, seen on
    Plasma 6.7 with xdg-desktop-portal 1.22: "App info not found")."""

    def test_written_hidden_quoted_and_only_when_it_changes(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/u/My Apps/DA.AppImage"}):
            path = keybindings.app_entry(self.dir)
            self.assertEqual(os.path.basename(path), "org.digitalarchon.DAAssistant.desktop")
            with open(path) as f:
                text = f.read()
            self.assertIn('Exec="/home/u/My Apps/DA.AppImage"\n', text)
            self.assertIn("NoDisplay=true\n", text)
            os.utime(path, (1, 1))
            keybindings.app_entry(self.dir)
            self.assertEqual(os.stat(path).st_mtime, 1)                 # unchanged
        with mock.patch.dict(os.environ, {"APPIMAGE": "/opt/DA.AppImage"}):
            keybindings.app_entry(self.dir)                             # moved: rewritten
            with open(path) as f:
                self.assertIn("Exec=/opt/DA.AppImage\n", f.read())

    def test_any_path_reads_back_whole_and_glib_still_knows_the_app(self):
        """GLib's key file reader takes the value, the shell rules give the path back as
        one argument, and GLib loads the entry - which the portal knows the app by, and
        which GLib does only when the program it names exists. A single backslash before
        $, " or a backtick was refused by the key file reader outright."""
        data_home = os.path.join(self.dir, "data")
        for name in ("My Apps", 'say "hi"', "back`tick", "dol$lar", "back\\slash",
                     "50%off", "Jason's", "Téléchargements", "tab\there"):
            with self.subTest(name=name):
                launcher = os.path.join(self.dir, name, "DA.AppImage")
                os.makedirs(os.path.dirname(launcher))
                with open(launcher, "w") as f:
                    f.write("#!/bin/sh\n")
                os.chmod(launcher, 0o755)
                with mock.patch.dict(os.environ, {"APPIMAGE": launcher}):
                    entry = keybindings.app_entry(data_home)
                keyfile = GLib.KeyFile()
                keyfile.load_from_file(entry, GLib.KeyFileFlags.NONE)
                exec_line = keyfile.get_string("Desktop Entry", "Exec")
                self.assertEqual(GLib.shell_parse_argv(exec_line)[1], [launcher])
                try:
                    loaded = Gio.DesktopAppInfo.new_from_filename(entry)
                except TypeError:           # PyGObject's "constructor returned NULL"
                    loaded = None
                self.assertIsNotNone(loaded, f"GLib would not load Exec={exec_line}")


class Autostart(TempDir):
    """tools/install-autostart.sh, which the manual gives for starting at login from a
    checkout. Its recipe before - sed putting $PWD into Exec - made R&D into
    R@LAUNCHER@D, and left a path with a space unquoted."""

    def install(self, name, *args):
        """Run the script from a checkout under `name`: its own parts, linked."""
        root = os.path.join(self.dir, name, "DAAssistant")
        os.makedirs(root, exist_ok=True)
        for part in ("tools", "assistant", "run-assistant"):
            if not os.path.lexists(os.path.join(root, part)):
                os.symlink(os.path.join(ROOT, part), os.path.join(root, part))
        env = dict(os.environ, HOME=self.dir, XDG_CONFIG_HOME=os.path.join(self.dir, "cfg"))
        done = subprocess.run(["bash", os.path.join(root, "tools", "install-autostart.sh"),
                               *args], env=env, capture_output=True, text=True, timeout=60)
        return root, done

    def entry(self):
        return os.path.join(self.dir, "cfg", "autostart", "da-assistant.desktop")

    def test_a_checkout_anywhere_is_started_whole(self):
        for name in ("R&D", "My Apps", 'say "hi"', "dol$lar `x`", "back\\slash", "Jason's",
                     "a|b;c", "Téléchargements"):
            with self.subTest(name=name):
                root, done = self.install(name)
                self.assertEqual(done.returncode, 0, done.stderr)
                keyfile = GLib.KeyFile()
                keyfile.load_from_file(self.entry(), GLib.KeyFileFlags.NONE)
                exec_line = keyfile.get_string("Desktop Entry", "Exec")
                self.assertEqual(GLib.shell_parse_argv(exec_line)[1],
                                 [os.path.join(root, "run-assistant")])
                self.assertEqual(keyfile.get_string("Desktop Entry", "Icon"),
                                 os.path.join(root, "assistant/data/icons/hicolor/256x256/"
                                                    "apps/da-assistant.png"))
                try:
                    loaded = Gio.DesktopAppInfo.new_from_filename(self.entry())
                except TypeError:           # PyGObject's "constructor returned NULL"
                    loaded = None
                self.assertIsNotNone(loaded, f"GLib would not load Exec={exec_line}")

    def test_a_path_the_session_would_misread_is_refused_and_remove_removes(self):
        for name in ("50%off", "two\nlines"):
            with self.subTest(name=name):
                _root, done = self.install(name)
                self.assertEqual(done.returncode, 1)
                self.assertIn("nothing was", done.stderr)
                self.assertFalse(os.path.exists(self.entry()))
        self.install("fine")
        self.assertTrue(os.path.exists(self.entry()))
        _root, done = self.install("fine", "--remove")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(os.path.exists(self.entry()))


# ---- the manager -------------------------------------------------------------------

class Manager(TempDir):
    def manager(self, config, host):
        return hotkeys.HotkeyManager(config, lambda action: None, run=host)

    def test_desktop_bindings_are_written_once_and_recorded(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        host = FakeHost("['custom0']")
        with Patched(session_of(desktop="cinnamon")):
            manager = self.manager(config, host)
            manager.start()
            self.assertEqual(manager.mode, "desktop")
            self.assertEqual(manager.trigger_label("ocr_only"), "Ctrl+Shift+5")
            self.assertEqual(host.listed,
                             f"['custom0', '{cinnamon_name('ocr_only')}', '__dummy__']")
            self.assertEqual(self.config().hotkeys_installed["mechanism"],
                             "desktop:cinnamon")
            writes = len(host.writes())
            manager.start()
            self.assertEqual(len(host.writes()), writes)    # same as recorded: no write

    def test_an_action_renamed_is_written_again_under_its_new_name(self):
        config = self.config()
        config.custom_actions = [{"slot": 1, "name": "Summarise", "prompt": "Sum up {text}",
                                  "source": "clipboard"}]
        config.hotkeys = {"custom_1": "<Control><Shift>7"}
        host = FakeHost()
        with Patched(session_of(desktop="gnome")):
            self.manager(config, host).start()
            config.custom_actions[0]["name"] = "Summarize"
            self.manager(config, host).start()
        self.assertEqual([gvariant_string(c[4]) for c in host.calls if c[3:4] == ["name"]],
                         ["DA Assistant: Summarise", "DA Assistant: Summarize"])

    def test_switching_off_takes_them_back_out(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        host = FakeHost("['custom0']")
        with Patched(session_of(desktop="cinnamon")):
            self.manager(config, host).start()
            config.hotkey_mode = "off"
            manager = self.manager(config, host)
            manager.start()
        self.assertEqual(manager.mode, "off")
        self.assertEqual(manager.detail, "switched off in Settings > Advanced")
        self.assertEqual(host.listed, "['custom0']")
        self.assertEqual(self.config().hotkeys_installed, {})

    def test_an_x11_session_after_a_wayland_one_removes_the_desktops_copy(self):
        config = self.config()
        config.hotkeys = {}
        config.hotkeys_installed = {"mechanism": "desktop:gnome"}
        mine = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/c0/"
        host = FakeHost(f"['{mine}', '/org/gnome/settings-daemon/plugins/media-keys/"
                        f"custom-keybindings/da-assistant-ocr_only/']")
        with Patched(session_of(desktop="gnome", kind="x11")):
            self.manager(config, host).start()
        self.assertEqual(host.listed, f"['{mine}']")

    def test_the_old_scripts_bindings_are_taken_over(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        config.hotkeys_installed = {"mechanism": "legacy"}
        old = {action: f"{CINNAMON_ITEM}:{CINNAMON_BASE}/da-assistant-{action}/"
               for action in ("ocr_only", "query_text")}
        host = FakeCinnamon.with_the_users(
            "da-assistant-ocr_only", "da-assistant-query_text",
            items={old["ocr_only"]: {"binding": "['<Control><Shift>5']"},
                   old["query_text"]: {"binding": "['<Control><Shift>3']"}},
            bound={"da-assistant-ocr_only": (["<Control><Shift>5"], "old"),
                   "da-assistant-query_text": (["<Control><Shift>3"], "old")})
        new = cinnamon_name("ocr_only")
        with Patched(session_of(desktop="cinnamon")):
            self.manager(config, host).start()
            # Let go of by Cinnamon at once, and still listed for that.
            self.assertEqual(set(host.ours()), {new})
            self.assertEqual(host.listing(), ["custom0", "da-assistant-ocr_only",
                                              "da-assistant-query_text", new])
            self.assertTrue(self.config().hotkeys_installed["unfinished"])
            self.manager(config, host).start()                  # the next start
            self.assertEqual(host.listing(), ["custom0", new, "__dummy__"])
            self.assertNotIn("unfinished", self.config().hotkeys_installed)
            self.assertEqual(set(host.ours()), {new})
            calls = len(host.writes())
            self.manager(config, host).start()
            self.assertEqual(len(host.writes()), calls)          # done: nothing more

    def test_switched_off_with_entries_from_before_they_go_over_two_starts(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        config.hotkeys_installed = {"mechanism": "desktop:cinnamon"}
        config.hotkey_mode = "off"
        old = "da-assistant-ocr_only"
        host = FakeCinnamon.with_the_users(
            old, items={f"{CINNAMON_ITEM}:{CINNAMON_BASE}/{old}/":
                        {"binding": "['<Control><Shift>5']"}},
            bound={old: (["<Control><Shift>5"], "old")})
        with Patched(session_of(desktop="cinnamon")):
            self.manager(config, host).start()
            self.assertEqual(host.ours(), {})
            self.assertEqual(host.listing(), ["custom0", old, "__dummy__"])
            self.manager(config, host).start()
        self.assertEqual(host.listing(), ["custom0"])
        self.assertEqual(self.config().hotkeys_installed, {})

    def test_a_desktop_that_cannot_be_written_says_so(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "F5"}
        with Patched(session_of(desktop="hyprland")), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}):
            manager = self.manager(config, FakeHost())
            manager.start()
        self.assertIn("does not exist", manager.error)
        self.assertIsNone(manager.trigger_label("ocr_only"))
        self.assertEqual(self.config().hotkeys_installed, {})       # tried again next time

    def test_a_first_start_with_no_shortcuts_changes_nothing_in_the_desktop(self):
        os.makedirs(os.path.join(self.dir, "hypr"))
        path = os.path.join(self.dir, "hypr", "hyprland.conf")
        with open(path, "w") as f:
            f.write("bind = SUPER, Q, exec, kitty\n\n")
        host = FakeHost()
        with Patched(session_of(desktop="hyprland")), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}):
            manager = self.manager(self.config(), host)
            manager.start()
        self.assertEqual(manager.mode, "desktop")
        self.assertIsNone(manager.error)
        with open(path) as f:
            self.assertEqual(f.read(), "bind = SUPER, Q, exec, kitty\n\n")
        self.assertFalse(os.path.exists(path + ".da-assistant.bak"))
        self.assertEqual(host.calls, [])

    def test_a_config_with_latin1_in_it_is_written_and_the_app_starts(self):
        # Read as strict UTF-8 it raised UnicodeDecodeError out of start(), which runs
        # in the app's constructor: the app stopped at every start.
        os.makedirs(os.path.join(self.dir, "hypr"))
        path = os.path.join(self.dir, "hypr", "hyprland.conf")
        latin1 = b"# Configur\xe9 en 2019\nbind = SUPER, Q, exec, kitty\n"
        with open(path, "wb") as f:
            f.write(latin1)
        config = self.config()
        config.hotkeys = {"ocr_only": "F5"}
        with Patched(session_of(desktop="hyprland")), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}):
            manager = self.manager(config, FakeHost())
            manager.start()
        self.assertIsNone(manager.error)
        with open(path, "rb") as f:
            written = f.read()
        self.assertTrue(written.startswith(latin1), written)
        self.assertIn(b"--action=ocr_only", written)

    def test_nothing_a_desktop_writer_raises_gets_out_of_start(self):
        class Raising:
            def __init__(self, failure):
                self.failure = failure

            def describe(self):
                return "~/.config/hypr/hyprland.conf"

            def apply(self, *_args):
                raise self.failure

            remove = apply

        failures = (UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte"),
                    RuntimeError("the compositor said no"))
        for failure in failures:
            for earlier in (None, {"mechanism": "desktop:sway"}):
                with self.subTest(failure=type(failure).__name__, earlier=earlier):
                    config = self.config()
                    config.hotkeys = {"ocr_only": "F5"}
                    config.hotkeys_installed = earlier
                    changed = []
                    with Patched(session_of(desktop="hyprland")), \
                            mock.patch.object(keybindings, "writer",
                                              return_value=Raising(failure)), \
                            mock.patch("assistant.errors.record") as record:
                        manager = hotkeys.HotkeyManager(
                            config, lambda action: None,
                            on_change=lambda: changed.append(True), run=FakeHost())
                        manager.start()                         # does not raise
                    self.assertEqual(changed, [True])
                    if isinstance(failure, ValueError):          # expected and caught
                        self.assertFalse(record.called)
                        self.assertIn("invalid continuation byte", manager.error)
                    else:                                        # unforeseen: logged
                        self.assertEqual(manager.error, "the compositor said no")
                        self.assertIn("RuntimeError", record.call_args[0][0])


class FakeXDisplay:
    """A python-xlib Display with a keyboard of the test's own, which records the grabs
    made on its root window and is never connected to anything."""

    KEYBOARD = {0x6c6: 41, 0x7e1: 38, 0xfe52: 35, 0x1008ff1d: 148}   # keysym -> keycode

    def __init__(self, *_args):
        self.asked, self.grabs = [], []
        self.root = self

    def screen(self):
        return self

    def keysym_to_keycode(self, keysym):
        self.asked.append(keysym)
        return self.KEYBOARD.get(keysym, 0)

    def grab_key(self, keycode, mask, *_args, **_kw):
        self.grabs.append((keycode, mask))

    def ungrab_key(self, *_args):
        pass

    def sync(self):
        pass

    def close(self):
        pass

    def fileno(self):
        return -1


class X11Keysyms(unittest.TestCase):
    """A key's X keysym comes from GDK, whose key values are keysyms. python-xlib's own
    table has only Latin-1 and the miscellany, and gave 0 for all of these."""

    def test_keys_beyond_latin1_have_their_keysyms(self):
        for name, keysym in (("Cyrillic_ef", 0x6c6), ("Greek_alpha", 0x7e1),
                             ("dead_circumflex", 0xfe52), ("EuroSign", 0x20ac),
                             ("XF86Calculator", 0x1008ff1d), ("F5", 0xffc2), ("1", 0x31)):
            self.assertEqual(hotkeys.X11Hotkeys.keysym(name), keysym, name)
        self.assertEqual(hotkeys.X11Hotkeys.keysym("NoSuchKey"), 0)

    def test_they_are_grabbed_and_only_a_key_the_keyboard_lacks_is_missing(self):
        if not hotkeys.X11Hotkeys.available():
            self.skipTest("needs python-xlib")
        wanted = {"ocr_only": "<Control>Cyrillic_ef", "query_text": "<Alt>Greek_alpha",
                  "translate_text": "<Super>dead_circumflex",
                  "look_up_word": "<Control>XF86Calculator",
                  "ask_anything": "<Control>EuroSign"}
        with mock.patch("Xlib.display.Display", FakeXDisplay), \
                mock.patch.object(hotkeys.GLib, "io_add_watch", return_value=None):
            grabber = hotkeys.X11Hotkeys(lambda action: None, wanted)
            bound = grabber.start()
            keyboard = grabber.display
            grabber.stop()
        self.assertEqual(sorted(bound), ["look_up_word", "ocr_only", "query_text",
                                         "translate_text"])
        self.assertEqual(grabber.failed, {"ask_anything": "no such key on this keyboard"})
        self.assertIn(0x20ac, keyboard.asked)       # asked for, and not on this keyboard
        self.assertIn(148, {code for code, _mask in keyboard.grabs})


class X11Grabs(TempDir):
    """Real grabs on the test's own X display; keys pressed through XTEST."""

    @classmethod
    def setUpClass(cls):
        if not os.environ.get("DISPLAY") or not hotkeys.X11Hotkeys.available():
            raise unittest.SkipTest("needs an X display and python-xlib")
        # XWayland, inside a Wayland session, does not route XTEST input like an X
        # server (seen on KDE Plasma 6.7), and the app never grabs there anyway:
        # choose() picks X11 grabs for X11 sessions only.
        if os.environ.get("WAYLAND_DISPLAY"):
            raise unittest.SkipTest("an X server, not XWayland in a Wayland session")

    def press(self, *keys):
        from Xlib import X, XK, display
        from Xlib.ext import xtest
        d = display.Display()
        codes = [d.keysym_to_keycode(XK.string_to_keysym(k)) for k in keys]
        for code in codes:
            xtest.fake_input(d, X.KeyPress, code)
        for code in reversed(codes):
            xtest.fake_input(d, X.KeyRelease, code)
        d.sync()
        d.close()

    def pump(self, until, timeout=3.0):
        context = GLib.MainContext.default()
        import time
        end = time.monotonic() + timeout
        while not until() and time.monotonic() < end:
            context.iteration(False)
            time.sleep(0.01)

    def test_a_chosen_key_fires_its_action_with_numlock_and_all(self):
        fired = []
        grabber = hotkeys.X11Hotkeys(fired.append, {"ocr_only": "<Control><Shift>F9"})
        self.addCleanup(grabber.stop)
        self.assertEqual(grabber.start(), ["ocr_only"])
        self.press("F8")        # a fresh Xvfb remaps its keyboard on the first XTEST key
        self.press("Control_L", "Shift_L", "F9")
        self.pump(lambda: fired)
        self.assertEqual(fired, ["ocr_only"])
        self.press("Control_L", "F9")                   # not the combination
        self.pump(lambda: len(fired) > 1, timeout=0.5)
        self.assertEqual(fired, ["ocr_only"])

    def test_a_key_another_program_holds_is_reported_and_the_rest_bind(self):
        from Xlib import X, XK, display
        other = display.Display()
        self.addCleanup(other.close)
        code = other.keysym_to_keycode(XK.string_to_keysym("F10"))
        other.screen().root.grab_key(code, X.ControlMask | X.ShiftMask, True,
                                     X.GrabModeAsync, X.GrabModeAsync)
        other.sync()
        grabber = hotkeys.X11Hotkeys(lambda a: None, {"ocr_only": "<Control><Shift>F10",
                                                      "query_text": "<Control><Shift>F11"})
        self.addCleanup(grabber.stop)
        self.assertEqual(grabber.start(), ["query_text"])
        self.assertEqual(grabber.failed, {"ocr_only": "taken by another program"})


# ---- Settings > Shortcuts ----------------------------------------------------------

def key_event(keyval, state=0, keycode=None):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    event.state = Gdk.ModifierType(state)
    if keycode is None:
        keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
        keycode = keymap.get_entries_for_keyval(keyval)[1][0].keycode
    event.hardware_keycode = keycode
    event.group = 0
    return event


class CaptureButton(unittest.TestCase):
    def setUp(self):
        from assistant.ui.shortcuts import ShortcutButton
        self.changes = []
        self.button = ShortcutButton("", self.changes.append)
        self.window = Gtk.Window()
        self.window.add(self.button)
        self.window.show_all()
        self.addCleanup(self.window.destroy)

    def test_shift_and_a_digit_is_the_digit(self):
        keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
        one = keymap.get_entries_for_keyval(Gdk.KEY_1)[1][0].keycode
        self.button.start_capture()
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        self.button._on_key(self.button, key_event(Gdk.KEY_exclam, ctrl_shift, one))
        self.assertEqual(self.changes, ["<Control><Shift>1"])
        self.assertEqual(self.button.get_label(), "Ctrl+Shift+1")
        self.assertFalse(self.button.capturing)

    def test_a_bare_key_is_refused_escape_cancels_backspace_clears(self):
        self.button.set_accel("F5")
        self.button.start_capture()
        self.button._on_key(self.button, key_event(Gdk.KEY_a))
        self.assertTrue(self.button.capturing)
        self.assertEqual(self.changes, [])
        self.button._on_key(self.button, key_event(Gdk.KEY_Escape))
        self.assertEqual(self.button.get_label(), "F5")
        self.button.start_capture()
        self.button._on_key(self.button, key_event(Gdk.KEY_BackSpace))
        self.assertEqual(self.changes, [""])
        self.assertEqual(self.button.get_label(), "Disabled")

    def test_keys_go_nowhere_unless_capturing(self):
        self.assertFalse(self.button._on_key(
            self.button, key_event(Gdk.KEY_a, Gdk.ModifierType.CONTROL_MASK)))

    def test_the_modifiers_pressed_on_the_way_are_waited_through(self):
        # As real events arrive, is_modifier unset: PyGObject reads it as 0 for every
        # key, and Ctrl then Shift was captured as Ctrl+Shift_L.
        ctrl, shift = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK
        alt = Gdk.ModifierType.MOD1_MASK
        self.button.start_capture()
        for keyval, state in ((Gdk.KEY_Control_L, 0), (Gdk.KEY_Shift_L, ctrl),
                              (Gdk.KEY_Alt_L, ctrl | shift), (Gdk.KEY_Super_L, ctrl)):
            self.assertTrue(self.button._on_key(self.button, key_event(keyval, state)))
            self.assertTrue(self.button.capturing, Gdk.keyval_name(keyval))
        self.assertEqual(self.changes, [])
        self.button._on_key(self.button, key_event(Gdk.KEY_5, ctrl | shift | alt))
        self.assertEqual(self.changes, ["<Control><Alt><Shift>5"])

    def test_a_letter_gdk_calls_u_plus_is_captured_as_the_desktops_name_it(self):
        # It showed as Ctrl+Alt+0xffffff, and was gone when Settings opened again.
        from assistant.ui.shortcuts import ShortcutButton
        ctrl_alt = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        with mock.patch.object(ShortcutButton, "_base_keyval", return_value=0x1000561):
            self.button.start_capture()
            self.button._on_key(self.button, key_event(Gdk.KEY_a, ctrl_alt))
        self.assertEqual(self.changes, ["<Control><Alt>U0561"])
        self.assertEqual(self.button.get_label(), "Ctrl+Alt+Ա")


class Page(TempDir):
    def page(self, mode="desktop", **config_values):
        from assistant.ui.shortcuts import ShortcutsPage
        config = self.config()
        for key, value in config_values.items():
            setattr(config, key, value)
        with mock.patch.object(hotkeys, "choose", return_value=(mode, "")):
            page = ShortcutsPage(config)
        window = Gtk.Window()
        window.add(page)
        self.addCleanup(window.destroy)
        return page

    def test_suggested_clear_and_values(self):
        page = self.page()
        self.assertEqual(page.values(), {"hotkeys": {}})
        page.use_suggested()
        self.assertEqual(page.values()["hotkeys"]["ask_anything"], "<Control><Shift>9")
        # Every action with a suggested key - not the slots for the user's own actions.
        self.assertEqual(len(page.values()["hotkeys"]),
                         len([action for action in hotkeys.ACTIONS if action[2]]))
        page.clear_all()
        self.assertEqual(page.values(), {"hotkeys": {}})

    def test_one_key_one_action(self):
        page = self.page(hotkeys={"ocr_only": "F5"})
        page.buttons["query_text"].set_accel("F5")
        page._changed("query_text", "F5")
        self.assertEqual(page.values()["hotkeys"], {"query_text": "F5"})
        self.assertEqual(page.buttons["ocr_only"].get_label(), "Disabled")
        self.assertIn("went to Query clipboard text", page.notes["ocr_only"].get_text())

    def test_a_key_saved_in_gdks_spelling_is_there_when_settings_opens(self):
        page = self.page(hotkeys={"ocr_only": "<Control><Alt>U+0561"})
        self.assertEqual(page.buttons["ocr_only"].get_label(), "Ctrl+Alt+Ա")
        self.assertEqual(page.values()["hotkeys"], {"ocr_only": "<Control><Alt>U0561"})

    def test_by_hand_the_commands_follow_the_keys(self):
        page = self.page(mode="manual")
        self.assertIn("no shortcuts chosen", page.command_text())
        page._set("ocr_only", "<Control><Shift>5")
        text = page.commands.get_buffer().get_property("text")
        self.assertIn("Ctrl+Shift+5", text)
        self.assertIn("--action=ocr_only", text)

    def test_settings_saves_the_pages_keys(self):
        from assistant.ui.dialogs import SettingsDialog
        config = self.config()
        with mock.patch.object(hotkeys, "choose", return_value=("x11", "")):
            dialog = SettingsDialog(None, config, type("C", (), {"config": config}))
        self.addCleanup(dialog.destroy)
        dialog.shortcuts_page._set("ocr_only", "F5")
        self.assertEqual(dialog.values()["hotkeys"], {"ocr_only": "F5"})
        self.assertEqual(dialog.values()["hotkey_mode"], "auto")


def pump(until=lambda: False, timeout=1.0):
    """Run the main loop until `until()`, or for `timeout` seconds."""
    import time
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)


class CapturingABoundKey(TempDir):
    """A key that is already a shortcut never reaches the capture button - the app's own
    X11 grab takes it, or the desktop does - so its activation is handed over instead."""

    KEY = "<Control><Shift>5"

    def manager(self, mode="x11"):
        self.fired = []
        config = self.config()
        config.hotkeys = {"ocr_only": self.KEY}
        manager = hotkeys.HotkeyManager(config, self.fired.append, run=FakeHost())
        manager.mode, manager.wanted = mode, {"ocr_only": self.KEY}
        return manager

    def page(self, manager):
        from assistant.ui.shortcuts import ShortcutsPage
        page = ShortcutsPage(manager.config, manager)
        window = Gtk.Window()
        window.add(page)
        self.addCleanup(window.destroy)
        return page, window

    def test_a_grab_or_the_portal_hands_the_press_to_a_waiting_capture(self):
        manager = self.manager()
        taken = []
        manager.capture = lambda action, accel: taken.append((action, accel)) or True
        manager._activated("ocr_only")
        self.assertEqual((taken, self.fired), ([("ocr_only", self.KEY)], []))
        manager.capture = lambda action, accel: False       # not taken after all: run
        manager._activated("ocr_only")
        manager.capture = None
        manager._activated("ocr_only")
        self.assertEqual(self.fired, ["ocr_only", "ocr_only"])

    def test_the_x11_grabs_and_the_portal_report_presses_through_the_manager(self):
        manager = self.manager()
        manager.config.hotkey_mode = "x11"
        taken = []
        manager.capture = lambda action, accel: taken.append(accel) or True
        with Patched(session_of(desktop="cinnamon", kind="x11")), \
                mock.patch.object(hotkeys.X11Hotkeys, "start", return_value=[]):
            manager.start()
        GLib.idle_add(manager.impl.dispatch, "ocr_only")    # as _on_readable does
        pump(lambda: taken)

        # GNOME 48's portal: on "kde" start() would look for the old script's launchers
        # in the real ~/.local/share/applications.
        manager.config.hotkey_mode = "portal"
        with Patched(session_of(desktop="gnome", portal=True)), \
                mock.patch.object(hotkeys.PortalHotkeys, "start", return_value=True):
            manager.start()
        portal = manager.impl
        portal.session_handle = "/org/freedesktop/portal/desktop/session/1_2/t"
        portal._on_activated(None, None, None, None, "Activated", GLib.Variant(
            "(osta{sv})", (portal.session_handle, "ocr_only", 0, {})))
        pump(lambda: len(taken) > 1)
        self.assertEqual(taken, [self.KEY, self.KEY])
        self.assertEqual(self.fired, [])

    def test_a_desktop_keybindings_action_reaches_the_capture_through_dispatch(self):
        from assistant.app import Assistant
        app = Assistant.__new__(Assistant)
        app.hotkeys = self.manager("desktop")
        app.config = app.hotkeys.config
        ran = []
        app.ocr_only = lambda: ran.append("ocr_only")
        taken = []
        app.hotkeys.capture = lambda action, accel: taken.append(accel) or True
        app.dispatch("ocr_only")                # run-assistant --action=ocr_only
        self.assertEqual((taken, ran), ([self.KEY], []))
        # On X11 the presses come through the grab; an --action there is the tray
        # menu, or typed, and runs whatever is waiting.
        app.hotkeys.mode = "x11"
        app.dispatch("ocr_only")
        app.hotkeys.capture = None
        app.hotkeys.mode = "desktop"
        app.dispatch("ocr_only")
        self.assertEqual((taken, ran), ([self.KEY], ["ocr_only", "ocr_only"]))

    def test_the_waiting_button_takes_the_key_from_the_action_that_had_it(self):
        manager = self.manager()
        page, window = self.page(manager)
        self.assertIsNone(manager.capture)
        page.buttons["query_text"].start_capture()
        self.assertIsNotNone(manager.capture)
        manager._activated("ocr_only")          # the grab's report of the press
        self.assertEqual(self.fired, [])
        self.assertEqual(page.values()["hotkeys"], {"query_text": self.KEY})
        self.assertEqual(page.buttons["query_text"].get_label(), "Ctrl+Shift+5")
        self.assertEqual(page.buttons["ocr_only"].get_label(), "Disabled")
        self.assertIn("went to Query clipboard text", page.notes["ocr_only"].get_text())
        self.assertFalse(page.buttons["query_text"].capturing)
        self.assertIsNone(manager.capture)      # done waiting: the key runs its action
        manager._activated("ocr_only")
        self.assertEqual(self.fired, ["ocr_only"])

    def test_only_one_button_waits_and_closing_settings_lets_go(self):
        manager = self.manager()
        page, window = self.page(manager)
        page.buttons["query_text"].start_capture()
        page.buttons["ocr_only"].start_capture()
        self.assertFalse(page.buttons["query_text"].capturing)
        self.assertEqual(page.waiting, "ocr_only")
        page.buttons["ocr_only"].stop_capture()             # Escape
        self.assertIsNone(manager.capture)
        page.buttons["query_text"].start_capture()
        window.destroy()
        self.assertIsNone(manager.capture)
        manager._activated("ocr_only")
        self.assertEqual(self.fired, ["ocr_only"])

    def test_focus_taken_by_the_grab_does_not_end_the_wait_before_the_press(self):
        from assistant.ui.shortcuts import ShortcutButton
        manager = self.manager()
        page, window = self.page(manager)
        button = page.buttons["query_text"]
        with mock.patch.object(ShortcutButton, "FOCUS_GRACE_MS", 20):
            # The X server takes focus away as the grab starts, and the grab's report
            # of the key follows: the press is still captured.
            button.start_capture()
            button._on_focus_out(button, None)
            GLib.idle_add(manager._activated, "ocr_only")
            pump(lambda: not button.capturing)
            self.assertEqual(page.values()["hotkeys"], {"query_text": self.KEY})
            # Focus that comes back keeps the wait; focus that stays away ends it.
            button.start_capture()
            button._on_focus_out(button, None)
            button._stay()
            pump(timeout=0.1)
            self.assertTrue(button.capturing)
            button._on_focus_out(button, None)
            pump(lambda: not button.capturing)
            self.assertFalse(button.capturing)
            self.assertIsNone(manager.capture)
        self.assertEqual(self.fired, [])


class TrayMenu(TempDir):
    def test_keys_show_beside_their_actions(self):
        from assistant.app import Assistant
        app = Assistant.__new__(Assistant)
        app.config = self.config()
        app.dispatch = lambda name: None
        app._on_premium_toggled = app._on_privacy_toggled = lambda *a: None
        app.hotkeys = mock.Mock()
        app.hotkeys.trigger_label = lambda name: ("Ctrl+Shift+5" if name == "ocr_only"
                                                  else None)
        menu = app._build_menu()
        app._refresh_menu_keys()
        labels = [item.get_label() for item in menu.get_children()
                  if isinstance(item, Gtk.MenuItem) and item.get_label()]
        self.assertEqual(labels[0], "Ask anything")
        self.assertIn("OCR only\tCtrl+Shift+5", labels)
        self.assertIn("Query clipboard text", labels)



def lua_strings(line):
    """The double-quoted Lua string literals in `line`, decoded as Lua does: a decimal
    escape is one byte, and the bytes are the UTF-8 that Hyprland reads."""
    found, i = [], 0
    while True:
        i = line.find('"', i)
        if i < 0:
            return found
        i += 1
        out = bytearray()
        while line[i] != '"':
            if line[i] == "\\":
                nxt = line[i + 1]
                if nxt.isdigit():
                    out.append(int(line[i + 1:i + 4]))
                    i += 4
                    continue
                out += {"n": "\n"}.get(nxt, nxt).encode()
                i += 2
                continue
            out += line[i].encode()
            i += 1
        found.append(out.decode())
        i += 1


class HyprlandLuaConfig(TempDir):
    """Hyprland 0.55 and later: hyprland.lua, in Lua, and hyprland.conf unread beside it
    (measured on 0.56, Omarchy)."""

    OMARCHY = ('dofile((os.getenv("OMARCHY_PATH") or "/usr/share/omarchy") .. '
               '"/default/hypr/bootstrap.lua")\nrequire("default.hypr.omarchy")\n'
               'require("hypr.bindings")\n')

    def setUp(self):
        super().setUp()
        self.hypr = os.path.join(self.dir, "hypr")
        os.makedirs(self.hypr)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, name, text):
        with open(os.path.join(self.hypr, name), "w", encoding="utf-8") as f:
            f.write(text)
        return os.path.join(self.hypr, name)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_lua_config_is_the_one_written_where_there_is_one(self):
        self.put("hyprland.conf", "bind = SUPER, Q, exec, kitty\n")
        self.assertTrue(keybindings.ConfigFileKeybindings("hyprland").path
                        .endswith("hyprland.conf"))
        lua = self.put("hyprland.lua", self.OMARCHY)
        writer = keybindings.ConfigFileKeybindings("hyprland")
        self.assertEqual(writer.path, lua)
        self.assertEqual(writer.describe().rsplit("/", 1)[-1], "hyprland.lua")

    def test_binds_are_written_in_hyprlands_lua(self):
        path = self.put("hyprland.lua", self.OMARCHY)
        host = FakeHost()
        writer = keybindings.ConfigFileKeybindings("hyprland", host)
        with mock.patch.dict(os.environ, {"APPIMAGE": '/home/u/My "Apps"/DA\\1 #2.AppImage'}):
            writer.apply({"ocr_only": "<Control><Shift>5", "ask_anything": "<Super>F9"},
                         LABELS, NAMES)
            wanted = keybindings.command("ocr_only")
        text = self.read(path)
        self.assertTrue(text.startswith(self.OMARCHY), text)
        self.assertEqual(text.count(keybindings.LUA_BEGIN), 1)
        self.assertTrue(text.rstrip().endswith(keybindings.LUA_END))
        bind = next(line for line in text.splitlines() if "ocr_only" in line)
        self.assertTrue(bind.startswith('hl.bind("CTRL + SHIFT + 5", hl.dsp.exec_cmd('), bind)
        self.assertEqual(lua_strings(bind), ["CTRL + SHIFT + 5", wanted,
                                             "DA Assistant: OCR only"])
        self.assertIn('hl.bind("SUPER + F9", ', text)
        self.assertEqual(self.read(path + ".da-assistant.bak"), self.OMARCHY)
        self.assertIn(["hyprctl", "reload"], host.calls)

    def test_remove_takes_the_lua_block_out(self):
        path = self.put("hyprland.lua", self.OMARCHY)
        writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost())
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        writer.remove(NAMES)
        self.assertEqual(self.read(path), self.OMARCHY)

    def test_after_a_move_to_lua_the_keys_go_there_and_out_of_the_conf(self):
        # Hyprland reads hyprland.conf no more once there is a hyprland.lua, and the
        # record did not say which file had the keys: nothing was written into the new
        # one, while Settings said it had been.
        conf = self.put("hyprland.conf", "bind = SUPER, Q, exec, kitty\n")
        config = Config(os.path.join(self.dir, "c.json"))
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        host = FakeHost()
        with Patched(session_of(desktop="hyprland")):
            hotkeys.HotkeyManager(config, lambda action: None, run=host).start()
            self.assertIn("--action=ocr_only", self.read(conf))
            lua = self.put("hyprland.lua", self.OMARCHY)
            host.calls.clear()
            manager = hotkeys.HotkeyManager(config, lambda action: None, run=host)
            manager.start()
            self.assertIsNone(manager.error)
            self.assertIn("hyprland.lua, each running DA Assistant", manager.describe())
            self.assertIn('hl.bind("CTRL + SHIFT + 5", ', self.read(lua))
            self.assertEqual(self.read(conf), "bind = SUPER, Q, exec, kitty\n")
            self.assertEqual(host.writes(), [["hyprctl", "reload"]])   # once, for both
            self.assertTrue(Config(config.config_file).hotkeys_installed["into"]
                            .endswith("hyprland.lua"))
            host.calls.clear()
            manager.start()                                     # written: not again
            self.assertEqual(host.writes(), [])
            config.hotkey_mode = "off"                          # and taken out of both
            hotkeys.HotkeyManager(config, lambda action: None, run=host).start()
        self.assertEqual(self.read(lua), self.OMARCHY)
        self.assertEqual(self.read(conf), "bind = SUPER, Q, exec, kitty\n")

    def test_a_lua_block_that_does_not_end_is_left_alone(self):
        broken = self.OMARCHY + keybindings.LUA_BEGIN + " ---\nhl.bind(\"F5\", x)\n"
        path = self.put("hyprland.lua", broken)
        writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost())
        with self.assertRaisesRegex(OSError, "line 4 has no"):
            writer.apply({"ocr_only": "F6"}, LABELS, NAMES)
        self.assertEqual(self.read(path), broken)

    # Characters that Lua and Hyprland read like any other, and str.splitlines, which
    # read the file back, took for line breaks.
    SEPARATORS = ("\x85", "\u2028", "\u2029")

    def test_a_name_never_ends_the_block_or_puts_a_line_outside_it(self):
        # Harmless as first written; the next rewrite ended the block at the marker the
        # name hid, and left the rest of it among the user's own lines, where Lua runs
        # it at every reload. Omarchy's hyprland.lua calls os.getenv: Lua's os library,
        # os.execute and all, is there.
        for sep in self.SEPARATORS:
            with self.subTest(sep=hex(ord(sep))):
                path = self.put("hyprland.lua", self.OMARCHY)
                name = (f"Explain -- {sep}{keybindings.LUA_END}{sep}"
                        f"os.execute('notify-send injected') --")
                named = dict(LABELS, custom_1=name)
                writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost())
                writer.apply({"custom_1": "<Control><Shift>1"}, named, NAMES)
                text = self.read(path)
                self.assertNotIn(sep, text)
                bind = next(line for line in text.split("\n") if "custom_1" in line)
                self.assertEqual(lua_strings(bind)[2], f"DA Assistant: {name}")
                # Any later change rewrites the block: another key, here.
                writer.apply({"custom_1": "<Control><Shift>1",
                              "ocr_only": "<Control><Shift>5"}, named, NAMES)
                lines = self.read(path).split("\n")
                self.assertFalse([line for line in lines if line.startswith("os.")], lines)
                writer.remove(NAMES)
                self.assertEqual(self.read(path), self.OMARCHY)

    def test_a_name_cannot_keep_the_block_from_being_taken_out(self):
        # A begin marker hidden the same way read as a second block begun inside the
        # first, with no end of its own: nothing could be changed or taken out again.
        for sep in self.SEPARATORS:
            with self.subTest(sep=hex(ord(sep))):
                path = self.put("hyprland.lua", self.OMARCHY)
                named = dict(LABELS, custom_1=f"x{sep}{keybindings.LUA_BEGIN}")
                writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost())
                writer.apply({"custom_1": "F5"}, named, NAMES)
                writer.apply({"custom_1": "F6"}, named, NAMES)
                writer.remove(NAMES)
                self.assertEqual(self.read(path), self.OMARCHY)

    def test_a_block_written_with_such_a_name_in_it_comes_out_whole(self):
        # As 4.0.0 wrote the name, unescaped: a line ends at "\n", for Lua as for the
        # app, and the marker inside it is no marker.
        for sep in self.SEPARATORS:
            with self.subTest(sep=hex(ord(sep))):
                name = f"x{sep}{keybindings.LUA_END}{sep}os.execute('notify-send injected')"
                path = self.put("hyprland.lua", "\n".join([
                    self.OMARCHY, keybindings.LUA_BEGIN + " (managed by DA Assistant) ---",
                    'hl.bind("F5", hl.dsp.exec_cmd("/x --action=custom_1"), '
                    '{ description = "DA Assistant: ' + name + '" })',
                    keybindings.LUA_END, ""]))
                keybindings.ConfigFileKeybindings("hyprland", FakeHost()).remove(NAMES)
                self.assertEqual(self.read(path), self.OMARCHY)


# ---- a key the desktop has already ---------------------------------------------------

GNOME_ITEM = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
GNOME_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"


class FakeDesktopKeys(FakeHost):
    """gsettings with the desktop's own keybindings in it: `schemas` maps a schema, or
    SCHEMA:PATH, to the "key value" lines `gsettings list-recursively` prints for it.
    Any other schema is one this version of the desktop does not have."""

    def __init__(self, listed="@as []", schemas=None):
        super().__init__(listed)
        self.schemas = dict(schemas or {})

    def __call__(self, args):
        if args[:2] == ["gsettings", "list-recursively"]:
            self.calls.append(list(args))
            if args[2] not in self.schemas:
                return 1, f"No such schema “{args[2]}”\n"
            schema = args[2].split(":", 1)[0]
            return 0, "".join(f"{schema} {line}\n" for line in self.schemas[args[2]])
        return super().__call__(args)


class TheDesktopsOwnKeys(TempDir):
    """A key written into GNOME's or Cinnamon's custom shortcuts that the desktop, or a
    shortcut of the user's own, had already fired both - with nothing said, the menu
    showing it as working. Such a key is left to what has it, and said to be."""

    WM = "org.gnome.desktop.wm.keybindings"

    def test_a_key_gnome_binds_itself_is_not_taken_and_is_said(self):
        host = FakeDesktopKeys(schemas={
            self.WM: ["switch-applications ['<Super>Tab', '<Primary><Shift>5']",
                      "close @as []"],
            "org.gnome.shell.keybindings": ["toggle-overview ['<Super>s']"]})
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5",
                          "query_text": "<Control><Shift>3"}
        with Patched(session_of(desktop="gnome")):
            manager = hotkeys.HotkeyManager(config, lambda action: None, run=host)
            manager.start()
            self.assertEqual(manager.mode, "desktop")
            self.assertEqual(manager.problems,
                             {"ocr_only": "already GNOME's shortcut for "
                                          "“switch-applications”"})
            self.assertIsNone(manager.trigger_label("ocr_only"))
            self.assertEqual(manager.trigger_label("query_text"), "Ctrl+Shift+3")
            self.assertIn("da-assistant-query_text", host.listed)
            self.assertNotIn("da-assistant-ocr_only", host.listed)
            self.assertEqual(manager.report(),
                             "Not bound: Ctrl+Shift+5 (already GNOME's shortcut for "
                             "“switch-applications”).")
            self.assertEqual(self.config().hotkeys_installed["bindings"],
                             {"query_text": "<Control><Shift>3"})
            # Freed in GNOME's settings: the next start writes it.
            host.schemas[self.WM] = ["switch-applications ['<Super>Tab']"]
            manager.start()
        self.assertEqual(manager.problems, {})
        self.assertIn("da-assistant-ocr_only", host.listed)
        self.assertEqual(manager.trigger_label("ocr_only"), "Ctrl+Shift+5")

    def test_a_custom_shortcut_of_the_users_is_named_and_ours_are_not_clashes(self):
        theirs = f"{CINNAMON_ITEM}:{CINNAMON_BASE}/custom0/"
        ours = f"{CINNAMON_ITEM}:{CINNAMON_BASE}/{cinnamon_name('query_text')}/"
        host = FakeDesktopKeys(
            f"['custom0', '{cinnamon_name('query_text')}', '__dummy__']",
            schemas={theirs: ["binding ['<Control><Shift>5']", "command 'xterm'",
                              "name 'Terminal'"],
                     ours: ["binding ['<Control><Shift>3']", "name 'DA Assistant: x'"],
                     "org.cinnamon.desktop.keybindings.media-keys":
                         ["area-screenshot ['<Shift>Print']",
                          "calculator ['XF86Calculator']"]})
        writer = keybindings.GSettingsKeybindings("cinnamon", host)
        self.assertEqual(writer.clashes({"ocr_only": "<Control><Shift>5",
                                         "query_text": "<Control><Shift>3"}),
                         {"ocr_only": "already your shortcut “Terminal”"})
        self.assertNotIn(ours, [c[2] for c in host.calls if c[1] == "list-recursively"])

    def test_gnomes_custom_shortcuts_are_read_by_their_paths(self):
        path = f"{GNOME_BASE}/custom0/"
        host = FakeDesktopKeys(f"['{path}']", schemas={
            f"{GNOME_ITEM}:{path}": ["binding '<Super>F9'", "name 'Notes'"]})
        writer = keybindings.GSettingsKeybindings("gnome", host)
        self.assertEqual(writer.clashes({"ask_anything": "<Super>F9"}),
                         {"ask_anything": "already your shortcut “Notes”"})

    def test_what_cannot_be_read_stops_nothing_being_written(self):
        class Unreadable(FakeHost):
            def __call__(self, args):
                if args[1] == "list-recursively":
                    raise RuntimeError("gsettings crashed")
                return super().__call__(args)

        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        host = Unreadable()
        with Patched(session_of(desktop="gnome")), \
                mock.patch("assistant.errors.record") as record:
            manager = hotkeys.HotkeyManager(config, lambda action: None, run=host)
            manager.start()
        self.assertIsNone(manager.error)
        self.assertEqual(manager.problems, {})
        self.assertIn("da-assistant-ocr_only", host.listed)
        self.assertIn("RuntimeError", record.call_args[0][0])


class TheUsersOwnConfig(TempDir):
    """Hyprland and Sway: a key the user's config binds already. Hyprland fired both,
    and Sway's bindsym of ours, coming last, silently replaced the user's own."""

    def put(self, relative, text):
        path = os.path.join(self.dir, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_hyprland_conf_its_variables_sources_and_unbinds(self):
        path = self.put("hypr/hyprland.conf", "\n".join([
            "$mainMod = SUPER",
            "source = binds/*.conf",
            "bind = $mainMod SHIFT, F5, exec, foot  # a terminal",
            "bind = CTRL SHIFT, 3, exec, kitty",
            "bindm = CTRL SHIFT, mouse:272, movewindow",
            "unbind = CTRL SHIFT, 3",
            "# bind = CTRL SHIFT, 4, exec, commented out",
            keybindings.BEGIN + " ---",
            "bind = CTRL SHIFT, 6, exec, /x --action=look_up_word  # Look up word",
            keybindings.END,
            "bind = SUPER ALT, F7, exec, after-the-block", ""]))
        extra = self.put("hypr/binds/extra.conf",
                         "bindd = CTRL_ALT, Q, Close it, killactive\n")
        writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost(), path)
        found = writer.clashes({"ocr_only": "<Shift><Super>F5",
                                "query_text": "<Control><Shift>3",
                                "query_image": "<Control><Shift>4",
                                "look_up_word": "<Control><Shift>6",
                                "translate_text": "<Control><Alt>q",
                                "ask_anything": "<Alt><Super>F7"})
        self.assertEqual(set(found), {"ocr_only", "translate_text", "ask_anything"})
        self.assertIn("line 3 of ", found["ocr_only"])
        self.assertIn("line 11 of ", found["ask_anything"])  # numbered as in the file
        self.assertIn("bind = $mainMod SHIFT, F5, exec, foot", found["ocr_only"])
        self.assertIn(f"line 1 of {extra}", found["translate_text"])

    def test_a_sway_bindcode_on_the_key_is_a_clash_too(self):
        """Measured on Sway 1.11: a bindcode on the physical key took it first, and the
        app's bindsym on the same key never ran - while Settings showed it working."""
        path = self.put("sway/config", "\n".join([
            "bindcode Ctrl+Shift+14 exec theirs",          # 14: the 5 key
            "bindcode --release Mod4+24 exec q-on-release",  # 24: the q key
            "bindcode Ctrl+Shift+13 exec gone",             # 13: the 4 key, unbound
            "unbindcode Ctrl+Shift+13", ""]))
        writer = keybindings.ConfigFileKeybindings("sway", FakeHost(), path)
        with mock.patch.object(keybindings, "_keycode_name",
                               lambda code: keybindings._US_KEYCODES.get(int(code))):
            found = writer.clashes({"ocr_only": "<Control><Shift>5",
                                    "query_image": "<Super>q",
                                    "query_text": "<Control><Shift>4"})
        self.assertEqual(set(found), {"ocr_only", "query_image"})
        self.assertIn("line 1 of", found["ocr_only"])

    def test_a_keycode_is_named_as_the_keyboard_has_it(self):
        self.assertEqual(keybindings._US_KEYCODES[14], "5")
        self.assertEqual(keybindings._US_KEYCODES[10], "1")
        self.assertEqual(keybindings._US_KEYCODES[19], "0")
        self.assertEqual(keybindings._US_KEYCODES[38], "a")
        self.assertIsNone(keybindings._keycode_name("not-a-number"))
        self.assertIn(keybindings._keycode_name(14), ("5", "percent", None, "5"))

    def test_line_ends_are_kept_as_they_were(self):
        """Read with universal newlines, a lone \\r ended a line: "# a\\rb" came back as a
        comment and a live line "b", and a CRLF config was written back LF."""
        path = self.put("sway/config", "")
        with open(path, "w", newline="") as f:
            f.write("# a comment\rb-not-a-line\r\nbindsym Mod4+t exec foot\r\n")
        writer = keybindings.ConfigFileKeybindings("sway", FakeHost(), path)
        writer.apply({"ocr_only": "<Control><Shift>5"}, {"ocr_only": "OCR"}, ["ocr_only"])
        with open(path, newline="") as f:
            written = f.read()
        self.assertTrue(written.startswith(
            "# a comment\rb-not-a-line\r\nbindsym Mod4+t exec foot\r\n"))
        writer.remove(["ocr_only"])
        with open(path, newline="") as f:
            self.assertEqual(f.read(),
                             "# a comment\rb-not-a-line\r\nbindsym Mod4+t exec foot\r\n")

    def test_sway_in_its_default_mode_and_what_it_includes(self):
        self.put("sway/config.d/50-apps", "bindsym --to-code Ctrl+Shift+4 exec bar\n")
        self.put("sway/config", "\n".join([
            "set $mod Mod4",
            "bindsym $mod+Shift+5 exec foo",
            'mode "resize" {',
            "    bindsym Ctrl+Shift+3 resize shrink width 10px",
            "}",
            "bindsym {",
            "    Ctrl+Alt+q kill",
            "}",
            "unbindsym Ctrl+Alt+q",
            "bindsym Ctrl+Shift+F6 \\",
            "    exec baz",
            "include config.d/*", ""]))
        config = self.config()
        config.hotkeys = {"ocr_only": "<Shift><Super>5", "query_text": "<Control><Shift>3",
                          "query_image": "<Control><Shift>4",
                          "translate_text": "<Control><Alt>q",
                          "look_up_word": "<Control><Shift>F6"}
        with Patched(session_of(desktop="sway")), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}):
            manager = hotkeys.HotkeyManager(config, lambda action: None, run=FakeHost())
            manager.start()
        self.assertEqual(set(manager.problems), {"ocr_only", "query_image", "look_up_word"})
        self.assertIn("line 2 of", manager.problems["ocr_only"])
        self.assertIn("line 10 of", manager.problems["look_up_word"])
        self.assertIn("50-apps: bindsym --to-code Ctrl+Shift+4",
                      manager.problems["query_image"])
        with open(os.path.join(self.dir, "sway", "config")) as f:
            text = f.read()
        block = text[text.index(keybindings.BEGIN):]
        self.assertIn("--action=query_text", block)        # only in a mode of its own
        self.assertIn("--action=translate_text", block)    # unbound before
        for action in ("ocr_only", "query_image", "look_up_word"):
            self.assertNotIn(f"--action={action}", block)
        self.assertNotIn("Shift+Mod4+5", text)              # theirs stays the only one
        self.assertIsNone(manager.trigger_label("ocr_only"))
        self.assertEqual(manager.trigger_label("query_text"), "Ctrl+Shift+3")

    def test_hyprland_lua_and_the_modules_it_requires(self):
        path = self.put("hypr/hyprland.lua", HyprlandLuaConfig.OMARCHY)
        bindings = self.put("hypr/bindings.lua", "\n".join([
            'hl.bind("CTRL + SHIFT + 5", hl.dsp.exec_cmd("kitty"))',
            '-- hl.bind("CTRL + SHIFT + 4", hl.dsp.exec_cmd("commented"))',
            "--[[",
            'hl.bind("CTRL + SHIFT + 3", hl.dsp.exec_cmd("commented too"))',
            "]]",
            "local s = \"-- in a string\" "
            "hl.bind('SUPER + F9', hl.dsp.exec_cmd('x'))", ""]))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}):
            writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost())
            self.assertEqual(writer.path, path)
            found = writer.clashes({"ocr_only": "<Control><Shift>5",
                                    "query_image": "<Control><Shift>4",
                                    "query_text": "<Control><Shift>3",
                                    "ask_anything": "<Super>F9"})
        self.assertEqual(set(found), {"ocr_only", "ask_anything"})
        self.assertIn(f"line 1 of {bindings}", found["ocr_only"])
        self.assertIn("line 6 of", found["ask_anything"])

    def omarchy_binds(self, *extra):
        """What `hyprctl binds -j` gave on Omarchy (Hyprland 0.56), cut down: every bind
        made in Lua shows as dispatcher "__lua", with the description it was given."""
        binds = [
            {"modmask": 64, "submap": "", "key": "W", "keycode": 0, "mouse": False,
             "description": "Close window", "dispatcher": "__lua", "arg": "17"},
            {"modmask": 76, "submap": "", "key": "T", "keycode": 0, "mouse": False,
             "description": "Show time", "dispatcher": "__lua", "arg": "210"},
            {"modmask": 64, "submap": "", "key": "RETURN", "keycode": 0, "mouse": False,
             "description": "Terminal", "dispatcher": "__lua", "arg": "269"},
            # Ours, as the Lua block writes them, and as hyprland.conf's does.
            {"modmask": 5, "submap": "", "key": "9", "keycode": 0, "mouse": False,
             "description": "DA Assistant: Ask anything", "dispatcher": "__lua",
             "arg": "11"},
            {"modmask": 5, "submap": "", "key": "8", "keycode": 0, "mouse": False,
             "description": "", "dispatcher": "exec",
             "arg": "'/opt/DA.AppImage' --action=query_text"},
            # Not keys of the app's kind: a mouse bind, one in a submap, a keycode.
            {"modmask": 64, "submap": "", "key": "mouse:272", "keycode": 0,
             "mouse": True, "description": "Move window", "dispatcher": "__lua",
             "arg": "5"},
            {"modmask": 0, "submap": "resize", "key": "L", "keycode": 0, "mouse": False,
             "description": "", "dispatcher": "resizeactive", "arg": "10 0"},
            {"modmask": 64, "submap": "", "key": "", "keycode": 191, "mouse": False,
             "description": "", "dispatcher": "exec", "arg": "x"},
            *extra]
        host = FakeHost()

        def run(args):
            if args == ["hyprctl", "binds", "-j"]:
                return 0, json.dumps(binds)
            return host(args)

        return run

    def test_what_hyprland_has_in_force_is_what_a_key_clashes_with(self):
        """Omarchy binds every key through o.bind, from modules found through a search
        path of its own and a dofile built at run time: read off the files, it bound
        nothing, and a key it held was written and fired both. Hyprland is asked."""
        self.put("hypr/hyprland.lua", HyprlandLuaConfig.OMARCHY)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}):
            writer = keybindings.ConfigFileKeybindings("hyprland", self.omarchy_binds())
            found = writer.clashes({"look_up_word": "<Control><Alt><Super>t",
                                    "query_image": "<Super>w",
                                    "ocr_only": "<Super>Return",
                                    "ask_anything": "<Control><Shift>9",
                                    "query_text": "<Control><Shift>8",
                                    "translate_text": "<Super>l",
                                    "ocr_translate": "<Control><Shift>5"})
        self.assertEqual(found, {
            "look_up_word": "already bound in Hyprland: Show time",
            "query_image": "already bound in Hyprland: Close window",
            "ocr_only": "already bound in Hyprland: Terminal"})

    def test_a_key_hyprland_binds_by_keycode_clashes_too(self):
        """hyprctl binds -j gives a bind by keycode - Omarchy's "SUPER + code:10",
        workspace 1 - an empty key and keycode 0 (0.56.2): Super+1 was taken for free,
        and one press switched workspace and ran the app's action. The plain output
        still names the code."""
        plain = "\n".join([
            "bindd", "\tmodmask: 64", "\tsubmap: ", "\tkey: SUPER + code:10",
            "\tkeycode: 0", "\tcatchall: false",
            "\tdescription: Switch to workspace 1", "\tdispatcher: __lua", "\targ: 71",
            "", "bindd", "\tmodmask: 65", "\tsubmap: ", "\tkey: SUPER + SHIFT + code:11",
            "\tkeycode: 0", "\tdescription: Move window to workspace 2",
            "\tdispatcher: __lua", "\targ: 73",
            "", "bindd", "\tmodmask: 64", "\tsubmap: ", "\tkey: SUPER + code:12",
            "\tkeycode: 0", "\tdescription: DA Assistant: Ask anything",
            "\tdispatcher: __lua", "\targ: 9", ""])
        by_code = {"modmask": 64, "submap": "", "key": "", "keycode": 0, "mouse": False,
                   "description": "Switch to workspace 1", "dispatcher": "__lua",
                   "arg": "71"}
        json_run = self.omarchy_binds(by_code)

        def run(args):
            if args == ["hyprctl", "binds"]:
                return 0, plain
            return json_run(args)

        self.put("hypr/hyprland.lua", HyprlandLuaConfig.OMARCHY)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}), \
                mock.patch.object(keybindings, "_keycode_name",
                                  lambda code: keybindings._US_KEYCODES.get(int(code))):
            writer = keybindings.ConfigFileKeybindings("hyprland", run)
            found = writer.clashes({"query_text": "<Super>1",
                                    "ocr_only": "<Shift><Super>2",
                                    "ask_anything": "<Super>3",
                                    "query_image": "<Super>4"})
        self.assertEqual(found, {
            "query_text": "already bound in Hyprland: Switch to workspace 1",
            "ocr_only": "already bound in Hyprland: Move window to workspace 2"})

    def test_a_bind_the_files_name_but_hyprland_does_not_have_is_free(self):
        # Read off the page, but not in force - behind an if, say: Hyprland's answer
        # decides. One it has in force that the files also name is named by its line.
        self.put("hypr/hyprland.lua", HyprlandLuaConfig.OMARCHY)
        bindings = self.put("hypr/bindings.lua", "\n".join([
            'if false then hl.bind("CTRL + SHIFT + 5", hl.dsp.exec_cmd("kitty")) end',
            'hl.bind("SUPER + W", hl.dsp.window.close())', ""]))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir}):
            writer = keybindings.ConfigFileKeybindings("hyprland", self.omarchy_binds())
            found = writer.clashes({"ocr_only": "<Control><Shift>5",
                                    "query_image": "<Super>w"})
        self.assertEqual(list(found), ["query_image"])
        self.assertIn(f"line 2 of {bindings}", found["query_image"])

    def test_without_an_answer_from_hyprland_the_files_decide(self):
        self.assertIsNone(keybindings._hyprland_live(lambda args: (127, "")))
        self.assertIsNone(keybindings._hyprland_live(lambda args: (0, "not json")))
        self.assertIsNone(keybindings._hyprland_live(lambda args: (0, '{"a": 1}')))
        path = self.put("hypr/hyprland.conf", "bind = CTRL SHIFT, 5, exec, kitty\n")
        writer = keybindings.ConfigFileKeybindings("hyprland", lambda a: (1, "no"), path)
        self.assertEqual(list(writer.clashes({"ocr_only": "<Control><Shift>5"})),
                         ["ocr_only"])

    def test_a_config_that_sources_itself_is_read_once(self):
        path = self.put("hypr/hyprland.conf",
                        "source = hyprland.conf\nbind = CTRL SHIFT, 5, exec, kitty\n")
        writer = keybindings.ConfigFileKeybindings("hyprland", FakeHost(), path)
        self.assertEqual(list(writer.clashes({"ocr_only": "<Control><Shift>5"})),
                         ["ocr_only"])


# ---- the portal's answer -------------------------------------------------------------

class FakeBus:
    """Enough of a D-Bus connection for PortalHotkeys._bind: the Response handler it
    subscribes is kept, for the test to answer as the desktop would."""

    def __init__(self):
        self.handlers = []

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, *args):
        self.handlers.append(args[-1])
        return len(self.handlers)

    def signal_unsubscribe(self, _sub):
        pass

    def call(self, *args):
        pass

    def answer(self, code, shortcuts=()):
        results = {"shortcuts": GLib.Variant("a(sa{sv})", [
            (name, {"trigger_description": GLib.Variant("s", said)})
            for name, said in shortcuts])}
        self.handlers.pop()(None, None, None, None, "Response",
                            GLib.Variant("(ua{sv})", (code, results)))


class ThePortalsAnswerIsSaid(TempDir):
    """The portal answers BindShortcuts after start() has returned, and after its
    dialog, if it shows one: Settings' report of the keys was made before, with nothing
    to say, and the answer only refreshed the menu. A key declined in KDE's dialog, or
    one GNOME kept the old key for, was never said."""

    def app(self, keys, desktop="gnome"):
        # Not "kde": start() would look for the old script's launchers in the user's
        # real applications folder.
        from assistant.app import Assistant
        config = self.config()
        config.hotkeys = keys
        config.hotkey_mode = "portal"
        app = Assistant.__new__(Assistant)
        app.config = config
        app.notices = []
        app.notify = lambda message, urgency="low": app.notices.append(message)
        app.hotkeys = hotkeys.HotkeyManager(config, lambda action: None, run=FakeHost())
        with Patched(session_of(desktop=desktop, portal=True)), \
                mock.patch.object(hotkeys.PortalHotkeys, "start", return_value=True):
            app.hotkeys.start()
        app.hotkeys.impl.bus = self.bus = FakeBus()
        app.hotkeys.impl.session_handle = "/org/freedesktop/portal/desktop/session/1_42/t"
        self.patches = Patched(session_of(desktop=desktop, portal=True))
        self.patches.__enter__()
        self.addCleanup(self.patches.__exit__)
        app.hotkeys.impl._bind()
        return app

    def test_a_key_declined_in_the_desktops_dialog_is_said_once_answered(self):
        app = self.app({"ocr_only": "<Control><Shift>5"})
        app._report_hotkeys()                           # as Save does, at once
        self.assertEqual(app.notices, [])
        self.bus.answer(1)                              # the user said no
        self.assertEqual(app.notices, ["Not bound: Ctrl+Shift+5 (not confirmed in the "
                                       "desktop's dialog)."])
        app.hotkeys.impl._take([])                      # ShortcutsChanged, later
        self.assertEqual(len(app.notices), 1)

    def test_gnome_keeping_the_key_it_had_is_said(self):
        app = self.app({"ocr_only": "F5", "query_text": "<Control><Shift>3"})
        app._report_hotkeys()
        self.bus.answer(0, [("ocr_only", "Press <Shift><Control>5"),
                            ("query_text", "Press <Shift><Control>3")])
        self.assertEqual(app.notices, [
            "GNOME kept the key it had: Ctrl+Shift+5 for OCR only, not F5. Change it in "
            "GNOME's Settings > Apps > DA Assistant."])
        # It still works, and the menu says which key it is.
        self.assertEqual(app.hotkeys.trigger_label("ocr_only"), "Ctrl+Shift+5")

    def test_a_key_the_desktop_assigned_none_is_said(self):
        app = self.app({"ocr_only": "<Control><Shift>5"})
        app._report_hotkeys()
        self.bus.answer(0, [("ocr_only", "")])
        self.assertEqual(app.notices, ["Not bound: Ctrl+Shift+5 (not assigned by the "
                                       "desktop)."])

    def test_all_bound_as_asked_says_nothing(self):
        app = self.app({"ocr_only": "<Control><Shift>5"})
        app._report_hotkeys()
        self.bus.answer(0, [("ocr_only", "Press <Shift><Control>5")])
        self.assertEqual(app.notices, [])


class APortalStillStarting(TempDir):
    """Started at login, the app can ask before xdg-desktop-portal answers. The answer
    it did not get was kept for the whole run, and the shortcuts were written into
    GNOME's keybindings where its portal would have bound them. The portal is asked
    again now (test_capture_cli.APortalStillStarting), and once it answers, the
    shortcuts are set up again where that changes how."""

    def test_the_shortcuts_are_set_up_again_once_it_answers(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        host = FakeHost()
        manager = hotkeys.HotkeyManager(config, lambda action: None, run=host)
        self.addCleanup(manager.stop)
        with Patched(session_of(desktop="gnome", portal=False, answered=False)):
            manager.start()
        self.assertEqual(manager.mode, "desktop")
        self.assertIn("da-assistant-ocr_only", host.listed)
        self.assertIsNotNone(manager._recheck_source)
        with Patched(session_of(desktop="gnome", portal=True)), \
                mock.patch.object(hotkeys.PortalHotkeys, "start", return_value=True):
            self.assertFalse(manager._recheck())
        self.assertEqual(manager.mode, "portal")
        self.assertNotIn("da-assistant-ocr_only", host.listed)  # GNOME's copy taken out
        self.assertIsNone(manager._recheck_source)

    def test_a_portal_that_never_answers_is_given_up_on(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        manager = hotkeys.HotkeyManager(config, lambda action: None, run=FakeHost())
        self.addCleanup(manager.stop)
        with Patched(session_of(desktop="gnome", answered=False)), \
                mock.patch.object(hotkeys.HotkeyManager, "RECHECKS", 2):
            manager.start()
            self.assertTrue(manager._recheck())
            self.assertFalse(manager._recheck())
        self.assertEqual(manager.mode, "desktop")

    def test_nothing_is_looked_at_again_where_the_portal_does_not_matter(self):
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        with Patched(session_of(desktop="cinnamon", kind="x11", answered=False)), \
                mock.patch.object(hotkeys.X11Hotkeys, "start", return_value=[]):
            manager = hotkeys.HotkeyManager(config, lambda action: None, run=FakeHost())
            manager.start()
        self.assertEqual(manager.mode, "x11")
        self.assertIsNone(manager._recheck_source)


class RenamedActionsReachTheDesktop(TempDir):
    """The desktop shows each of the user's own actions under its name - GNOME's and
    Cinnamon's entries, Hyprland's comment, the portal's description - and a rename
    did not set the shortcuts up again, so the old name stayed there until a restart."""

    def save(self, values):
        from assistant import app as app_module
        config = self.config()
        config.custom_actions = [{"slot": 1, "name": "Summarise", "prompt": "Sum up {text}",
                                  "source": "clipboard"}]
        config.hotkeys = {"custom_1": "<Control><Shift>7"}
        app = app_module.Assistant.__new__(app_module.Assistant)
        app.config, app.client, app.encyclopedia = config, None, None
        app.hotkeys, app._settings_dialog = mock.Mock(), None
        app.notify = lambda message, urgency="low": None
        for name in ("_warm_catalogue", "_refresh_reviews", "_sync_privacy_item",
                     "_make_open_windows_private", "_release_privacy_holds",
                     "_sync_encyclopedia_items", "_sync_custom_items",
                     "_refresh_menu_keys"):
            setattr(app, name, lambda: None)
        dialog = mock.Mock()
        dialog.run.return_value = Gtk.ResponseType.OK
        dialog.values.return_value = values
        with mock.patch.object(app_module, "SettingsDialog", return_value=dialog):
            app.show_settings()
        return app.hotkeys

    def test_a_rename_sets_them_up_again(self):
        manager = self.save({"custom_actions": [{"slot": 1, "name": "Summarize",
                                                 "prompt": "Sum up {text}",
                                                 "source": "clipboard"}]})
        manager.stop.assert_called_once()
        manager.start.assert_called_once()

    def test_a_save_about_something_else_does_not(self):
        manager = self.save({"default_language": "French"})
        manager.start.assert_not_called()


class SuggestedKeysTakeTheirKeys(TempDir):
    def test_a_users_action_lets_go_of_a_suggested_key_and_says_so(self):
        # It held Ctrl+Shift+3; Use suggested keys gave Query clipboard text the same,
        # and on Save the user's action had none, with nothing said.
        from assistant.ui.shortcuts import ShortcutsPage
        config = self.config()
        config.custom_actions = [{"slot": 1, "name": "Summarise", "prompt": "Sum up {text}",
                                  "source": "clipboard"}]
        config.hotkeys = {"custom_1": "<Control><Shift>3"}
        with mock.patch.object(hotkeys, "choose", return_value=("desktop", "")):
            page = ShortcutsPage(config)
        window = Gtk.Window()
        window.add(page)
        self.addCleanup(window.destroy)
        page.use_suggested()
        chosen = page.values()["hotkeys"]
        self.assertEqual(chosen["query_text"], "<Control><Shift>3")
        self.assertNotIn("custom_1", chosen)
        self.assertEqual(len(set(chosen.values())), len(chosen))
        self.assertEqual(page.buttons["custom_1"].get_label(), "Disabled")
        self.assertIn("Ctrl+Shift+3 went to Query clipboard text",
                      page.notes["custom_1"].get_text())
        self.assertEqual(page.notes["query_text"].get_text(), "")

    def test_a_problem_keeps_the_case_of_what_it_names(self):
        from assistant.ui.shortcuts import ShortcutsPage
        config = self.config()
        config.hotkeys = {"ocr_only": "<Control><Shift>5"}
        manager = mock.Mock(mode="desktop", error=None, impl=None,
                            problems={"ocr_only": "already your shortcut “Terminal”"},
                            wanted={"ocr_only": "<Control><Shift>5"})
        manager.describe.return_value = ""
        page = ShortcutsPage(config, manager)
        window = Gtk.Window()
        window.add(page)
        self.addCleanup(window.destroy)
        self.assertEqual(page.notes["ocr_only"].get_text(),
                         "Already your shortcut “Terminal”")


if __name__ == "__main__":
    unittest.main(verbosity=2)
