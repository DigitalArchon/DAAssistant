"""Where a request goes, and what is said about it, when something changed on the way.

The request waits in a review dialog while Settings change, is re-cropped, or is asked
again after its first step failed; a web search that cannot run is still ticked; a
connection breaks once the request was on it. Each of these sent something somewhere it
should not have gone, or said one thing and did another.
"""

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
from gi.repository import Gtk  # noqa: F401,E402

import requests  # noqa: E402

from assistant import app as app_module, llm, privacy as privacy_module  # noqa: E402
from assistant import providers, websearch, webkitguard  # noqa: E402
from assistant.app import Assistant  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.llm import LLMClient, LLMError  # noqa: E402
from assistant.ui import chat  # noqa: E402
from assistant.ui.dialogs import CaptureReviewDialog, TextReviewDialog  # noqa: E402

from test_chat_requests import Scripted, StubApp as ChatApp, run_until  # noqa: E402

TEXT, PREMIUM, VISION = "good/text-model", "best/premium-model", "cheap/ocr-model"


class StubApp:
    """The real review bookkeeping, without tray or chat window."""

    _start_clipboard = Assistant._start_clipboard
    query_text = Assistant.query_text
    _watch_review = Assistant._watch_review
    _offer_lookups = Assistant._offer_lookups
    look_up_selection = Assistant.look_up_selection
    _refresh_reviews = Assistant._refresh_reviews
    _confirm_privacy = Assistant._confirm_privacy
    _start_privacy = Assistant._start_privacy
    _image_answerer = Assistant._image_answerer
    _answer_role = Assistant._answer_role
    _catalogue_providers = Assistant._catalogue_providers
    _settings_changed_in_chats = Assistant._settings_changed_in_chats

    def __init__(self, config):
        self.config = config
        self._reviews = []
        self.opened = []

    def _require_api_key(self):
        return True

    def _open_chat(self, title, prompt, display, **kwargs):
        self.opened.append(kwargs)

    def notify(self, message, urgency="low"):
        pass


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        self.config.text_model, self.config.premium_text_model = TEXT, PREMIUM
        self.config.vision_model = VISION
        self.app = StubApp(self.config)
        patch = mock.patch.object(app_module.clipboard, "get_text",
                                  return_value=("Guten Morgen", "text"))
        patch.start()
        self.addCleanup(patch.stop)

    def open(self, operation="query_text"):
        made = []

        def make(*args, **kwargs):
            dialog = TextReviewDialog(*args, **kwargs)
            made.append(dialog)
            return dialog

        with mock.patch.object(app_module, "TextReviewDialog", side_effect=make):
            getattr(self.app, operation)()
        [dialog] = made
        self.addCleanup(dialog.destroy)
        return dialog


class ASessionTheSettingsMovedOnFrom(Case):
    """A review dialog's private session is made when its operation starts. Strict
    switched on, or a private model changed, before Send is what it must go with:
    nothing has been sent yet."""

    def test_strict_switched_on_while_it_waits_is_what_it_is_sent_with(self):
        self.config.privacy_mode = True
        dialog = self.open()
        first = dialog.route_privacy
        self.assertFalse(first.strict)
        self.config.privacy_strict = True
        self.app._refresh_reviews()
        self.assertIsNot(dialog.route_privacy, first)
        self.assertTrue(dialog.route_privacy.strict)
        dialog.prompt_view.get_buffer().set_text("What does it say?")
        dialog._confirm()
        [opened] = self.app.opened
        self.assertTrue(opened["privacy"].strict)

    def test_at_send_a_session_the_settings_left_behind_is_made_again(self):
        self.config.privacy_mode = True
        old = privacy_module.PrivacySession.start(self.config, uses=("text",))
        self.config.privacy_strict = True
        fresh = self.app._confirm_privacy(old, ("text",))
        self.assertIsNot(fresh, old)
        self.assertTrue(fresh.strict)
        self.assertIs(self.app._confirm_privacy(fresh, ("text",)), fresh)

    def test_switched_off_since_it_stays_private_as_it_was(self):
        self.config.privacy_mode = True
        old = privacy_module.PrivacySession.start(self.config, uses=("text",))
        self.config.privacy_mode = False
        self.config.privacy_strict = True
        self.assertIs(self.app._confirm_privacy(old, ("text",)), old)


