"""Who answers a question about a captured image.

The selected text model answers - the premium one when the star is on by default, the
private text model in Privacy Mode. What it is given depends on whether it can see:

  - a model that accepts images gets the image itself, with the vision model's literal
    transcription beside the question, and the conversation stays with it, image
    attached;
  - one that cannot (or that the catalogue does not list) gets the vision model's
    reading instead - the text verbatim and a description of the rest - and the
    conversation is a text one from then on.

`vision_model` stays the cheap, literal reader: OCR, stage one of Translate image
selection, and the reading above. Nothing in the UI would show the plumbing going wrong
- some model still answers, just worse - hence these.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib, Gtk
from PIL import Image

from assistant import app as app_module, config as config_module, prompts
from assistant.app import Assistant
from assistant.config import Config
from assistant.ui.chat import ChatWindow
from assistant.ui.dialogs import SettingsDialog

OCR_MODEL = "cheap/ocr-model"
TEXT_MODEL = "good/text-model"
PREMIUM_MODEL = "premium/text-model"
QUERY_MODEL = "expensive/thinking-vision-model"
TRANSCRIPTION = "君の瞳に映る星"     # what the OCR pass reads out of the capture


class RecordingClient:
    def __init__(self, transcription=TRANSCRIPTION):
        self.calls = []
        self.transcription = transcription

    def complete(self, model, messages, image_base64=None, cancel=None,
                 web_search=None, system=None):
        self.calls.append({"kind": "complete", "model": model,
                           "image": bool(image_base64),
                           "prompt": messages[-1]["content"]})
        return self.transcription

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None):
        self.calls.append({"kind": "stream", "model": model,
                           "image": bool(image_base64),
                           "messages": [dict(m) for m in messages]})
        return "It is a permissions error."


class CaptureDialogStub:
    """Stands in for CaptureReviewDialog and confirms immediately."""

    prompt = "what does this error mean?"
    web_search = False

    plans = []

    def __init__(self, image, title, config, on_confirm, ask_prompt=False,
                 on_recrop=None, web_search_default=None, privacy=None, plan=None):
        CaptureDialogStub.plans.append(plan)
        on_confirm(image, "IMAGEDATA", {}, self.prompt, self.web_search)

    def connect(self, _signal, _handler):
        pass

    def set_route(self, privacy, plan, refused=None):
        CaptureDialogStub.plans.append(plan)


class StubAssistant:
    """The real methods under test, without the tray, capture stack or dialogs."""

    query_image = Assistant.query_image
    ocr_only = Assistant.ocr_only
    ocr_translate = Assistant.ocr_translate
    _review_capture = Assistant._review_capture
    _read_preflight = Assistant._read_preflight
    _describe_preflight = Assistant._describe_preflight
    _image_answer = Assistant._image_answer
    _image_answerer = Assistant._image_answerer
    _answer_role = Assistant._answer_role
    _ocr = Assistant._ocr
    _capture_plan = Assistant._capture_plan
    _watch_review = Assistant._watch_review
    _transcribe_preflight = Assistant._transcribe_preflight
    _start_privacy = Assistant._start_privacy
    _confirm_privacy = Assistant._confirm_privacy

    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.started = None
        self.opened = None
        self._reviews = []

    def _start_capture(self, title, ask_prompt, build, web_search_default=None,
                       transcribe_first=False, system=None, answer_by_text=False):
        self.started = {"title": title, "ask_prompt": ask_prompt, "build": build,
                        "web_search_default": web_search_default,
                        "transcribe_first": transcribe_first,
                        "system": system, "answer_by_text": answer_by_text}

    def _open_chat(self, title, prompt, display, image_base64=None, first_model=None,
                   image_first=True, web_search=None, preflight=None, system=None,
                   privacy=None, screenshot=None):
        self.opened = {"title": title, "prompt": prompt, "display": display,
                       "image": image_base64, "first_model": first_model,
                       "image_first": image_first, "preflight": preflight,
                       "system": system, "privacy": privacy,
                       "screenshot": screenshot}

    def notify(self, message, urgency="low"):
        pass


class ImageQueryCase(unittest.TestCase):
    """A config, a recording client, and a capture confirmed through the real path,
    with the catalogue's answer about images decided by the test."""

    sees_images = True

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        self.config.vision_model = OCR_MODEL
        self.config.text_model = TEXT_MODEL
        self.config.premium_text_model = PREMIUM_MODEL
        self.config.use_premium = False
        self.client = RecordingClient()
        self.app = StubAssistant(self.config, self.client)
        self.asked = []
        self._accepts = app_module.catalogue.accepts_images
        app_module.catalogue.accepts_images = (
            lambda _config, model: self.asked.append(model) or self.sees_images)

    def tearDown(self):
        import shutil
        app_module.catalogue.accepts_images = self._accepts
        shutil.rmtree(self.dir, ignore_errors=True)

    def _confirm(self, web_search=False):
        self.app.query_image()
        started = self.app.started
        original = app_module.CaptureReviewDialog
        app_module.CaptureReviewDialog = type("Stub", (CaptureDialogStub,),
                                              {"web_search": web_search})
        try:
            self.app._review_capture(
                Image.new("RGB", (40, 20)), started["title"], started["ask_prompt"],
                started["build"], lambda _m: None, started["web_search_default"],
                started["transcribe_first"], started["system"],
                answer_by_text=started["answer_by_text"])
        finally:
            app_module.CaptureReviewDialog = original
        return self.app.opened


