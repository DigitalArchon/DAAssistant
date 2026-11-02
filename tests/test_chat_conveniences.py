"""The chat window's conveniences: an answer copied on its own, the text made larger or
smaller and kept so, Ctrl+W and Escape, and what had come kept when an answer is stopped."""

import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from assistant import webkitguard  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.ui import chat  # noqa: E402

TURNS = [{"role": "user", "content": "q", "display": "What is a quince?"},
         {"role": "assistant", "content": "A **quince** is a fruit.", "model": "m"}]


class HalfAnAnswer:
    """Streams a few words, then waits to be stopped."""

    def __init__(self):
        self.calls = []
        self.started = threading.Event()

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               **_kwargs):
        self.calls.append(messages)
        on_delta("Half an ans", "Half an ans")
        self.started.set()
        cancel.wait(10)
        return None


class Answers:
    def __init__(self):
        self.calls = []

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               **_kwargs):
        self.calls.append(messages)
        return "Fine."


class StubApp:
    def __init__(self, config, client):
        self.config, self.client = config, client

    def notify(self, *_args, **_kwargs):
        pass


class WindowCase(unittest.TestCase):
    webkit = True

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
        self.config = Config(os.path.join(self.dir, "c.json"))

    def window(self, client=None, turns=TURNS, autostart=False):
        window = chat.ChatWindow(StubApp(self.config, client or Answers()), "Ask anything",
                                 [dict(t) for t in turns], autostart=autostart)
        self.addCleanup(lambda: window._destroyed or window.destroy())
        return window

    def run_until(self, predicate, timeout=15):
        state = {"done": False}

        def poll():
            if predicate():
                state["done"] = True
                Gtk.main_quit()
                return False
            return True

        def expire():
            Gtk.main_quit()
            return False

        GLib.timeout_add(50, poll)
        source = GLib.timeout_add_seconds(timeout, expire)
        Gtk.main()
        if state["done"]:
            GLib.source_remove(source)
        return state["done"]

    @staticmethod
    def key(keyval, state=0):
        return types.SimpleNamespace(keyval=keyval, state=state)


class StoppingKeepsWhatCame(WindowCase):
    webkit = False

    def test_the_words_that_came_are_kept_said_to_be_cut_short(self):
        client = HalfAnAnswer()
        window = self.window(client, turns=TURNS[:1], autostart=True)
        self.assertTrue(self.run_until(client.started.is_set))
        self.run_until(lambda: window._streaming_text == "Half an ans")
        window._stop()
        last = window.conversation[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertTrue(last["content"].startswith("Half an ans"))
        self.assertTrue(last["content"].endswith(chat.STOPPED_NOTE))
        self.assertEqual(window.status.get_text(), "Stopped")

    def test_escape_stops_an_answer(self):
        client = HalfAnAnswer()
        window = self.window(client, turns=TURNS[:1], autostart=True)
        self.assertTrue(self.run_until(client.started.is_set))
        self.assertTrue(window._on_window_key(window, self.key(Gdk.KEY_Escape)))
        self.assertIsNone(window._cancel)

    def test_escape_with_nothing_running_is_left_alone(self):
        window = self.window()
        self.assertFalse(window._on_window_key(window, self.key(Gdk.KEY_Escape)))


class KeysAndTextSize(WindowCase):
    webkit = False

    def test_ctrl_w_closes_the_window(self):
        window = self.window()
        window._on_window_key(window, self.key(Gdk.KEY_w, Gdk.ModifierType.CONTROL_MASK))
        self.assertTrue(window._destroyed)

    def test_the_size_steps_and_is_kept_for_the_next_window(self):
        window = self.window()
        control = Gdk.ModifierType.CONTROL_MASK
        window._on_window_key(window, self.key(Gdk.KEY_plus, control))
        window._on_window_key(window, self.key(Gdk.KEY_equal, control))
        self.assertEqual(self.config.chat_zoom, 1.25)
        self.assertEqual(Config(self.config.config_file).chat_zoom, 1.25)
        window._on_window_key(window, self.key(Gdk.KEY_minus, control))
        self.assertEqual(self.config.chat_zoom, 1.1)
        window._on_window_key(window, self.key(Gdk.KEY_0, control))
        self.assertEqual(self.config.chat_zoom, 1.0)

    def test_the_plain_text_view_is_sized_too(self):
        self.config.chat_zoom = 2.0
        window = self.window()
        css = window.bubbles._zoom.to_string()
        self.assertIn("font-size", css)
        size = float(css.split("font-size:")[1].split("pt")[0])
        self.assertAlmostEqual(size, chat.bubbles._theme_font_points() * 2.0, places=0)

    def test_each_answer_has_its_own_copy(self):
        window = self.window()
        window._render(full=True)
        buttons = [child for row in window.bubbles._rows if row["role"] == "assistant"
                   for child in row["heading"].get_children()
                   if isinstance(child, Gtk.Button)]
        self.assertEqual(len(buttons), 1)
        buttons[0].clicked()
        self.assertEqual(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text(),
                         window.bubbles._rows[1]["text"])


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class InTheWebKitView(WindowCase):
    def test_the_view_opens_at_the_kept_size(self):
        self.config.chat_zoom = 1.5
        window = self.window()
        self.assertEqual(window.webview.get_zoom_level(), 1.5)

    def test_the_size_steps_in_the_webkit_view_too(self):
        window = self.window()
        control = Gdk.ModifierType.CONTROL_MASK
        window._on_window_key(window, self.key(Gdk.KEY_plus, control))
        self.assertEqual(window.webview.get_zoom_level(), 1.1)
        self.assertEqual(Config(self.config.config_file).chat_zoom, 1.1)
        self.assertEqual(window.status.get_text(), "Text size 110%")
        window._on_window_key(window, self.key(Gdk.KEY_0, control))
        self.assertEqual(window.webview.get_zoom_level(), 1.0)

    def test_an_answers_copy_copies_its_markdown(self):
        window = self.window()
        window.show_all()
        self.assertTrue(self.run_until(lambda: window._loaded))
        result = types.SimpleNamespace(
            get_js_value=lambda: types.SimpleNamespace(to_string=lambda: "1"))
        window._on_copy_answer(None, result)
        self.assertEqual(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text(),
                         "A **quince** is a fruit.")
        self.assertEqual(window.status.get_text(), "Copied the answer")

    def test_the_page_gives_each_finished_answer_a_copy_button(self):
        window = self.window()
        window.show_all()
        self.assertTrue(self.run_until(lambda: window._loaded))
        counted = {}

        def done(view, res):
            counted["n"] = int(view.evaluate_javascript_finish(res).to_int32())

        def ask():
            window.webview.evaluate_javascript(
                "document.querySelectorAll('.copy-answer').length", -1, None, None,
                None, done)
            return False

        GLib.timeout_add(300, ask)
        self.assertTrue(self.run_until(lambda: "n" in counted))
        self.assertEqual(counted["n"], 1)


if __name__ == "__main__":
    unittest.main()