class ADialogMadeAgain(Case):
    def test_it_says_at_once_where_it_goes_now(self):
        """A dialog made by Re-crop is given the route as it was when the crop began;
        Privacy Mode switched on meanwhile is said before Send, not only at it."""
        dialog = TextReviewDialog("text", "Query text", lambda *a: None, ask_prompt=True,
                                  config=self.config, plan=[(TEXT, "answers")])
        self.addCleanup(dialog.destroy)
        self.config.privacy_mode = True
        route = {"privacy": None}
        self.app._watch_review(dialog, route, lambda: ("text",),
                               lambda privacy: [(privacy.text_model if privacy
                                                 else TEXT, "answers")])
        self.assertIsNotNone(route["privacy"])
        self.assertEqual(dialog.send.get_label(), "_Send privately")

    def test_the_privacy_note_names_the_models_the_plan_does(self):
        self.config.privacy_mode = True
        dialog = self.open()
        note = dialog.privacy_slot.get_children()[0].get_children()[1].get_text()
        self.assertIn(self.config.private_text_model, note)
        self.config.use_premium = True
        self.app._refresh_reviews()
        note = dialog.privacy_slot.get_children()[0].get_children()[1].get_text()
        self.assertIn(self.config.private_premium_model, note)
        self.assertNotIn(self.config.private_text_model + " (", note)


class AReCrop(Case):
    """Re-crop destroyed the review and made a new one from the operation's defaults:
    the question typed, the search ticked and the language chosen were lost, and a
    crop cancelled dropped the capture without a word."""

    class Stub:
        _review_capture = Assistant._review_capture
        _capture_plan = Assistant._capture_plan
        _capture_uses = Assistant._capture_uses
        _image_answerer = Assistant._image_answerer
        _answer_role = Assistant._answer_role
        _watch_review = Assistant._watch_review
        _offer_lookups = Assistant._offer_lookups
        look_up_selection = Assistant.look_up_selection
        _confirm_privacy = Assistant._confirm_privacy
        _start_privacy = Assistant._start_privacy

        def __init__(self, config):
            self.config, self._reviews, self.notices = config, [], []

        def notify(self, message, urgency="low"):
            self.notices.append(message)

    def setUp(self):
        super().setUp()
        from PIL import Image
        self.stub = self.Stub(self.config)
        self.image = Image.new("RGB", (120, 80), (255, 255, 255))
        self.dialogs, self.crops = [], []

        def review(*args, **kwargs):
            dialog = CaptureReviewDialog(*args, **kwargs)
            self.dialogs.append(dialog)
            self.addCleanup(lambda: dialog.get_realized() and dialog.destroy())
            return dialog

        def crop(image, on_done, on_error, title=None):
            self.crops.append((image, on_done, on_error))

        for patch in (mock.patch.object(app_module, "CaptureReviewDialog",
                                        side_effect=review),
                      mock.patch("assistant.capture.CropDialog", side_effect=crop)):
            patch.start()
            self.addCleanup(patch.stop)

    def review(self):
        self.stub._review_capture(self.image, "Query image", True,
                                  lambda prompt, extracted=None: (prompt, prompt),
                                  lambda _m: None, answer_by_text=True)
        return self.dialogs[-1]

    def test_the_question_and_the_search_come_back_with_the_new_crop(self):
        dialog = self.review()
        dialog.prompt_view.get_buffer().set_text("what is this error?")
        dialog.search_check.set_active(True)
        dialog._on_recrop(None)
        [(_image, on_done, _on_error)] = self.crops
        on_done(self.image.crop((0, 0, 60, 40)))
        again = self.dialogs[-1]
        self.assertIsNot(again, dialog)
        self.assertEqual(again._prompt_text(), "what is this error?")
        self.assertTrue(again.search_check.get_active())

    def test_a_crop_cancelled_goes_back_to_the_review(self):
        from assistant.capture import CANCELLED
        dialog = self.review()
        dialog.prompt_view.get_buffer().set_text("keep me")
        dialog._on_recrop(None)
        [(image, _on_done, on_error)] = self.crops
        on_error(CANCELLED)
        again = self.dialogs[-1]
        self.assertIsNot(again, dialog)
        self.assertIs(again.image, image)
        self.assertEqual(again._prompt_text(), "keep me")


