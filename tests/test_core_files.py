"""A crash puts nothing of the app's memory on disk: no core file, and nothing the
system's crash collector keeps - a conversation, its captures and attachments, and a key
typed in with no keyring live only there (docs/security-and-privacy.md). And the desktop
portal, which refuses a process that is not dumpable, is still answered: the process is
dumpable while it asks (coredumps.py).

The real thing runs in processes of their own, each started from a shell's state: the
suite's own process has no core dumps either (support.py), and what it starts inherits
its core limit of 1 and its filter of 0, which would read as the app's doing whatever the
app did. TheSuiteItself checks the suite's own.
"""

import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: E402,F401  (pins GI versions)
from gi.repository import GLib  # noqa: E402

from assistant import coredumps, hotkeys  # noqa: E402
from assistant.capture.portal import PortalBackend  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Started as a shell with `ulimit -c unlimited` would start it, then the app's own
# launcher run as it is from a desktop entry or a terminal, with an argument it answers
# before anything else; then the core size limit it left, and whether it is dumpable.
CHILD = r"""
import ctypes, resource, runpy, sys
soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
if hard == 0:
    print("already none")
    sys.exit(0)
resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
sys.argv = [sys.argv[1]] + sys.argv[2:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    pass
print("limit", resource.getrlimit(resource.RLIMIT_CORE), "of", hard)
print("dumpable", ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))
print("filter", open("/proc/self/coredump_filter").read().strip())
"""


# Before each probe: the kernel's own filter, as a shell started at login has it, where
# the suite's would leave 0 (support.py). A process started is dumpable again, and may
# write its own.
AS_A_SHELL_HAS_IT = ("with open('/proc/self/coredump_filter', 'w', encoding='ascii') as f:\n"
                     "    f.write('0x33')\n")


def run(code, *args, env=None):
    return subprocess.run([sys.executable, "-c", AS_A_SHELL_HAS_IT + code, *args],
                          capture_output=True, text=True, timeout=120, cwd=ROOT, env=env)


