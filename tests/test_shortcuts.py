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
        return 0, ""

    def sets(self, key):
        return [c for c in self.calls if c[:2] == ["gsettings", "set"] and c[3] == key]


def session_of(desktop="cinnamon", kind="wayland", portal=False, x11=True):
    """Patches deciding the session for one test."""
    return [mock.patch.object(session, "desktop", return_value=desktop),
            mock.patch.object(session, "session_type", return_value=kind),
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

    def test_another_layout_gives_its_own_symbol(self):
        class German:
            def get_entries_for_keyval(self, keyval):
                return True, [type("Key", (), {"keycode": 18, "group": 0})()]

            def translate_keyboard_state(self, keycode, state, group):
                return True, Gdk.KEY_parenright, 0, 1, 0

        self.assertEqual(shortcuts.portal_trigger("<Control><Shift>9", German()),
                         "CTRL+parenright")


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


class GSettingsWriter(unittest.TestCase):
    def test_cinnamon_adds_names_to_the_users_list(self):
        host = FakeHost("['custom0', '__dummy__']")
        writer = keybindings.GSettingsKeybindings("cinnamon", host)
        writer.apply({"ocr_only": "<Control><Shift>5"}, LABELS, NAMES)
        self.assertEqual(host.listed, "['custom0', '__dummy__', 'da-assistant-ocr_only']")
        path = f"{CINNAMON_ITEM}:/org/cinnamon/desktop/keybindings/custom-keybindings/" \
               "da-assistant-ocr_only/"
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
        host = FakeHost("['da-assistant-ocr_only', 'custom0', 'da-assistant-query_text']")
        writer = keybindings.GSettingsKeybindings("cinnamon", host)
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        self.assertEqual(host.listed, "['custom0', 'da-assistant-ocr_only']")
        self.assertTrue(any(c[1] == "reset-recursively" and "query_text" in c[2]
                            for c in host.calls))
        before = len(host.sets("custom-list"))
        writer.apply({"ocr_only": "F5"}, LABELS, NAMES)
        self.assertEqual(len(host.sets("custom-list")), before)     # unchanged: not set

    def test_remove_keeps_the_users_own_and_writes_nothing_if_none_are_ours(self):
        host = FakeHost("['custom0', 'da-assistant-ocr_only']")
        writer = keybindings.GSettingsKeybindings("cinnamon", host)
        writer.remove(NAMES)
        self.assertEqual(host.listed, "['custom0']")
        host = FakeHost("@as []")
        keybindings.GSettingsKeybindings("cinnamon", host).remove(NAMES)
        self.assertEqual([c for c in host.calls if c[1] != "get"], [])

    def test_quotes_in_the_users_list_survive(self):
        host = FakeHost("['it\\'s mine']")
        keybindings.GSettingsKeybindings("cinnamon", host).apply(
            {"ocr_only": "F5"}, LABELS, NAMES)
        self.assertEqual(host.listed, "['it\\'s mine', 'da-assistant-ocr_only']")


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
            self.assertEqual(host.listed, "['custom0', 'da-assistant-ocr_only']")
            self.assertEqual(self.config().hotkeys_installed["mechanism"],
                             "desktop:cinnamon")
            writes = len(host.calls)
            manager.start()
            self.assertEqual(len(host.calls), writes)       # same as recorded: no write

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
        host = FakeHost("['custom0', 'da-assistant-ocr_only', 'da-assistant-query_text']")
        with Patched(session_of(desktop="cinnamon")):
            self.manager(config, host).start()
        self.assertEqual(host.listed, "['custom0', 'da-assistant-ocr_only']")

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
        self.assertEqual(len(page.values()["hotkeys"]), len(hotkeys.ACTIONS))
        page.clear_all()
        self.assertEqual(page.values(), {"hotkeys": {}})

    def test_one_key_one_action(self):
        page = self.page(hotkeys={"ocr_only": "F5"})
        page.buttons["query_text"].set_accel("F5")
        page._changed("query_text", "F5")
        self.assertEqual(page.values()["hotkeys"], {"query_text": "F5"})
        self.assertEqual(page.buttons["ocr_only"].get_label(), "Disabled")
        self.assertIn("went to Query clipboard text", page.notes["ocr_only"].get_text())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