class ASearchThatCannotRun(Case):
    """Switched off in Settings, or with no NanoGPT key, a search is never made: the
    box must not be ticked, planned, waited for or read for."""

    def test_switched_off_in_settings_the_box_is_greyed_and_says_why(self):
        self.config.web_search_mode = "off"
        dialog = self.open()
        self.assertFalse(dialog.search_check.get_sensitive())
        self.assertFalse(dialog.search_check.get_active())
        self.assertIn("Settings > Web search", dialog.search_check.get_tooltip_text())
        self.assertNotIn("Web search", dialog.plan.get_text())

    def test_without_a_nanogpt_key_the_same(self):
        self.config.api_key = ""
        self.assertIn("API key", websearch.unavailable(self.config))
        dialog = self.open()
        self.assertFalse(dialog.search_check.get_sensitive())

    def test_ocr_offers_no_search_for_its_transcription(self):
        from PIL import Image
        dialog = CaptureReviewDialog(Image.new("RGB", (60, 30)), "OCR", self.config,
                                     lambda *a: None, offer_search=False,
                                     web_search_default=True)
        self.addCleanup(dialog.destroy)
        self.assertFalse(dialog.search_check.get_visible())
        self.assertFalse(dialog.search_check.get_active())

    def test_the_query_image_plan_reads_for_no_search_that_cannot_run(self):
        self.config.web_search_mode = "off"
        from PIL import Image
        plans = []
        dialog = CaptureReviewDialog(
            Image.new("RGB", (60, 30)), "Query image", self.config, lambda *a: None,
            ask_prompt=True, web_search_default=True,
            plan=lambda web_search: plans.append(web_search) or [(TEXT, "answers")])
        self.addCleanup(dialog.destroy)
        self.assertEqual(set(plans), {False})


class PrivacyModeAsksOnlyNanoGPTForItsList(Case):
    def test_only_nanogpt_s_catalogue_is_fetched_in_privacy_mode(self):
        self.config.text_provider = providers.OPENROUTER
        self.config.openrouter_api_key = "or-key"
        self.assertIn(providers.OPENROUTER,
                      [p.id for p in self.app._catalogue_providers()])
        self.config.privacy_mode = True
        self.assertEqual([p.id for p in self.app._catalogue_providers()],
                         [providers.NANOGPT])


class AConnectionThatBrokeAfterSending(unittest.TestCase):
    """Read timeouts were never sent again - the same completion could be billed twice
    - but a connection the server dropped once the request was on it was, three times."""

    def test_which_failures_may_have_run(self):
        import http.client
        import urllib3
        aborted = requests.exceptions.ConnectionError(urllib3.exceptions.ProtocolError(
            "Connection aborted.", http.client.RemoteDisconnected("closed")))
        refused = requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(
            None, "/", urllib3.exceptions.NewConnectionError(None, "refused")))
        self.assertTrue(llm._may_have_run(aborted))
        self.assertTrue(llm._may_have_run(
            requests.exceptions.ChunkedEncodingError("Connection broken")))
        self.assertFalse(llm._may_have_run(refused))
        self.assertFalse(llm._may_have_run(requests.exceptions.SSLError("handshake")))
        self.assertFalse(llm._may_have_run(requests.exceptions.ConnectTimeout("slow")))

    def test_it_is_not_sent_again(self):
        import http.client
        import urllib3
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        config = Config(os.path.join(directory, "c.json"))
        config.api_key = "k"
        client = LLMClient(config)
        aborted = requests.exceptions.ConnectionError(urllib3.exceptions.ProtocolError(
            "Connection aborted.", http.client.RemoteDisconnected("closed")))
        with mock.patch.object(client.session, "post", side_effect=aborted) as post:
            with self.assertRaises(LLMError) as caught:
                client._post({"model": "some/model", "messages": []}, False)
        self.assertEqual(post.call_count, 1)
        self.assertIn("not sent again", str(caught.exception))

    def test_a_refused_connection_is_still_tried_again(self):
        import urllib3
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        config = Config(os.path.join(directory, "c.json"))
        config.api_key = "k"
        client = LLMClient(config)
        refused = requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(
            None, "/", urllib3.exceptions.NewConnectionError(None, "refused")))
        with mock.patch.object(client.session, "post", side_effect=refused) as post, \
                mock.patch.object(llm.time, "sleep"):
            with self.assertRaises(LLMError):
                client._post({"model": "some/model", "messages": []}, False)
        self.assertEqual(post.call_count, llm.MAX_RETRIES)