class NoCoreDumps(unittest.TestCase):
    def test_every_way_in_turns_them_off_first(self):
        """--version is answered by the argument parser, before the app does anything
        else: core dumps are off before even that."""
        for args in (["--version"], ["--action", "no-such-action"]):
            with self.subTest(args=args):
                done = run(CHILD, os.path.join(ROOT, "run-assistant"), *args)
                if "already none" in done.stdout:
                    self.skipTest("this test's own process may have no core file at all")
                # A soft limit of 1, at which the kernel skips the dump even of a
                # dumpable process, the hard one left as it was; undumpable; and no
                # memory in any dump (the DA apps' security standard, Nov 2026).
                hard = done.stdout.split(" of ")[1].split()[0]
                self.assertIn(f"limit (1, {hard}) of {hard}", done.stdout, done.stderr)
                self.assertIn("dumpable 0", done.stdout, done.stderr)
                self.assertIn("filter 00000000", done.stdout, done.stderr)

    def test_a_process_it_starts_has_no_core_file_either(self):
        """WebKit's processes, which draw the conversation, are started from the app.
        (An exec'd program is dumpable again, as the kernel has it: its limit is what
        passes on.)"""
        probe = ("import resource, subprocess, sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from assistant.coredumps import no_core_dumps\n"
                 "resource.setrlimit(resource.RLIMIT_CORE, (0, resource.RLIM_INFINITY))\n"
                 "no_core_dumps()\n"
                 "print(subprocess.run([sys.executable, '-c', 'import resource; "
                 "print(resource.getrlimit(resource.RLIMIT_CORE)[0], "
                 "open(\"/proc/self/coredump_filter\").read().strip())'], "
                 "capture_output=True, text=True).stdout.strip())\n")
        done = run(probe, ROOT)
        if "Operation not permitted" in done.stderr:
            self.skipTest("this test's own process may not raise its hard limit")
        self.assertEqual(done.stdout.strip(), "1 00000000", done.stderr)

    def test_with_a_hard_limit_of_0_already_0_stands_and_the_rest_holds(self):
        probe = ("import ctypes, resource, sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from assistant.coredumps import no_core_dumps\n"
                 "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
                 "no_core_dumps()\n"
                 "print(resource.getrlimit(resource.RLIMIT_CORE),"
                 " ctypes.CDLL(None).prctl(3, 0, 0, 0, 0),"
                 " open('/proc/self/coredump_filter').read().strip())\n")
        self.assertEqual(run(probe, ROOT).stdout.strip(), "(0, 0) 0 00000000")

    def test_the_filter_is_written_before_the_process_is_undumpable(self):
        """Once undumpable, a process's /proc entries are root's, and writing its own
        coredump_filter is refused (EACCES, measured): written after, it was never set."""
        probe = ("import sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from assistant import coredumps\n"
                 "order = []\n"
                 "real_open, real_prctl = open, coredumps._prctl\n"
                 "import builtins\n"
                 "def opened(path, *a, **k):\n"
                 "    if str(path).endswith('coredump_filter'): order.append('filter')\n"
                 "    return real_open(path, *a, **k)\n"
                 "def prctl(option, value=0):\n"
                 "    order.append(('prctl', option, value))\n"
                 "    return real_prctl(option, value)\n"
                 "builtins.open, coredumps._prctl = opened, prctl\n"
                 "coredumps.no_core_dumps()\n"
                 "builtins.open = real_open\n"
                 "print(order[:2], real_open('/proc/self/coredump_filter').read().strip())\n")
        self.assertEqual(run(probe, ROOT).stdout.strip(),
                         "['filter', ('prctl', 4, 0)] 00000000")

    def test_it_is_dumpable_only_while_the_portal_is_asked(self):
        probe = ("import sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "import os\n"
                 "from assistant import coredumps, hostenv\n"
                 "seen = [coredumps.dumpable()]\n"
                 "coredumps.no_core_dumps()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "first = coredumps.asking_the_portal()\n"
                 "second = coredumps.asking_the_portal()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "first(); first()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "second()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "with coredumps.for_the_portal():\n"
                 "    seen.append(coredumps.dumpable())\n"
                 "seen.append(coredumps.dumpable())\n"
                 "print(seen, os.environ['GTK_USE_PORTAL'],"
                 " hostenv.host_env()['GTK_USE_PORTAL'])\n")
        done = run(probe, ROOT, env=dict(os.environ, GTK_USE_PORTAL="1"))
        # Dumpable at the start; not, once turned off; while two requests are under way
        # and until the last is answered; asked again; not after. GTK's own dialogs for
        # the app; the desktop's choice for what it starts.
        self.assertEqual(done.stdout.strip(), "[1, 0, 1, 1, 0, 1, 0] 0 1", done.stderr)

    def test_quitting_closes_it_at_once_and_nothing_asked_after_opens_it(self):
        """The shortcuts' Close, sent through the window as the app quit, was never
        answered - the connection closed first - and the process stayed dumpable until
        it exited (1 to 66 ms on the lab's desktops, Nov 14 2026)."""
        probe = ("import sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from assistant import coredumps\n"
                 "coredumps.no_core_dumps()\n"
                 "unanswered = coredumps.asking_the_portal()\n"
                 "seen = [coredumps.dumpable()]\n"
                 "coredumps.quitting()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "late = coredumps.asking_the_portal()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "with coredumps.for_the_portal():\n"
                 "    seen.append(coredumps.dumpable())\n"
                 "late(); unanswered()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "print(seen)\n")
        done = run(probe, ROOT)
        # Dumpable for a request not answered (a screenshot's picker left open); not, the
        # moment the app quits; nor for anything asked after.
        self.assertEqual(done.stdout.strip(), "[1, 0, 0, 0, 0]", done.stderr)

    def test_it_is_not_dumpable_as_it_exits_whatever_way_it_leaves(self):
        probe = ("import atexit, sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from assistant import coredumps\n"
                 "coredumps.no_core_dumps()\n"
                 "coredumps.asking_the_portal()\n"
                 "seen = [coredumps.dumpable()]\n"
                 "atexit._run_exitfuncs()\n"
                 "seen.append(coredumps.dumpable())\n"
                 "print(seen)\n")
        done = run(probe, ROOT)
        self.assertEqual(done.stdout.strip(), "[1, 0]", done.stderr)

    def test_the_desktop_portal_answers_it_while_it_asks_and_refuses_it_otherwise(self):
        """Measured with xdg-desktop-portal 1.20.0: "Portal operation not allowed:
        Unable to open /proc/PID/root" for every request of an undumpable process. On
        this session's own portal, with a request that shows nothing (Settings.Read);
        skipped where there is none."""
        probe = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from gi.repository import Gio, GLib\n"
            "from assistant import coredumps\n"
            "def read(bus):\n"
            "    try:\n"
            "        bus.call_sync('org.freedesktop.portal.Desktop',\n"
            "            '/org/freedesktop/portal/desktop', 'org.freedesktop.portal.Settings',\n"
            "            'Read', GLib.Variant('(ss)', ('org.freedesktop.appearance',\n"
            "            'color-scheme')), None, Gio.DBusCallFlags.NONE, 10000, None)\n"
            "        return 'answered'\n"
            "    except GLib.Error as e:\n"
            "        return 'refused' if 'not allowed' in e.message else 'no portal'\n"
            "try:\n"
            "    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)\n"
            "except GLib.Error:\n"
            "    print('no portal'); sys.exit(0)\n"
            "coredumps.no_core_dumps()\n"
            "seen = [read(bus)]\n"
            "with coredumps.for_the_portal():\n"
            "    seen.append(read(bus))\n"
            "seen.append(read(bus))\n"
            "print(' '.join(seen))\n")
        done = run(probe, ROOT)
        if "no portal" in done.stdout or not done.stdout.strip():
            self.skipTest(f"no desktop portal answers here ({done.stderr.strip()[-200:]})")
        # Refused undumpable; answered while it asks; and on that connection from then
        # on, undumpable again.
        self.assertEqual(done.stdout.strip(), "refused answered answered")


