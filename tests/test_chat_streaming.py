"""Regression test for the duplicated streamed response.

While a response streams, ChatWindow._render() writes a placeholder .turn into the DOM
holding the partial text. That element is a DOM-only artefact - it is deliberately not in
self.conversation. When the stream finished, _append_turn() used to INSERT the finished
turn before #end, leaving the placeholder in place, so the completed answer appeared
twice: once from the stream, once appended below it.

This drives the real ChatWindow against a stubbed client and counts the .turn elements
the browser actually ends up with.
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

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


class ChunksOnRequest:
    """Streams only what a test hands it, so a test can act between two chunks."""

    def __init__(self):
        self.chunks = queue.Queue()
        self.calls = 0

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None):
        self.calls += 1
        sent = ""
        while True:
            chunk = self.chunks.get()
            if chunk is None:
                return sent
            sent += chunk
            on_delta(chunk, sent)


LINES = "".join(f"line {i} of the answer\n\n" for i in range(60))
MORE = "".join(f"and line {i} more\n\n" for i in range(20))


class TheWebViewFollowsAnAnswerUntilTheReaderMoves(unittest.TestCase):
    """Down with the answer by default; left alone once the reader scrolls up, until
    they scroll back down to it or send something new."""

    @classmethod
    def setUpClass(cls):
        if not gtkcompat.has_webkit():
            raise unittest.SkipTest("WebKit typelib unavailable")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        config = Config(os.path.join(self.dir, "c.json"))
        config.api_key = "test"
        self.client = ChunksOnRequest()
        self.addCleanup(self.client.chunks.put, None)   # a worker still waiting ends
        # Long enough to scroll before anything streams.
        turns = [{"role": "user", "content": "p", "display": "Question?"},
                 {"role": "assistant", "content": LINES}]
        self.window = ChatWindow(StubApp(config, self.client), "Test", turns)
        self.addCleanup(self.window.destroy)
        self.window.set_default_size(600, 400)
        self.assertTrue(self._wait(lambda: self.window._loaded))

    def _wait(self, predicate, timeout=10):
        done = {"ok": False}
        deadline = GLib.get_monotonic_time() + timeout * 1000000

        def poll():
            if predicate() or GLib.get_monotonic_time() > deadline:
                done["ok"] = bool(predicate())
                Gtk.main_quit()
                return False
            return True

        GLib.timeout_add(20, poll)
        Gtk.main()
        return done["ok"]

    def _settle(self, ms=250):
        """Let the page deliver its scroll events, as it does a frame or so later."""
        GLib.timeout_add(ms, Gtk.main_quit)
        Gtk.main()

    def _page(self, script="void 0"):
        """Run `script`, then (scrollY, how far from the end of the page that is)."""
        got = {}

        def finish(view, result):
            got["v"] = json.loads(view.run_javascript_finish(result).get_js_value()
                                  .to_string())
            Gtk.main_quit()

        self.window.webview.run_javascript(
            f"{script};JSON.stringify([window.scrollY, "
            "document.documentElement.scrollHeight-window.innerHeight-window.scrollY])",
            None, finish)
        Gtk.main()
        return got["v"]

    def _scroll_to(self, y):
        """Where the reader's scrolling leaves the page, as far as the page can tell."""
        self._page(f"window.scrollTo(0,{y})")
        self._settle()

    def _start(self):
        self.window._start_stream()
        self.assertTrue(self._wait(lambda: self.client.calls == 1 and self.window._loaded))
        self._settle()

    def _chunk(self, text):
        """One more of the answer, on the page by the time this returns."""
        before = self.window._streaming_text or ""
        self.client.chunks.put(text)
        self.assertTrue(self._wait(
            lambda: (self.window._streaming_text or "") == before + text))
        self._settle(150)

    def _send(self, text):
        self.window.entry.get_buffer().set_text(text)
        self.window._send()
        self.assertTrue(self._wait(lambda: self.window._loaded))
        self._settle()

    def _finish(self):
        self.client.chunks.put(None)
        self.assertTrue(self._wait(lambda: self.window._cancel is None))
        self._settle()

    def test_a_request_waiting_for_its_first_chunk_is_shown_at_the_end(self):
        self._send("another question")
        self.assertLessEqual(self._page()[1], 1)

    def test_it_follows_an_answer_nobody_has_touched(self):
        self._start()
        for piece in (LINES, MORE):
            self._chunk(piece)
            self.assertLessEqual(self._page()[1], 1)
        self._finish()
        self.assertLessEqual(self._page()[1], 1)

    def test_scrolling_up_stays_put_as_the_answer_grows(self):
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self._chunk(MORE)
        self._chunk(MORE)
        self.assertEqual(self._page()[0], 200)

    def test_even_a_small_move_up_counts(self):
        self._start()
        self._chunk(LINES)
        y = self._page()[0]
        self._scroll_to(y - 10)
        self._chunk(MORE)
        self.assertEqual(self._page()[0], y - 10)

    def test_a_wheel_up_counts_before_the_page_has_moved(self):
        # The next chunk can land before the scroll event does: it must not undo the move.
        self._start()
        self._chunk(LINES)
        y = self._page("window.dispatchEvent(new WheelEvent('wheel',{deltaY:-40}))")[0]
        self._chunk(MORE)
        self.assertEqual(self._page()[0], y)

    def test_the_finished_answer_does_not_move_a_reader_who_went_up(self):
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self._finish()
        self.assertEqual(self._page()[0], 200)

    def test_scrolling_back_down_to_the_answer_follows_it_again(self):
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self._chunk(MORE)
        self._scroll_to(10 ** 7)
        for _ in range(2):
            self._chunk(MORE)
            self.assertLessEqual(self._page()[1], 1)
        self._finish()
        self.assertLessEqual(self._page()[1], 1)

    def test_scrolling_down_short_of_the_answer_does_not(self):
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self._scroll_to(400)
        self._chunk(MORE)
        self.assertEqual(self._page()[0], 400)

    def test_the_end_key_follows_a_fast_answer_it_would_otherwise_fall_short_of(self):
        # It scrolls to where the end was when pressed, and takes a moment to get there.
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self._page("window.dispatchEvent(new KeyboardEvent('keydown',{key:'End'}))")
        self._scroll_to(400)
        self._chunk(MORE)
        self.assertLessEqual(self._page()[1], 1)

    def test_going_up_again_after_coming_back_stays_put_again(self):
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self._scroll_to(10 ** 7)
        self._chunk(MORE)
        self._scroll_to(300)
        self._chunk(MORE)
        self.assertEqual(self._page()[0], 300)

    def test_a_new_prompt_goes_back_to_following(self):
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self._finish()
        self.assertEqual(self._page()[0], 200)
        self._send("and another")
        self.assertLessEqual(self._page()[1], 1)
        self.assertTrue(self._wait(lambda: self.client.calls == 2))
        self._chunk(MORE)
        self.assertLessEqual(self._page()[1], 1)

    def test_a_redraw_of_every_turn_keeps_a_readers_place(self):
        # A private conversation badges its question while the answer is streaming.
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self.window._render(full=True)
        self._settle()
        self.assertEqual(self._page()[0], 200)
        self._chunk(MORE)
        self.assertEqual(self._page()[0], 200)

    def test_stop_keeps_a_readers_place(self):
        self._start()
        self._chunk(LINES)
        self._scroll_to(200)
        self.window._stop()
        self._settle()
        self.assertEqual(self._page()[0], 200)

    def test_stop_leaves_a_follower_at_the_end(self):
        self._start()
        self._chunk(LINES)
        self.window._stop()
        self._settle()
        self.assertLessEqual(self._page()[1], 1)

    def test_a_crashed_page_comes_back_with_the_next_redraw(self):
        # Redrawn in place, it would stay blank: there is no page left to draw on.
        self.window.webview.terminate_web_process()
        self.assertTrue(self._wait(lambda: not self.window._loaded))
        self.window._render(full=True)
        self.assertTrue(self._wait(lambda: self.window._loaded))
        got = {}

        def finish(view, result):
            got["n"] = view.run_javascript_finish(result).get_js_value().to_int32()
            Gtk.main_quit()

        self.window.webview.run_javascript(
            "document.querySelectorAll('.turn').length", None, finish)
        Gtk.main()
        self.assertEqual(got["n"], 2)

    def test_a_redraw_with_nothing_streaming_does_not_scroll(self):
        # A dictionary switched off, with the page read from its top.
        self.assertEqual(self._page()[0], 0)
        self.window._render(full=True)
        self._settle()
        self.assertEqual(self._page()[0], 0)


