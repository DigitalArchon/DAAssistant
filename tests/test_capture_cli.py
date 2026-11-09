"""The desktop's own screenshot tools: Cinnamon's D-Bus, slurp and gnome-screenshot -
and the chain that runs them, the portal's rung, and the windows a region is chosen in.

Nothing here runs a screenshot tool or reaches the session bus. The programs are
answered by a stand-in for cli._run that records what it was asked to run, the session
(desktop, portal, tools installed) is decided by each test, and every file is written
under a temporary directory of the test's own. The selection windows are real ones, on
the test's virtual display, grabbing a stand-in for the seat.
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

    CHATTER = ("Gtk-Message: 10:21:07.316: Failed to load module \"canberra-gtk-module\"\n"
               "(gnome-screenshot:4242): GLib-GIO-DEBUG: 10:21:07.400: _g_io_module_get\n"
               "qt.qpa.wayland: Wayland does not support QWindow::requestActivate()\n"
               "kf.kirigami.platform: Failed to find a Kirigami platform plugin\n"
               "libEGL warning: DRI3: Screen seems not DRI3 capable\n"
               "MESA-LOADER: failed to open iris: cannot open shared object file\n")

    def test_what_every_run_prints_does_not_make_an_escape_a_failure(self):
        # Taken for a complaint, it sent the chain on to a window for cropping the
        # whole screen after the user had pressed Esc.
        self.assertIsNone(self.select("grim", 1, self.CHATTER + "selection cancelled\n"))
        self.assertIsNone(self.select("gnome-screenshot", 1, self.CHATTER))
        with mock.patch.object(cli, "_spectacle_args",
                               side_effect=lambda path, region: ["spectacle", path]):
            self.assertIsNone(self.select("spectacle", 1, self.CHATTER))
            with self.assertRaises(cli.CaptureError) as raised:
                self.select("spectacle", 1, self.CHATTER + "Could not take a screenshot\n")
        self.assertEqual(str(raised.exception), "spectacle failed: Could not take a screenshot")
        with self.assertRaises(cli.CaptureError):
            self.select("gnome-screenshot", 1,
                        "(gnome-screenshot:4242): Gtk-WARNING **: cannot open display: :9")

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


class SpectacleOfItsOwn(unittest.TestCase):
    """Spectacle runs once per session: started while it was running, it handed the
    capture to the one running and exited at once, which read as a cancel - and the
    directory it was to write into was removed before the picture came."""

    HELP = ("Usage: spectacle [options]\n"
            "  -b, --background            Take a screenshot and exit without showing the GUI\n"
            "  -n, --nonotify              In background mode, do not pop up a notification\n"
            "  -o, --output <fileName>     In background mode, save image to specified file\n"
            "  -r, --region                Capture a rectangular region of the screen\n"
            "  -f, --fullscreen            Capture the entire desktop (default)\n")
    NEW_INSTANCE = ("  -i, --new-instance          Starts a new GUI instance of spectacle "
                    "without registering to DBus\n")

    def args(self, help_text):
        # What a Spectacle installed here said is kept (cli._spectacle_help): each of
        # these is a Spectacle of its own.
        cli._spectacle_help_cache.clear()
        self.addCleanup(cli._spectacle_help_cache.clear)
        with mock.patch.object(cli.subprocess, "run",
                               return_value=answered(["spectacle"], stdout=help_text)):
            return cli._spectacle_args("/run/x/capture.png", region=True)

    def test_a_new_instance_where_it_offers_one(self):
        self.assertEqual(self.args(self.HELP + self.NEW_INSTANCE),
                         ["spectacle", "--background", "--new-instance", "--nonotify",
                          "--region", "--output", "/run/x/capture.png"])
        self.assertNotIn("--new-instance", self.args(self.HELP))


class SpectacleIsAskedOnce(ToolCase):
    """`spectacle --help` was run at every capture - a Qt program started, and waited
    for, before the picker could open."""

    def setUp(self):
        super().setUp()
        cli._spectacle_help_cache.clear()
        self.addCleanup(cli._spectacle_help_cache.clear)
        self.program = os.path.join(self.dir, "spectacle")
        with open(self.program, "w") as f:
            f.write("#!/bin/sh\n")
        self.runs = []

    def run_help(self, cmd, **_kwargs):
        self.runs.append(cmd)
        return answered(cmd, stdout=SpectacleOfItsOwn.HELP + SpectacleOfItsOwn.NEW_INSTANCE)

    def test_once_a_run_and_again_for_a_program_updated(self):
        with mock.patch.object(cli.shutil, "which", return_value=self.program), \
                mock.patch.object(cli.subprocess, "run", side_effect=self.run_help):
            cli._spectacle_args("/run/x/a.png", region=True)
            second = cli._spectacle_args("/run/x/b.png", region=False)
            self.assertEqual(len(self.runs), 1)
            self.assertEqual(second, ["spectacle", "--background", "--new-instance",
                                      "--nonotify", "--fullscreen", "--output",
                                      "/run/x/b.png"])
            with open(self.program, "a") as f:
                f.write("# a newer Spectacle\n")
            cli._spectacle_args("/run/x/c.png", region=True)
        self.assertEqual(len(self.runs), 2)

    def test_not_kept_when_it_cannot_be_told_which_program_it_was(self):
        with mock.patch.object(cli.shutil, "which", return_value=None), \
                mock.patch.object(cli.subprocess, "run", side_effect=self.run_help):
            cli._spectacle_args("/run/x/a.png", region=True)
            cli._spectacle_args("/run/x/b.png", region=True)
        self.assertEqual(len(self.runs), 2)

    def test_nor_when_it_did_not_answer(self):
        def silent(cmd, **_kwargs):
            self.runs.append(cmd)
            raise subprocess.TimeoutExpired(cmd, 10)

        with mock.patch.object(cli.shutil, "which", return_value=self.program), \
                mock.patch.object(cli.subprocess, "run", side_effect=silent):
            self.assertEqual(cli._spectacle_args("/run/x/a.png", region=True)[1], "-b")
            cli._spectacle_args("/run/x/b.png", region=True)
        self.assertEqual(len(self.runs), 2)


class PortalsThatPickNoRegion(unittest.TestCase):
    """xdg-desktop-portal-wlr - Sway's, and other wlroots compositors' - reads the
    portal's `interactive` and opens no picker: it answers with the whole of one output,
    which the chain handed back as the region chosen, and grim+slurp was never reached."""

    def chain(self, desktop, wlr=False, slurp=True):
        def which(name):
            return f"/usr/bin/{name}" if name == "grim" or (name == "slurp" and slurp) \
                else None

        with mock.patch.object(session, "session_type", return_value="wayland"), \
                mock.patch.object(session, "desktop", return_value=desktop), \
                mock.patch.object(session, "portal_has", return_value=True), \
                mock.patch.object(session, "portal_version", return_value=2), \
                mock.patch.object(session, "has_cinnamon_screenshot", return_value=False), \
                mock.patch.object(session, "portal_backend_running",
                                  side_effect=lambda name: wlr and name == "wlr"), \
                mock.patch.object(cli.shutil, "which", side_effect=which):
            return capture.CaptureManager().describe_chain()

    def test_sway_leads_with_grim_and_slurp(self):
        self.assertEqual(self.chain("sway"), ["cli-grim", "portal-fullscreen+crop",
                                              "cli-grim-fullscreen+crop"])

    def test_without_slurp_the_whole_screen_is_cropped_in_a_window_of_ours(self):
        self.assertEqual(self.chain("sway", slurp=False),
                         ["portal-fullscreen+crop", "cli-grim-fullscreen+crop"])

    def test_another_compositor_is_known_by_the_wlr_portal_running(self):
        self.assertEqual(self.chain("unknown", wlr=True)[0], "cli-grim")
        self.assertEqual(self.chain("unknown", wlr=False)[0], "portal-interactive")

    def test_hyprlands_own_portal_picks_a_region_and_keeps_its_rung(self):
        self.assertEqual(self.chain("hyprland")[0], "portal-interactive")

    def test_the_wlr_portal_is_looked_for_on_the_session_bus(self):
        saved = dict(session._backends)
        self.addCleanup(session._backends.update, saved)
        self.addCleanup(session._backends.clear)
        session._backends.clear()
        with mock.patch.object(session.subprocess, "run",
                               return_value=answered([], stdout="(true,)\n")) as run, \
                mock.patch.object(session, "portal_answered", return_value=True):
            self.assertTrue(session.portal_backend_running("wlr"))
            self.assertTrue(session.portal_backend_running("wlr"))
        self.assertEqual(run.call_count, 1)
        self.assertIn("org.freedesktop.impl.portal.desktop.wlr", run.call_args[0][0])
        # A "no" before the portal has answered may be "not yet": asked again.
        session._backends.clear()
        with mock.patch.object(session.subprocess, "run",
                               return_value=answered([], stdout="(false,)\n")) as run, \
                mock.patch.object(session, "portal_answered", return_value=False):
            self.assertFalse(session.portal_backend_running("wlr"))
            self.assertFalse(session.portal_backend_running("wlr"))
        self.assertEqual(run.call_count, 2)


class APortalStillStarting(unittest.TestCase):
    """Started at login, the app can ask before xdg-desktop-portal answers, and gdbus
    gives up after 5 s. That "" was kept as the answer for the whole run: no portal
    screenshots, no region picker, and no portal shortcuts."""

    XML = ("node /org/freedesktop/portal/desktop {\n"
           "  interface org.freedesktop.portal.Screenshot {\n"
           "    properties:\n      readonly u version = 2;\n  };\n"
           "  interface org.freedesktop.portal.GlobalShortcuts {\n"
           "    properties:\n      readonly u version = 1;\n  };\n};\n")

    def setUp(self):
        saved = {name: getattr(session, name) for name in
                 ("_portal_xml", "_portal_names", "_portal_unanswered")}

        def restore():
            for name, value in saved.items():
                setattr(session, name, value)
        self.addCleanup(restore)
        session._portal_xml = session._portal_names = session._portal_unanswered = None

    def test_no_answer_is_asked_again_and_an_answer_is_kept(self):
        answers = ["", self.XML]
        with mock.patch.object(session, "_dbus_introspect",
                               side_effect=lambda *a: answers.pop(0)) as probe:
            self.assertFalse(session.portal_has("Screenshot"))
            self.assertFalse(session.portal_answered())
            self.assertEqual(probe.call_count, 1)                # not asked at once
            with mock.patch.object(session, "PROBE_RETRY", 0):
                self.assertTrue(session.portal_has("Screenshot"))
            self.assertTrue(session.portal_answered())
            self.assertEqual(session.portal_version("Screenshot"), 2)
            self.assertEqual(session.portal_version("GlobalShortcuts"), 1)
            self.assertTrue(session.portal_has("GlobalShortcuts"))
        self.assertEqual(probe.call_count, 2)

    def test_the_screenshot_portals_version_is_not_kept_while_unknown(self):
        from assistant.capture.portal import PortalBackend
        backend = PortalBackend()
        with mock.patch.object(session, "portal_version", side_effect=[None, 2]):
            self.assertEqual(backend.version(), 1)
            self.assertEqual(backend.version(), 2)
            self.assertEqual(backend.version(), 2)          # kept, not asked again


class FakePortalBus:
    def __init__(self):
        self.handlers = []

    def get_unique_name(self):
        return ":1.5"

    def signal_subscribe(self, *args):
        self.handlers.append(args[-1])
        return len(self.handlers)

    def signal_unsubscribe(self, _sub):
        pass

    def call(self, *args):
        pass


class TheReviewIsNotTheCapture(ToolCase):
    """What is done with a picture once it is taken - the review dialog that opens - is
    not the capture's to fail. Its failure went down the chain as one, and the next
    rung's picker came up."""

    def answer(self, handler, uri):
        handler(None, None, None, None, "Response",
                GLib.Variant("(ua{sv})", (0, {"uri": GLib.Variant("s", uri)})))

    def test_the_portals_picture_goes_to_the_review_outside_the_loading(self):
        from assistant.capture.portal import PortalBackend
        backend = PortalBackend()
        backend._version, backend._bus = 2, FakePortalBus()
        path = os.path.join(self.dir, "shot.png")
        with open(path, "wb") as f:
            f.write(png())
        errors = []

        def review(image):
            raise RuntimeError("the review's own fault")

        backend._screenshot(True, review, errors.append)
        with self.assertRaisesRegex(RuntimeError, "the review's own fault"):
            self.answer(backend._bus.handlers[-1], "file://" + path)
        self.assertEqual(errors, [])
        self.assertFalse(os.path.exists(path))          # the portal's file, taken
        # A picture that cannot be loaded is still the capture's failure.
        backend._screenshot(True, review, errors.append)
        self.answer(backend._bus.handlers[-1], "file://" + path)
        self.assertEqual(errors, [f"Screenshot file vanished: {path}"])

    def test_the_chain_does_not_try_the_next_rung_for_it(self):
        manager = capture.CaptureManager.__new__(capture.CaptureManager)
        manager.last_backend = None
        second = mock.Mock()
        manager._chain = lambda: [("first", lambda done, failed: done("picture")),
                                  ("second", second)]

        def review(image):
            raise RuntimeError("the review's own fault")

        with self.assertRaises(RuntimeError):
            manager.select_region(review, lambda why: self.fail(why))
        second.assert_not_called()
        self.assertFalse(manager.busy())

    def test_a_failure_of_every_rung_says_screenshot_failed_once(self):
        manager = capture.CaptureManager.__new__(capture.CaptureManager)
        manager.last_backend = None
        manager._chain = lambda: [("a", lambda done, failed: failed("no portal")),
                                  ("b", lambda done, failed: failed("no grim"))]
        errors = []
        manager.select_region(lambda image: None, errors.append)
        # The app puts "Screenshot failed: " before it.
        self.assertEqual(errors, ["no way of taking one worked.\n  a: no portal\n"
                                  "  b: no grim"])
        self.assertNotIn("failed", errors[0].split("\n")[0].lower())


