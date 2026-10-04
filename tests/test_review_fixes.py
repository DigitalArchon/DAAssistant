"""Regression tests for the defects found in the September 2026 review.

Each of these was live in a build where "all features are working as intended": they sit
on paths that are only reached when something else has already gone slightly wrong, or
on a desktop other than the one the app was written on.

Run:  python3 -m unittest tests.test_review_fixes -v
"""

import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests
from PIL import Image

from assistant import gtkcompat  # noqa: F401
from assistant import (clipboard, errors, gtkcompat, imaging, models, session,
                       theming, webkitguard)
from assistant.capture import cli
from assistant.config import Config
from assistant.llm import LLMClient, LLMError
from assistant.ui.chat import history_for
from assistant.ui.dialogs import sends_key_in_the_clear
from assistant.ui.markdown_html import render_markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class UnclosedTagsDoNotEatTheAnswer(unittest.TestCase):
    def test_a_mentioned_script_tag_keeps_what_follows(self):
        out = render_markdown("Add a <script> tag to your page.\n\nThen reload it.")
        self.assertIn("Then reload it.", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script", out)

    def test_the_same_for_iframe_and_style(self):
        for tag in ("iframe", "style", "object"):
            out = render_markdown(f"Use an <{tag}> for that.\n\nSecond paragraph.")
            self.assertIn("Second paragraph.", out, tag)
            self.assertNotIn(f"<{tag}", out, tag)

    def test_a_real_script_element_is_still_dropped_whole(self):
        out = render_markdown("before <script>alert(1)</script> after")
        self.assertNotIn("alert(1)", out)
        self.assertIn("after", out)

    def test_an_unclosed_one_cannot_smuggle_a_closed_one_through(self):
        out = render_markdown("<script>alert(1)</script> and then a bare <script> tag")
        self.assertNotIn("<script", out)


class HistorySentUpstream(unittest.TestCase):
    def test_errors_are_for_the_reader_only(self):
        history = history_for([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "**Error:** HTTP 503", "error": True},
        ])
        self.assertEqual(history, [{"role": "user", "content": "q"}])

    def test_an_unanswered_turn_is_folded_into_the_next_not_dropped(self):
        # The first turn is the one carrying the captured text; losing it would leave
        # "try again" with nothing to try again on.
        history = history_for([
            {"role": "user", "content": "what is this?\n\n---\nERR 0x80070643",
             "search_query": "0x80070643"},
            {"role": "assistant", "content": "**Error:** timed out", "error": True},
            {"role": "user", "content": "try again"},
        ])
        self.assertEqual([turn["role"] for turn in history], ["user"])
        self.assertIn("0x80070643", history[0]["content"])
        self.assertIn("try again", history[0]["content"])
        self.assertEqual(history[0]["search_query"], "0x80070643")

    def test_an_ordinary_conversation_is_untouched(self):
        turns = [{"role": "user", "content": "a"},
                 {"role": "assistant", "content": "b", "is_premium": True},
                 {"role": "user", "content": "c", "display": "shown"}]
        self.assertEqual(history_for(turns), [{"role": "user", "content": "a"},
                                              {"role": "assistant", "content": "b"},
                                              {"role": "user", "content": "c"}])


class CorruptConfigIsSetAside(TempDir):
    def test_saving_does_not_destroy_the_file_it_could_not_read(self):
        path = os.path.join(self.dir, "config.json")
        broken = '{"api_key": "sk-precious", "max_tokens": 4096,}'
        with open(path, "w") as f:
            f.write(broken)
        os.chmod(path, 0o600)

        config = Config(path)
        self.assertIsNotNone(config.load_error)
        config.use_premium = True           # the tray checkbox
        config.save()

        with open(path + ".corrupt") as f:
            self.assertEqual(f.read(), broken)
        self.assertEqual(os.stat(path + ".corrupt").st_mode & 0o777, 0o600)
        self.assertTrue(Config(path).use_premium)


