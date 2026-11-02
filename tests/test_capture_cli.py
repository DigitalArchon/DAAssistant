"""The desktop's own screenshot tools: Cinnamon's D-Bus, slurp and gnome-screenshot.

Nothing here runs a screenshot tool or reaches the session bus. The programs are
answered by a stand-in for cli._run that records what it was asked to run, the session
(desktop, portal, tools installed) is decided by each test, and every file is written
under a temporary directory of the test's own.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import BytesIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib
from PIL import Image

from assistant import capture, session
from assistant.capture import cli


def png(size=(64, 48)):
    """A PNG that does not compress to nothing, so that half of it is half a file."""
    buf = BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buf, "PNG")
    return buf.getvalue()


def answered(cmd, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)


def gvariant(text, kind="s"):
    return GLib.Variant.parse(GLib.VariantType(kind), text, None, None).unpack()


def pump(until, timeout=3.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        # Where _tempfile makes its directories: this test's own.
        patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.dir})
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []

    def backend(self, tool, desktop="cinnamon"):
        backend = cli.CliBackend.__new__(cli.CliBackend)
        backend.desktop, backend.tool = desktop, tool
        return backend

    def programs(self, answer):
        """Answer every program the backend runs with `answer(cmd)`."""
        def fake(cmd, timeout=120):
            self.calls.append(list(cmd))
            return answer(cmd)

        patch = mock.patch.object(cli, "_run", side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)


class CinnamonsWholeScreen(ToolCase):
    """org.Cinnamon.Screenshot is (b include_cursor, b flash, s filename), and answers
    before the file is written (see /usr/share/cinnamon/js/ui/cinnamonDBus.js). It was
    sent ("false", path), which fits no signature, so it failed every time."""

    def test_it_is_called_with_its_own_signature(self):
        data = png()

        def answer(cmd):
            with open(gvariant(cmd[-1]), "wb") as f:
                f.write(data)
            return answered(cmd, stdout="()\n")

        self.programs(answer)
        image = self.backend("cinnamon").capture_fullscreen()
        self.assertEqual(image.size, (64, 48))
        cmd = self.calls[0]
        self.assertEqual(cmd[:9], ["gdbus", "call", "--session", "--dest", "org.Cinnamon",
                                   "--object-path", "/org/Cinnamon", "--method",
                                   "org.Cinnamon.Screenshot"])
        include_cursor, flash, path = gvariant(f"({', '.join(cmd[9:])})", "(bbs)")
        self.assertEqual((include_cursor, flash), (False, False))
        self.assertTrue(path.startswith(self.dir), path)
        self.assertEqual(os.listdir(self.dir), [])          # and nothing left behind

    def test_the_file_is_waited_for_until_it_is_whole(self):
        data = png((400, 300))
        writers = []

        def answer(cmd):
            path = gvariant(cmd[-1])

            def cinnamon_writes():
                time.sleep(0.1)                     # the grab is taken on the next frame
                with open(path, "wb") as f:
                    f.write(data[:len(data) // 2])
                    f.flush()
                    time.sleep(0.4)                 # still, but only half written
                    f.write(data[len(data) // 2:])

            writers.append(threading.Thread(target=cinnamon_writes, daemon=True))
            writers[0].start()
            return answered(cmd, stdout="()\n")     # before any of it is written

        self.programs(answer)
        image = self.backend("cinnamon").capture_fullscreen()
        writers[0].join(5)
        self.assertEqual(image.size, (400, 300))
        self.assertEqual(image.tobytes(), Image.open(BytesIO(data)).tobytes())

    def test_a_file_that_never_comes_is_a_failure_not_a_hang(self):
        self.programs(lambda cmd: answered(cmd, stdout="()\n"))
        with mock.patch.object(cli, "CINNAMON_WRITE_TIMEOUT", 0.3):
            with self.assertRaises(cli.CaptureError) as raised:
                self.backend("cinnamon").capture_fullscreen()
        self.assertIn("no screenshot was written", str(raised.exception))

    def test_a_refused_call_says_why(self):
        self.programs(lambda cmd: answered(cmd, 1, stderr=(
            "Error: GDBus.Error:org.freedesktop.DBus.Error.InvalidArgs: Type of message, "
            "“(ss)”, does not match expected type “(bbs)”\n")))
        with self.assertRaises(cli.CaptureError) as raised:
            self.backend("cinnamon").capture_fullscreen()
        self.assertIn("does not match expected type", str(raised.exception))

    def test_there_is_no_region_step_only_the_whole_screen_cropped_here(self):
        """ScreenshotArea takes a rectangle and picks none, and its failure read as the
        user's cancel, which ended the chain. --probe prints this chain."""
        def chain(portal):
            with mock.patch.object(session, "session_type", return_value="wayland"), \
                    mock.patch.object(session, "desktop", return_value="cinnamon"), \
                    mock.patch.object(session, "portal_has", return_value=portal), \
                    mock.patch.object(session, "portal_version", return_value=2), \
                    mock.patch.object(session, "has_cinnamon_screenshot",
                                      return_value=True), \
                    mock.patch.object(cli.shutil, "which", return_value=None):
                return capture.CaptureManager().describe_chain()

        self.assertEqual(chain(portal=False), ["cli-cinnamon-fullscreen+crop"])
        self.assertEqual(chain(portal=True), ["portal-interactive", "portal-fullscreen+crop",
                                              "cli-cinnamon-fullscreen+crop"])
        self.assertFalse(self.backend("cinnamon").supports_region_selection())


