"""Ctrl+F finds text in a conversation - in the WebKit view and in the plain-text one -
and in Search Encyclopedia, in the article when that is where the user was."""

import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gdk, GLib, Gtk

from assistant import webkitguard
from assistant.config import Config
from assistant.ui import chat

TURNS = [{"role": "user", "content": "prompt", "display": "Tell me about apples."},
         {"role": "assistant", "content": "An apple a day. Apples are red; APPLE pie.",
          "model": "org/model-x"}]


def key(keyval, state=0):
    return types.SimpleNamespace(keyval=keyval, state=state)


def run_until(predicate, timeout=10):
    """Run the main loop until `predicate()` holds; whether it did in time."""
    def poll():
        if predicate():
            Gtk.main_quit()
            return False
        return True
    expired = []

    def expire():
        expired.append(True)
        Gtk.main_quit()
        return False
    source = GLib.timeout_add_seconds(timeout, expire)
    GLib.timeout_add(50, poll)
    Gtk.main()
    if not expired:
        GLib.source_remove(source)
    return predicate()


class FindWindow(unittest.TestCase):
    webkit = False

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        patches = [mock.patch.object(webkitguard, "STATE_FILE",
                                     os.path.join(self.dir, "webkit-crash")),
                   mock.patch.object(webkitguard, "LOG_DIR", self.dir)]
        if not self.webkit:
            patches.append(mock.patch.object(chat, "WebKit2", None))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def window(self, turns=TURNS):
        config = Config(os.path.join(self.dir, "c.json"))
        app = type("App", (), {"config": config, "client": None,
                               "notify": lambda self, *a, **k: None})()
        window = chat.ChatWindow(app, "Test", [dict(t) for t in turns])
        self.addCleanup(window.destroy)
        # First (cleanups run last in, first out): what the window left to do when idle
        # - scrolling the text to its end - done while it is still there to do it to.
        self.addCleanup(lambda: [Gtk.main_iteration() for _ in iter(Gtk.events_pending,
                                                                     False)])
        return window

    def find(self, window, text):
        self.assertTrue(window._on_window_key(window, key(Gdk.KEY_f,
                                                          Gdk.ModifierType.CONTROL_MASK)))
        self.assertTrue(window.find_bar.is_open())
        window.find_bar.entry.set_text(text)
        window.find_bar._search()           # what search-changed does, undelayed


class InPlainText(FindWindow):
    def marked(self, window, name):
        out = []
        for view in window.bubbles.text_views():
            buffer = view.get_buffer()
            tag = buffer.get_tag_table().lookup(name)
            where = buffer.get_start_iter()
            while tag is not None and where.forward_to_tag_toggle(tag):
                if where.starts_tag(tag):
                    end = where.copy()
                    end.forward_to_tag_toggle(tag)
                    out.append(buffer.get_text(where, end, False))
        return out

    def test_every_match_is_marked_and_stepped_through(self):
        window = self.window()
        self.find(window, "apple")
        self.assertEqual(window.find_bar.count.get_text(), "1 of 4")
        self.assertEqual(self.marked(window, "find-match"),
                         ["apple", "apple", "Apple", "APPLE"])
        self.assertEqual(self.marked(window, "find-current"), ["apple"])
        window.find_bar.step(1)
        self.assertEqual(window.find_bar.count.get_text(), "2 of 4")
        window.find_bar.step(-1)
        window.find_bar.step(-1)
        self.assertEqual(window.find_bar.count.get_text(), "4 of 4")
        self.assertEqual(self.marked(window, "find-current"), ["APPLE"])

    def test_nothing_found_says_so(self):
        window = self.window()
        self.find(window, "pear")
        self.assertEqual(window.find_bar.count.get_text(), "No matches")
        self.assertTrue(window.find_bar.entry.get_style_context().has_class("error"))

    def test_escape_closes_it_and_takes_the_marks_away(self):
        window = self.window()
        self.find(window, "apple")
        self.assertTrue(window._on_window_key(window, key(Gdk.KEY_Escape)))
        self.assertFalse(window.find_bar.is_open())
        self.assertEqual(self.marked(window, "find-match"), [])
        self.assertTrue(window.entry.is_focus())

    def test_an_answer_redrawn_since_is_searched_as_it_now_reads(self):
        window = self.window()
        self.find(window, "apple")
        window.conversation.append({"role": "assistant", "content": "More apple."})
        window._render()
        window.find_bar.step(1)
        self.assertEqual(window.find_bar.count.get_text(), "2 of 5")

    def test_ctrl_l_and_other_keys_still_reach_the_window(self):
        window = self.window()
        self.assertFalse(window._on_window_key(window, key(Gdk.KEY_Escape)))
        self.assertFalse(window._on_window_key(window, key(Gdk.KEY_F3)))


class InWebKit(FindWindow):
    webkit = True

    @classmethod
    def setUpClass(cls):
        if not gtkcompat.has_webkit():
            raise unittest.SkipTest("WebKit typelib unavailable")

    def test_matches_are_counted(self):
        window = self.window()
        self.assertIsNotNone(window.webview)
        self.assertTrue(run_until(lambda: window._loaded))
        self.find(window, "apple")
        self.assertTrue(run_until(lambda: window.find_bar.count.get_text()))
        self.assertEqual(window.find_bar.count.get_text(), "4 matches")
        self.find(window, "pear")
        self.assertTrue(run_until(
            lambda: window.find_bar.count.get_text() == "No matches"))


class InSearchEncyclopedia(unittest.TestCase):
    def test_the_article_when_the_user_was_reading_it(self):
        from assistant.ui.encyclopedia_windows import SearchEncyclopediaWindow
        # Its own choice, apart from building a window round an encyclopedia.
        window = mock.Mock(spec=SearchEncyclopediaWindow)
        window.pane, window.view = Gtk.Box(), Gtk.Label()
        window.pane.webview = None
        reading = Gtk.Button()
        window.pane.add(reading)
        window.view.set_visible(True)
        def target(window, focus):
            return SearchEncyclopediaWindow._find_target(window, focus)
        self.assertEqual(target(window, reading)[0], "article")
        with mock.patch.object(chat.ChatWindow, "_find_target",
                               lambda self, focus: ("conversation",)):
            self.assertEqual(target(window, Gtk.Entry())[0], "conversation")
            self.assertEqual(target(window, None)[0], "conversation")
            window.view.set_visible(False)      # answers hidden: only the article
            self.assertEqual(target(window, None)[0], "article")


if __name__ == "__main__":
    unittest.main()
