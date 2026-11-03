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
import time
import types
import unittest
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from assistant import collector, webkitguard  # noqa: E402

# Windows are made and closed here while worker threads run; a cycle collected on one
# of those would finalize a WebKit object there, and WebKit aborts. On the main loop
# instead, as the app does.
collector.install()
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


class DeepNestingCostsLittle(unittest.TestCase):
    """A streaming answer is rendered whole on the main loop several times a second, so
    the sanitiser's cost is the window's responsiveness. Asking the stack of open
    elements what was open made it quadratic in their depth: 8,000 nested tags took a
    second a redraw."""

    DEPTH = 20000

    def test_thousands_deep_is_still_quick(self):
        # A run of text inside every element, and an end tag for none of them: each
        # used to search the whole stack.
        raw = "<b>w " * self.DEPTH + "</i>" * self.DEPTH
        started = time.perf_counter()
        html = markdown_html.sanitise(raw)
        self.assertLess(time.perf_counter() - started, 1.5)
        self.assertEqual(html.count("</b>"), self.DEPTH)
        self.assertEqual(unbalanced(html), [])

    def test_text_in_a_link_or_code_is_still_not_linked(self):
        html = markdown_html.sanitise('<a href="https://a.test/"><b>see https://b.test/'
                                      '</b></a> and https://c.test/ <code>https://d.test/'
                                      '</code>')
        self.assertNotIn('href="https://b.test/"', html)
        self.assertIn('href="https://c.test/"', html)
        self.assertNotIn('href="https://d.test/"', html)


def run_until(predicate, timeout=15):
    """Turn the main loop until `predicate()`, for up to `timeout` seconds - entering it
    again if something else quits it first, as a timer another test left can - and
    leave no timer of its own behind."""
    deadline = time.monotonic() + timeout

    def poll():
        if predicate() or time.monotonic() >= deadline:
            Gtk.main_quit()
        return True

    source = GLib.timeout_add(30, poll)
    try:
        while not predicate() and time.monotonic() < deadline:
            Gtk.main()
    finally:
        GLib.source_remove(source)
    return predicate()


def settle(ms=200):
    """Let the page catch up for `ms`."""
    until = time.monotonic() + ms / 1000
    run_until(lambda: time.monotonic() >= until, timeout=ms / 1000 + 1)


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

        window.webview.evaluate_javascript(f"JSON.stringify({expression})", -1, None,
                                           None, None, finish)
        run_until(lambda: "value" in got, timeout=10)
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


def clipboard_text():
    return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()


def js_result(text):
    """What copyAnswer's handler is given: a message whose value is `text`."""
    return types.SimpleNamespace(
        get_js_value=lambda: types.SimpleNamespace(to_string=lambda: text))


ANSWERS = [{"role": "user", "content": "question one"},
           {"role": "assistant", "content": "first answer", "model": "m"},
           {"role": "user", "content": "question two"},
           {"role": "assistant", "content": "second answer", "model": "m"}]


class EachTurnCarriesItsPlace(unittest.TestCase):
    def test_the_turn_says_where_it_is_and_an_answer_cannot_say_otherwise(self):
        html = markdown_html.turn_html("assistant", '<div data-turn="0">x</div>', index=3)
        self.assertTrue(html.startswith('<div class="turn assistant" data-turn="3">'))
        self.assertEqual(html.count("data-turn"), 1)


class AnAnswersCopyCopiesThatAnswer(WindowCase):
    webkit = False

    def test_a_place_that_is_not_a_finished_answer_copies_nothing(self):
        window = self.window(ANSWERS)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("untouched", -1)
        window._streaming_text = "half an answer"
        for place in ("0", "2", "4", "-1", "99", "x"):
            window._on_copy_answer(None, js_result(place))
            self.assertEqual(clipboard_text(), "untouched", place)
        window._streaming_text = None
        window._on_copy_answer(None, js_result("3"))
        self.assertEqual(clipboard_text(), "second answer")


class ThePlainTextViewCopiesWhatTheWebKitViewDoes(WindowCase):
    webkit = False

    CITED = {"role": "assistant", "model": "m",
             "content": "Ottawa is the capital [1].",
             "display": "Ottawa is the capital [\\[1\\]](enc://cite/1/1).",
             "export": "Ottawa is the capital [1].",
             "links": ("enc://cite/",)}

    def _copy(self, window, index):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("untouched", -1)
        [button] = [child for child in window.bubbles._rows[index]["heading"]
                    .get_children() if isinstance(child, Gtk.Button)]
        button.clicked()
        return clipboard_text()

    def test_an_answer_with_links_into_the_window_copies_without_them(self):
        window = self.window([{"role": "user", "content": "q"}, self.CITED])
        self.assertEqual(self._copy(window, 1), "Ottawa is the capital [1].")
        window._on_copy_answer(None, js_result("1"))
        self.assertEqual(clipboard_text(), "Ottawa is the capital [1].")

    def test_an_answer_still_coming_copies_without_the_cursor(self):
        window = self.window([{"role": "user", "content": "q"}])
        window._streaming_text = "Half an ans"
        window._render()
        self.assertEqual(self._copy(window, 1), "Half an ans")


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class TheButtonNamesItsOwnAnswer(WindowCase):
    def test_stray_turns_on_the_page_do_not_send_it_to_another_answer(self):
        window = self.window(ANSWERS)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("untouched", -1)
        # A turn the conversation does not have, ahead of the first answer - as markup
        # that escaped its bubble would leave: counted among the page's turns, the
        # first answer's button was the second question's.
        self.page(window, "(function () { var t = document.querySelectorAll("
                          "'body > .turn'); t[1].insertAdjacentHTML('beforebegin', "
                          "'<div class=\"turn\">stray</div>'); return 1; })()")
        settle()
        self.page(window, "(function () { var a = Array.prototype.filter.call("
                          "document.querySelectorAll('body > .turn.assistant'), "
                          "function (t) { return t.textContent.indexOf('first answer') "
                          ">= 0; })[0]; a.querySelector('.copy-answer').click(); "
                          "return 1; })()")
        self.assertTrue(run_until(lambda: clipboard_text() != "untouched"))
        self.assertEqual(clipboard_text(), "first answer")


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class ACodeBlocksCopyCopiesTheCode(WindowCase):
    def _copy_block(self, window, index):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("untouched", -1)
        settle()                            # the buttons are added as the page settles
        self.page(window, f"(function () {{ document.querySelectorAll('pre')[{index}]"
                          ".querySelector('button').click(); return 1; })()")
        self.assertTrue(run_until(lambda: clipboard_text() != "untouched"))
        return clipboard_text()

    def test_a_pre_without_code_copies_its_text_and_not_the_buttons_label(self):
        window = self.window([{"role": "user", "content": "q"},
                              {"role": "assistant",
                               "content": "<pre>raw pre text</pre>\n\n```\nfenced\n```"}])
        self.assertEqual(self._copy_block(window, 0), "raw pre text")
        # Highlighted code ends in the newline Pygments gives it.
        self.assertEqual(self._copy_block(window, 1).rstrip("\n"), "fenced")


if __name__ == "__main__":
    unittest.main()