class OneCaptureAtATime(unittest.TestCase):
    """The shortcut pressed again while slurp, the portal's picker or a crop window was
    up started a second picker over the first."""

    def manager(self, steps):
        manager = capture.CaptureManager.__new__(capture.CaptureManager)
        manager.last_backend = None
        manager._chain = lambda: steps
        return manager

    def test_busy_until_the_capture_ends_however_it_ends(self):
        for ending in ("picture", "Cancelled", "it broke"):
            with self.subTest(ending=ending):
                pending = {}

                def picker(done, failed):
                    pending.update(done=done, failed=failed)

                manager = self.manager([("picker", picker)])
                said = []
                manager.select_region(lambda image: said.append(manager.busy()),
                                      lambda why: said.append(manager.busy()))
                self.assertTrue(manager.busy())
                if ending == "picture":
                    pending["done"]("picture")
                else:
                    pending["failed"](ending)
                self.assertFalse(manager.busy())
                self.assertEqual(said, [False])     # over before the review opens

    def test_a_picker_that_never_answers_is_over_in_the_end(self):
        manager = self.manager([("picker", lambda done, failed: None)])
        manager.select_region(lambda image: None, lambda why: None)
        self.assertTrue(manager.busy())
        later = time.monotonic() + manager.STALE_AFTER + 1
        with mock.patch.object(capture.time, "monotonic", return_value=later):
            self.assertFalse(manager.busy())

    def test_a_crop_window_of_ours_is_brought_forward(self):
        manager = self.manager(None)
        manager._chain = lambda: [("x11-fullscreen+crop", manager._x11_fullscreen_crop)]
        manager.x11 = mock.Mock()
        manager.x11.capture_fullscreen.return_value = Image.new("RGB", (8, 8))
        with mock.patch.object(capture, "CropDialog") as dialog:
            manager.select_region(lambda image: None, lambda why: None)
            window = dialog.return_value
            later = time.monotonic() + manager.STALE_AFTER + 1
            with mock.patch.object(capture.time, "monotonic", return_value=later):
                self.assertTrue(manager.busy())         # open as long as it is open
            manager.present()
            window.present.assert_called_once()
            on_done = dialog.call_args[0][1]
            on_done(Image.new("RGB", (4, 4)))
        self.assertFalse(manager.busy())

    def test_the_overlays_failure_at_once_keeps_the_next_rungs_window(self):
        manager = self.manager(None)
        manager._chain = lambda: [("x11-frozen-overlay", manager._x11_overlay),
                                  ("x11-fullscreen+crop", manager._x11_fullscreen_crop)]
        manager.x11 = mock.Mock()
        manager.x11.select_region.side_effect = \
            lambda done, failed: failed("No monitors reported")
        manager.x11.capture_fullscreen.return_value = Image.new("RGB", (8, 8))
        with mock.patch.object(capture, "CropDialog") as dialog:
            manager.select_region(lambda image: None, lambda why: None)
            manager.present()
        dialog.return_value.present.assert_called_once()

    def test_the_app_brings_the_one_open_forward_rather_than_open_another(self):
        from assistant.app import Assistant
        app = Assistant.__new__(Assistant)
        app.capture = mock.Mock()
        app.capture.busy.return_value = True
        app._require_api_key = mock.Mock(return_value=False)
        app._start_capture("OCR only", ask_prompt=False, build=lambda p: (p, p))
        app.capture.present.assert_called_once()
        app._require_api_key.assert_not_called()
        app.capture.select_region.assert_not_called()
        # A picture from the clipboard is no capture, and is reviewed all the same.
        app._start_capture("OCR only", ask_prompt=False, build=lambda p: (p, p),
                           image=Image.new("RGB", (4, 4)))
        app._require_api_key.assert_called_once()


