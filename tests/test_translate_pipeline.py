"""OCR + Translate must use two models, and translation must not search by default.

Two regressions this guards:

1. OCR+Translate collapsed into a single vision-model pass. That reads nicely but is
   wrong in practice: the cheap, accurate OCR models are usually poor translators. The
   vision model transcribes and the selected text model translates.

2. Translation was inheriting the global 'web search on' default. A translation is
   performed on text already supplied, so a search only adds cost and latency.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib, Gtk
from PIL import Image

from assistant.config import Config
from assistant.ui.chat import ChatWindow
from assistant.ui.dialogs import CaptureReviewDialog, TextReviewDialog


class RecordingClient:
    """Records every model actually used, for both complete() and stream()."""

    def __init__(self):
        self.calls = []

    def complete(self, model, messages, image_base64=None, cancel=None,
                 web_search=None, system=None):
        self.calls.append({"kind": "complete", "model": model,
                           "image": bool(image_base64), "web_search": web_search,
                           "messages": messages})
        return "TRANSCRIBED TEXT FROM IMAGE"

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None):
        self.calls.append({"kind": "stream", "model": model,
                           "image": bool(image_base64), "web_search": web_search,
                           "system": system,
                           "messages": [dict(m) for m in messages]})
        return "die Übersetzung"


class StubApp:
    def __init__(self, config, client):
        self.config = config
        self.client = client

    def notify(self, message, urgency="low"):
        pass


class TwoStageTranslate(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        self.config.vision_model = "cheap/ocr-model"
        self.config.text_model = "good/text-model"
        self.config.premium_text_model = "premium/text-model"
        self.client = RecordingClient()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_until(self, predicate, timeout=20):
        expired = []
        source = GLib.timeout_add_seconds(
            timeout, lambda: (expired.append(True), Gtk.main_quit(), False)[-1])

        def poll():
            if predicate():
                Gtk.main_quit()
                return False
            return True

        GLib.timeout_add(80, poll)
        Gtk.main()
        if not expired:
            GLib.source_remove(source)
        return not expired

    def _preflight(self, image_b64="IMAGEDATA"):
        """Mirrors Assistant._transcribe_preflight without building a full tray app."""
        from assistant import prompts

        def preflight(cancel, status):
            status("Reading text from the image…")
            extracted = self.client.complete(
                self.config.vision_model,
                [{"role": "user", "content": prompts.OCR}],
                image_base64=image_b64, cancel=cancel)
            return {"content": prompts.translate_text(extracted),
                    "display": "translating"}

        return preflight

    def _window(self, autostart=True):
        """The window as app.py builds it: how to translate is a system prompt."""
        from assistant import prompts
        return ChatWindow(StubApp(self.config, self.client), "OCR + Translate",
                          [{"role": "user", "content": "", "display": "…"}],
                          autostart=autostart, web_search=False,
                          preflight=self._preflight(),
                          system=prompts.translate_system("German"))

    def test_transcription_uses_vision_model_and_translation_uses_text_model(self):
        window = self._window()
        try:
            self.assertTrue(self._run_until(lambda: len(self.client.calls) >= 2))
            ocr, translate = self.client.calls[0], self.client.calls[1]

            self.assertEqual(ocr["kind"], "complete")
            self.assertEqual(ocr["model"], "cheap/ocr-model")
            self.assertTrue(ocr["image"], "the OCR pass must receive the image")

            self.assertEqual(translate["kind"], "stream")
            self.assertEqual(translate["model"], "good/text-model",
                             "translation must not run on the OCR model")
            self.assertFalse(translate["image"],
                             "the image is consumed by the OCR pass")
        finally:
            window.destroy()

    def test_transcribed_text_is_what_gets_translated(self):
        window = self._window()
        try:
            self.assertTrue(self._run_until(lambda: len(self.client.calls) >= 2))
            translate = self.client.calls[1]
            sent = translate["messages"][-1]["content"]
            self.assertIn("TRANSCRIBED TEXT FROM IMAGE", sent)
            # The user's turn is the text and nothing else; how to translate it, and
            # into what, is the system prompt. That split is what keeps a web search
            # from searching for our own instructions.
            self.assertNotIn("German", sent)
            self.assertIn("German", translate["system"])
        finally:
            window.destroy()

    def test_the_model_menu_still_chooses_the_translation_model(self):
        window = self._window(autostart=False)
        try:
            self.assertTrue(window._choose("premium"))
            window._start_stream()
            self.assertTrue(self._run_until(lambda: len(self.client.calls) >= 2))
            self.assertEqual(self.client.calls[1]["model"], "premium/text-model")
        finally:
            window.destroy()

    def test_followups_stay_on_the_text_model(self):
        window = self._window()
        try:
            self.assertTrue(self._run_until(lambda: len(self.client.calls) >= 2))
            # _send() is a no-op while a stream is in flight, and the call being
            # recorded is not the same moment as _on_stream_done reaching the main
            # loop. Without this wait the follow-up is dropped about half the time.
            self.assertTrue(self._run_until(lambda: window._cancel is None))
            window.entry.get_buffer().set_text("why that wording?")
            window._send()
            self.assertTrue(self._run_until(lambda: len(self.client.calls) >= 3))
            follow = self.client.calls[2]
            self.assertEqual(follow["model"], "good/text-model")
            self.assertFalse(follow["image"])
            # The preflight must not run a second time.
            self.assertEqual(sum(1 for c in self.client.calls
                                 if c["kind"] == "complete"), 1)
        finally:
            window.destroy()


class StallingClient(RecordingClient):
    """A provider that has accepted the request and then gone quiet."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def stream(self, *args, **kwargs):
        super().stream(*args, **kwargs)
        self.release.wait(20)
        return "an answer nobody is waiting for any more"