class OnlyASilentExitIsACancel(ToolCase):
    """A cancel ends the whole chain; a failure goes on to the next rung. So a tool that
    exits non-zero is taken for the user's Esc only when it said nothing - or, slurp,
    only that the selection was cancelled."""

    def select(self, tool, code, stderr="", write=False):
        def answer(cmd):
            if cmd[0] == "slurp":
                return answered(cmd, code, stdout="" if code else "10,20 300x200\n",
                                stderr=stderr)
            if write:
                with open(cmd[-1], "wb") as f:
                    f.write(png())
            return answered(cmd, code, stderr=stderr)

        self.programs(answer)
        backend = self.backend(tool, desktop="sway" if tool == "grim" else "gnome")
        return backend._select_blocking()

    def test_escape_in_slurp_is_a_cancel(self):
        self.assertIsNone(self.select("grim", 1, "selection cancelled\n"))
        self.assertIsNone(self.select("grim", 1))

    def test_slurp_failing_is_a_failure(self):
        with self.assertRaises(cli.CaptureError) as raised:
            self.select("grim", 1, "compositor doesn't support wlr-layer-shell-unstable-v1")
        self.assertIn("layer-shell", str(raised.exception))

    def test_gnome_screenshot(self):
        self.assertIsNone(self.select("gnome-screenshot", 0))      # Esc
        self.assertIsNone(self.select("gnome-screenshot", 1))      # Esc, some versions
        with self.assertRaises(cli.CaptureError) as raised:
            self.select("gnome-screenshot", 1,
                        "Unable to use GNOME Shell's builtin screenshot interface")
        self.assertIn("builtin screenshot interface", str(raised.exception))
        self.assertEqual(self.select("gnome-screenshot", 0, write=True).size, (64, 48))

    def test_after_a_tool_that_failed_the_next_rung_is_tried(self):
        self.programs(lambda cmd: answered(cmd, 1, stderr="cannot open display"))
        manager = capture.CaptureManager.__new__(capture.CaptureManager)
        manager.last_backend = None
        backend = self.backend("grim", desktop="sway")
        manager._chain = lambda: [("cli-grim", backend.select_region),
                                  ("next", lambda done, failed: done("the next rung's"))]
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/slurp"):
            outcome = []
            manager.select_region(outcome.append, outcome.append)
            pump(lambda: outcome)
        self.assertEqual(outcome, ["the next rung's"])
        self.assertEqual(manager.last_backend, "next")


if __name__ == "__main__":
    unittest.main()