def event(x, y, button=1):
    return type("Event", (), {"button": button, "x": x, "y": y})()


class TheSelectionEndsWhereTheButtonIsLetGo(unittest.TestCase):
    """In the crop window the selection went on following the pointer after the button
    was let go: on its way to Use selection the rectangle stretched to the area's edge,
    and that was what was sent."""

    def test_moving_to_use_selection_leaves_it_as_it_was(self):
        from assistant.capture.selector import CropDialog
        delivered = []
        dialog = CropDialog(Image.new("RGB", (400, 300), "white"), delivered.append,
                            delivered.append)
        self.addCleanup(lambda: dialog._finished or dialog.destroy())
        pump(lambda: dialog.area.get_allocated_width() > 100)
        dialog._on_press(None, event(100, 100))
        dialog._on_motion(None, event(160, 150))
        dialog._on_release(None, event(200, 180))
        chosen = dialog._to_image_rect()
        self.assertTrue(dialog.use_button.get_sensitive())
        for x, y in ((150, 40), (40, 2), (0, 0)):           # up to the header bar
            dialog._on_motion(None, event(x, y))
        self.assertEqual(dialog._to_image_rect(), chosen)
        dialog._finish()
        [crop] = delivered
        self.assertEqual(crop.size, tuple(chosen[2:]))

    def test_a_second_drag_starts_a_new_selection(self):
        from assistant.capture.selector import CropDialog
        dialog = CropDialog(Image.new("RGB", (400, 300)), lambda image: None,
                            lambda why: None)
        self.addCleanup(lambda: dialog._finished or dialog.destroy())
        pump(lambda: dialog.area.get_allocated_width() > 100)
        dialog._on_press(None, event(100, 100))
        dialog._on_release(None, event(200, 180))
        dialog._on_press(None, event(300, 300))
        dialog._on_motion(None, event(320, 330))
        self.assertEqual((dialog.start, dialog.end), ((300, 300), (320, 330)))
        self.assertFalse(dialog._on_release(None, event(320, 330, button=3)))
        self.assertTrue(dialog._dragging)