# A Python started from a test, as a test window's WebKit process is: its core limit and
# its filter.
WHAT_IT_STARTS = ("import resource\n"
                  "with open('/proc/self/coredump_filter', encoding='ascii') as f:\n"
                  "    print(resource.getrlimit(resource.RLIMIT_CORE)[0], f.read().strip())\n")


class TheSuiteItself(unittest.TestCase):
    """The suite has no core dumps either, with the app's own guard (support.py): a
    test's WebKitWebProcess that aborted left a core of 5-6 MB in systemd-coredump on the
    lab's Fedora and Omarchy (Nov 14 2026)."""

    REPORT = "DA_ASSISTANT_CORE_REPORT"

    def test_its_process_and_what_it_starts_leave_no_core(self):
        """Its core limit and its filter, which every test window's WebKit process
        inherits. Whether it is undumpable is asked of a run of this test alone (below):
        in the whole suite, a test that asks the portal opens the window for a while."""
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        with open("/proc/self/coredump_filter", encoding="ascii") as f:
            own = f.read().strip()
        child = subprocess.run([sys.executable, "-c", WHAT_IT_STARTS], capture_output=True,
                               text=True, timeout=60, check=False).stdout.split()
        report = os.environ.get(self.REPORT)
        if report:
            with open(report, "w", encoding="utf-8") as f:
                json.dump({"dumpable": coredumps.dumpable(), "limit": [soft, hard],
                           "filter": own, "child": child}, f)
        limit = 0 if hard == 0 else 1          # where the hard limit is 0, 0 stands
        self.assertEqual((soft, own), (limit, "00000000"))
        self.assertEqual(child, [str(limit), "00000000"])

    def test_a_run_of_it_started_from_a_shell_is_undumpable_from_the_start(self):
        """Started as a shell with `ulimit -c unlimited` starts it, with the kernel's own
        filter: the suite's documented runner (MANUAL.md, Tests), for the test above."""
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        report = os.path.join(folder, "report.json")
        starter = ("import os, resource, sys\n"
                   "soft, hard = resource.getrlimit(resource.RLIMIT_CORE)\n"
                   "if hard == 0:\n"
                   "    print('already none')\n"
                   "    sys.exit(0)\n"
                   "resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))\n"
                   "os.execv(sys.executable, [sys.executable, '-m', 'unittest', 'discover',"
                   " '-s', 'tests', '-p', 'test_core_files.py',"
                   " '-k', 'test_its_process_and_what_it_starts_leave_no_core'])\n")
        done = run(starter, env=dict(os.environ, **{self.REPORT: report}))
        if "already none" in done.stdout:
            self.skipTest("this test's own process may have no core file at all")
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        with open(report, encoding="utf-8") as f:
            state = json.load(f)
        hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
        self.assertEqual(state, {"dumpable": 0, "limit": [1, hard], "filter": "00000000",
                                 "child": ["1", "00000000"]})