class TheReviewSaysWhichModels(ImageQueryCase):
    """The capture is on screen with Send under it: which models it is about to go to,
    and for what, is said there - not only afterwards, in the chat window."""

    def plan(self, operation):
        getattr(self.app, operation)()
        started = self.app.started
        original = app_module.CaptureReviewDialog
        app_module.CaptureReviewDialog = CaptureDialogStub
        try:
            self.app._review_capture(
                Image.new("RGB", (40, 20)), started["title"], started["ask_prompt"],
                started["build"], lambda _m: None, started["web_search_default"],
                started["transcribe_first"], started["system"],
                answer_by_text=started["answer_by_text"])
        finally:
            app_module.CaptureReviewDialog = original
        return [(str(model), job) for model, job in CaptureDialogStub.plans[-1]]

    def test_ocr_is_the_vision_model_alone(self):
        self.assertEqual(self.plan("ocr_only"), [(OCR_MODEL, "transcribes the text")])

    def test_translation_is_the_vision_model_then_the_answering_one(self):
        self.assertEqual(self.plan("ocr_translate"),
                         [(OCR_MODEL, "reads the text"), (TEXT_MODEL, "translates it")])
        self.config.use_premium = True
        self.assertEqual(self.plan("ocr_translate")[1], (PREMIUM_MODEL, "translates it"))

    def test_a_question_says_whether_the_answering_model_sees_the_image(self):
        self.sees_images = True
        self.assertEqual(self.plan("query_image"),
                         [(OCR_MODEL, "reads the text"),
                          (TEXT_MODEL, "answers, seeing the image")])
        self.sees_images = False
        self.assertEqual(self.plan("query_image"),
                         [(OCR_MODEL, "reads and describes the image"),
                          (TEXT_MODEL, "answers from that reading")])

    def test_the_models_carry_their_providers(self):
        self.config.vision_provider = "ollama"
        getattr(self.app, "ocr_only")()
        self.plan("ocr_only")
        [(model, _job)] = CaptureDialogStub.plans[-1]
        self.assertEqual(model.provider, "ollama")