class NothingGoesBeforeAnAnswerThatCannot(unittest.TestCase):
    def test_no_search_for_a_model_whose_provider_has_no_key(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        config = Config(os.path.join(directory, "c.json"))
        config.api_key = "k"
        config.web_search_enabled = True
        config.text_provider = providers.OPENROUTER         # and no OpenRouter key
        client = LLMClient(config)
        with mock.patch.object(client, "search") as search:
            with self.assertRaises(LLMError) as caught:
                client.stream(config.model("text"),
                              [{"role": "user", "content": "hello"}], web_search=True)
        search.assert_not_called()
        self.assertTrue(str(caught.exception).startswith("Nothing was sent."))


class TheChatWindowAfterSettings(unittest.TestCase):
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
        self.config.api_key = "k"
        self.config.text_model = TEXT

    def window(self, client, turns, **kwargs):
        window = chat.ChatWindow(ChatApp(self.config, client), "Ask anything",
                                 [dict(t) for t in turns], **kwargs)
        self.addCleanup(lambda: window._destroyed or window.destroy())
        return window

    def test_the_header_names_the_model_the_settings_now_give(self):
        window = self.window(Scripted(), [])
        self.assertEqual(window.header.get_subtitle(), TEXT)
        self.config.text_model = "other/text-model"
        window.settings_changed()
        self.assertEqual(window.header.get_subtitle(), "other/text-model")

    def test_a_search_switched_off_is_not_offered_or_made(self):
        window = self.window(Scripted(), [], web_search=True)
        self.assertTrue(window._search_now())
        self.config.web_search_mode = "off"
        window.settings_changed()
        self.assertFalse(window.search_toggle.get_sensitive())
        self.assertFalse(window._search_now())

    def test_a_first_step_that_failed_leaves_the_question_as_asked(self):
        def preflight(cancel, status, **_request):
            raise LLMError("HTTP 503: overloaded", model=VISION)

        window = self.window(Scripted(), [{
            "role": "user", "content": "what is this?",
            "display": "what is this?\n\n_Reading the image with x…_",
            "plain_display": "what is this?"}], autostart=True, preflight=preflight,
            image_base64="IMAGEDATA", image_first=False)
        self.assertTrue(run_until(lambda: window._cancel is None
                                  and window.conversation[-1].get("error")))
        self.assertEqual(window.conversation[0]["display"], "what is this?")
        self.assertIsNotNone(window._preflight, "made again with the next request")

    def test_a_first_step_is_given_this_request_s_privacy_and_search(self):
        seen = []

        def preflight(cancel, status, **request):
            seen.append(request)
            return None

        window = self.window(Scripted("Fine."), [{"role": "user", "content": "q"}],
                             autostart=True, preflight=preflight, web_search=False)
        self.assertTrue(run_until(lambda: window._cancel is None and seen))
        self.assertEqual(seen, [{"privacy": None, "web_search": False}])


    def test_a_private_reading_whose_signature_failed_is_said_on_the_question(self):
        class Receipt:
            state, detail = privacy_module.MISMATCH, "signed by another key"

        class Session:
            def pop_receipt(self):
                return Receipt()

        window = self.window(Scripted(), [{"role": "user", "content": "q"}])
        window._note_reading_receipt(Session(), "Image reading")
        self.assertTrue(run_until(lambda: window.conversation[0].get("privacy")))
        badge = window.conversation[0]["privacy"]
        self.assertEqual(badge["tone"], "bad")
        self.assertIn("Image reading", badge["label"])


class TestConnectionTriesTheAnsweringModel(unittest.TestCase):
    def test_with_premium_on_it_is_the_private_premium_model(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        config = Config(os.path.join(directory, "c.json"))
        config.api_key = "k"
        config.privacy_mode = True
        config.use_premium = True
        client = LLMClient(config)
        asked = []
        with mock.patch.object(client, "complete",
                               side_effect=lambda model, *a, **k: asked.append(model)
                               or "ok"):
            client.test_connection()
        self.assertEqual(asked, [config.private_premium_model])


if __name__ == "__main__":
    unittest.main(verbosity=2)
