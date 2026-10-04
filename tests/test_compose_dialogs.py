"""The compose dialogs must carry a per-request web-search choice.

The chat window's header toggle cannot cover the first message: by the time that window
exists the request is already streaming. So the dialog where the message is actually
composed owns the choice, seeded from the saved default and overridable per request.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from PIL import Image

from assistant.config import Config
from assistant.ui.dialogs import CaptureReviewDialog, TextReviewDialog


class TextDialogWebSearch(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.result = {}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _dialog(self, ask_prompt=True):
        def on_confirm(text, prompt, web_search):
            self.result.update(text=text, prompt=prompt, web_search=web_search)
        dialog = TextReviewDialog("some clipboard text", "Query text", on_confirm,
                                  ask_prompt=ask_prompt, config=self.config)
        if ask_prompt:
            dialog.prompt_view.get_buffer().set_text("a question")
        return dialog

    def test_checkbox_seeds_from_saved_default_off(self):
        self.config.web_search_enabled = False
        dialog = self._dialog()
        try:
            self.assertFalse(dialog.search_check.get_active())
        finally:
            dialog.destroy()

    def test_checkbox_seeds_from_saved_default_on(self):
        self.config.web_search_enabled = True
        dialog = self._dialog()
        try:
            self.assertTrue(dialog.search_check.get_active())
        finally:
            dialog.destroy()

    def test_enabling_for_one_request_when_default_is_off(self):
        self.config.web_search_enabled = False
        dialog = self._dialog()
        dialog.search_check.set_active(True)
        dialog._confirm()
        self.assertTrue(self.result["web_search"])
        # The per-request choice must not rewrite the saved default.
        self.assertFalse(self.config.web_search_enabled)

    def test_disabling_for_one_request_when_default_is_on(self):
        self.config.web_search_enabled = True
        dialog = self._dialog()
        dialog.search_check.set_active(False)
        dialog._confirm()
        self.assertFalse(self.result["web_search"])
        self.assertTrue(self.config.web_search_enabled)

    def test_choice_survives_on_dialogs_without_a_prompt_field(self):
        """Translate uses ask_prompt=False; it still needs the toggle."""
        self.config.web_search_enabled = False
        dialog = self._dialog(ask_prompt=False)
        dialog.search_check.set_active(True)
        dialog._confirm()
        self.assertTrue(self.result["web_search"])


class CaptureDialogWebSearch(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.result = {}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _dialog(self):
        def on_confirm(image, image_base64, info, prompt, web_search):
            self.result.update(prompt=prompt, web_search=web_search,
                               has_image=bool(image_base64))
        dialog = CaptureReviewDialog(Image.new("RGB", (120, 60), (255, 255, 255)),
                                     "Query image", self.config, on_confirm,
                                     ask_prompt=True)
        dialog.prompt_view.get_buffer().set_text("what is this?")
        return dialog

    def test_capture_dialog_carries_the_choice(self):
        self.config.web_search_enabled = False
        dialog = self._dialog()
        dialog.search_check.set_active(True)
        dialog._confirm()
        self.assertTrue(self.result["web_search"])
        self.assertTrue(self.result["has_image"])

    def test_capture_dialog_defaults_to_saved_setting(self):
        self.config.web_search_enabled = True
        dialog = self._dialog()
        try:
            self.assertTrue(dialog.search_check.get_active())
        finally:
            dialog.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
