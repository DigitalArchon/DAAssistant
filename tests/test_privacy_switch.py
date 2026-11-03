"""Privacy Mode switched on with conversations open.

A conversation fixed its privacy when it started, so ticking Privacy Mode left every
open window - and the encyclopedia's kept reader, where a private answer's citations
open - sending to the ordinary models, web search and all, while the tray said
"Privacy Mode on". Privacy only ever tightens: an open conversation now goes private
from its next message, and one whose private models cannot be used sends nothing.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import GLib, Gtk  # noqa: E402

from assistant.privacy import PrivacySession  # noqa: E402
from test_privacy import (FakeNanoGpt, RecordingClient, StubApp, TempConfig,  # noqa: E402
                          session_for)


class GatedClient(RecordingClient):
    """Streams like RecordingClient, but holds its answer until `release` is set."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def stream(self, *args, **kwargs):
        self.release.wait(10)
        return super().stream(*args, **kwargs)


class OpenConversationsGoPrivate(TempConfig):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check(sys.argv)[0]:
            raise unittest.SkipTest("no display")

    def run_until(self, predicate, timeout=15):
        state = {"expired": False}

        def expire():
            state["expired"] = True
            Gtk.main_quit()
            return False

        source = GLib.timeout_add_seconds(timeout, expire)

        def poll():
            if predicate():
                Gtk.main_quit()
                return False
            return True

        GLib.timeout_add(50, poll)
        Gtk.main()
        if not state["expired"]:
            GLib.source_remove(source)
        return not state["expired"]

    def window(self, client, **kwargs):
        from assistant.ui.chat import ChatWindow
        window = ChatWindow(StubApp(self.config, client), "Query text",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            autostart=True, web_search=True, **kwargs)
        self.addCleanup(window.destroy)
        return window

    def send(self, window, text):
        window.entry.get_buffer().set_text(text)
        window._send()

    def test_the_next_message_goes_to_the_attested_models_without_search(self):
        client = RecordingClient()
        window = self.window(client)
        self.assertTrue(self.run_until(lambda: window._cancel is None
                                       and len(window.conversation) >= 2))
        self.assertIsNone(client.calls[0]["privacy"])
        session = session_for(FakeNanoGpt())
        self.assertTrue(window.go_private(session))
        self.assertIs(window.privacy, session)
        self.assertFalse(window.search_toggle.get_active())
        self.assertFalse(window.search_toggle.get_visible())
        self.assertTrue(window.privacy_banner.get_visible())
        self.send(window, "and a follow-up")
        self.assertTrue(self.run_until(lambda: len(client.calls) >= 2
                                       and window._cancel is None))
        self.assertIs(client.calls[-1]["privacy"], session)
        self.assertFalse(client.calls[-1]["web_search"])
        self.assertFalse(window.go_private(session_for(FakeNanoGpt())),
                         "a private conversation keeps its own session")

    def test_an_answer_already_on_its_way_finishes_where_it_was_asked(self):
        client = GatedClient()
        window = self.window(client)
        self.assertTrue(self.run_until(lambda: client.calls or window._cancel is not None))
        session = session_for(FakeNanoGpt())
        window.go_private(session)
        self.assertIsNone(window.privacy, "switched mid-answer")
        client.release.set()
        self.assertTrue(self.run_until(lambda: window._cancel is None
                                       and len(window.conversation) >= 2))
        self.assertIs(window.privacy, session)

    def test_an_answer_stopped_on_its_way_is_not_badged_as_private(self):
        """Privacy Mode came while an ordinary answer streamed; Stop kept what had come,
        badged "It came only from this conversation's attested models"."""
        client = GatedClient()
        window = self.window(client)
        self.addCleanup(client.release.set)
        self.assertTrue(self.run_until(lambda: client.calls or window._cancel is not None))
        session = session_for(FakeNanoGpt())
        window.go_private(session)
        window._streaming_text = "The first words of an ordinary answer"
        window._stop()
        self.assertIs(window.privacy, session)
        stopped = window.conversation[-1]
        self.assertTrue(stopped.get("stopped"))
        self.assertIsNone(stopped.get("privacy"))

    def test_a_window_privacy_mode_cannot_serve_sends_nothing(self):
        client = RecordingClient()
        window = self.window(client)
        self.assertTrue(self.run_until(lambda: window._cancel is None
                                       and len(window.conversation) >= 2))
        window.hold_for_privacy("The private text model, gpt-4, is not a TEE model.")
        self.send(window, "a follow-up")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("not a TEE model", window.status.get_text())
        window.release_privacy_hold()
        self.send(window, "a follow-up")
        self.assertTrue(self.run_until(lambda: len(client.calls) == 2
                                       and window._cancel is None))

    def test_the_app_upgrades_every_open_window_or_holds_it(self):
        from assistant.app import Assistant
        self.config.privacy_mode = True
        first, second = self.window(RecordingClient()), self.window(RecordingClient())
        self.assertTrue(self.run_until(lambda: first._cancel is None
                                       and second._cancel is None))

        class Stub:
            _make_open_windows_private = Assistant._make_open_windows_private

            def __init__(stub):
                stub.config = self.config

        self.assertEqual(Stub()._make_open_windows_private(), (2, 0))
        self.assertIsInstance(first.privacy, PrivacySession)
        self.assertIsInstance(second.privacy, PrivacySession)

        self.config.private_text_model = "gpt-4"
        third = self.window(RecordingClient())
        self.assertTrue(self.run_until(lambda: third._cancel is None))
        self.assertEqual(Stub()._make_open_windows_private(), (0, 1))
        self.assertIsNone(third.privacy)
        self.assertTrue(third._privacy_hold)

    def test_ticked_with_the_config_unwritable_it_holds_all_the_same(self):
        """A full disk or a read-only config folder: the tray stayed ticked and new
        conversations were private, while the open ones were left as they were."""
        from unittest import mock

        from assistant.app import Assistant
        window = self.window(RecordingClient())
        self.assertTrue(self.run_until(lambda: window._cancel is None))
        told = []

        class Stub:
            _on_privacy_toggled = Assistant._on_privacy_toggled
            _make_open_windows_private = Assistant._make_open_windows_private

            def __init__(stub):
                stub.config = self.config

            def _refresh_reviews(stub):
                pass

            def notify(stub, message, urgency="low"):
                told.append(message)

        class Tick:
            def get_active(self):
                return True

        full = OSError(28, "No space left on device")
        with mock.patch.object(type(self.config), "save", side_effect=full):
            Stub()._on_privacy_toggled(Tick())
        self.assertTrue(self.config.privacy_mode)
        self.assertIsInstance(window.privacy, PrivacySession)
        self.assertIn("Not saved", told[-1])
        self.assertIn("until the app quits", told[-1])

    def test_a_conversation_with_pictures_needs_the_vision_model(self):
        window = self.window(RecordingClient(), image_base64="iVBORw0KGgo=")
        self.assertIn("vision", window.privacy_uses())
        text_only = self.window(RecordingClient())
        self.assertEqual(text_only.privacy_uses(), ("text",))


if __name__ == "__main__":
    unittest.main()
