"""The bar between the answers and the message box: what dragging it does, and that the
height it is left at outlasts the window."""

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
from gi.repository import GLib, Gtk

from assistant import webkitguard
from assistant.config import Config
from assistant.ui import chat
from assistant.ui.chat import ChatWindow


class NoWebKit:
    """The plain-text view: the bar is the same over either, and this one is quick."""

    def __getattr__(self, name):
        raise TypeError("could not get a reference to type class")


class StubApp:
    client = None

    def __init__(self, config):
        self.config = config
        self.notices = []

    def notify(self, message, urgency="low"):
        self.notices.append(message)


class TheBarBetweenAnswersAndMessageBox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for patch in (mock.patch.object(webkitguard, "STATE_FILE",
                                        os.path.join(self.dir, "webkit-crash")),
                      mock.patch.object(webkitguard, "LOG_DIR", self.dir),
                      mock.patch.object(chat, "WebKit2", NoWebKit())):
            patch.start()
            self.addCleanup(patch.stop)
        self.path = os.path.join(self.dir, "c.json")
        self.config = Config(self.path)
        self.config.api_key = "test"

    def _settle(self, ms=150):
        GLib.timeout_add(ms, Gtk.main_quit)
        Gtk.main()

    def _window(self, size=(800, 700)):
        window = ChatWindow(StubApp(self.config), "Test",
                            [{"role": "user", "content": "p", "display": "Question?"}])
        self.addCleanup(window.destroy)
        window.resize(*size)
        self._settle()
        return window

    def _drag_to(self, window, height):
        """What dragging does: the bar moved, so the message box comes to `height`."""
        shown = window.prompt_box.get_allocated_height()
        window.splitter.set_position(window.splitter.get_position() + shown - height)
        self._settle(chat.PROMPT_HEIGHT_SAVE_MS + 150)

    def _saved(self):
        with open(self.path) as f:
            return json.load(f).get("chat_prompt_height")

    def test_raising_it_takes_room_from_the_answers_for_the_text_box(self):
        window = self._window()
        answers = window.view.get_allocated_height()
        typing = window.entry.get_allocated_height()
        self._drag_to(window, 300)
        self.assertEqual(window.prompt_box.get_allocated_height(), 300)
        self.assertLess(window.view.get_allocated_height(), answers)
        self.assertGreater(window.entry.get_allocated_height(), typing + 100)

    def test_lowering_it_again_gives_the_room_back(self):
        window = self._window()
        self._drag_to(window, 300)
        answers = window.view.get_allocated_height()
        self._drag_to(window, 150)
        self.assertEqual(window.prompt_box.get_allocated_height(), 150)
        self.assertEqual(window.view.get_allocated_height(), answers + 150)

    def test_the_answers_are_never_dragged_away(self):
        window = self._window()
        window.splitter.set_position(0)
        self._settle()
        self.assertGreaterEqual(window.view.get_allocated_height(),
                                chat.MIN_ANSWERS_HEIGHT)

    def test_nor_is_the_message_box(self):
        window = self._window()
        natural = window.prompt_box.get_allocated_height()
        window.splitter.set_position(10 ** 6)
        self._settle()
        self.assertGreaterEqual(window.prompt_box.get_allocated_height(), natural)
        self.assertGreaterEqual(window.entry.get_parent().get_allocated_height(), 54)

    def test_the_height_it_is_left_at_is_kept_for_the_next_window(self):
        first = self._window()
        self._drag_to(first, 260)
        self.assertEqual(self._saved(), 260)
        first.destroy()
        self.assertEqual(self._window().prompt_box.get_allocated_height(), 260)

    def test_closing_straight_after_dragging_keeps_it_too(self):
        window = self._window()
        shown = window.prompt_box.get_allocated_height()
        window.splitter.set_position(window.splitter.get_position() + shown - 260)
        self._settle(50)                    # laid out, and well short of the save
        self.assertIsNotNone(window._prompt_height_timer)
        window.destroy()
        self.assertEqual(self._saved(), 260)

    def test_a_window_nobody_dragged_saves_nothing(self):
        window = self._window()
        window.resize(900, 500)
        self._settle(chat.PROMPT_HEIGHT_SAVE_MS + 150)
        window.destroy()
        self.assertFalse(os.path.exists(self.path) and self._saved())

    def test_resizing_the_window_goes_to_the_answers(self):
        window = self._window()
        self._drag_to(window, 220)
        answers = window.view.get_allocated_height()
        window.resize(800, 500)
        self._settle()
        self.assertEqual(window.prompt_box.get_allocated_height(), 220)
        self.assertEqual(window.view.get_allocated_height(), answers - 200)

    def test_a_small_window_does_not_change_what_is_remembered(self):
        window = self._window()
        self._drag_to(window, 300)
        window.resize(800, 340)             # less than the box and the answers want
        self._settle(chat.PROMPT_HEIGHT_SAVE_MS + 150)
        self.assertEqual(self._saved(), 300)

    def test_a_height_from_a_big_window_leaves_a_small_one_its_answers(self):
        self.config.chat_prompt_height = 2000
        window = self._window()
        self._settle(chat.PROMPT_HEIGHT_SAVE_MS + 150)
        self.assertGreater(window.view.get_allocated_height(), chat.MIN_ANSWERS_HEIGHT)
        self.assertEqual(self.config.chat_prompt_height, 2000)  # and it stays remembered

    def test_a_failed_save_is_said_rather_than_raised(self):
        window = self._window()
        with mock.patch.object(self.config, "save", side_effect=OSError("disk full")):
            self._drag_to(window, 240)
        self.assertIn("disk full", " ".join(window.app.notices))


if __name__ == "__main__":
    unittest.main(verbosity=2)
