"""Translate the highlighted text without copying it, and use a picture from the
clipboard: pasted into a chat, or handed to the operation's image twin."""

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

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402
from PIL import Image  # noqa: E402

from assistant import app as app_module, clipboard, hotkeys, webkitguard  # noqa: E402
from assistant.app import Assistant  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.ui import chat  # noqa: E402
from assistant.ui.dialogs import TextReviewDialog  # noqa: E402


def ran(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class ReadingTheSelection(unittest.TestCase):
    def test_gtks_primary_selection_first(self):
        primary = mock.Mock()
        primary.wait_for_text.return_value = "Guten Tag"
        with mock.patch.object(clipboard.Gtk.Clipboard, "get", return_value=primary), \
                mock.patch.object(clipboard.session, "session_type", return_value="x11"):
            self.assertEqual(clipboard.get_selection(), "Guten Tag")

    def test_on_wayland_without_focus_the_tools_read_it(self):
        primary = mock.Mock()
        primary.wait_for_text.return_value = None
        with mock.patch.object(clipboard.Gtk.Clipboard, "get", return_value=primary), \
                mock.patch.object(clipboard.shutil, "which",
                                  lambda name: name == "wl-paste"), \
                mock.patch.object(clipboard, "_run", return_value=ran("Bonjour")) as run:
            self.assertEqual(clipboard.get_selection(), "Bonjour")
        self.assertIn("--primary", run.call_args[0][0])

    def test_nothing_highlighted_is_none(self):
        primary = mock.Mock()
        primary.wait_for_text.return_value = ""
        with mock.patch.object(clipboard.Gtk.Clipboard, "get", return_value=primary), \
                mock.patch.object(clipboard.shutil, "which", lambda name: None):
            self.assertIsNone(clipboard.get_selection())


class NothingStaleOnWayland(unittest.TestCase):
    """On Wayland GTK hears of a new clipboard only while one of the app's windows has
    focus, and otherwise answers with what it heard last - the app's own Copy from an
    hour before, as often as not. With no wl-paste and no Klipper to ask, that came back
    as the clipboard, and nothing said otherwise; GTK is asked now only while current."""

    def read(self, focused, text="copied an hour ago", ours=False):
        board = mock.Mock()
        board.wait_for_text.return_value = text
        board.wait_is_image_available.return_value = False
        board.wait_is_uris_available.return_value = True
        board.wait_for_image.return_value = None
        with mock.patch.object(clipboard.Gtk.Clipboard, "get", return_value=board), \
                mock.patch.object(clipboard.session, "session_type",
                                  return_value="wayland"), \
                mock.patch.object(clipboard.shutil, "which", lambda name: None), \
                mock.patch.object(clipboard, "_klipper_text", return_value=None), \
                mock.patch.object(clipboard, "_focused", return_value=focused), \
                mock.patch.object(clipboard.Gdk, "selection_owner_get",
                                  return_value=object() if ours else None):
            return (clipboard.get_text(), clipboard.get_selection(), clipboard.get_image(),
                    clipboard.unreadable_hint(), board)

    def test_with_no_window_of_ours_focused_gtk_is_not_asked(self):
        text, selection, image, hint, board = self.read(focused=False)
        self.assertEqual(text, (None, "empty"))
        self.assertIsNone(selection)
        self.assertIsNone(image)
        self.assertIn("Install wl-clipboard", hint)       # what app.py says with "empty"
        board.wait_for_text.assert_not_called()
        board.wait_is_uris_available.assert_not_called()

    def test_with_one_focused_what_gtk_says_is_current(self):
        text, selection, _image, _hint, _board = self.read(focused=True, text="just now")
        self.assertEqual(text, ("just now", "text"))
        self.assertEqual(selection, "just now")


    def test_what_is_still_the_apps_own_is_current_focused_or_not(self):
        # Its Copy, the window it was copied from closed since: until another app
        # copies, GTK holds it as its own, and is told when that changes.
        text, selection, _image, _hint, _board = self.read(focused=False, text="ours",
                                                           ours=True)
        self.assertEqual(text, ("ours", "text"))
        self.assertEqual(selection, "ours")

ENDLESS = [sys.executable, "-c", "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"]


class ReadsHaveALimit(unittest.TestCase):
    """A reader's output was taken in whole: 100 MB on the clipboard took the app from 85
    to 372 MB, and 300 MB had it killed under a 900 MB limit. A read stops at its limit
    now, and says so."""

    def setUp(self):
        for patch in (mock.patch.object(clipboard, "TEXT_LIMIT", 1 << 20),
                      mock.patch.object(clipboard, "IMAGE_LIMIT", 2 << 20)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_reader_is_stopped_at_the_limit(self):
        for binary, limit in ((False, 1 << 20), (True, 2 << 20)):
            with self.subTest(binary=binary), \
                    self.assertRaises(clipboard._TooLarge) as raised:
                clipboard._run(ENDLESS, timeout=60, binary=binary)
            self.assertEqual(raised.exception.args[0], limit)

    def text_from(self, gtk_text=None, readers=(), session_type="x11"):
        board = mock.Mock()
        board.wait_for_text.return_value = gtk_text
        board.wait_is_image_available.return_value = False
        board.wait_is_uris_available.return_value = False
        with mock.patch.object(clipboard.Gtk.Clipboard, "get", return_value=board), \
                mock.patch.object(clipboard.session, "session_type",
                                  return_value=session_type), \
                mock.patch.object(clipboard, "FALLBACKS", list(readers)), \
                mock.patch.object(clipboard, "SELECTION_FALLBACKS", list(readers)), \
                mock.patch.object(clipboard.shutil, "which",
                                  lambda name: name if name == sys.executable else None):
            return (clipboard.get_text(), clipboard.unreadable_hint(),
                    clipboard.get_selection(), clipboard.unreadable_hint(selection=True))

    def test_more_than_a_read_takes_in_is_said_to_be(self):
        for case in ({"readers": [(ENDLESS, "endless")]},
                     {"readers": [(ENDLESS, "endless")], "session_type": "wayland"},
                     {"gtk_text": "x" * ((1 << 20) + 1)}):
            with self.subTest(**{k: str(v)[:40] for k, v in case.items()}):
                text, hint, selection, selection_hint = self.text_from(**case)
                self.assertEqual(text, (None, "empty"))
                self.assertEqual(hint, "It holds more than 1 MB, more than DA Assistant "
                                       "reads at once.")
                self.assertIsNone(selection)
                self.assertEqual(selection_hint, "The highlighted text is more than 1 MB, "
                                                 "more than DA Assistant reads at once.")

    def test_a_picture_too_large_is_not_taken_for_klippers_text(self):
        def run(argv, timeout=3, binary=False):
            if binary:
                raise clipboard._TooLarge(clipboard.IMAGE_LIMIT)
            return None

        with mock.patch.object(clipboard.Gtk.Clipboard, "get", return_value=mock.Mock()), \
                mock.patch.object(clipboard.session, "session_type",
                                  return_value="wayland"), \
                mock.patch.object(clipboard.shutil, "which", lambda name: "/usr/bin/" + name), \
                mock.patch.object(clipboard, "_run", side_effect=run), \
                mock.patch.object(clipboard, "_klipper_text", return_value="image/png 9x9"):
            self.assertEqual(clipboard.get_text(), (None, "empty"))
            self.assertIn("more than 2 MB", clipboard.unreadable_hint())
            self.assertIsNone(clipboard.get_image())

    def test_within_the_limit_all_of_it_is_read(self):
        text, hint, _selection, _ = self.text_from(gtk_text="x" * (1 << 20))
        self.assertEqual(text, ("x" * (1 << 20), "text"))
        self.assertEqual(hint, "")


class APictureIsNotKlippersDescriptionOfIt(unittest.TestCase):
    def test_a_copied_image_reads_as_one(self):
        board = mock.Mock()
        board.wait_for_text.return_value = None
        board.wait_is_image_available.return_value = False
        with mock.patch.object(clipboard.Gtk.Clipboard, "get", return_value=board), \
                mock.patch.object(clipboard.shutil, "which", lambda name: None), \
                mock.patch.object(clipboard, "get_image", return_value=Image.new("RGB", (4, 4))), \
                mock.patch.object(clipboard, "_klipper_text", return_value="image/png 4x4"):
            self.assertEqual(clipboard.get_text(), (None, "image"))


class TheNewAction(unittest.TestCase):
    def test_it_is_an_action_with_a_suggested_key_and_no_legacy_one(self):
        self.assertIn("translate_selection", hotkeys.action_names())
        self.assertEqual(hotkeys.LABELS["translate_selection"], "Translate selected text")
        self.assertIn(("translate_selection", "Translate selected text",
                       "<Control><Shift>0"), hotkeys.ACTIONS)
        self.assertNotIn("translate_selection", hotkeys.LEGACY_KEYS)


class StubApp:
    _start_clipboard = Assistant._start_clipboard
    translate_text = Assistant.translate_text
    translate_selection = Assistant.translate_selection
    query_text = Assistant.query_text
    _watch_review = Assistant._watch_review
    _offer_lookups = Assistant._offer_lookups
    look_up_selection = Assistant.look_up_selection
    _refresh_reviews = Assistant._refresh_reviews
    _confirm_privacy = Assistant._confirm_privacy
    _start_privacy = Assistant._start_privacy
    _image_answerer = Assistant._image_answerer
    _answer_role = Assistant._answer_role

    def __init__(self, config):
        self.config = config
        self._reviews = []
        self.notices = []
        self.images = []

    def _require_api_key(self):
        return True

    def ocr_translate(self, image=None):
        self.images.append(("ocr_translate", image))

    def query_image(self, image=None):
        self.images.append(("query_image", image))

    def notify(self, message, urgency="low"):
        self.notices.append(message)


class Operations(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        self.app = StubApp(self.config)

    def reviewed(self, operation):
        made = []

        def make(*args, **kwargs):
            made.append(args[0])
            dialog = TextReviewDialog(*args, **kwargs)
            self.addCleanup(dialog.destroy)
            return dialog

        with mock.patch.object(app_module, "TextReviewDialog", side_effect=make):
            getattr(self.app, operation)()
        return made

    def test_translate_selection_reviews_the_highlighted_text(self):
        with mock.patch.object(app_module.clipboard, "get_selection",
                               return_value="Hallo Welt"), \
                mock.patch.object(app_module.clipboard, "get_text",
                                  side_effect=AssertionError("read the clipboard")):
            self.assertEqual(self.reviewed("translate_selection"), ["Hallo Welt"])

    def test_with_nothing_highlighted_it_says_so(self):
        with mock.patch.object(app_module.clipboard, "get_selection", return_value=None):
            self.assertEqual(self.reviewed("translate_selection"), [])
        self.assertIn("Nothing is highlighted", self.app.notices[-1])

    def test_a_picture_on_the_clipboard_goes_to_the_image_twin(self):
        picture = Image.new("RGB", (8, 8))
        with mock.patch.object(app_module.clipboard, "get_text", return_value=(None, "image")), \
                mock.patch.object(app_module.clipboard, "get_image", return_value=picture):
            self.assertEqual(self.reviewed("translate_text"), [])
            self.assertEqual(self.reviewed("query_text"), [])
        self.assertEqual(self.app.images, [("ocr_translate", picture),
                                           ("query_image", picture)])


class PastingAPictureIntoAChat(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for patch in (mock.patch.object(webkitguard, "STATE_FILE",
                                        os.path.join(self.dir, "webkit-crash")),
                      mock.patch.object(webkitguard, "LOG_DIR", self.dir),
                      mock.patch.object(chat, "WebKit2", None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.config = Config(os.path.join(self.dir, "c.json"))

    def test_ctrl_v_with_a_picture_attaches_it_through_the_editor(self):
        app = mock.Mock(config=self.config)
        window = chat.ChatWindow(app, "Ask anything", [])
        self.addCleanup(window.destroy)
        board = Gtk.Clipboard.get(clipboard.Gdk.SELECTION_CLIPBOARD)
        board.set_image(GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 6, 4))
        GLib.MainContext.default().iteration(False)
        edited = []
        with mock.patch.object(window, "_redact",
                               lambda image, done, name: (edited.append(name),
                                                          done(image, []))):
            window.entry.emit("paste-clipboard")
        self.assertEqual(edited, ["Pasted picture"])
        self.assertEqual([a["name"] for a in window._attachments], ["Pasted picture"])
        self.assertEqual(window.entry.get_buffer().get_text(
            *window.entry.get_buffer().get_bounds(), False), "")

    def test_text_on_the_clipboard_pastes_as_text(self):
        app = mock.Mock(config=self.config)
        window = chat.ChatWindow(app, "Ask anything", [])
        self.addCleanup(window.destroy)
        Gtk.Clipboard.get(clipboard.Gdk.SELECTION_CLIPBOARD).set_text("plain words", -1)
        with mock.patch.object(window, "_redact",
                               side_effect=AssertionError("opened the editor")):
            window._on_entry_paste(window.entry)
        self.assertEqual(window._attachments, [])


if __name__ == "__main__":
    unittest.main()