class TheTextModelAnswers(ImageQueryCase):
    def test_an_image_query_is_answered_by_the_text_model(self):
        self.app.query_image()
        self.assertTrue(self.app.started["answer_by_text"])
        self.assertTrue(self.app.started["ask_prompt"])
        opened = self._confirm()
        self.assertEqual(set(self.asked), {TEXT_MODEL})
        # No first model of its own: the window's model menu starts on the answerer.
        self.assertIsNone(opened["first_model"])

    def test_with_the_premium_option_on_the_premium_model_answers(self):
        self.config.use_premium = True
        self._confirm()
        self.assertEqual(set(self.asked), {PREMIUM_MODEL})

    def test_ocrs_first_answer_is_the_vision_models_transcription(self):
        self.app.ocr_only()
        self.assertFalse(self.app.started["answer_by_text"])
        self.assertFalse(self.app.started["transcribe_first"])
        started = self.app.started
        original = app_module.CaptureReviewDialog
        app_module.CaptureReviewDialog = CaptureDialogStub
        try:
            self.app._review_capture(Image.new("RGB", (40, 20)), started["title"],
                                     started["ask_prompt"], started["build"],
                                     lambda _m: None, started["web_search_default"],
                                     started["transcribe_first"], started["system"])
        finally:
            app_module.CaptureReviewDialog = original
        opened = self.app.opened
        self.assertEqual(opened["first_model"], OCR_MODEL)
        self.assertEqual(opened["prompt"], prompts.OCR)
        self.assertEqual(opened["image"], "IMAGEDATA")
        self.assertTrue(opened["image_first"])

    def test_translation_still_reads_with_the_ocr_model_then_hands_over(self):
        self.app.ocr_translate()
        started = self.app.started
        self.assertTrue(started["transcribe_first"])

    def test_there_is_no_separate_image_query_model_any_more(self):
        fields = [field[0] for field in SettingsDialog.MODEL_FIELDS]
        self.assertNotIn("image_query_model", fields)
        self.assertNotIn("image_query_model", config_module.DEFAULTS)


class ATextModelThatSeesImages(ImageQueryCase):
    sees_images = True

    def test_it_is_given_the_image_itself_and_keeps_it(self):
        opened = self._confirm()
        self.assertTrue(opened["image_first"])
        self.assertEqual(opened["image"], "IMAGEDATA",
                         "the image itself must be sent, not a reading of it")
        self.assertIsNone(opened["first_model"])
        self.assertEqual(opened["system"],
                         prompts.query_image_system(self.config.default_language))
        self.assertIn(CaptureDialogStub.prompt, opened["prompt"])

    def test_the_cheap_model_reads_the_text_beside_the_question(self):
        """A literal reading keeps an error code exact, and gives a search words the
        image only holds in its pixels. One cheap call, whether searching or not."""
        for web_search in (True, False):
            self.client.calls.clear()
            opened = self._confirm(web_search=web_search)
            update = opened["preflight"](threading.Event(), lambda _m: None)
            self.assertIn(TRANSCRIPTION, update["content"])
            self.assertIn(CaptureDialogStub.prompt, update["content"])
            self.assertEqual(update["display"], CaptureDialogStub.prompt)
            passes = [c for c in self.client.calls if c["kind"] == "complete"]
            self.assertEqual([p["model"] for p in passes], [OCR_MODEL])
            self.assertEqual(passes[0]["prompt"], prompts.OCR)
            self.assertTrue(passes[0]["image"])

    def test_an_image_with_no_text_is_sent_as_it_was(self):
        self.client.transcription = "NO TEXT FOUND"
        opened = self._confirm(web_search=True)
        self.assertIsNone(opened["preflight"](threading.Event(), lambda _m: None),
                          "an unreadable image should not rewrite the question")

    def test_the_model_gets_the_whole_reading_and_the_search_gets_an_excerpt(self):
        """Opposite needs. A page of dense text is exactly what the model must see in
        full - it is why the reading happens - while a query built from all of it finds
        nothing."""
        wall = "\n".join(f"line {i} of a dense stack trace" for i in range(200))
        self.client.transcription = wall
        opened = self._confirm(web_search=True)
        update = opened["preflight"](threading.Event(), lambda _m: None)

        self.assertIn("line 0 of a dense stack trace", update["content"])
        self.assertIn("line 150 of a dense stack trace", update["content"],
                      "the reading must not be trimmed to the query's length")
        self.assertLessEqual(len(update["content"]), prompts.TRANSCRIPTION_CHARS + 500)

        query = update["search_query"]
        self.assertLessEqual(len(query), prompts.SEARCH_EXCERPT_CHARS + 100)
        # Whole lines only: a query cut mid-word searches for a word nobody wrote.
        self.assertTrue(query.endswith("trace"))