class DetachedConfig(TempDir):
    def test_trying_values_out_leaves_the_live_config_alone(self):
        path = os.path.join(self.dir, "config.json")
        live = Config(path)
        live.api_key = "saved"
        live.save()

        trial = live.detached(api_key="typed-but-not-saved", use_premium=True,
                              not_a_setting=1)
        self.assertEqual(trial.api_key, "typed-but-not-saved")
        self.assertEqual(trial.active_text_model(), live.premium_text_model)
        self.assertFalse(hasattr(trial, "not_a_setting"))

        self.assertEqual(live.api_key, "saved")
        # Nothing marked dirty either: that set is what the next save() writes over
        # whatever is on disk.
        with open(path, "w") as f:
            json.dump({"vision_model": "edited-by-hand"}, f)
        live.save()
        self.assertEqual(Config(path).vision_model, "edited-by-hand")


class ClipboardThatIsNotText(unittest.TestCase):
    def test_wl_paste_is_asked_for_text(self):
        argv = next(argv for argv, name in clipboard.FALLBACKS if name == "wl-paste")
        self.assertEqual(argv[argv.index("--type") + 1], "text")

    def test_undecodable_output_is_not_an_exception(self):
        gtk_clipboard = mock.Mock()
        gtk_clipboard.wait_for_text.return_value = None
        gtk_clipboard.wait_is_image_available.return_value = True

        def png_bytes(*_args, **_kwargs):
            raise UnicodeDecodeError("utf-8", b"\x89PNG", 0, 1, "invalid start byte")

        with mock.patch.object(clipboard.Gtk.Clipboard, "get",
                               return_value=gtk_clipboard), \
                mock.patch.object(clipboard.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(clipboard.subprocess, "run", side_effect=png_bytes):
            self.assertEqual(clipboard.get_text(), (None, "image"))


class FakeSession:
    def __init__(self, error):
        self.error = error
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append(kwargs)
        raise self.error


class TimeoutsAndRetries(TempDir):
    def _client(self, error):
        config = Config(os.path.join(self.dir, "c.json"))
        config.api_key = "k"
        client = LLMClient(config)
        client.session = FakeSession(error)
        return client

    def test_a_request_that_was_received_is_not_sent_again(self):
        client = self._client(requests.exceptions.ReadTimeout("read timed out"))
        with self.assertRaises(LLMError) as raised:
            client.complete("m", [{"role": "user", "content": "hi"}])
        self.assertEqual(len(client.session.posts), 1)
        self.assertIn("180", str(raised.exception))

    def test_one_that_never_arrived_still_is(self):
        client = self._client(requests.exceptions.ConnectTimeout("no route"))
        with mock.patch("assistant.llm.time.sleep"), self.assertRaises(LLMError):
            client.complete("m", [{"role": "user", "content": "hi"}])
        self.assertEqual(len(client.session.posts), 3)

    def test_connecting_gets_seconds_and_answering_gets_minutes(self):
        client = self._client(requests.exceptions.ReadTimeout())
        with self.assertRaises(LLMError):
            client.complete("m", [{"role": "user", "content": "hi"}])
        connect, read = client.session.posts[0]["timeout"]
        self.assertLessEqual(connect, 15)
        self.assertEqual(read, client.config.request_timeout)


class ModelCacheBelongsToAProvider(TempDir):
    def test_another_providers_catalogue_is_a_miss(self):
        cache = os.path.join(self.dir, "models.json")
        with mock.patch.object(models, "CACHE_DIR", self.dir), \
                mock.patch.object(models, "CACHE_FILE", cache):
            models.save_cache([{"id": "a/b"}], "https://one.example/v1/models")
            self.assertIsNotNone(models.load_cache(url="https://one.example/v1/models"))
            self.assertIsNone(models.load_cache(url="https://two.example/v1/models"))


class CaptureTempFiles(unittest.TestCase):
    def test_the_path_sits_in_a_directory_nobody_else_can_enter(self):
        path = cli._tempfile()
        try:
            directory = os.path.dirname(path)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
            # Left for the tool to create: spectacle and Cinnamon refuse an existing file.
            self.assertFalse(os.path.exists(path))
        finally:
            cli._discard(path)
        self.assertFalse(os.path.exists(directory))

    def test_discard_copes_with_a_tool_that_wrote_nothing(self):
        path = cli._tempfile()
        cli._discard(path)
        cli._discard(path)          # and with being called twice
        self.assertFalse(os.path.exists(os.path.dirname(path)))


class DebugCapturesArePrivate(TempDir):
    def test_directory_and_file_modes(self):
        with mock.patch.object(imaging.os.path, "expanduser",
                               return_value=os.path.join(self.dir, "captures")):
            os.makedirs(os.path.join(self.dir, "captures"), mode=0o775)
            path = imaging._save_debug(b"png")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class PlainHttpWarning(unittest.TestCase):
    def test_only_a_remote_http_url_is_flagged(self):
        self.assertTrue(sends_key_in_the_clear("http://api.example.com/v1/chat/completions"))
        self.assertFalse(sends_key_in_the_clear("https://nano-gpt.com/api/v1/chat/completions"))
        self.assertFalse(sends_key_in_the_clear("http://localhost:11434/v1/chat/completions"))
        self.assertFalse(sends_key_in_the_clear("http://127.0.0.1:8080/v1"))
        self.assertFalse(sends_key_in_the_clear(""))


class HotkeyRestart(TempDir):
    def test_switching_hotkeys_off_stops_reporting_the_old_mechanism(self):
        from assistant.hotkeys import HotkeyManager
        config = Config(os.path.join(self.dir, "c.json"))
        manager = HotkeyManager(config, lambda action: None)
        manager.mode, manager.detail = "x11", "X11 grabs (5 actions, lock-modifier safe)"
        config.hotkey_mode = "off"
        manager.start()
        self.assertEqual(manager.mode, "off")
        self.assertEqual(manager.detail, "disabled in settings")


FAKE_GSETTINGS = """#!/bin/sh
# Stands in for gsettings: answers `get` with a list the user already had, and writes
# down what it is told to `set` instead of doing it.
[ "$1" = get ] && echo "$FAKE_LIST"
[ "$1" = set ] && echo "gsettings $*" >> "$FAKE_LOG"
exit 0
"""


class HotkeyInstallerLeavesOtherShortcutsAlone(TempDir):
    """Against a stub gsettings; nothing on this machine is read or written.

    --remove has no dry run of its own, so the stub records the calls. The memory backend
    is a second line of defence: were the real binary ever reached, it would have
    nowhere to write.
    """

    def _dry_run(self, desktop, existing, *args, encyclopedia=True):
        # A home of its own, holding a config with the encyclopedia on or off.
        config = os.path.join(self.dir, ".config", "da-assistant")
        os.makedirs(config, exist_ok=True)
        with open(os.path.join(config, "config.json"), "w") as f:
            json.dump({"encyclopedia_enabled": encyclopedia}, f, indent=2)
        stub = os.path.join(self.dir, "gsettings")
        with open(stub, "w") as f:
            f.write(FAKE_GSETTINGS)
        os.chmod(stub, 0o755)
        log = os.path.join(self.dir, "set.log")
        env = dict(os.environ, PATH=self.dir + os.pathsep + os.environ["PATH"],
                   FAKE_LIST=existing, FAKE_LOG=log, GSETTINGS_BACKEND="memory",
                   HOME=self.dir)
        env.pop("APPIMAGE", None)
        result = subprocess.run(
            ["bash", str(ROOT / "tools" / "install-hotkeys.sh"),
             f"--desktop={desktop}", *args],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        if os.path.exists(log):
            with open(log) as f:
                output += f.read()
            os.unlink(log)
        return next(line for line in output.splitlines()
                    if "gsettings set" in line and "custom-" in line
                    and "custom-keybinding:" not in line)

    def test_cinnamon_install_appends_names_to_what_is_there(self):
        line = self._dry_run("cinnamon", "['custom0', '__dummy__']")
        self.assertIn("['custom0', '__dummy__', 'da-assistant-translate_text',", line)
        self.assertIn("'da-assistant-search_encyclopedia', "
                      "'da-assistant-ask_encyclopedia']", line)
        # Cinnamon builds base/<entry>/ itself; a path here resolves to nothing.
        self.assertNotIn("/org/cinnamon", line.split("custom-list", 1)[1])

    def test_the_encyclopedias_keys_only_when_it_is_on(self):
        line = self._dry_run("cinnamon", "['custom0']", encyclopedia=False)
        self.assertTrue(line.rstrip().endswith("'da-assistant-look_up_word']"), line)
        self.assertNotIn("encyclopedia", line)
        # And taken back if they were there from before.
        line = self._dry_run("cinnamon", "['da-assistant-ask_encyclopedia']",
                             encyclopedia=False)
        self.assertNotIn("encyclopedia", line)

    def test_installing_twice_lists_them_once(self):
        line = self._dry_run(
            "cinnamon", "['da-assistant-ocr_only', 'custom0', 'da-assistant-query_text']")
        self.assertEqual(line.count("da-assistant-ocr_only"), 1)
        self.assertIn("'custom0'", line)

    def test_cinnamon_remove_keeps_the_users_own(self):
        line = self._dry_run("cinnamon", "['custom0', 'da-assistant-ocr_only']",
                             "--remove")
        self.assertTrue(line.rstrip().endswith("custom-list ['custom0']"), line)

    def test_gnome_keeps_the_users_own_and_uses_paths(self):
        mine = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
        line = self._dry_run("gnome", f"['{mine}']")
        self.assertIn(f"['{mine}', ", line)
        self.assertIn("custom-keybindings/da-assistant-ask_encyclopedia/']", line)
        line = self._dry_run("gnome", f"['{mine}']", "--remove")
        self.assertTrue(line.rstrip().endswith(f"['{mine}']"), line)

    def test_an_empty_list_prints_the_way_gsettings_prints_it(self):
        line = self._dry_run("cinnamon", "@as []")
        self.assertIn("custom-list ['da-assistant-translate_text',", line)
        line = self._dry_run("cinnamon", "@as []", "--remove")
        self.assertTrue(line.rstrip().endswith("custom-list []"), line)

    def test_mate_is_not_sent_to_the_cinnamon_installer(self):
        env = dict(os.environ)
        env.pop("APPIMAGE", None)
        result = subprocess.run(
            ["bash", str(ROOT / "tools" / "install-hotkeys.sh"), "--desktop=mate"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertNotIn("org.cinnamon", result.stdout)
        self.assertIn("--action=ocr_only", result.stdout)


if __name__ == "__main__":
    unittest.main()


class KdePortalCannotPickARegion(unittest.TestCase):
    """The KDE portal's screenshot dialog offers whole screens and windows only.

    Its three choices - full screen, current screen, active window - contain no
    rectangular region, so taking that rung on KDE handed back an uncropped screen as
    though the user had selected it, and Spectacle's own picker was never reached.
    """

    def _manager(self, desktop, spectacle=True, portal_version=2):
        from assistant import capture

        which = lambda name: "/usr/bin/" + name if name == "spectacle" and spectacle else None
        with mock.patch.object(session, "session_type", return_value="wayland"), \
                mock.patch.object(session, "desktop", return_value=desktop), \
                mock.patch.object(session, "portal_has", return_value=True), \
                mock.patch.object(session, "portal_version", return_value=portal_version), \
                mock.patch.object(session, "has_cinnamon_screenshot", return_value=False), \
                mock.patch.object(capture.cli.shutil, "which", side_effect=which):
            manager = capture.CaptureManager()
            return manager.describe_chain()

    def test_kde_asks_spectacle_for_the_region_and_never_the_portal_picker(self):
        chain = self._manager("kde")
        self.assertNotIn("portal-interactive", chain)
        self.assertEqual(chain[0], "cli-spectacle")

    def test_other_desktops_still_lead_with_the_portal(self):
        chain = self._manager("gnome", spectacle=False)
        self.assertEqual(chain[0], "portal-interactive")

    def test_kde_without_spectacle_falls_to_fullscreen_and_our_own_crop(self):
        chain = self._manager("kde", spectacle=False)
        self.assertNotIn("portal-interactive", chain)
        self.assertEqual(chain[0], "portal-fullscreen+crop")

    def test_the_backend_refuses_the_rung_even_if_called_directly(self):
        from assistant.capture.portal import PortalBackend

        backend = PortalBackend()
        backend._version = 2
        errors = []
        with mock.patch.object(session, "desktop", return_value="kde"):
            backend.select_region(lambda image: None, errors.append)
        self.assertEqual(len(errors), 1)
        self.assertNotEqual(errors[0].strip().lower(), "cancelled")   # must fall through


class SpectacleFlagProbing(unittest.TestCase):
    def test_a_short_flag_is_not_found_inside_a_long_one(self):
        self.assertFalse(cli._has_flag("  -o, --output <file>\n  --nonotify", "-n"))
        self.assertTrue(cli._has_flag("  -n, --nonotify", "-n"))
        self.assertTrue(cli._has_flag("  -r, --region", "--region"))

    def test_plasma_6_options_produce_long_flags(self):
        help_text = ("-b, --background\n-n, --nonotify\n-r, --region\n"
                     "-f, --fullscreen\n-o, --output <fileName>")
        with mock.patch.object(cli.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, stdout=help_text, stderr="")):
            args = cli._spectacle_args("/tmp/x.png", region=True)
        self.assertEqual(args, ["spectacle", "--background", "--nonotify", "--region",
                                "--output", "/tmp/x.png"])


class SpectacleCancellationIsNotAFailure(unittest.TestCase):
    def _select(self, returncode, stderr, write=False):
        backend = cli.CliBackend.__new__(cli.CliBackend)
        backend.tool = "spectacle"

        def fake_run(cmd, timeout=120):
            if write:
                with open(cmd[-1], "wb") as fh:
                    Image.new("RGB", (4, 4)).save(fh, "PNG")
            return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

        with mock.patch.object(cli, "_run", side_effect=fake_run), \
                mock.patch.object(cli, "_spectacle_args",
                                  side_effect=lambda path, region: ["spectacle", path]):
            return backend._select_blocking()

    def test_escape_leaves_no_file_and_reads_as_cancelled(self):
        self.assertIsNone(self._select(0, ""))
        self.assertIsNone(self._select(1, ""))          # some versions exit non-zero

    def test_a_tool_that_complained_is_still_a_failure(self):
        with self.assertRaises(cli.CaptureError):
            self._select(1, "Unknown option 'region'")

    def test_a_written_file_comes_back_as_an_image(self):
        self.assertEqual(self._select(0, "", write=True).size, (4, 4))


class FakeClipboard:
    def __init__(self, text=None, image=False, uris=False):
        self.text, self.image, self.uris = text, image, uris

    def wait_for_text(self):
        return self.text

    def wait_for_image(self):
        return None

    def wait_is_image_available(self):
        return self.image

    def wait_is_uris_available(self):
        return self.uris


def _fake_gtk(board):
    return mock.Mock(Clipboard=mock.Mock(get=mock.Mock(return_value=board)))


class ClipboardOnWaylandWithoutFocus(unittest.TestCase):
    """A Wayland client is offered the clipboard only while it has keyboard focus.

    A tray app never has it, so GTK hands back nothing and every clipboard action
    reported "the clipboard is empty" - on KDE Wayland, always.
    """

    def _get_text(self, board, tools=(), klipper=None):
        gdbus = subprocess.CompletedProcess([], 0, stdout=klipper or "", stderr="")
        with mock.patch.object(clipboard, "Gtk", _fake_gtk(board)), \
                mock.patch.object(clipboard.shutil, "which",
                                  side_effect=lambda n: "/usr/bin/" + n if n in tools else None), \
                mock.patch.object(clipboard, "_run", return_value=gdbus):
            return clipboard.get_text()

    def test_klipper_answers_when_gtk_and_the_tools_cannot(self):
        text, kind = self._get_text(FakeClipboard(None), tools=("gdbus",),
                                    klipper="('hello from klipper',)\n")
        self.assertEqual((text, kind), ("hello from klipper", "text"))

    def test_klipper_text_keeps_its_quotes_and_newlines(self):
        text, _ = self._get_text(FakeClipboard(None), tools=("gdbus",),
                                 klipper="('it\\'s \"here\"\\nline two',)\n")
        self.assertEqual(text, "it's \"here\"\nline two")

    def test_an_empty_klipper_is_not_mistaken_for_content(self):
        _, kind = self._get_text(FakeClipboard(None), tools=("gdbus",), klipper="('',)\n")
        self.assertEqual(kind, "empty")

    def test_gtk_still_wins_when_it_has_an_answer(self):
        text, kind = self._get_text(FakeClipboard("from gtk"), tools=("gdbus",),
                                    klipper="('stale',)\n")
        self.assertEqual((text, kind), ("from gtk", "text"))

    def test_the_hint_names_the_missing_package_only_where_it_helps(self):
        with mock.patch.object(clipboard.session, "session_type", return_value="wayland"), \
                mock.patch.object(clipboard.shutil, "which", return_value=None), \
                mock.patch.object(clipboard, "_klipper_text", return_value=None):
            self.assertIn("wl-clipboard", clipboard.unreadable_hint())

        with mock.patch.object(clipboard.session, "session_type", return_value="x11"):
            self.assertEqual(clipboard.unreadable_hint(), "")   # empty really is empty

        with mock.patch.object(clipboard.session, "session_type", return_value="wayland"), \
                mock.patch.object(clipboard.shutil, "which", return_value="/usr/bin/wl-paste"):
            self.assertEqual(clipboard.unreadable_hint(), "")


class WebKitThatKillsTheProcess(TempDir):
    """A crash cannot be caught in the process it kills, so it is caught on the way back."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.dir, "webkit-crash")
        self.patches = [mock.patch.object(webkitguard, "STATE_FILE", self.state),
                        mock.patch.object(webkitguard, "LOG_DIR", self.dir),
                        mock.patch.dict(os.environ, {}, clear=False)]
        for p in self.patches:
            p.start()
        os.environ.pop(webkitguard.FORCE_OFF, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_two_views_that_never_rendered_disable_webkit(self):
        self.assertFalse(webkitguard.blocked()[0])
        webkitguard.note_attempt()          # the process dies here
        self.assertFalse(webkitguard.blocked()[0], "one strike is not a verdict")
        webkitguard.note_attempt()          # and again
        blocked, reason = webkitguard.blocked()
        self.assertTrue(blocked)
        self.assertIn(self.state, reason)   # and says how to undo it

    def test_a_view_that_rendered_leaves_nothing_behind(self):
        webkitguard.note_attempt()
        webkitguard.note_success()
        self.assertFalse(webkitguard.blocked()[0])

    def test_one_stray_kill_is_forgiven_by_the_next_view_that_works(self):
        # A logout or an OOM kill mid-load looks identical to a crash from here, and
        # must not cost the rendered view for good.
        webkitguard.note_attempt()
        webkitguard.note_attempt()
        webkitguard.note_success()
        webkitguard.note_attempt()
        self.assertFalse(webkitguard.blocked()[0])

    def test_quitting_mid_load_does_not_count_as_a_crash(self):
        webkitguard.note_attempt()
        webkitguard.note_clean_exit()
        self.assertFalse(webkitguard.blocked()[0])

    def test_the_environment_can_force_it_off(self):
        os.environ[webkitguard.FORCE_OFF] = "1"
        self.assertTrue(webkitguard.blocked()[0])
        os.environ[webkitguard.FORCE_OFF] = "0"
        self.assertFalse(webkitguard.blocked()[0])


class FailuresReachTheUser(TempDir):
    def test_an_unhandled_callback_error_is_logged_and_shown(self):
        seen = []
        log = os.path.join(self.dir, "errors.log")
        with mock.patch.object(errors, "LOG_DIR", self.dir), \
                mock.patch.object(errors, "LOG_FILE", log), \
                mock.patch.object(sys, "stderr", io.StringIO()) as printed:
            errors.install(lambda message, urgency="low": seen.append(message))
            try:
                errors._handle(*self._raise())
            finally:
                sys.excepthook = sys.__excepthook__
        self.assertIn("ValueError: from a callback", seen[0])
        self.assertIn("Traceback", printed.getvalue())
        with open(log) as fh:
            self.assertIn("Traceback", fh.read())

    @staticmethod
    def _raise():
        try:
            raise ValueError("from a callback")
        except ValueError:
            return sys.exc_info()


class WebKitThatImportsButDoesNotWork(unittest.TestCase):
    """The failure that left KDE users with no chat window and no message.

    gi reports a typelib whose shared library could not be loaded as a *warning*, then
    hands back a namespace whose every class is an unclassed 'void' type. Importing it
    succeeds; the first use raises TypeError, in the middle of building a window.
    """

    class Broken:
        @staticmethod
        def Settings():
            raise TypeError("could not get a reference to type class")

    class Working:
        @staticmethod
        def Settings():
            return object()

    def test_a_namespace_that_raises_on_first_use_is_not_usable(self):
        self.assertFalse(gtkcompat.webkit_is_usable(self.Broken))

    def test_one_that_answers_is(self):
        self.assertTrue(gtkcompat.webkit_is_usable(self.Working))

    def test_the_status_line_tells_the_two_kinds_of_no_apart(self):
        with mock.patch.object(gtkcompat, "has_webkit", return_value=False), \
                mock.patch.object(gtkcompat, "_WEBKIT", None):
            self.assertIn("typelib missing", gtkcompat.webkit_status())
        with mock.patch.object(gtkcompat, "has_webkit", return_value=False), \
                mock.patch.object(gtkcompat, "_WEBKIT", self.Broken):
            self.assertIn("unusable", gtkcompat.webkit_status())

    def test_nothing_loads_webkit_just_to_answer_version(self):
        # --version and the --action a hotkey spawns import this module and must not
        # pay for WebKit; only has_webkit() may.
        source = (ROOT / "assistant" / "gtkcompat.py").read_text()
        self.assertNotIn("_USABLE_WEBKIT = webkit_is_usable", source)
        self.assertIn("_USABLE_WEBKIT = None", source)


class ThemeThatTheDesktopAskedForButCannotSupply(TempDir):
    """An AppImage is styled by the host: the name comes from the desktop, the theme
    from /usr/share/themes. When only the first arrives, GTK silently draws Adwaita and
    the app matches nothing around it - on Wayland especially, where there is no
    XSettings daemon and the name comes from a settings.ini that KDE only writes when
    kde-gtk-config is installed."""

    def _with_themes(self, *names):
        for name in names:
            os.makedirs(os.path.join(self.dir, "themes", name, "gtk-3.0"))
            open(os.path.join(self.dir, "themes", name, "gtk-3.0", "gtk.css"), "w").close()
        return mock.patch.dict(os.environ, {"XDG_DATA_DIRS": self.dir,
                                            "XDG_DATA_HOME": self.dir,
                                            "HOME": self.dir})

    def test_a_theme_on_disk_is_found(self):
        with self._with_themes("Breeze"):
            self.assertTrue(theming.find_theme("Breeze").endswith("gtk.css"))

    def test_an_older_gtk_directory_still_counts(self):
        os.makedirs(os.path.join(self.dir, "themes", "Breeze", "gtk-3.20"))
        open(os.path.join(self.dir, "themes", "Breeze", "gtk-3.20", "gtk.css"), "w").close()
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": self.dir,
                                          "XDG_DATA_HOME": self.dir, "HOME": self.dir}):
            self.assertIsNotNone(theming.find_theme("Breeze"))

    def test_a_missing_one_is_named_as_missing_with_a_way_out(self):
        with self._with_themes("Adwaita"), \
                mock.patch.object(theming, "settings",
                                  return_value={"gtk-theme-name": "Breeze",
                                                "gtk-icon-theme-name": "breeze"}), \
                mock.patch.object(theming, "backend", return_value="wayland"):
            text = "\n".join(theming.summary())
        self.assertIn("NOT FOUND", text)
        self.assertIn("breeze-gtk", text)       # what to install
        self.assertIn("breeze", text)           # icons came through; the theme did not

    def test_gtks_own_themes_are_not_reported_as_missing(self):
        with self._with_themes(), \
                mock.patch.object(theming, "settings",
                                  return_value={"gtk-theme-name": "Adwaita"}), \
                mock.patch.object(theming, "backend", return_value="x11"):
            theme = theming.theme_line()
        self.assertIn("built into GTK", theme)
        self.assertNotIn("NOT FOUND", theme)

    def test_an_icon_theme_nobody_asked_for_is_not_called_missing(self):
        with mock.patch.object(theming, "settings", return_value={}), \
                mock.patch.object(theming, "backend", return_value="wayland"):
            lines = theming.summary()
        icons = [line for line in lines if line.startswith("Icon theme")][0]
        self.assertEqual(icons.split(":", 1)[1].strip(), "(unset)")

    def test_no_theme_setting_at_all_is_described_as_that(self):
        # Not "NOT FOUND, install (unset)": nothing was asked for, so nothing is
        # missing. It means no desktop settings reached this process.
        with mock.patch.object(theming, "settings", return_value={}), \
                mock.patch.object(theming, "backend", return_value="wayland"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GTK_THEME", None)
            line = theming.theme_line()
            advice = "\n".join(theming.summary())
        self.assertIn("no theme setting reached this process", line)
        self.assertNotIn("NOT FOUND", line)
        self.assertNotIn("(unset)", advice.split("->")[-1] if "->" in advice else "")


class WhyWebKitWouldNotLoad(unittest.TestCase):
    def test_the_loaders_own_message_is_reported(self):
        with mock.patch("ctypes.CDLL",
                        side_effect=OSError("/usr/lib/libavif.so.16: undefined symbol: "
                                            "SharpYuvConvertWithOptions")):
            detail = gtkcompat.webkit_failure_detail()
        self.assertIn("libavif", detail)
        self.assertIn("SharpYuvConvertWithOptions", detail)

    def test_a_library_that_loads_reports_nothing(self):
        with mock.patch("ctypes.CDLL", return_value=object()):
            self.assertEqual(gtkcompat.webkit_failure_detail(), "")

    def test_the_status_line_carries_it(self):
        with mock.patch.object(gtkcompat, "has_webkit", return_value=False), \
                mock.patch.object(gtkcompat, "_WEBKIT", object()), \
                mock.patch.object(gtkcompat, "webkit_failure_detail",
                                  return_value="libavif.so.16: undefined symbol: X"):
            status = gtkcompat.webkit_status()
        self.assertIn("library unusable", status)
        self.assertIn("libavif", status)


class AMissingThemeIsSaidOutLoud(TempDir):
    def test_a_theme_that_is_not_installed_is_reported_at_startup(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": self.dir,
                                          "XDG_DATA_HOME": self.dir, "HOME": self.dir},
                             clear=False), \
                mock.patch.object(theming, "settings",
                                  return_value={"gtk-theme-name": "Breeze"}):
            os.environ.pop("GTK_THEME", None)
            self.assertIn("breeze-gtk", theming.problem())

    def test_a_theme_that_is_installed_says_nothing(self):
        os.makedirs(os.path.join(self.dir, "themes", "Breeze", "gtk-3.0"))
        open(os.path.join(self.dir, "themes", "Breeze", "gtk-3.0", "gtk.css"), "w").close()
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": self.dir,
                                          "XDG_DATA_HOME": self.dir, "HOME": self.dir},
                             clear=False), \
                mock.patch.object(theming, "settings",
                                  return_value={"gtk-theme-name": "Breeze"}):
            os.environ.pop("GTK_THEME", None)
            self.assertEqual(theming.problem(), "")

    def test_nor_does_one_of_gtks_own(self):
        with mock.patch.object(theming, "settings",
                               return_value={"gtk-theme-name": "Adwaita"}):
            self.assertEqual(theming.problem(), "")
