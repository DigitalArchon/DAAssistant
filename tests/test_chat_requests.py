"""What a chat window sends next, after a request went wrong: asked again with another
model and failed, or stopped part way, or overtaken by the next one."""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import GLib, Gtk  # noqa: E402

from assistant import webkitguard  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.llm import LLMError  # noqa: E402
from assistant.ui import chat  # noqa: E402

ANSWERED = [{"role": "user", "content": "Which port does it listen on?"},
            {"role": "assistant", "content": "Port 8443.", "model": "m"}]


class Scripted:
    """Answers each request as the test lines it up: text, an exception to raise, or
    WAIT - a few words, then nothing until Stop - or STALL, nothing at all."""

    WAIT, STALL = object(), object()

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.waiting = threading.Event()

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               **_kwargs):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages],
                           "image": image_base64})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply is self.WAIT or reply is self.STALL:
            if reply is self.WAIT:
                on_delta("Half an ans", "Half an ans")
            self.waiting.set()
            cancel.wait(10)
            return None
        return reply


class StubApp:
    def __init__(self, config, client):
        self.config, self.client = config, client

    def notify(self, *_args, **_kwargs):
        pass


def run_until(predicate, timeout=15):
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

    GLib.timeout_add(30, poll)
    source = GLib.timeout_add_seconds(timeout, expire)
    Gtk.main()
    if state["done"]:
        GLib.source_remove(source)
    return state["done"]


class WindowCase(unittest.TestCase):
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
        self.config.web_search_enabled = False

    def window(self, client, turns=ANSWERED, **kwargs):
        window = chat.ChatWindow(StubApp(self.config, client), "Ask anything",
                                 [dict(t) for t in turns], **kwargs)
        self.addCleanup(lambda: window._destroyed or window.destroy())
        return window

    def settled(self, window):
        self.assertTrue(run_until(lambda: window._cancel is None), "never settled")

    def send(self, window, text):
        window.entry.get_buffer().set_text(text)
        window._send()
        self.settled(window)


class AskingAgainForNothing(WindowCase):
    """Ask again with… marks the answers since the question as earlier ones. An answer
    that never comes must not leave them so: the conversation went on as if the model
    had never answered, both questions in one turn."""

    def _asked_again(self, *replies, turns=ANSWERED):
        client = Scripted(*replies)
        window = self.window(client, turns)
        window._ask_again("premium")
        return window, client

    def assert_kept(self, window, client):
        answer = window.conversation[1]
        self.assertFalse(answer.get("superseded"))
        self.assertNotIn("earlier answer", window.bubbles.transcript_text())
        client.replies.append("Edit server.conf.")
        self.send(window, "And how do I change it?")
        sent = client.calls[-1]["messages"]
        self.assertEqual([(m["role"], m["content"]) for m in sent],
                         [("user", "Which port does it listen on?"),
                          ("assistant", "Port 8443."),
                          ("user", "And how do I change it?")])

    def test_an_error_gives_the_earlier_answer_back(self):
        window, client = self._asked_again(LLMError("HTTP 429: slow down"))
        self.settled(window)
        self.assertTrue(window.conversation[-1].get("error"))
        self.assert_kept(window, client)

    def test_so_does_an_empty_reply(self):
        window, client = self._asked_again("")
        self.settled(window)
        self.assert_kept(window, client)

    def test_so_does_stop_before_any_of_it_came(self):
        window, client = self._asked_again(Scripted.STALL)
        self.assertTrue(run_until(client.waiting.is_set))
        window._stop()
        self.assert_kept(window, client)

    def test_part_of_an_answer_stopped_still_replaces_it(self):
        window, client = self._asked_again(Scripted.WAIT)
        self.assertTrue(run_until(lambda: window._streaming_text == "Half an ans"))
        window._stop()
        self.assertTrue(window.conversation[1].get("superseded"))
        self.assertTrue(window.conversation[-1]["content"].startswith("Half an ans"))

    def test_an_answer_that_comes_replaces_it(self):
        window, client = self._asked_again("Port 8443, unless changed.")
        self.settled(window)
        self.assertTrue(window.conversation[1].get("superseded"))
        client.replies.append("Edit server.conf.")
        self.send(window, "And how do I change it?")
        self.assertEqual([m["content"] for m in client.calls[-1]["messages"]
                          if m["role"] == "assistant"], ["Port 8443, unless changed."])

    def test_only_what_this_attempt_replaced_comes_back(self):
        turns = ANSWERED[:1] + [dict(ANSWERED[1], superseded=True),
                                {"role": "assistant", "content": "8443, by default.",
                                 "model": "m2"}]
        window, client = self._asked_again(LLMError("HTTP 503"), turns=turns)
        self.settled(window)
        self.assertTrue(window.conversation[1].get("superseded"))
        self.assertFalse(window.conversation[2].get("superseded"))


if __name__ == "__main__":
    unittest.main()