class ChatWindowControls(unittest.TestCase):
    """The toggles and the Stop button, on a plain text conversation."""

    # The fixtures, without inheriting the tests and running them twice.
    setUp = TwoStageTranslate.setUp
    tearDown = TwoStageTranslate.tearDown
    _run_until = TwoStageTranslate._run_until

    def _plain_window(self, client=None):
        return ChatWindow(StubApp(self.config, client or self.client), "Query text",
                          [{"role": "user", "content": "a question"}],
                          autostart=False, web_search=False)

    def _settle(self, milliseconds=300):
        fired = []

        def done():
            fired.append(True)
            Gtk.main_quit()
            return False

        source = GLib.timeout_add(milliseconds, done)
        Gtk.main()
        if not fired:                   # something else ended the loop first
            GLib.source_remove(source)

    def test_choosing_the_text_model_in_the_window_beats_the_saved_default(self):
        # It used to fall back to active_text_model(), which is the premium model
        # whenever the saved default is on - so the window could only ever say yes.
        self.config.use_premium = True
        window = self._plain_window()
        try:
            self.assertEqual(window.current_model, "premium/text-model")
            self.assertTrue(window._choose("text"))
            window._start_stream()
            self.assertTrue(self._run_until(lambda: len(self.client.calls) >= 1))
            self.assertEqual(self.client.calls[0]["model"], "good/text-model")
        finally:
            window.destroy()

    def test_the_header_follows_the_model_menu(self):
        # The subtitle was set once at construction, so changing model in the window
        # looked like it had not taken.
        self.config.use_premium = True
        window = self._plain_window()
        try:
            self.assertEqual(window.header.get_subtitle(), "premium/text-model")
            self.assertEqual(window._model_label.get_text(), "Premium")
            window._choose("text")
            self.assertEqual(window.header.get_subtitle(), "good/text-model")
            self.assertEqual(window._model_label.get_text(), "Text")
        finally:
            window.destroy()

    def test_the_model_menu_is_offered_in_every_conversation(self):
        # Image conversations included: the capture goes to whichever model is chosen,
        # or a reading of it to one that cannot see.
        text = self._plain_window()
        image = ChatWindow(StubApp(self.config, self.client), "Query image",
                           [{"role": "user", "content": "q"}], image_base64="AAAA",
                           autostart=False, web_search=False)
        try:
            self.assertIsNotNone(text.model_button.get_parent())
            self.assertIsNotNone(image.model_button.get_parent())
        finally:
            text.destroy()
            image.destroy()

    def test_stop_gives_the_window_back_without_waiting_for_the_network(self):
        client = StallingClient()
        window = self._plain_window(client)
        try:
            window._start_stream()
            self.assertTrue(self._run_until(lambda: len(client.calls) >= 1))
            window._stop()
            self.assertIsNone(window._cancel)
            self.assertTrue(window.send_button.get_sensitive())

            # The worker comes back afterwards, to a request that is already over.
            client.release.set()
            self._settle()
            self.assertEqual([t["role"] for t in window.conversation], ["user"])
        finally:
            client.release.set()
            window.destroy()

    def test_a_late_answer_does_not_end_the_request_that_replaced_it(self):
        client = StallingClient()
        window = self._plain_window(client)
        try:
            window._start_stream()
            self.assertTrue(self._run_until(lambda: len(client.calls) >= 1))
            first = window._cancel
            window._stop()
            window.entry.get_buffer().set_text("a different question")
            window._send()
            second = window._cancel
            self.assertIsNot(first, second)

            window._on_stream_done("late", False, first)
            self.assertIs(window._cancel, second, "the new request was ended early")
            self.assertNotIn("late", [t["content"] for t in window.conversation])
        finally:
            client.release.set()
            window.destroy()

    def test_closing_the_window_mid_request_leaves_nothing_to_call_back_into(self):
        client = StallingClient()
        window = self._plain_window(client)
        window._start_stream()
        self.assertTrue(self._run_until(lambda: len(client.calls) >= 1))
        cancel = window._cancel
        window.destroy()
        self.assertTrue(cancel.is_set())
        client.release.set()
        self._settle()
        self.assertEqual([t["role"] for t in window.conversation], ["user"])

    def test_an_error_is_shown_but_not_sent_back_as_something_the_model_said(self):
        window = self._plain_window()
        try:
            window._show_error("HTTP 503: overloaded")
            window.entry.get_buffer().set_text("try again")
            window._send()
            self.assertTrue(self._run_until(lambda: len(self.client.calls) >= 1))
            sent = self.client.calls[0]["messages"]
            self.assertEqual([m["role"] for m in sent], ["user"])
            self.assertIn("a question", sent[0]["content"])
            self.assertIn("try again", sent[0]["content"])
            self.assertNotIn("503", str(sent))
        finally:
            window.destroy()