class TheRequestsItMakes(unittest.TestCase):
    """Each of the app's own portal requests opens the window before it goes and closes
    it once answered: its screenshot, and its global shortcuts' session."""

    def setUp(self):
        self.calls = []
        patches = (mock.patch.object(coredumps, "_undumpable", True),
                   mock.patch.object(coredumps, "_asking", 0),
                   mock.patch.object(coredumps, "_quitting", False),
                   mock.patch.object(coredumps, "_prctl",
                                     lambda option, value=0: self.calls.append(value)))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def opened(self):
        return self.calls[-1] == 1 if self.calls else False

    class Bus:
        def __init__(self, test):
            self.test, self.pending, self.sent = test, [], []

        def get_unique_name(self):
            return ":1.42"

        def signal_subscribe(self, *args):
            return 1

        def signal_unsubscribe(self, _sub):
            pass

        def call(self, *args):
            self.sent.append((args[3], self.test.opened()))
            self.pending.append(args[-1])

        def call_sync(self, *args):
            self.sent.append((args[3], self.test.opened()))

        def call_finish(self, _result):
            return None

        def answer(self):
            callback = self.pending.pop(0)
            if callback is not None:
                callback(self, None)

    def test_a_screenshot_is_asked_for_dumpable_and_the_process_is_not_once_answered(self):
        portal = PortalBackend()
        bus = portal._bus = self.Bus(self)
        with mock.patch.object(PortalBackend, "supports_interactive", return_value=False):
            portal._screenshot(False, lambda image: None, lambda message: None)
        self.assertEqual(bus.sent, [("Screenshot", True)])
        self.assertTrue(self.opened(), "until the portal has answered")
        bus.answer()
        self.assertEqual(self.calls, [1, 0])

    def test_the_global_shortcuts_session_is_asked_for_dumpable_at_every_step(self):
        portal = hotkeys.PortalHotkeys(lambda name: None, {"ocr_only": "<Control>1"})
        bus = self.Bus(self)
        with mock.patch.object(portal, "_connect", return_value=bus):
            portal.start()
        self.assertEqual(bus.sent, [("CreateSession", True)])
        self.assertEqual(self.calls, [1, 0])
        portal.session_handle = "/org/freedesktop/portal/desktop/session/1_42/t"
        portal._bind()
        self.assertEqual(bus.sent[-1], ("BindShortcuts", True))
        self.assertTrue(self.opened())
        bus.answer()
        self.assertFalse(self.opened())
        portal.configure()
        self.assertEqual(bus.sent[-1], ("ConfigureShortcuts", True))
        bus.answer()
        self.assertEqual(self.calls, [1, 0, 1, 0, 1, 0])
        bus.flush_sync = bus.close = lambda *_a: None
        portal.stop()
        # The Close has no answer to read: sent with none asked for (no callback), and
        # never through the window, which nothing would ever close again.
        self.assertEqual(bus.sent[-1], ("Close", False))
        self.assertEqual(bus.pending, [None])
        self.assertEqual(self.calls, [1, 0, 1, 0, 1, 0])

    def test_quitting_the_app_closes_the_window_first_and_its_shortcuts_leave_it_so(self):
        """A screenshot's picker still open as the app quits, and its shortcuts' session
        to close: undumpable before anything else Assistant.quit does, and so to the end."""
        from assistant import app as app_module
        bus = self.Bus(self)
        screenshot = PortalBackend()
        screenshot._bus = bus
        with mock.patch.object(PortalBackend, "supports_interactive", return_value=False):
            screenshot._screenshot(False, lambda image: None, lambda message: None)
        self.assertTrue(self.opened(), "the picker, not answered")
        portal = hotkeys.PortalHotkeys(lambda name: None, {"ocr_only": "<Control>1"})
        portal.bus = bus
        portal.session_handle = "/org/freedesktop/portal/desktop/session/1_42/t"
        bus.flush_sync = bus.close = lambda *_a: None
        manager = hotkeys.HotkeyManager.__new__(hotkeys.HotkeyManager)
        manager.impl, manager._recheck_source = portal, None
        assistant = app_module.Assistant.__new__(app_module.Assistant)
        assistant.hotkeys, assistant.service = manager, mock.Mock()
        assistant.encyclopedia = mock.Mock()
        seen = []
        assistant.service.release.side_effect = lambda: seen.append(self.opened())
        with mock.patch.object(app_module.webkitguard, "note_clean_exit",
                               side_effect=lambda: seen.append(self.opened())), \
                mock.patch.object(app_module.Gtk, "main_quit"), \
                mock.patch.object(app_module, "Notify", None):
            assistant.quit()
        self.assertEqual(seen, [False, False])
        self.assertEqual(bus.sent[-1], ("Close", False))
        self.assertEqual(self.calls, [1, 0])
        bus.answer()                        # the picker's answer, were it ever to come
        self.assertFalse(self.opened())

    def test_registering_its_name_with_the_portal_is_asked_dumpable(self):
        bus = self.Bus(self)
        with mock.patch("gi.repository.Gio.DBusConnection.new_for_address_sync",
                        return_value=bus), \
                mock.patch("gi.repository.Gio.dbus_address_get_for_bus_sync",
                           return_value="unix:path=/nowhere"), \
                mock.patch.object(hotkeys.keybindings, "app_entry"):
            hotkeys.PortalHotkeys(lambda name: None, {})._connect()
        self.assertEqual(bus.sent, [("Register", True)])
        self.assertEqual(self.calls, [1, 0])

    def test_a_request_that_could_not_go_closes_the_window(self):
        portal = hotkeys.PortalHotkeys(lambda name: None, {})
        portal.bus = mock.Mock()
        portal.bus.call.side_effect = GLib.Error("no")
        with self.assertRaises(GLib.Error):
            portal._ask("/p", "i", "M", None)
        self.assertEqual(self.calls, [1, 0])


if __name__ == "__main__":
    unittest.main()