class TheBubbleViewFollowsAnAnswerUntilTheReaderMoves(unittest.TestCase):
    """The same promise from the view without WebKit."""

    def setUp(self):
        self.view = bubbles.BubbleView()
        self.toplevel = Gtk.OffscreenWindow()
        self.toplevel.set_default_size(500, 300)
        self.toplevel.add(self.view)
        self.toplevel.show_all()
        self.addCleanup(self.toplevel.destroy)
        self.text = LINES
        self._show()

    def _settle(self):
        GLib.timeout_add(120, Gtk.main_quit)
        Gtk.main()

    def _show(self):
        """A redraw, as ChatWindow._render does it: the turns, then keep to the end."""
        self.view.set_turns([{"role": "assistant", "content": self.text,
                              "_streaming": True}])
        self._settle()
        self.view.follow_end()
        self._settle()

    def _grow(self):
        self.text += MORE
        self._show()

    def _value(self):
        return self.view.get_vadjustment().get_value()

    def _from_end(self):
        adjustment = self.view.get_vadjustment()
        return adjustment.get_upper() - adjustment.get_page_size() - adjustment.get_value()

    def test_it_follows_an_answer_nobody_has_touched(self):
        self._grow()
        self.assertGreater(self._value(), 0)
        self.assertLessEqual(self._from_end(), 1)

    def test_scrolling_up_stays_put_as_the_answer_grows(self):
        self.view.get_vadjustment().set_value(100)
        self._grow()
        self._grow()
        self.assertEqual(self._value(), 100)

    def test_even_a_small_move_up_counts(self):
        adjustment = self.view.get_vadjustment()
        at = adjustment.get_value() - 5
        adjustment.set_value(at)
        self._grow()
        self.assertEqual(self._value(), at)

    def test_scrolling_back_down_to_the_answer_follows_it_again(self):
        self.view.get_vadjustment().set_value(100)
        self._grow()
        adjustment = self.view.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        self._grow()
        self.assertLessEqual(self._from_end(), 1)

    def test_scrolling_down_short_of_the_answer_does_not(self):
        adjustment = self.view.get_vadjustment()
        adjustment.set_value(100)
        adjustment.set_value(200)
        self._grow()
        self.assertEqual(self._value(), 200)

    def test_an_answer_that_shrinks_does_not_look_like_the_reader_scrolling(self):
        # Half-finished Markdown can draw taller than the finished thing: the view is
        # pulled back as it gets shorter, and has to go on following regardless.
        self._grow()
        self.text = LINES
        self._show()
        self._grow()
        self.assertLessEqual(self._from_end(), 1)

    def test_a_new_prompt_goes_back_to_following(self):
        self.view.get_vadjustment().set_value(100)
        self._grow()
        self.view.resume_following()
        self._grow()
        self.assertLessEqual(self._from_end(), 1)


