"""The tray, the windows and the AppImage must all show the same icon.

The tray backends take an icon *name*, not a file, and resolve it through the GTK icon
theme. A PNG in the source tree is invisible to that lookup unless its directory has
been added to the theme's search path first, so the failure mode this guards is a tray
that silently falls back to a stock icon - or to nothing at all.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gtk

from assistant import icon
from assistant.ui.tray import Tray

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TheIconShips(unittest.TestCase):
    def test_every_size_the_generator_promises_is_committed(self):
        for size in icon.SIZES:
            file = (ROOT / "assistant" / "data" / "icons" / "hicolor" /
                    f"{size}x{size}" / "apps" / f"{icon.NAME}.png")
            self.assertTrue(file.exists(), f"missing {file.relative_to(ROOT)} - "
                                           f"run python3 packaging/make-icon.py")

    def test_the_files_are_the_size_they_claim(self):
        from PIL import Image
        for size in icon.SIZES:
            with Image.open(icon.path(size)) as image:
                self.assertEqual(image.size, (size, size))

    def test_a_panel_sized_request_does_not_get_handed_the_256px_one(self):
        self.assertEqual(os.path.basename(os.path.dirname(
            os.path.dirname(icon.path(22)))), "22x22")


class TheIconResolvesByName(unittest.TestCase):
    def test_registering_makes_the_name_findable_in_the_gtk_theme(self):
        icon.register()
        self.assertTrue(Gtk.IconTheme.get_default().has_icon(icon.NAME),
                        "the bundled icons are not on the theme search path, so every "
                        "tray backend will fall back to a stock icon")

    def test_the_name_is_what_callers_get(self):
        self.assertEqual(icon.name(), icon.NAME)

    def test_registering_twice_does_not_stack_search_paths(self):
        icon.register()
        before = Gtk.IconTheme.get_default().get_search_path()
        icon.register()
        self.assertEqual(Gtk.IconTheme.get_default().get_search_path(), before)


class TheTrayUsesIt(unittest.TestCase):
    """The panel draws the indicator in its own process, from these two properties."""

    def _build(self, legacy=False):
        asked = {}

        class FakeIndicator:
            @staticmethod
            def new(app_id, name, category):
                asked["name"] = name
                return FakeIndicator()

            @staticmethod
            def new_with_path(app_id, name, category, theme_path):
                asked.update(name=name, theme_path=theme_path)
                return FakeIndicator()

            def set_icon_theme_path(self, path):
                asked["theme_path"] = path

            def set_status(self, *a):
                pass

            def set_title(self, *a):
                pass

            def set_menu(self, *a):
                pass

        if legacy:
            del FakeIndicator.new_with_path

        class FakeModule:
            Indicator = FakeIndicator

            class IndicatorCategory:
                APPLICATION_STATUS = 0

            class IndicatorStatus:
                ACTIVE = 0

        from assistant.ui import tray as tray_module
        original = tray_module.gtkcompat.load_indicator
        tray_module.gtkcompat.load_indicator = lambda: (FakeModule, "appindicator")
        try:
            Tray("org.test.App", "Test", Gtk.Menu())
        finally:
            tray_module.gtkcompat.load_indicator = original
        return asked

    def test_the_tray_asks_for_the_application_icon(self):
        self.assertEqual(self._build().get("name"), icon.NAME,
                         "the tray is still asking for a stock icon")

    def test_the_panel_is_told_where_to_find_an_icon_it_does_not_have(self):
        # Nothing installs this icon system-wide, so without the theme path the panel
        # looks up a name it cannot resolve and draws nothing.
        self.assertEqual(self._build().get("theme_path"), icon.THEME_DIR)

    def test_an_older_indicator_library_still_gets_both(self):
        asked = self._build(legacy=True)
        self.assertEqual(asked.get("name"), icon.NAME)
        self.assertEqual(asked.get("theme_path"), icon.THEME_DIR)


if __name__ == "__main__":
    unittest.main()


class NothingShowsTheTray(unittest.TestCase):
    """Stock GNOME, and Ubuntu 26.04 with its extension off: no indicator is shown, and
    with it goes the menu - the app has to say so, or it seems not to have started."""

    def tray(self, kind):
        from unittest import mock
        fake = Tray.__new__(Tray)
        fake.kind = kind
        return fake, mock

    def test_an_indicator_on_wayland_with_no_watcher_is_unseen(self):
        from assistant.ui import tray as tray_module
        fake, mock = self.tray("appindicator")
        with mock.patch.object(tray_module, "watcher_present", return_value=False), \
                mock.patch.object(tray_module.session, "session_type", return_value="wayland"):
            self.assertTrue(fake.unseen())

    def test_with_a_watcher_or_on_x11_or_not_knowing_it_is_not_said(self):
        from assistant.ui import tray as tray_module
        fake, mock = self.tray("appindicator")
        for present, session in ((True, "wayland"), (None, "wayland"), (False, "x11")):
            with mock.patch.object(tray_module, "watcher_present", return_value=present), \
                    mock.patch.object(tray_module.session, "session_type",
                                      return_value=session):
                self.assertFalse(fake.unseen(), (present, session))
        fake, mock = self.tray("xapp")
        with mock.patch.object(tray_module, "watcher_present", return_value=False), \
                mock.patch.object(tray_module.session, "session_type", return_value="wayland"):
            self.assertFalse(fake.unseen())

    def test_the_app_says_how_to_get_one_and_what_works_meanwhile(self):
        from unittest import mock
        from assistant import app as app_module
        assistant = app_module.Assistant.__new__(app_module.Assistant)
        assistant.tray = mock.Mock(unseen=mock.Mock(return_value=True))
        assistant.notify = mock.Mock()
        self.assertFalse(assistant._check_tray())
        message = assistant.notify.call_args[0][0]
        self.assertIn("AppIndicator", message)
        self.assertIn("--action=settings", message)
        assistant.tray.unseen.return_value = False
        assistant.notify.reset_mock()
        assistant._check_tray()
        assistant.notify.assert_not_called()


class GnomeKeysThatLostTheirShift(unittest.TestCase):
    def test_said_once_with_the_keys_and_where_to_change_them(self):
        from unittest import mock
        from assistant import app as app_module
        assistant = app_module.Assistant.__new__(app_module.Assistant)
        assistant.hotkeys = mock.Mock(
            problems={"translate_text": "GNOME holds it as Ctrl+!, which no key press "
                                        "matches; change it in GNOME's Settings"},
            wanted={"translate_text": "<Control><Shift>1"})
        assistant.notify = mock.Mock()
        assistant._warn_lost_shift()
        assistant._warn_lost_shift()
        assistant.notify.assert_called_once()
        message = assistant.notify.call_args[0][0]
        self.assertIn("Ctrl+Shift+1", message)
        self.assertIn("Settings > Apps > DA Assistant", message)


class TheWindowsBelongToTheLauncher(unittest.TestCase):
    """A shell ties a window to its launcher by the program's name - WM_CLASS on X11,
    the app id on Wayland - against the entry's name or its StartupWMClass. GTK took
    the name from argv[0], "run-assistant", which no entry is called: a generic icon,
    and on Wayland a dock entry of its own."""

    def test_the_name_is_set_before_gtk_starts_whatever_the_program_was_run_as(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        launcher = os.path.join(folder, "run-assistant")       # as the AppImage runs it
        with open(launcher, "w") as f:
            f.write(f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
                    "from assistant import gtkcompat\n"
                    "from gi.repository import Gdk, GLib\n"
                    "print(GLib.get_prgname(), Gdk.get_program_class())\n")
        env = dict(os.environ, HOME=folder, XDG_CONFIG_HOME=folder, XDG_DATA_HOME=folder)
        out = subprocess.run([sys.executable, launcher], capture_output=True, text=True,
                             env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [gtkcompat.PROGRAM, "Da-assistant"])

    def _entry(self, path):
        from gi.repository import GLib
        entry = GLib.KeyFile()
        entry.load_from_file(str(path), GLib.KeyFileFlags.NONE)
        return entry

    def test_the_launchers_say_the_same_name(self):
        for path in (ROOT / "packaging" / "appimage" / "da-assistant.desktop",
                     ROOT / "tools" / "da-assistant.desktop"):
            self.assertEqual(path.stem, gtkcompat.PROGRAM)
            self.assertEqual(self._entry(path).get_string("Desktop Entry",
                                                          "StartupWMClass"),
                             gtkcompat.PROGRAM, path)

    def test_the_portals_hidden_entry_claims_no_window(self):
        # It is named after APP_ID, which the portal and the D-Bus name keep; claimed
        # by it, the windows would be a hidden app's.
        import assistant
        from assistant import keybindings
        self.assertNotEqual(assistant.APP_ID, gtkcompat.PROGRAM)
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with open(keybindings.app_entry(data_home=folder), encoding="utf-8") as f:
            self.assertNotIn("StartupWMClass", f.read())
