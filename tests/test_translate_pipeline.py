"""OCR + Translate must use two models, and translation must not search by default.

Two regressions this guards:

1. OCR+Translate collapsed into a single vision-model pass. That reads nicely but is
   wrong in practice: the cheap, accurate OCR models are usually poor translators. The
   vision model transcribes and the selected text model translates.

2. Translation was inheriting the global 'web search on' default. A translation is
   performed on text already supplied, so a search only adds cost and latency.
"""

import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

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
                 web_search=None, system=None, **_kwargs):
        self.calls.append({"kind": "complete", "model": model,
                           "image": bool(image_base64), "web_search": web_search,
                           "messages": messages})
        return "TRANSCRIBED TEXT FROM IMAGE"

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None, **_kwargs):
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
        """Turn the main loop until `predicate()`, for up to `timeout` seconds -
        entering it again if something else quits it first, as a timer another test
        left can, which used to read here as done."""
        import time
        deadline = time.monotonic() + timeout

        def poll():
            if predicate() or time.monotonic() >= deadline:
                Gtk.main_quit()
            return True

        source = GLib.timeout_add(40, poll)
        try:
            while not predicate() and time.monotonic() < deadline:
                Gtk.main()
        finally:
            GLib.source_remove(source)
        return predicate()

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

    def test_half_a_character_in_the_transcription_breaks_nothing(self):
        from assistant import prompts
        # The transcription is a reply too, and JSON can spell half a character.
        half = json.loads('"broken \\ud83d emoji"')
        window = ChatWindow(StubApp(self.config, self.client), "OCR + Translate",
                            [{"role": "user", "content": "", "display": "…"}],
                            autostart=True, web_search=False,
                            preflight=lambda cancel, status: {
                                "content": prompts.translate_text(half), "display": half},
                            system=prompts.translate_system("German"))
        try:
            self.assertTrue(self._run_until(lambda: window._cancel is None
                                            and len(window.conversation) == 2))
            question = window.conversation[0]
            self.assertEqual(question["display"], "broken \ufffd emoji")
            self.assertIn("broken \ufffd emoji", question["content"])
            [translate] = self.client.calls
            self.assertIn("broken \ufffd emoji", translate["messages"][-1]["content"])
            self.assertEqual(window.conversation[-1]["content"], "die Übersetzung")
        finally:
            window.destroy()

    def test_a_page_that_would_not_load_holds_up_no_redraw_after_it(self):
        from unittest import mock
        window = self._window(autostart=False)
        try:
            if window.webview is None:
                self.skipTest("no WebKit view here")
            self.assertTrue(self._run_until(lambda: not window._page_loading))
            refused = UnicodeEncodeError("utf-8", "\ud83d", 0, 1, "surrogates not allowed")
            with mock.patch.object(window.webview, "load_html", side_effect=refused):
                with self.assertRaises(UnicodeEncodeError):
                    window._render(new_page=True)
            self.assertFalse(window._page_loading)
            window._render(new_page=True)
            self.assertTrue(window._page_loading)
            self.assertTrue(self._run_until(lambda: window._loaded))
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


class ReadsSecondTime(RecordingClient):
    """A vision model that fails its first transcription - or, `stall`, takes it until
    Stop - and makes the second. `readings` counts the transcriptions: a follow-up to a
    model that cannot see has the image described for it once as well."""

    def __init__(self, stall=False):
        super().__init__()
        self.stall = stall
        self.readings = 0
        self.reading = threading.Event()

    def complete(self, model, messages, image_base64=None, cancel=None, **kwargs):
        from assistant import prompts
        if messages[-1]["content"] != prompts.OCR:
            return super().complete(model, messages, image_base64, cancel, **kwargs)
        self.readings += 1
        if self.readings == 1:
            self.reading.set()
            if self.stall:
                cancel.wait(10)
                return None
            from assistant.llm import LLMError
            raise LLMError("HTTP 503: overloaded", model=model)
        return super().complete(model, messages, image_base64, cancel, **kwargs)


class ATranscriptionThatNeverCameIsMadeAgain(unittest.TestCase):
    """OCR + Translate's first request is two: the vision model reads the selection,
    then the answering model translates what it read. When the reading fails or is
    stopped there is nothing yet to translate, and the turn waiting for it is empty: Ask
    again sent that, and a message typed went out on its own under the translation
    instructions - "try again", translated."""

    setUp = TwoStageTranslate.setUp
    tearDown = TwoStageTranslate.tearDown
    _run_until = TwoStageTranslate._run_until
    _preflight = TwoStageTranslate._preflight

    def _window(self, client):
        from assistant import prompts
        self.client = client
        window = ChatWindow(StubApp(self.config, client), "OCR + Translate",
                            [{"role": "user", "content": "",
                              "display": "_Reading the selection…_"}],
                            image_base64="IMAGEDATA", image_first=False, autostart=True,
                            web_search=False, preflight=self._preflight(),
                            system=prompts.translate_system("German"))
        self.addCleanup(window.destroy)
        return window

    def _failed(self, window):
        self.assertTrue(self._run_until(lambda: window._cancel is None
                                        and window.conversation[-1].get("error")))

    def _translations(self):
        return [c for c in self.client.calls if c["kind"] == "stream"]

    def _settled(self, window):
        self.assertTrue(self._run_until(lambda: window._cancel is None
                                        and len(self._translations()) >= 1))

    def test_ask_again_reads_the_selection_again_then_translates_it(self):
        window = self._window(ReadsSecondTime())
        self._failed(window)
        self.assertEqual(self._translations(), [])
        window._ask_again("text")
        self._settled(window)
        [translation] = self._translations()
        users = [m for m in translation["messages"] if m["role"] == "user"]
        self.assertEqual([m["content"] for m in users], ["TRANSCRIBED TEXT FROM IMAGE"])
        self.assertFalse(translation["image"], "the translation is given the text alone")
        self.assertEqual(window.conversation[0]["display"], "translating")

    def test_a_message_typed_instead_goes_with_the_text_read_again(self):
        window = self._window(ReadsSecondTime())
        self._failed(window)
        window.entry.get_buffer().set_text("try again")
        window._send()
        self._settled(window)
        [translation] = self._translations()
        [user] = [m for m in translation["messages"] if m["role"] == "user"]
        self.assertTrue(user["content"].startswith("TRANSCRIBED TEXT FROM IMAGE"))
        self.assertIn("try again", user["content"])
        # The reading goes on the turn it was made for, not the one typed.
        self.assertEqual(window.conversation[0]["display"], "translating")
        self.assertEqual(window.conversation[-2]["content"], "try again")

    def test_so_is_one_stopped_part_way(self):
        client = ReadsSecondTime(stall=True)
        window = self._window(client)
        self.assertTrue(self._run_until(client.reading.is_set))
        window._stop()
        window._ask_again("text")
        self._settled(window)
        self.assertEqual(client.readings, 2)
        [translation] = self._translations()
        self.assertEqual(translation["messages"][-1]["content"],
                         "TRANSCRIBED TEXT FROM IMAGE")

    def test_once_made_it_is_not_made_again(self):
        client = ReadsSecondTime()
        window = self._window(client)
        self._failed(window)
        window._ask_again("text")
        self._settled(window)
        window.entry.get_buffer().set_text("why that wording?")
        window._send()
        self.assertTrue(self._run_until(lambda: window._cancel is None
                                        and len(self._translations()) >= 2))
        self.assertEqual(client.readings, 2)


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
