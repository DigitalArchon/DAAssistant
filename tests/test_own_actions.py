"""The user's own actions (Settings > Actions), and a translation into a language
chosen for the one request."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import Gtk  # noqa: E402

from assistant import app as app_module, hotkeys, prompts  # noqa: E402
from assistant.app import Assistant  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.ui.dialogs import SettingsDialog, TextReviewDialog  # noqa: E402

PROOFREAD = {"slot": 2, "name": "Proofread", "prompt": "Proofread this.",
             "source": "clipboard"}
SCREEN = {"slot": 5, "name": "Explain this screen", "prompt": "Explain it.",
          "source": "region"}


class Configured(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"


class TheirSlots(Configured):
    def test_only_defined_actions_are_offered_under_their_names(self):
        self.config.custom_actions = [PROOFREAD, SCREEN,
                                      {"slot": 3, "name": "", "prompt": "half made"}]
        self.assertEqual(sorted(hotkeys.custom_defined(self.config)),
                         ["custom_2", "custom_5"])
        offered = {name: label for name, label, _key in hotkeys.enabled_actions(self.config)}
        self.assertEqual(offered["custom_2"], "Proofread")
        self.assertNotIn("custom_3", offered)
        self.assertNotIn("custom_1", offered)

    def test_a_key_goes_with_its_action_and_none_is_suggested(self):
        self.config.custom_actions = [PROOFREAD]
        self.config.hotkeys = {"custom_2": "<Control><Alt>p", "custom_4": "<Control><Alt>x"}
        self.assertEqual(hotkeys.bindings(self.config), {"custom_2": "<Control><Alt>p"})
        self.assertFalse(any(name in hotkeys.LEGACY_KEYS for name in hotkeys.CUSTOM_ACTIONS))

    def test_the_prompt_takes_the_text_where_it_says(self):
        self.assertEqual(prompts.custom_action("Fix {text} please", "teh"), "Fix teh please")
        self.assertEqual(prompts.custom_action("Fix this.", "teh"), "Fix this.\n\n---\n\nteh")


class SettingsPage(Configured):
    def dialog(self):
        dialog = SettingsDialog(None, self.config, type("C", (), {"config": self.config}))
        self.addCleanup(dialog.destroy)
        return dialog

    def test_an_action_added_is_saved_in_the_first_free_slot(self):
        self.config.custom_actions = [PROOFREAD]
        dialog = self.dialog()
        row = dialog._add_action_row()
        row["name"].set_text("Summarise")
        row["prompt"].get_buffer().set_text("Summarise this.")
        row["source"].set_active_id("selection")
        self.assertEqual(dialog.values()["custom_actions"], [
            {"slot": 1, "name": "Summarise", "prompt": "Summarise this.",
             "source": "selection"}, PROOFREAD])

    def test_an_action_taken_out_takes_its_key_with_it(self):
        self.config.custom_actions = [PROOFREAD]
        self.config.hotkeys = {"custom_2": "<Control><Alt>p", "ocr_only": "<Control><Shift>5"}
        dialog = self.dialog()
        dialog._remove_action_row(dialog._action_rows[0])
        values = dialog.values()
        self.assertEqual(values["custom_actions"], [])
        self.assertNotIn("custom_2", values["hotkeys"])
        self.assertIn("ocr_only", values["hotkeys"])

    def test_the_examples_fill_free_slots_once(self):
        dialog = self.dialog()
        dialog._add_examples()
        dialog._add_examples()
        names = [a["name"] for a in dialog.values()["custom_actions"]]
        self.assertEqual(names, ["Summarise", "Proofread", "Explain this screen"])

    def test_six_at_most(self):
        dialog = self.dialog()
        for _ in range(hotkeys.CUSTOM_SLOTS + 2):
            dialog._add_action_row()
        self.assertEqual(len(dialog._action_rows), hotkeys.CUSTOM_SLOTS)
        self.assertFalse(dialog.add_action_button.get_sensitive())

    def test_the_shortcuts_page_lists_the_defined_ones(self):
        self.config.custom_actions = [SCREEN]
        dialog = self.dialog()
        self.assertIn("custom_5", dialog.shortcuts_page.buttons)
        self.assertNotIn("custom_1", dialog.shortcuts_page.buttons)


class Running(Configured):
    def app(self):
        test = self

        class Stub:
            run_custom_action = Assistant.run_custom_action

            def __init__(stub):
                stub.config = test.config
                stub.started, stub.notices = [], []

            def _start_clipboard(stub, title, ask_prompt, build, **kwargs):
                stub.started.append(("clipboard", title, build("the text", None), kwargs))

            def _start_capture(stub, title, ask_prompt, build, **kwargs):
                stub.started.append(("capture", title, ask_prompt, kwargs))

            def notify(stub, message, urgency="low"):
                stub.notices.append(message)
        return Stub()

    def test_a_text_action_is_its_prompt_and_the_clipboard(self):
        self.config.custom_actions = [PROOFREAD]
        app = self.app()
        app.run_custom_action("custom_2")
        kind, title, (prompt, display), kwargs = app.started[0]
        self.assertEqual((kind, title), ("clipboard", "Proofread"))
        self.assertEqual(prompt, "Proofread this.\n\n---\n\nthe text")
        self.assertEqual(kwargs["source"], "clipboard")
        kwargs["picture"]("an image")         # a picture on the clipboard: as a capture
        self.assertEqual(app.started[1][0], "capture")

    def test_on_highlighted_text_it_reads_the_selection(self):
        self.config.custom_actions = [dict(PROOFREAD, source="selection")]
        app = self.app()
        app.run_custom_action("custom_2")
        self.assertEqual(app.started[0][3]["source"], "selection")

    def test_a_screen_action_asks_its_prompt_of_the_region(self):
        self.config.custom_actions = [SCREEN]
        app = self.app()
        app.run_custom_action("custom_5")
        kind, title, ask_prompt, kwargs = app.started[0]
        self.assertEqual((kind, title, ask_prompt), ("capture", "Explain this screen", True))
        self.assertEqual(kwargs["prompt_default"], "Explain it.")
        self.assertTrue(kwargs["answer_by_text"])

    def test_an_empty_slot_says_so(self):
        app = self.app()
        app.run_custom_action("custom_1")
        self.assertEqual(app.started, [])
        self.assertIn("not set up", app.notices[0])


class TranslateIntoAnotherLanguage(Configured):
    def test_the_dialog_starts_on_the_setting_and_takes_another(self):
        dialog = TextReviewDialog("Hallo", "Translate text", lambda *a: None,
                                  config=self.config, language="English")
        self.addCleanup(dialog.destroy)
        self.assertEqual(dialog.target_language(), "English")
        dialog.language_combo.get_child().set_text("Japanese")
        self.assertEqual(dialog.target_language(), "Japanese")

    def test_the_request_is_for_the_language_chosen_and_the_setting_stays(self):
        self.config.default_language = "English"

        class Stub:
            _start_clipboard = Assistant._start_clipboard
            translate_text = Assistant.translate_text
            _watch_review = Assistant._watch_review
            _offer_lookups = Assistant._offer_lookups
            look_up_selection = Assistant.look_up_selection
            _refresh_reviews = Assistant._refresh_reviews
            _confirm_privacy = Assistant._confirm_privacy
            _start_privacy = Assistant._start_privacy
            _image_answerer = Assistant._image_answerer
            _answer_role = Assistant._answer_role

            def __init__(stub):
                stub.config, stub._reviews, stub.opened = self.config, [], []

            def _require_api_key(stub):
                return True

            def _open_chat(stub, title, prompt, display, **kwargs):
                stub.opened.append((display, kwargs["system"]))

            def notify(stub, *_a, **_k):
                pass

        made = []

        def make(*args, **kwargs):
            dialog = TextReviewDialog(*args, **kwargs)
            self.addCleanup(dialog.destroy)
            made.append(dialog)
            return dialog

        app = Stub()
        with mock.patch.object(app_module, "TextReviewDialog", side_effect=make), \
                mock.patch.object(app_module.clipboard, "get_text",
                                  return_value=("Guten Morgen", "text")):
            app.translate_text()
        made[0].language_combo.get_child().set_text("Japanese")
        made[0]._confirm()
        display, system = app.opened[0]
        self.assertIn("into Japanese", display)
        self.assertIn("Japanese", system)
        self.assertEqual(self.config.default_language, "English")


if __name__ == "__main__":
    unittest.main()
