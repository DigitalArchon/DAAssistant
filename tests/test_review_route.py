"""The review dialogs say where their request will go, and keep saying it truly.

A capture or the clipboard text waits on screen with Send under it. The dialog names the
models it goes to, and whether it goes privately - and if Privacy Mode, the premium
option or the settings change while it waits, it says so at once, before Send, not
after: the request goes where the dialog says.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gtk  # noqa: F401

from assistant import app as app_module
from assistant.app import Assistant
from assistant.config import Config
from assistant.ui.dialogs import TextReviewDialog

TEXT, PREMIUM, VISION = "good/text-model", "best/premium-model", "cheap/ocr-model"


class StubApp:
    """The real clipboard path and review bookkeeping, without tray or chat window."""

    _start_clipboard = Assistant._start_clipboard
    translate_text = Assistant.translate_text
    query_text = Assistant.query_text
    _watch_review = Assistant._watch_review
    _refresh_reviews = Assistant._refresh_reviews
    _confirm_privacy = Assistant._confirm_privacy
    _start_privacy = Assistant._start_privacy
    _image_answerer = Assistant._image_answerer
    _answer_role = Assistant._answer_role

    def __init__(self, config):
        self.config = config
        self._reviews = []
        self.opened = []
        self.notices = []

    def _require_api_key(self):
        return True

    def _open_chat(self, title, prompt, display, **kwargs):
        self.opened.append(kwargs)

    def notify(self, message, urgency="low"):
        self.notices.append(message)


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

    def open(self, operation="translate_text"):
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


class TheClipboardDialogNamesItsModel(Case):
    def test_translation(self):
        self.assertEqual(self.open().plan.get_text(), f"Models: {TEXT} translates it.")

    def test_a_question(self):
        self.assertEqual(self.open("query_text").plan.get_text(), f"Models: {TEXT} answers.")

    def test_the_provider_where_it_is_not_nanogpt(self):
        self.config.text_provider = "ollama"
        self.assertEqual(self.open().plan.get_text(),
                         f"Models: {TEXT} on Ollama translates it.")

    def test_with_web_search_ticked_where_the_query_goes_too(self):
        """The search is data leaving as well: a query, made from the text, to NanoGPT."""
        dialog = self.open("query_text")
        dialog.search_check.set_active(True)
        self.assertIn(f"Web search: {TEXT} writes a search query from it, and NanoGPT "
                      f"runs the search.", dialog.plan.get_text())
        dialog.search_check.set_active(False)
        self.assertNotIn("Web search", dialog.plan.get_text())


class WhileTheDialogWaits(Case):
    def test_the_premium_option_changes_the_model_named(self):
        dialog = self.open()
        self.config.use_premium = True
        self.app._refresh_reviews()
        self.assertEqual(dialog.plan.get_text(), f"Models: {PREMIUM} translates it.")

    def test_privacy_mode_switched_on_makes_it_private_before_send(self):
        dialog = self.open("query_text")
        dialog.search_check.set_active(True)
        self.assertEqual(dialog.send.get_label(), "Send")
        self.config.privacy_mode = True
        self.app._refresh_reviews()
        self.assertEqual(dialog.send.get_label(), "Send privately")
        self.assertEqual(dialog.header.get_subtitle(), "Review before sending privately")
        self.assertTrue(dialog.privacy_slot.get_children(), "no Privacy Mode note")
        self.assertFalse(dialog.search_check.get_active())
        self.assertFalse(dialog.search_check.get_sensitive())
        self.assertEqual(dialog.plan.get_text(),
                         f"Models: {self.config.private_text_model} answers.")

        dialog.prompt_view.get_buffer().set_text("What does it say?")
        session = dialog.route_privacy
        dialog._confirm()
        [opened] = self.app.opened
        self.assertIs(opened["privacy"], session)          # sent the way it said
        self.assertFalse(opened["web_search"])

    def test_switched_off_again_it_stays_private_as_it_said(self):
        dialog = self.open()
        self.config.privacy_mode = True
        self.app._refresh_reviews()
        self.config.privacy_mode = False
        self.app._refresh_reviews()
        self.assertEqual(dialog.send.get_label(), "Send privately")

    def test_privacy_mode_that_cannot_be_honoured_holds_send_and_says_why(self):
        dialog = self.open("query_text")
        self.config.private_text_model = "not/a-tee-model"
        self.config.privacy_mode = True
        self.app._refresh_reviews()
        self.assertFalse(dialog.send.get_sensitive())
        self.assertEqual(dialog.header.get_subtitle(), "Nothing can be sent")
        self.assertFalse(dialog.plan.get_visible())
        self.config.privacy_mode = False                  # and switched off, it can go
        self.app._refresh_reviews()
        self.assertTrue(dialog.send.get_sensitive())
        self.assertEqual(dialog.send.get_label(), "Send")
        self.assertTrue(dialog.plan.get_visible())

    def test_web_search_comes_back_as_it_was_left(self):
        dialog = self.open("query_text")
        dialog.search_check.set_active(True)
        self.config.private_text_model = "not/a-tee-model"
        self.config.privacy_mode = True
        self.app._refresh_reviews()
        self.assertFalse(dialog.search_check.get_active())
        self.config.privacy_mode = False
        self.app._refresh_reviews()
        self.assertTrue(dialog.search_check.get_active())

    def test_a_closed_dialog_is_no_longer_kept_up_to_date(self):
        dialog = self.open()
        self.assertEqual(len(self.app._reviews), 1)
        dialog.destroy()
        self.assertEqual(self.app._reviews, [])
        self.app._refresh_reviews()                         # nothing left to touch


class TheCaptureDialogToo(Case):
    def test_privacy_mode_switched_on_while_a_capture_waits(self):
        from PIL import Image
        from assistant import privacy
        from assistant.ui.dialogs import CaptureReviewDialog
        dialog = CaptureReviewDialog(Image.new("RGB", (60, 30), (255, 255, 255)),
                                     "OCR + Translate", self.config, lambda *a: None,
                                     plan=[(self.config.model("vision"), "reads the text"),
                                           (self.config.model("text"), "translates it")])
        self.addCleanup(dialog.destroy)
        self.assertEqual(dialog.plan.get_text(),
                         f"Models: {VISION} reads the text, then {TEXT} translates it.")
        self.config.privacy_mode = True
        session = privacy.PrivacySession.start(self.config, uses=("vision", "text"))
        dialog.set_route(session, [(session.vision_model, "reads the text"),
                                   (session.text_model, "translates it")])
        self.assertEqual(dialog.send.get_label(), "Send privately")
        self.assertIn(self.config.private_vision_model, dialog.plan.get_text())
        self.assertTrue(dialog.privacy_slot.get_children())


if __name__ == "__main__":
    unittest.main(verbosity=2)
