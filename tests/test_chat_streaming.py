"""Regression test for the duplicated streamed response.

While a response streams, ChatWindow._render() writes a placeholder .turn into the DOM
holding the partial text. That element is a DOM-only artefact - it is deliberately not in
self.conversation. When the stream finished, _append_turn() used to INSERT the finished
turn before #end, leaving the placeholder in place, so the completed answer appeared
twice: once from the stream, once appended below it.

This drives the real ChatWindow against a stubbed client and counts the .turn elements
the browser actually ends up with.
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib, Gtk

from assistant import webkitguard
from assistant.config import Config
from assistant.ui import bubbles, chat, markdown_text
from assistant.ui.chat import ChatWindow


class StubClient:
    """Streams a canned answer through the same on_delta contract as LLMClient.

    Chunks are spaced wider than ChatWindow's 80ms redraw throttle, and the caller
    waits for the page to load first. Both matter: if deltas arrive before WebKit has
    loaded, every render falls back to a full reload from self.conversation, which
    cannot reproduce the duplicate. The bug lives purely in the incremental DOM path.
    """

    def __init__(self, text="Hello **world**", chunks=4, delay=0.15):
        self.text = text
        self.chunks = chunks
        self.delay = delay
        self.calls = []

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None):
        import time
        self.calls.append({"model": model, "web_search": web_search,
                           "messages": list(messages)})
        size = max(1, len(self.text) // self.chunks)
        sent = ""
        for i in range(0, len(self.text), size):
            sent += self.text[i:i + size]
            if on_delta:
                on_delta(self.text[i:i + size], sent)
            time.sleep(self.delay)      # let the main loop apply each render
        return self.text


class StubApp:
    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.notices = []

    def notify(self, message, urgency="low"):
        self.notices.append(message)


class StreamingDom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not gtkcompat.has_webkit():
            raise unittest.SkipTest("WebKit typelib unavailable")

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "test"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_until(self, predicate, timeout=20):
        deadline = [False]

        def expire():
            deadline[0] = True
            Gtk.main_quit()
            return False

        source = GLib.timeout_add_seconds(timeout, expire)

        def poll():
            if predicate():
                Gtk.main_quit()
                return False
            return True

        GLib.timeout_add(100, poll)
        Gtk.main()
        GLib.source_remove(source) if not deadline[0] else None
        return not deadline[0]

    def _turn_count(self, window):
        """Count .turn elements in the live document."""
        result = {}

        def got(view, res):
            try:
                result["n"] = int(view.run_javascript_finish(res)
                                  .get_js_value().to_string())
            except Exception as e:
                result["n"] = f"ERR {e}"
            Gtk.main_quit()

        window.webview.run_javascript(
            "document.querySelectorAll('.turn').length", None, got)
        # Removed once the answer is in: left behind, it quit whatever main loop a
        # later test was running ten seconds on, and that test read a page not loaded.
        source = GLib.timeout_add_seconds(10, Gtk.main_quit)
        Gtk.main()
        if "n" in result:
            GLib.source_remove(source)
        return result.get("n")

    def _make_window(self, client, autostart=False):
        app = StubApp(self.config, client)
        window = ChatWindow(app, "Test",
                            [{"role": "user", "content": "prompt",
                              "display": "Question?"}],
                            autostart=autostart)
        # Wait for the initial load so that streaming uses the incremental DOM path,
        # which is where the duplication happened.
        self.assertTrue(self._run_until(lambda: window._loaded), "page never loaded")
        return window

    def test_streamed_response_appears_exactly_once(self):
        client = StubClient("The quick brown fox jumps over the lazy dog.")
        window = self._make_window(client)
        try:
            window._start_stream()
            done = self._run_until(
                lambda: window._cancel is None and len(window.conversation) >= 2
                and window._loaded)
            self.assertTrue(done, "stream did not finish")
            # One user turn + one assistant turn. Three means the streaming placeholder
            # was left in the DOM alongside the appended answer.
            self.assertEqual(len(window.conversation), 2)
            self.assertEqual(self._turn_count(window), 2,
                             "the response was duplicated in the DOM")
        finally:
            window.destroy()

    def test_error_replaces_the_placeholder_too(self):
        class Failing(StubClient):
            def stream(self, *a, **kw):
                import time
                on_delta = kw.get("on_delta") or (a[4] if len(a) > 4 else None)
                if on_delta:
                    on_delta("partial", "partial")
                time.sleep(0.25)        # let the placeholder reach the DOM
                raise RuntimeError("provider exploded")

        window = self._make_window(Failing())
        try:
            window._start_stream()
            done = self._run_until(
                lambda: window._cancel is None and len(window.conversation) >= 2
                and window._loaded)
            self.assertTrue(done, "error path did not settle")
            self.assertEqual(len(window.conversation), 2)
            self.assertEqual(self._turn_count(window), 2,
                             "error message duplicated the placeholder")
            self.assertIn("Error", window.conversation[-1]["content"])
        finally:
            window.destroy()

    def test_compose_dialog_choice_reaches_the_first_request(self):
        """The first message is sent before the chat window is interactive.

        Its header toggle therefore only affects follow-ups, so the choice made on the
        compose dialog has to be carried in. Without this, turning search on for a
        one-off question was impossible - the request had already gone.
        """
        self.config.web_search_enabled = False      # saved default is off
        client = StubClient("ok", delay=0.0)
        app = StubApp(self.config, client)
        window = ChatWindow(app, "Test",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            autostart=True, web_search=True)   # ...but on for this one
        try:
            self._run_until(lambda: window._cancel is None and bool(client.calls))
            self.assertTrue(client.calls[0]["web_search"],
                            "compose-dialog choice did not reach the first request")
            self.assertTrue(window.search_toggle.get_active())
        finally:
            window.destroy()

    def test_compose_dialog_can_turn_search_off_for_one_request(self):
        self.config.web_search_enabled = True       # saved default is on
        client = StubClient("ok", delay=0.0)
        app = StubApp(self.config, client)
        window = ChatWindow(app, "Test",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            autostart=True, web_search=False)
        try:
            self._run_until(lambda: window._cancel is None and bool(client.calls))
            self.assertFalse(client.calls[0]["web_search"])
        finally:
            window.destroy()

    def test_default_is_used_when_compose_made_no_choice(self):
        self.config.web_search_enabled = True
        client = StubClient("ok", delay=0.0)
        app = StubApp(self.config, client)
        window = ChatWindow(app, "Test",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            autostart=True)          # web_search not specified
        try:
            self._run_until(lambda: window._cancel is None and bool(client.calls))
            self.assertTrue(client.calls[0]["web_search"])
        finally:
            window.destroy()

    def test_web_search_toggle_is_passed_to_the_client(self):
        client = StubClient("ok", delay=0.0)
        window = self._make_window(client)
        try:
            window.search_toggle.set_active(True)
            window._start_stream()
            self._run_until(lambda: window._cancel is None and bool(client.calls))
            self.assertTrue(client.calls[0]["web_search"])
        finally:
            window.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class WebKitStrikesFollowTheProcess(unittest.TestCase):
    """The crash guard must only count a WebView that ended the process.

    A window closed before its page loaded leaves the same breadcrumb on disk as one
    whose WebKit killed the app, so the guard is cleared on destroy as well as on load.
    Without that, ordinary use accumulates strikes and the app eventually talks itself
    out of the rendered view for no reason - which is exactly what this suite did to
    itself when the guard was written.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "webkit-crash")
        self.patches = [mock.patch.object(webkitguard, "STATE_FILE", self.state),
                        mock.patch.object(webkitguard, "LOG_DIR", self.dir)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_window_closed_before_it_loaded_leaves_no_strike(self):
        config = Config(os.path.join(self.dir, "c.json"))
        window = ChatWindow(StubApp(config, StubClient()), "Test",
                            [{"role": "user", "content": "p", "display": "Q"}])
        window.destroy()
        while Gtk.events_pending():
            Gtk.main_iteration()
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(webkitguard.blocked()[0])


class AChatWindowAlwaysHasAView(unittest.TestCase):
    """A window with no view is worse than an ugly one: the screenshot goes nowhere.

    On an Arch/KDE host the WebKit typelib imported and then raised on first use, so
    _build_view blew up inside __init__ and no window appeared at all - and the only
    trace of it was a traceback on a stderr the tray app does not have.
    """

    class BrokenWebKit:
        """Imports fine, raises the moment anything is built - as gi's does."""

        def __getattr__(self, name):
            raise TypeError("could not get a reference to type class")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(webkitguard, "STATE_FILE",
                              os.path.join(self.dir, "webkit-crash")),
            mock.patch.object(webkitguard, "LOG_DIR", self.dir),
            mock.patch.object(chat, "WebKit2", self.BrokenWebKit()),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_webkit_that_raises_gives_the_plain_text_view_instead(self):
        app = StubApp(Config(os.path.join(self.dir, "c.json")), StubClient())
        app.notify = lambda message, urgency="low": app.messages.append(message)
        app.messages = []

        window = ChatWindow(app, "Test", [{"role": "user", "content": "p",
                                           "display": "Question?"}])
        self.addCleanup(window.destroy)

        self.assertIsNone(window.webview)
        self.assertIsNotNone(window.bubbles)           # and there IS a window
        self.assertTrue(window.get_visible())
        self.assertIn("plain text", " ".join(app.messages))

    def test_and_the_transcript_is_still_readable_in_it(self):
        app = StubApp(Config(os.path.join(self.dir, "c.json")), StubClient())
        app.notify = lambda message, urgency="low": None
        window = ChatWindow(app, "Test", [{"role": "user", "content": "the question",
                                           "display": "the question"}])
        self.addCleanup(window.destroy)
        self.assertIn("the question", window.bubbles.transcript_text())

    def test_raising_is_not_counted_as_a_crash(self):
        # It raised, so the process survived: the next window may try WebKit again.
        app = StubApp(Config(os.path.join(self.dir, "c.json")), StubClient())
        app.notify = lambda message, urgency="low": None
        window = ChatWindow(app, "Test", [{"role": "user", "content": "p",
                                           "display": "Q"}])
        self.addCleanup(window.destroy)
        self.assertFalse(webkitguard.blocked()[0])


class ThePlainTextViewShowsWhatTheReaderAskedFor(unittest.TestCase):
    """The fallback view used to print the turn's `content`.

    For OCR that is the whole instruction prompt sent to the model, which the reader
    never asked for and which the WebKit view has never shown - it prints `display`.
    On a host without a usable WebKit, that made every OCR result open with the prompt.
    """

    def _buffer_text(self, turns):
        view = bubbles.BubbleView()
        view.set_turns(turns)
        return view.transcript_text()

    def test_the_prompt_behind_a_turn_is_not_shown(self):
        text = self._buffer_text([
            {"role": "user", "content": "Transcribe every character you can see. "
                                        "Output nothing else.",
             "display": "_Transcribe all text in the selected region._"}])
        self.assertIn("Transcribe all text in the selected region.", text)
        self.assertNotIn("Output nothing else", text)

    def test_a_turn_with_no_display_still_shows_its_content(self):
        # Follow-ups the user typed have no separate display text.
        text = self._buffer_text([{"role": "user", "content": "what does line 3 say?"}])
        self.assertIn("what does line 3 say?", text)

    def test_both_views_label_a_turn_the_same_way(self):
        text = self._buffer_text([{"role": "user", "content": "q"},
                                  {"role": "assistant", "content": "a"}])
        self.assertIn("YOU", text)
        self.assertIn("ASSISTANT", text)

    def test_markdown_becomes_formatting_rather_than_punctuation(self):
        text = self._buffer_text([{"role": "assistant", "content":
                                   "# Title\n\n**bold** and `code`\n\n- item\n\n"
                                   "```\nfenced\n```\n\n> quoted"}])
        for gone in ("#", "**", "`", "```"):
            self.assertNotIn(gone, text)
        self.assertIn("Title", text)
        self.assertIn("bold", text)
        self.assertIn("• item", text)
        self.assertIn("fenced", text)
        self.assertIn("quoted", text)

    def test_the_tags_that_formatting_needs_exist(self):
        buffer = Gtk.TextBuffer()
        markdown_text.render_body(buffer, "**x**")
        table = buffer.get_tag_table()
        for name in ("role", "h1", "bold", "italic", "code", "pre", "quote", "list"):
            self.assertIsNotNone(table.lookup(name), name)

    def test_rendering_twice_does_not_duplicate_the_transcript(self):
        view = bubbles.BubbleView()
        turns = [{"role": "assistant", "content": "only once"}]
        view.set_turns(turns)
        view.set_turns(turns)
        self.assertEqual(view.transcript_text().count("only once"), 1)

    def test_a_turn_that_disappears_takes_its_bubble_with_it(self):
        view = bubbles.BubbleView()
        view.set_turns([{"role": "user", "content": "one"},
                        {"role": "assistant", "content": "two"}])
        view.set_turns([{"role": "user", "content": "one"}])
        self.assertNotIn("two", view.transcript_text())

    def test_a_streaming_turn_is_updated_rather_than_rebuilt(self):
        # Several redraws a second: replacing the widgets each time would throw away
        # the reader's selection and scroll position.
        view = bubbles.BubbleView()
        view.set_turns([{"role": "assistant", "content": "partial", "_streaming": True}])
        first = view._rows[0]["view"]
        view.set_turns([{"role": "assistant", "content": "partial answer",
                         "_streaming": True}])
        self.assertIs(view._rows[0]["view"], first)
        self.assertIn("partial answer", view.transcript_text())


class TheFallbackViewTakesItsColoursFromTheTheme(unittest.TestCase):
    """Panels, not a colour of our own: the view has to work in whatever theme is on."""

    def test_code_and_quotes_are_panelled_from_the_themes_own_colours(self):
        view = Gtk.TextView()
        buffer = view.get_buffer()
        markdown_text.render_body(buffer, "> quoted", widget=view)
        table = buffer.get_tag_table()
        for name in ("pre", "quote"):
            self.assertIsNotNone(table.lookup(name), name)
        self.assertTrue(table.lookup("pre").get_property("paragraph-background-set"))

    def test_a_bubble_is_marked_up_for_the_stylesheet_to_find(self):
        view = bubbles.BubbleView()
        view.set_turns([{"role": "user", "content": "hello"}])
        classes = view._rows[0]["bubble"].get_style_context().list_classes()
        self.assertIn("chat-bubble", classes)
        self.assertIn("chat-user", classes)

    def test_it_still_renders_with_no_widget_to_ask(self):
        buffer = Gtk.TextBuffer()
        markdown_text.render_body(buffer, "**hi**")
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.assertIn("hi", text)
