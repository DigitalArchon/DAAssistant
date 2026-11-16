"""The move rule held for every model that is given a conversation's content, not only
its answering model: the text, premium and vision roles, the OCR and transcribe-then-
answer chains where one role's output is the next one's input, a picture's reading, and
follow-ups answered by another role. A conversation on Tinfoil (the highest privacy) gives
nothing of itself, in the clear, to an ordinary or Anthropic model in another role; it is
refused in words naming the role and both levels, and nothing is sent. In Privacy Mode the
session holds it (privacy.PrivacySession.enlist, .start), outside it the chat window
(ChatWindow._hold)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: E402,F401  (pins GI versions)
from assistant import privacy, providers  # noqa: E402
from assistant.privacy import PrivacyError, PrivacySession  # noqa: E402

from test_chat_requests import ANSWERED, Scripted, WindowCase  # noqa: E402

NANOGPT = "https://nano-gpt.com/api/v1/chat/completions"
SERVICES = {"tinfoil": ("https://inference.tinfoil.sh/v1/chat/completions", "tk", ""),
            "chutes": ("https://llm.chutes.ai/v1/chat/completions", "cpk", "")}


class InPrivacyMode(unittest.TestCase):
    def session(self, text, vision, uses=("text", "vision"), premium=None):
        return PrivacySession(NANOGPT, "k", text, vision, uses=uses, premium_model=premium,
                              services=SERVICES)

    def test_a_conversation_is_kept_to_its_most_private_starting_model(self):
        self.assertEqual(self.session("Tinfoil::glm", "TEE/x").kept.level, "highest")
        self.assertEqual(self.session("TEE/x", "Chutes::a-TEE").kept.level, "partial")
        self.assertEqual(self.session("Tinfoil::glm", "TEE/x", uses=("text",))
                         .kept.level, "highest")

    def test_the_vision_model_reading_for_a_tinfoil_answer_is_held_to_its_level(self):
        session = self.session("Tinfoil::glm", "TEE/glm-5.3", uses=("text",))
        with self.assertRaises(PrivacyError) as caught:
            session.enlist("TEE/glm-5.3")       # the capture's reader, in OCR or a reading
        said = str(caught.exception)
        for words in ("private vision model, TEE/glm-5.3", "partial privacy",
                      "kept to the highest privacy", "nothing was sent"):
            self.assertIn(words, said)
        self.assertNotIn("TEE/glm-5.3", session.models())

    def test_a_follow_up_by_another_role_or_a_chosen_model_is_held_too(self):
        session = self.session("Tinfoil::glm", "Tinfoil::glm", uses=("text",),
                               premium="Chutes::a/b-TEE")
        with self.assertRaisesRegex(PrivacyError, "private premium model.*partial privacy"):
            session.enlist("Chutes::a/b-TEE")
        with self.assertRaisesRegex(PrivacyError, "chosen model, TEE/x, has partial"):
            session.enlist("TEE/x")
        session.enlist("private/glm-5-3")       # as private: goes

    def test_moving_up_raises_what_the_conversation_is_kept_to(self):
        session = self.session("TEE/x", "TEE/x", uses=("text",))
        session.enlist("Tinfoil::glm")
        self.assertEqual(session.kept.level, "highest")
        with self.assertRaisesRegex(PrivacyError, "kept to the highest privacy"):
            session.enlist("Chutes::a/b-TEE")

    def test_starting_an_operation_whose_roles_differ_in_level_is_refused_in_words(self):
        config = type("C", (), {})()
        for name, value in (("privacy_mode", True), ("api_url", NANOGPT), ("api_key", "k"),
                            ("private_text_model", "Tinfoil::glm"),
                            ("private_premium_model", "Tinfoil::glm"),
                            ("private_vision_model", "TEE/glm-5.3"),
                            ("request_timeout", 60), ("privacy_strict", False)):
            setattr(config, name, value)
        from unittest import mock
        with mock.patch.object(privacy, "problems", return_value=[]), \
                mock.patch.object(privacy, "service_settings",
                                  side_effect=lambda c, s: SERVICES[s]):
            with self.assertRaises(PrivacyError) as caught:
                PrivacySession.start(config, uses=("text", "vision"))
            said = str(caught.exception)
            for words in ("vision model, TEE/glm-5.3, has partial privacy",
                          "text model, Tinfoil::glm", "the highest privacy",
                          "Nothing was sent"):
                self.assertIn(words, said)
            self.assertIsNotNone(PrivacySession.start(config, uses=("text",)))

    def test_the_rule_is_said_by_role_and_both_levels(self):
        self.assertIsNone(privacy.held_below([("text", "private/a"),
                                              ("vision", "Tinfoil::b")]))
        said = privacy.held_below([("text", "TEE/a"), ("vision", "Chutes::b-TEE"),
                                   ("premium", "Tinfoil::c")])
        self.assertIn("The text model, TEE/a, has partial privacy", said)
        self.assertIn("the premium model, Tinfoil::c", said)


class OutsidePrivacyMode(WindowCase):
    """The chat window, a conversation answered on Tinfoil (the highest privacy) and its
    vision and premium roles ordinary."""

    def setUp(self):
        super().setUp()
        self.config.tinfoil_api_key = "tk"
        self.config.text_provider = "tinfoil"
        self.config.text_model = "glm-5-3"
        self.config.vision_provider = "openrouter"
        self.config.vision_model = "qwen/qwen3-vl"
        self.config.premium_text_provider = "anthropic"
        self.config.premium_text_model = "claude-opus-5-5"

    def refused(self, window, client, words=("vision model", "no privacy",
                                             "kept to the highest privacy")):
        self.assertEqual(client.calls, [], "nothing was sent")
        shown = window.bubbles.transcript_text() + " " + window.status.get_text()
        for word in words:
            self.assertIn(word, shown)

    def test_ocr_whose_vision_model_is_ordinary_is_refused_before_the_capture_goes(self):
        client = Scripted("never")
        window = self.window(client, [{"role": "user", "content": "Read it"}],
                             image_base64="aGk=", first_model=self.config.model("vision"),
                             autostart=True)
        self.settled(window)
        self.refused(window, client)

    def test_transcribe_then_answer_is_refused_before_the_transcription(self):
        asked = []

        def preflight(cancel, status, privacy=None, web_search=False):
            asked.append("read")
            return {"content": "read"}

        client = Scripted("never")
        window = self.window(client, [{"role": "user", "content": "Translate it"}],
                             image_base64="aGk=", image_first=False, preflight=preflight,
                             autostart=True)
        self.settled(window)
        self.assertEqual(asked, [], "the vision model was never asked")
        self.refused(window, client)

    def test_a_follow_up_by_an_ordinary_role_is_refused_once_tinfoil_answered(self):
        client = Scripted("ok from Tinfoil")
        window = self.window(client, [{"role": "user", "content": "Hi"}], autostart=True)
        self.settled(window)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["model"].provider, "tinfoil")
        self.assertFalse(window._choose("premium"))
        for word in ("premium model, claude-opus-5-5", "no privacy",
                     "kept to the highest privacy"):
            self.assertIn(word, window.status.get_text())
        self.assertEqual(window.current_model.provider, "tinfoil")

    def test_an_ordinary_conversation_is_as_it_was(self):
        self.config.text_provider = "openrouter"
        self.config.text_model = "some/model"
        client = Scripted("one", "two")
        window = self.window(client, [{"role": "user", "content": "Hi"}], autostart=True)
        self.settled(window)
        self.assertTrue(window._choose("premium"))
        self.send(window, "And?")
        self.assertEqual(len(client.calls), 2)

    def test_a_conversation_may_move_up_and_is_then_kept_there(self):
        self.config.text_provider = "openrouter"
        self.config.text_model = "some/model"
        self.config.premium_text_provider = "tinfoil"
        self.config.premium_text_model = "glm-5-3"
        client = Scripted("one", "two")
        window = self.window(client, [{"role": "user", "content": "Hi"}], autostart=True)
        self.settled(window)
        self.assertTrue(window._choose("premium"))
        self.send(window, "And?")
        self.assertEqual(client.calls[1]["model"].provider, "tinfoil")
        self.assertFalse(window._choose("text"))
        self.assertIn("text model, some/model, has no privacy", window.status.get_text())


del ANSWERED, providers

if __name__ == "__main__":
    unittest.main()