class FakeSeat:
    def __init__(self, answers):
        self.answers = list(answers)
        self.grabs = []
        self.ungrabs = 0

    def grab(self, window, *args):
        self.grabs.append(window)
        from gi.repository import Gdk
        return self.answers.pop(0) if self.answers else Gdk.GrabStatus.SUCCESS

    def ungrab(self):
        self.ungrabs += 1


class TheOverlayWaitsForTheKeyboard(unittest.TestCase):
    """Started by the app's own X11 shortcut, the overlay opens with that shortcut's
    keys still down, and X keeps the keyboard with the key grab until they are let go:
    the overlay's grab was refused, its answer never looked at, and Esc never reached
    it."""

    def overlay(self, answers, attempts=20):
        from gi.repository import Gdk
        from assistant.capture import selector
        seat = FakeSeat([getattr(Gdk.GrabStatus, name) for name in answers])
        for patch in (mock.patch.object(selector.FrozenOverlay, "_seat",
                                        staticmethod(lambda: seat)),
                      mock.patch.object(selector.FrozenOverlay, "GRAB_RETRY_MS", 5),
                      mock.patch.object(selector.FrozenOverlay, "GRAB_ATTEMPTS", attempts)):
            patch.start()
            self.addCleanup(patch.stop)
        self.errors = []
        overlay = selector.FrozenOverlay(Image.new("RGB", (64, 48)),
                                         {"x": 0, "y": 0, "width": 64, "height": 48},
                                         lambda image: None, self.errors.append)
        self.addCleanup(lambda: overlay._finished or overlay.destroy())
        return overlay, seat

    def settle(self):
        pump(lambda: False, timeout=0.15)

    def test_the_grab_is_asked_for_again_until_it_is_had(self):
        overlay, seat = self.overlay(["ALREADY_GRABBED", "ALREADY_GRABBED", "SUCCESS"])
        pump(lambda: len(seat.grabs) >= 3)
        self.settle()
        self.assertEqual(len(seat.grabs), 3)

    def test_it_is_given_up_on_after_a_while(self):
        overlay, seat = self.overlay(["ALREADY_GRABBED"] * 50, attempts=4)
        pump(lambda: len(seat.grabs) >= 4)
        self.settle()
        self.assertEqual(len(seat.grabs), 4)

    def test_esc_ends_the_asking(self):
        from gi.repository import Gdk
        overlay, seat = self.overlay(["ALREADY_GRABBED"] * 50, attempts=50)
        pump(lambda: seat.grabs)
        overlay._on_key(None, type("Key", (), {"keyval": Gdk.KEY_Escape})())
        asked = len(seat.grabs)
        self.settle()
        self.assertEqual(len(seat.grabs), asked)
        self.assertEqual(self.errors, ["Cancelled"])
        self.assertGreaterEqual(seat.ungrabs, 1)


if __name__ == "__main__":
    unittest.main()
