"""--probe: a report, whatever the session it is run in."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WithoutADisplay(unittest.TestCase):
    """Over ssh, or from a text console - the first thing the README suggests running.
    It used to end in a traceback from the icon theme, and then a core dump from the
    WebKit check."""

    def test_it_reports_what_it_can_and_says_what_needs_the_desktop(self):
        with tempfile.TemporaryDirectory() as home:
            env = {key: value for key, value in os.environ.items()
                   if key not in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                                  "XDG_RUNTIME_DIR")}
            env.update(HOME=home, XDG_CACHE_HOME=os.path.join(home, "cache"))
            done = subprocess.run([sys.executable, os.path.join(ROOT, "run-assistant"),
                                   "--probe"], env=env, capture_output=True, text=True,
                                  timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("Display        : none", done.stdout)
        self.assertIn("Shortcuts      :", done.stdout)
        self.assertIn("Privacy Mode   :", done.stdout)


class TrayHost(unittest.TestCase):
    """With no StatusNotifierWatcher, an indicator on X11 goes to the XEmbed tray, and
    its icon shows there: --probe said it would not show at all. Where there is no
    XEmbed tray either - GNOME on Xorg without the AppIndicator extension, a bare window
    manager - it shows nowhere, and --probe said it went to the XEmbed tray."""

    def test_with_no_watcher_x11_is_told_of_the_xembed_tray(self):
        from assistant.__main__ import tray_host
        self.assertEqual(tray_host(True, wayland=True), "yes")
        self.assertEqual(tray_host(True, wayland=False), "yes")
        self.assertIn("will not show", tray_host(False, wayland=True))
        self.assertIn("AppIndicator extension", tray_host(False, wayland=True))
        for xembed in (True, None):
            x11 = tray_host(False, wayland=False, xembed=xembed)
            self.assertNotIn("will not show", x11)
            self.assertIn("XEmbed system tray", x11)
        # Not known - no python-xlib, say - and said so.
        self.assertIn("if there is one", tray_host(False, False, xembed=None))
        self.assertNotIn("if there is one", tray_host(False, False, xembed=True))

    def test_x11_with_no_xembed_tray_either_is_told_the_icon_will_not_show(self):
        from assistant.__main__ import tray_host
        x11 = tray_host(False, wayland=False, xembed=False)
        self.assertIn("will not show", x11)
        self.assertIn("no XEmbed system tray", x11)
        self.assertIn("AppIndicator extension", x11)


class FakeXDisplay:
    """python-xlib's Display, as far as asking who owns a selection: `owner` is the
    tray's window, or X.NONE where there is no tray; `named`, whether any client ever
    interned the selection's atom."""

    def __init__(self, owner, screen=0, named=True):
        self.owner, self.screen, self.named = owner, screen, named
        self.asked, self.made, self.closed = [], [], False

    def get_default_screen(self):
        return self.screen

    def intern_atom(self, name, only_if_exists=False):
        self.asked.append(name)
        if not (self.named or only_if_exists):
            self.made.append(name)
        return 312 if self.named or not only_if_exists else 0

    def get_selection_owner(self, atom):
        if atom != 312:
            raise AssertionError("BadAtom: X would refuse to say")
        return self.owner

    def close(self):
        self.closed = True


class XEmbedTray(unittest.TestCase):
    """The System Tray Protocol: a tray owns the _NET_SYSTEM_TRAY_S<screen> selection of
    its screen. Asked over a connection of its own, which opens no window."""

    def setUp(self):
        try:
            from Xlib import X
        except ImportError:
            self.skipTest("python-xlib is not installed")
        self.none = X.NONE

    def present(self, display):
        from assistant.ui import tray
        with mock.patch("Xlib.display.Display", lambda *args: display):
            return tray.xembed_tray_present()

    def test_a_tray_is_one_that_owns_the_selection(self):
        there = FakeXDisplay(owner=object(), screen=1)
        self.assertIs(self.present(there), True)
        self.assertEqual(there.asked, ["_NET_SYSTEM_TRAY_S1"])
        self.assertTrue(there.closed)
        nobody = FakeXDisplay(owner=self.none)
        self.assertIs(self.present(nobody), False)
        self.assertTrue(nobody.closed)
        # No client ever named the atom - a bare window manager - and asking makes none.
        never = FakeXDisplay(owner=self.none, named=False)
        self.assertIs(self.present(never), False)
        self.assertEqual(never.made, [])

    def test_not_known_without_a_display_or_python_xlib(self):
        from Xlib import error
        from assistant.ui import tray

        for raised in (error.DisplayNameError(None),
                       error.DisplayConnectionError(":9", "refused"),
                       error.ConnectionClosedError("server")):
            def refused(*args, raised=raised):
                raise raised

            with mock.patch("Xlib.display.Display", refused):
                self.assertIsNone(tray.xembed_tray_present(), raised)
        with mock.patch.dict(sys.modules, {"Xlib": None}):
            self.assertIsNone(tray.xembed_tray_present())


if __name__ == "__main__":
    unittest.main()
