"""The compose dialogs must carry a per-request web-search choice.

The chat window's header toggle cannot cover the first message: by the time that window
exists the request is already streaming. So the dialog where the message is actually
composed owns the choice, seeded from the saved default and overridable per request.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401
from PIL import Image

from assistant.config import Config
from assistant.ui.dialogs import CaptureReviewDialog, TextReviewDialog


class TextDialogWebSearch(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"          # web search runs on NanoGPT, with its key
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
        self.config.api_key = "k"
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


class CaptureDialogNamesItsModels(unittest.TestCase):
    """Before Send, the capture dialog says which models will read and answer."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.vision_provider, self.config.vision_model = "ollama", "glm-ocr"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def dialog(self, plan):
        dialog = CaptureReviewDialog(Image.new("RGB", (120, 60), (255, 255, 255)),
                                     "OCR + Translate", self.config, lambda *a: None,
                                     plan=plan)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_each_model_and_its_job_in_order(self):
        dialog = self.dialog([(self.config.model("vision"), "reads the text"),
                              (self.config.model("text"), "translates it")])
        self.assertEqual(dialog.plan.get_text(),
                         f"Models: glm-ocr on Ollama reads the text, then "
                         f"{self.config.text_model} translates it.")

    def test_without_a_plan_there_is_no_line(self):
        self.assertFalse(self.dialog(None).plan.get_visible())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SaveEveryImageSent(unittest.TestCase):
    """"Save every image sent" keeps what is sent: blacked out as sent, and only when it
    is - not the capture as the review dialog opened on it, nor one that was cancelled."""

    def setUp(self):
        import shutil
        import tempfile
        from assistant import paths
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.captures = os.path.join(paths.CACHE_DIR, "captures")
        shutil.rmtree(self.captures, ignore_errors=True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.debug_save_captures = True
        self.sent = {}

    def saved(self):
        return sorted(os.listdir(self.captures)) if os.path.isdir(self.captures) else []

    def dialog(self):
        def on_confirm(image, image_base64, info, prompt, web_search):
            self.sent.update(image_base64=image_base64, info=info)
        return CaptureReviewDialog(Image.new("RGB", (120, 60), (255, 255, 255)),
                                   "OCR only", self.config, on_confirm)

    def test_nothing_is_kept_when_the_dialog_opens_or_is_cancelled(self):
        dialog = self.dialog()
        self.assertEqual(self.saved(), [])
        dialog.destroy()
        self.assertEqual(self.saved(), [])

    def test_the_copy_is_what_was_sent_blacked_out(self):
        import base64
        import io
        from unittest import mock
        from assistant.ui import redact

        def black_out(_parent, image, done, apply_label=None):
            blacked = image.copy()
            blacked.paste((0, 0, 0), (0, 0, 60, 60))
            done(blacked, [(0, 0, 60, 60)])

        dialog = self.dialog()
        with mock.patch.object(redact, "RedactDialog", black_out), \
                mock.patch.object(dialog, "get_realized", lambda: True):
            dialog._on_redact()
        dialog._confirm()
        [name] = self.saved()
        with open(os.path.join(self.captures, name), "rb") as f:
            kept = f.read()
        self.assertEqual(kept, base64.b64decode(self.sent["image_base64"]))
        self.assertEqual(Image.open(io.BytesIO(kept)).convert("RGB").getpixel((10, 10)),
                         (0, 0, 0))
        self.assertEqual(self.sent["info"]["debug_path"], os.path.join(self.captures, name))