class TranslateDefaultsToNoSearch(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.web_search_enabled = True       # global default is ON

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_text_dialog_override_beats_the_global_default(self):
        dialog = TextReviewDialog("etwas Text", "Translate text", lambda *a: None,
                                  ask_prompt=False, config=self.config,
                                  web_search_default=False)
        try:
            self.assertFalse(dialog.search_check.get_active())
        finally:
            dialog.destroy()

    def test_text_dialog_without_override_follows_the_global_default(self):
        dialog = TextReviewDialog("etwas Text", "Query text", lambda *a: None,
                                  ask_prompt=True, config=self.config)
        try:
            self.assertTrue(dialog.search_check.get_active())
        finally:
            dialog.destroy()

    def test_capture_dialog_override_beats_the_global_default(self):
        dialog = CaptureReviewDialog(Image.new("RGB", (40, 20)), "OCR + Translate",
                                     self.config, lambda *a: None,
                                     web_search_default=False)
        try:
            self.assertFalse(dialog.search_check.get_active())
        finally:
            dialog.destroy()

    def test_user_can_still_turn_search_on_for_a_translation(self):
        result = {}
        dialog = TextReviewDialog(
            "etwas Text", "Translate text",
            lambda text, prompt, web_search: result.update(web_search=web_search),
            ask_prompt=False, config=self.config, web_search_default=False)
        dialog.search_check.set_active(True)
        dialog._confirm()
        self.assertTrue(result["web_search"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