class ATextModelThatCannotSeeImages(ImageQueryCase):
    sees_images = False
    READING = "## Text\nERROR 0x80070643\n\n## Description\nA Windows Update dialog."

    def setUp(self):
        super().setUp()
        self.client.transcription = self.READING

    def test_it_answers_from_the_vision_models_reading(self):
        opened = self._confirm()
        self.assertFalse(opened["image_first"], "the text model cannot be sent the image")
        self.assertEqual(opened["image"], "IMAGEDATA",
                         "the capture stays with the conversation for later models")
        self.assertEqual(opened["system"],
                         prompts.query_described_system(self.config.default_language))

        update = opened["preflight"](threading.Event(), lambda _m: None)
        passes = [c for c in self.client.calls if c["kind"] == "complete"]
        self.assertEqual([p["model"] for p in passes], [OCR_MODEL])
        self.assertEqual(passes[0]["prompt"], prompts.IMAGE_BRIEF,
                         "the vision model must be asked for the text and a description")
        self.assertTrue(passes[0]["image"])
        self.assertIn(CaptureDialogStub.prompt, update["content"])
        self.assertIn("ERROR 0x80070643", update["content"])
        self.assertIn("A Windows Update dialog.", update["content"])

    def test_the_reader_sees_what_the_answer_was_given(self):
        opened = self._confirm()
        update = opened["preflight"](threading.Event(), lambda _m: None)
        self.assertTrue(update["display"].startswith(CaptureDialogStub.prompt))
        self.assertIn(f"Read from the image by {OCR_MODEL}", update["display"])
        self.assertIn("> ERROR 0x80070643", update["display"])

    def test_a_search_gets_the_question_and_the_reading(self):
        opened = self._confirm(web_search=True)
        update = opened["preflight"](threading.Event(), lambda _m: None)
        self.assertIn("0x80070643", update["search_query"])
        self.assertTrue(update["search_query"].startswith(CaptureDialogStub.prompt))

    def test_a_model_the_catalogue_does_not_list_is_treated_as_blind(self):
        self.sees_images = None
        opened = self._confirm()
        self.assertFalse(opened["image_first"])

    def test_the_answer_goes_to_the_text_model_through_the_real_window(self):
        from assistant.ui import chat
        opened = self._confirm()
        window = ChatWindow(self.app, opened["title"],
                            [{"role": "user", "content": opened["prompt"],
                              "display": opened["display"]}],
                            image_base64=opened["image"],
                            image_first=opened["image_first"],
                            autostart=True, preflight=opened["preflight"],
                            system=opened["system"])
        try:
            done = []
            source = GLib.timeout_add_seconds(20, lambda: (done.append(0),
                                                            Gtk.main_quit(), False)[-1])

            def poll():
                if window._cancel is None and len(window.conversation) >= 2:
                    Gtk.main_quit()
                    return False
                return True

            GLib.timeout_add(80, poll)
            Gtk.main()
            if not done:
                GLib.source_remove(source)
            streamed = [c for c in self.client.calls if c["kind"] == "stream"]
            self.assertEqual(streamed[0]["model"], TEXT_MODEL)
            self.assertFalse(streamed[0]["image"])
            self.assertIn("ERROR 0x80070643", streamed[0]["messages"][-1]["content"])
        finally:
            window.destroy()


