"""--probe: a report, whatever the session it is run in."""
import os
import subprocess
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
