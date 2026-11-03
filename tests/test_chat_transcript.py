"""The transcript keeps its shape whatever an answer's markup does, and its Copy buttons
copy what they say they copy.

Model output - steered, perhaps, by a page it was asked to read - and a provider's error
body are rendered as Markdown into one page with every other turn. Markup that leaves an
element open runs on into what follows: the rest of the conversation one link, or nested
inside the answer, where the next redraw took it out of view and a fresh load left no
#end to draw at. No script runs either way (the page's policy holds); this is about the
page's structure, and what its buttons copy.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from assistant import webkitguard  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.ui import chat, markdown_html  # noqa: E402
from assistant.ui.markdown_html import render_markdown  # noqa: E402

VOID = {"br", "hr", "img"}


def unbalanced(html):
    """The elements in `html` left open or closed out of turn, as a list; [] when every
    element is closed, and in order."""
    problems, stack = [], []

    class Check(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                stack.append(tag)

        def handle_startendtag(self, tag, attrs):
            if tag not in VOID:
                problems.append(f"<{tag}/>")

        def handle_endtag(self, tag):
            if not stack or stack[-1] != tag:
                problems.append(f"</{tag}>")
            else:
                stack.pop()

    Check().feed(html)
    return problems + [f"<{tag}>" for tag in stack]


class SelfClosingTagsAreClosed(unittest.TestCase):
    def test_a_self_closing_link_does_not_take_the_rest_of_the_answer(self):
        html = render_markdown('See <a href="https://evil.example/"/> rest of answer')
        self.assertIn('<a href="https://evil.example/"></a>', html)
        self.assertEqual(unbalanced(html), [])
        self.assertLess(html.index("</a>"), html.index("rest of answer"))

    def test_every_allowed_element_written_self_closing_is_empty(self):
        for tag in ("div", "b", "s", "code", "span", "em", "blockquote", "table", "li"):
            html = render_markdown(f"before <{tag}/> after")
            self.assertIn(f"<{tag}></{tag}>", html, tag)
            self.assertEqual(unbalanced(html), [], tag)

    def test_a_void_element_stays_void(self):
        html = render_markdown('one<br/>two <img src="data:image/png;base64,AAAA" alt="x"/>')
        self.assertIn("<br>", html)
        self.assertIn('<img src="data:image/png;base64,AAAA" alt="x">', html)
        self.assertNotIn("</br>", html)
        self.assertNotIn("</img>", html)

    def test_a_self_closing_script_or_unknown_tag_is_dropped_and_the_text_kept(self):
        html = render_markdown("one <script/> two <blink/> three")
        self.assertNotIn("script", html)
        self.assertNotIn("blink", html)
        self.assertIn("three", html)
        self.assertEqual(unbalanced(html), [])

    def test_an_error_body_shown_as_markdown_is_held_to_the_same(self):
        html = markdown_html.turn_html("assistant", '**Error:** HTTP 500: <div/><a href="'
                                       'https://evil.example/"/>')
        self.assertEqual(unbalanced(html), [])


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

    GLib.timeout_add(40, poll)
    source = GLib.timeout_add_seconds(timeout, expire)
    Gtk.main()
    if state["done"]:
        GLib.source_remove(source)
    return state["done"]


def settle(ms=200):
    GLib.timeout_add(ms, Gtk.main_quit)
    Gtk.main()


class StubApp:
    def __init__(self, config, client=None):
        self.config, self.client = config, client

    def notify(self, *_args, **_kwargs):
        pass


class WindowCase(unittest.TestCase):
    """A chat window on a config of the test's own, with no strike left behind."""

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

    def window(self, turns, client=None):
        window = chat.ChatWindow(StubApp(self.config, client), "Ask anything",
                                 [dict(t) for t in turns])
        self.addCleanup(lambda: window._destroyed or window.destroy())
        if self.webkit:
            self.assertTrue(run_until(lambda: window._loaded), "the page never loaded")
        return window

    @staticmethod
    def page(window, expression):
        """What `expression` comes to in the window's page, through JSON."""
        got = {}

        def finish(view, result):
            try:
                got["value"] = json.loads(view.evaluate_javascript_finish(result)
                                          .to_string())
            except Exception as e:          # the test fails on it, below
                got["value"] = f"ERROR {e}"
            Gtk.main_quit()

        window.webview.evaluate_javascript(f"JSON.stringify({expression})", -1, None,
                                           None, None, finish)
        source = GLib.timeout_add_seconds(10, Gtk.main_quit)
        Gtk.main()
        if "value" in got:
            GLib.source_remove(source)
        return got.get("value")


SHAPE = ("[document.querySelectorAll('body > .turn').length,"
         " document.querySelectorAll('.turn .turn').length,"
         " (document.getElementById('end') || {}).parentNode === document.body]")


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class ThePageKeepsItsShape(WindowCase):
    def test_a_self_closing_div_does_not_swallow_the_turns_after_it(self):
        window = self.window([{"role": "user", "content": "q"},
                              {"role": "assistant", "content": "A <div/> B"},
                              {"role": "user", "content": "the next question"}])
        self.assertEqual(self.page(window, SHAPE), [3, 0, True])
        window._render(full=True)
        settle()
        self.assertEqual(self.page(window, SHAPE), [3, 0, True])

    def test_a_self_closing_link_does_not_make_the_next_question_a_link(self):
        window = self.window([{"role": "user", "content": "q"},
                              {"role": "assistant",
                               "content": 'See <a href="https://evil.example/"/> rest'},
                              {"role": "user", "content": "the next question"}])
        linked = ("Array.prototype.map.call(document.querySelectorAll("
                  "'a[href=\"https://evil.example/\"]'), function (a) {"
                  " return a.textContent; })")
        self.assertEqual(self.page(window, linked), [""])

    def test_a_redraw_puts_back_an_end_that_a_turn_had_wrapped(self):
        # However it got there - a reference turn's own markup, say - the full redraw
        # used to throw on it and leave the page blank.
        window = self.window([{"role": "user", "content": "q"},
                              {"role": "assistant", "content": "a"}])
        self.page(window, "(function () { var t = document.querySelectorAll("
                          "'body > .turn'); t[t.length - 1].appendChild("
                          "document.getElementById('end')); return 1; })()")
        window._render(full=True)
        settle()
        self.assertEqual(self.page(window, SHAPE), [2, 0, True])


if __name__ == "__main__":
    unittest.main()