class OneRedrawWaitsAtATime(unittest.TestCase):
    """A streamed answer is redrawn from the main loop, and each redraw renders the
    whole answer so far. They used to be queued every 80 ms whether or not the last
    had been run: behind a main loop slower than that - a long answer is enough to
    make it so - they piled up, each for text the next had already replaced, and the
    finished answer waited behind them all."""

    setUpClass = StreamingDom.setUpClass
    setUp, tearDown = StreamingDom.setUp, StreamingDom.tearDown
    _run_until, _turn_count = StreamingDom._run_until, StreamingDom._turn_count
    _make_window = StreamingDom._make_window

    def _settled(self, window, turns=2):
        return window._cancel is None and len(window.conversation) >= turns \
            and window._loaded

    def _counted(self, window):
        redraws = []
        real = window._on_stream_progress
        window._on_stream_progress = lambda *a: (redraws.append(a[0]), real(*a))[1]
        return redraws

    def test_a_busy_main_loop_is_handed_one_redraw_not_one_per_interval(self):
        import time
        # 15 pieces over 0.3 s - four redraws' worth - all sent while the main loop
        # is busy with something else.
        client = StubClient("word " * 15, chunks=15, delay=0.02)
        window = self._make_window(client)
        try:
            redraws = self._counted(window)
            window._start_stream()
            time.sleep(0.6)             # the main loop, busy: nothing is run
            self.assertTrue(self._run_until(lambda: self._settled(window)))
            self.assertEqual(len(redraws), 1, "redraws queued up behind the main loop")
            self.assertEqual(self._turn_count(window), 2)
            self.assertEqual(window.conversation[-1]["content"], "word " * 15)
        finally:
            window.destroy()

    def test_a_main_loop_that_keeps_up_redraws_as_often_as_before(self):
        client = StubClient("word " * 6, chunks=6, delay=0.15)
        window = self._make_window(client)
        try:
            redraws = self._counted(window)
            window._start_stream()
            self.assertTrue(self._run_until(lambda: self._settled(window)))
            self.assertEqual(len(redraws), 6)
        finally:
            window.destroy()

    def test_a_redraw_lost_with_one_request_does_not_hold_up_the_next(self):
        window = self._make_window(StubClient("word " * 4))
        try:
            window._redraw_queued = True    # as if one never came back
            redraws = self._counted(window)
            window._start_stream()
            self.assertTrue(self._run_until(lambda: self._settled(window)))
            self.assertTrue(redraws, "nothing was redrawn while the answer arrived")
        finally:
            window.destroy()


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
