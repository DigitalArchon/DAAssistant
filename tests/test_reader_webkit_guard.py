"""Every WebKit view the app makes is guarded as the chat window's is.

Where WebKit is missing, switched off with DA_ASSISTANT_NO_WEBKIT, or has twice ended
the process (webkitguard), a chat window is plain text. Search Encyclopedia's article
pane and an animation's window built their WebViews regardless: on a host whose WebKit
kills the process, the app died with them, and no strike was counted. And a window that
built no view, closed, cleared the strikes that had kept it from building one.
"""

import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import GLib, Gtk  # noqa: E402

from assistant import __version__, collector, webkitguard  # noqa: E402

# WebViews are made and closed here; a cycle collected on another thread would finalize
# one there, and WebKit aborts. On the main loop instead, as the app does.
collector.install()
from assistant.config import Config  # noqa: E402
from assistant.ui import article_view, chat, encscheme  # noqa: E402


class Forbidden:
    """A WebKit namespace no view may be built from: the test fails on first use."""

    def __getattr__(self, name):
        raise AssertionError(f"WebKit used while it is off ({name})")


class Broken:
    """Imports fine, raises the moment anything is built - as gi's does."""

    def __getattr__(self, name):
        raise TypeError("could not get a reference to type class")


def run_until(predicate, timeout=15):
    """Turn the main loop until `predicate()`, for up to `timeout` seconds."""
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


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "webkit-crash")
        for patch in (mock.patch.object(webkitguard, "STATE_FILE", self.state),
                      mock.patch.object(webkitguard, "LOG_DIR", self.dir),
                      mock.patch.object(webkitguard, "_loading", set()),
                      mock.patch.object(encscheme, "_encyclopedia",
                                        encscheme._encyclopedia)):
            patch.start()
            self.addCleanup(patch.stop)
        self.encyclopedia = types.SimpleNamespace()

    def strikes(self, count):
        with open(self.state, "w", encoding="utf-8") as f:
            f.write(f"{count} 0 {__version__}\n")

    def off(self):
        """WebKit off by DA_ASSISTANT_NO_WEBKIT, and any use of it a failure."""
        for patch in (mock.patch.dict(os.environ, {webkitguard.FORCE_OFF: "1"}),
                      mock.patch.object(article_view, "WebKit2", Forbidden())):
            patch.start()
            self.addCleanup(patch.stop)

    def pane(self):
        pane = article_view.ArticlePane(self.encyclopedia)
        holder = Gtk.Window()
        holder.add(pane)
        self.addCleanup(holder.destroy)
        return pane, holder


class WithWebKitOff(GuardCase):
    def test_the_article_pane_is_text(self):
        self.off()
        pane, _holder = self.pane()
        self.assertIsNone(pane.webview)
        pane.message("Search Encyclopedia", "Search for an article above.")
        buffer = pane.text_views()[0].get_buffer()
        self.assertIn("Search for an article above.",
                      buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))

    def test_an_animation_says_why_it_cannot_play(self):
        self.off()
        window = article_view.FlashWindow(self.encyclopedia, "13.swf")
        self.addCleanup(window.destroy)
        self.assertIsNone(window.webview)
        [label] = window.get_children()
        self.assertIn(webkitguard.FORCE_OFF, label.get_text())

    def test_two_strikes_keep_both_from_building_a_view(self):
        self.strikes(webkitguard.STRIKES)
        with mock.patch.object(article_view, "WebKit2", Forbidden()):
            pane, holder = self.pane()
            window = article_view.FlashWindow(self.encyclopedia, "13.swf")
            self.assertIsNone(pane.webview)
            self.assertIsNone(window.webview)
            # Closed, they leave the strikes as they were: they say nothing of WebKit.
            window.destroy()
            holder.destroy()
        self.assertTrue(webkitguard.blocked()[0])

    def test_nor_does_a_plain_text_chat_window_closed(self):
        self.strikes(webkitguard.STRIKES)
        app = types.SimpleNamespace(config=Config(os.path.join(self.dir, "c.json")),
                                    client=None, notify=lambda *a, **k: None)
        window = chat.ChatWindow(app, "Ask anything", [])
        self.assertIsNone(window.webview)
        window.destroy()
        self.assertTrue(webkitguard.blocked()[0], "the next window would build one")


class AWebKitThatRaises(GuardCase):
    def test_gives_the_text_pane_and_no_strike(self):
        with mock.patch.object(article_view, "WebKit2", Broken()):
            pane, _holder = self.pane()
            window = article_view.FlashWindow(self.encyclopedia, "13.swf")
            self.addCleanup(window.destroy)
        self.assertIsNone(pane.webview)
        self.assertIsNone(window.webview)
        self.assertFalse(os.path.exists(self.state))


@unittest.skipUnless(article_view.WebKit2 is not None, "needs WebKitGTK")
class AViewIsCountedWhileItLoads(GuardCase):
    def test_from_when_it_is_built_to_its_first_page(self):
        pane, holder = self.pane()
        self.assertIsNotNone(pane.webview)
        self.assertTrue(os.path.exists(self.state), "built without being counted")
        holder.show_all()
        pane.message("Search Encyclopedia", "Search for an article above.")
        self.assertTrue(run_until(lambda: not os.path.exists(self.state)))

    def test_closed_before_it_loaded_it_leaves_no_strike(self):
        pane, holder = self.pane()
        # The animation is not asked for: there is no DVD here to serve it.
        with mock.patch.object(article_view.WebKit2.WebView, "load_uri"):
            window = article_view.FlashWindow(self.encyclopedia, "13.swf")
        self.assertIsNotNone(window.webview)
        self.assertEqual(webkitguard._loading, {pane, window})
        window.destroy()
        self.assertTrue(os.path.exists(self.state), "the pane is still loading")
        holder.destroy()
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(webkitguard.blocked()[0])


if __name__ == "__main__":
    unittest.main()
