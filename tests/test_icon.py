"""The tray, the windows and the AppImage must all show the same icon.

The tray backends take an icon *name*, not a file, and resolve it through the GTK icon
theme. A PNG in the source tree is invisible to that lookup unless its directory has
been added to the theme's search path first, so the failure mode this guards is a tray
that silently falls back to a stock icon - or to nothing at all.
"""

import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