class PrivacyModeImageQueries(ImageQueryCase):
    """The same rule with the private models: the private text model answers, and the
    private vision model reads for it when it cannot see - through the session, so the
    reading is attested and checked like every other private request."""

    PRIVATE_TEXT = "private/glm-5-3"
    PRIVATE_VISION = "private/glm-5-3-flash"

    class PrivateClient(RecordingClient):
        def complete(self, model, messages, image_base64=None, cancel=None,
                     web_search=None, system=None, privacy=None, status=None):
            self.calls.append({"kind": "complete", "model": model, "privacy": privacy,
                               "image": bool(image_base64),
                               "prompt": messages[-1]["content"]})
            return self.transcription

    def setUp(self):
        super().setUp()
        self.config.privacy_mode = True
        self.config.private_text_model = self.PRIVATE_TEXT
        self.config.private_vision_model = self.PRIVATE_VISION
        self.client = self.PrivateClient()
        self.app.client = self.client
        self._problems = app_module.privacy_module.problems
        # The E2EE client need not be installed for the routing to be right.
        app_module.privacy_module.problems = lambda _config: []

    def tearDown(self):
        app_module.privacy_module.problems = self._problems
        super().tearDown()

    def test_a_blind_private_text_model_answers_from_the_private_readers_reading(self):
        self.sees_images = False
        opened = self._confirm()
        session = opened["privacy"]
        self.assertCountEqual(session.models(), [self.PRIVATE_TEXT, self.PRIVATE_VISION])
        self.assertEqual(self.asked[-1], self.PRIVATE_TEXT)       # at Send
        self.assertFalse(opened["image_first"])
        opened["preflight"](threading.Event(), lambda _m: None)
        read = self.client.calls[0]
        self.assertEqual(read["model"], self.PRIVATE_VISION)
        self.assertIs(read["privacy"], session)
        self.assertEqual(read["prompt"], prompts.IMAGE_BRIEF)

    def test_a_private_text_model_that_sees_is_given_the_image(self):
        self.sees_images = True
        opened = self._confirm()
        self.assertTrue(opened["image_first"])
        self.assertEqual(opened["image"], "IMAGEDATA")
        self.assertEqual(self.asked[-1], self.PRIVATE_TEXT)       # at Send


class AConversationFollowsItsModelMenu(unittest.TestCase):
    """Through the real chat window: the first request, the follow-ups, a switch of
    model part way, and asking again - with the capture going to whichever model can
    see it, and a reading of it to any that cannot."""

    SEEING = {TEXT_MODEL: False, PREMIUM_MODEL: True, QUERY_MODEL: True,
              OCR_MODEL: True}

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        self.config.vision_model = OCR_MODEL
        self.config.text_model = TEXT_MODEL
        self.config.premium_text_model = PREMIUM_MODEL
        self.config.use_premium = False
        self.client = RecordingClient(transcription="## Text\nOK\n\n## Description\nA dialog")
        self.app = StubAssistant(self.config, self.client)
        self._accepts = app_module.catalogue.accepts_images
        from assistant.ui import chat
        self._chat_accepts = chat.catalogue.accepts_images
        chat.catalogue.accepts_images = lambda _c, model: self.SEEING.get(model)

    def tearDown(self):
        import shutil
        from assistant.ui import chat
        chat.catalogue.accepts_images = self._chat_accepts
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

    def _window(self, **kwargs):
        kwargs.setdefault("image_base64", "IMAGEDATA")
        return ChatWindow(self.app, "Query image",
                          [{"role": "user", "content": "what is this?",
                            "display": "what is this?"}], **kwargs)

    def _answered(self, window, count):
        return self._run_until(lambda: window._cancel is None
                               and sum(1 for t in window.conversation
                                       if t["role"] == "assistant") >= count)

    def _follow_up(self, window, text):
        window.entry.get_buffer().set_text(text)
        window._send()

    def streams(self):
        return [c for c in self.client.calls if c["kind"] == "stream"]

    def test_ocrs_transcription_comes_from_the_vision_model_and_follow_ups_do_not(self):
        window = self._window(first_model=OCR_MODEL, autostart=True)
        try:
            self.assertTrue(self._answered(window, 1))
            self.assertEqual(self.streams()[0]["model"], OCR_MODEL)
            self.assertTrue(self.streams()[0]["image"])
            self.assertEqual(window.conversation[-1]["model"], OCR_MODEL)
            self._follow_up(window, "what does it mean?")
            self.assertTrue(self._answered(window, 2))
            self.assertEqual(self.streams()[1]["model"], TEXT_MODEL)
        finally:
            window.destroy()

    def test_a_model_that_cannot_see_is_given_one_reading_of_the_image(self):
        window = self._window(first_model=OCR_MODEL, autostart=True)
        try:
            self.assertTrue(self._answered(window, 1))
            self._follow_up(window, "what does it mean?")
            self.assertTrue(self._answered(window, 2))
            self._follow_up(window, "and then?")
            self.assertTrue(self._answered(window, 3))
            reads = [c for c in self.client.calls if c["kind"] == "complete"]
            self.assertEqual(len(reads), 1, "the image is read once, not per message")
            self.assertEqual(reads[0]["model"], OCR_MODEL)
            self.assertEqual(reads[0]["prompt"], prompts.IMAGE_BRIEF)
            for call in self.streams()[1:]:
                self.assertFalse(call["image"])
                self.assertEqual(call["model"], TEXT_MODEL)
            self.assertIn("A dialog", window.reading)
        finally:
            window.destroy()

    def test_switching_to_a_model_that_sees_gives_it_the_image(self):
        window = self._window(first_model=OCR_MODEL, autostart=True)
        try:
            self.assertTrue(self._answered(window, 1))
            self.assertTrue(window._choose("premium"))
            self.assertEqual(window.header.get_subtitle(), PREMIUM_MODEL)
            self._follow_up(window, "look again")
            self.assertTrue(self._answered(window, 2))
            last = self.streams()[-1]
            self.assertEqual(last["model"], PREMIUM_MODEL)
            self.assertTrue(last["image"])
            self.assertEqual(window.conversation[-1]["model"], PREMIUM_MODEL)
        finally:
            window.destroy()

    def test_the_premium_option_is_where_the_menu_starts(self):
        self.config.use_premium = True
        window = self._window(image_base64=None)
        try:
            self.assertEqual(window.current_model, PREMIUM_MODEL)
            self.assertEqual(window._model_label.get_text(), "Premium")
        finally:
            window.destroy()

    def test_any_model_can_be_chosen(self):
        window = self._window(image_base64=None)
        try:
            window._chosen.append("someone/else")
            self.assertTrue(window._choose("model:someone/else"))
            self.assertEqual(window.current_model, "someone/else")
            self.assertIn(("model:someone/else", "Chosen", "someone/else"),
                          window._choices())
        finally:
            window.destroy()

    def test_asking_again_replaces_the_answer_in_the_history_but_not_on_screen(self):
        window = self._window(image_base64=None, autostart=True)
        try:
            self.assertTrue(self._answered(window, 1))
            window._ask_again("premium")
            self.assertTrue(self._run_until(lambda: window._cancel is None
                                            and len(window.conversation) >= 3))
            first, second = [t for t in window.conversation if t["role"] == "assistant"]
            self.assertTrue(first.get("superseded"))
            self.assertEqual(second["model"], PREMIUM_MODEL)
            again = self.streams()[-1]
            self.assertEqual(again["model"], PREMIUM_MODEL)
            self.assertEqual([m["role"] for m in again["messages"]], ["user"],
                             "the earlier answer must not be sent as history")
            self.assertEqual(window.current_model, PREMIUM_MODEL,
                             "the conversation goes on with the model asked")
        finally:
            window.destroy()


if __name__ == "__main__":
    unittest.main()
