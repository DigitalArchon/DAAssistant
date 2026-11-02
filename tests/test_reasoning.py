"""Reasoning effort: who is asked for it, how it is spelled, and room to think.

GLM 5.3 reasons before it answers, and at its own default it reasons at length: live
(Sept 2026) private/glm-5-3 spent a 20,480-token limit reasoning about a 5,000-character
translation and never began it, and the window said only "empty response". At low
effort the same translation took 10s and no reasoning tokens. So models that always
reason are asked for the configured effort (low by default), with room to think on top
of the answer's limit - and models that think only when asked are never asked, because
asking switched deepseek/deepseek-v4-pro's thinking on.

Run:  python3 -m unittest tests.test_reasoning -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import models, sealed
from assistant.config import Config
from assistant import websearch
from assistant.llm import (CUT_OFF_NOTE, FILTERED_NOTE, REASONING_ROOM, UNFINISHED_NOTE,
                           LLMClient, LLMError)

API_URL = "https://nano-gpt.com/api/v1/chat/completions"

# As nano-gpt lists them (reasoning_efforts, Sept 2026).
CATALOGUE = [
    {"id": "TEE/glm-5.3", "reasoning_efforts": ["low", "high", "max"]},
    {"id": "private/glm-5-3", "reasoning_efforts": ["low", "high", "max"]},
    {"id": "deepseek/deepseek-v4-pro", "reasoning_efforts": ["none", "high", "max"]},
    {"id": "anthropic/claude-sonnet-5:thinking",
     "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"id": "openai/tiny", "reasoning_efforts": ["minimal", "medium"]},
    {"id": "TEE/qwen3.6-27b"},
]


class CatalogueCase(unittest.TestCase):
    """A config and a model catalogue of our own, never the user's cache."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_url = API_URL
        self.config.api_key = "k"
        cache = os.path.join(self.dir, "models.json")
        self.patches = [mock.patch.object(models, "CACHE_DIR", self.dir),
                        mock.patch.object(models, "CACHE_FILE", cache)]
        for patch in self.patches:
            patch.start()
        models.save_cache([models.normalise(e) for e in CATALOGUE],
                          models.models_url(API_URL))

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class WhoIsAskedToReason(CatalogueCase):
    def test_a_model_that_always_reasons_is_asked_for_low_by_default(self):
        self.assertEqual(self.config.reasoning_effort, "low")
        self.assertEqual(models.effort_for(self.config, "TEE/glm-5.3"), "low")
        self.assertEqual(models.effort_for(self.config, "private/glm-5-3"), "low")

    def test_a_model_that_thinks_only_when_asked_is_never_asked(self):
        for wanted in ("low", "medium", "high"):
            self.config.reasoning_effort = wanted
            self.assertIsNone(models.effort_for(self.config, "deepseek/deepseek-v4-pro"))

    def test_a_model_the_catalogue_says_nothing_about_is_left_alone(self):
        self.assertIsNone(models.effort_for(self.config, "TEE/qwen3.6-27b"))
        self.assertIsNone(models.effort_for(self.config, "someone/unlisted"))

    def test_the_setting_becomes_the_nearest_level_the_model_offers(self):
        self.config.reasoning_effort = "medium"
        self.assertEqual(models.effort_for(self.config, "TEE/glm-5.3"), "high")
        self.assertEqual(
            models.effort_for(self.config, "anthropic/claude-sonnet-5:thinking"), "medium")
        self.config.reasoning_effort = "low"
        self.assertEqual(models.effort_for(self.config, "openai/tiny"), "medium",
                         "rounded up: a model that offers no low is asked for its next")
        self.config.reasoning_effort = "high"
        self.assertEqual(models.effort_for(self.config, "openai/tiny"), "medium",
                         "and down when there is nothing higher")


class NoneWhereAModelAllowsIt(CatalogueCase):
    def setUp(self):
        super().setUp()
        self.config.reasoning_effort = "none"

    def test_none_goes_to_a_model_that_can_switch_thinking_off(self):
        self.assertEqual(models.effort_for(self.config, "deepseek/deepseek-v4-pro"), "none")

    def test_a_model_that_always_thinks_is_given_its_lowest_level_instead(self):
        # nano-gpt refuses "none" for GLM 5.3 outright, as HTTP 400.
        self.assertEqual(models.effort_for(self.config, "TEE/glm-5.3"), "low")
        self.assertEqual(models.effort_for(self.config, "openai/tiny"), "minimal")

    def test_a_model_the_catalogue_says_nothing_about_is_still_left_alone(self):
        self.assertIsNone(models.effort_for(self.config, "TEE/qwen3.6-27b"))

    def test_none_asks_for_no_room_to_think(self):
        payload = LLMClient(self.config)._payload(
            "deepseek/deepseek-v4-pro", [{"role": "user", "content": "q"}], None, True)
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["max_tokens"], self.config.max_tokens)


class WhatIsSent(CatalogueCase):
    def payload(self, model, **kwargs):
        return LLMClient(self.config)._payload(
            model, [{"role": "user", "content": "q"}], None, True, **kwargs)

    def test_a_reasoning_model_is_asked_for_its_effort_with_room_to_think(self):
        payload = self.payload("TEE/glm-5.3")
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["max_tokens"],
                         self.config.max_tokens + REASONING_ROOM["low"])

    def test_other_models_are_sent_what_they_always_were(self):
        payload = self.payload("deepseek/deepseek-v4-pro")
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["max_tokens"], self.config.max_tokens)

    def test_the_sealed_body_carries_each_familys_own_spelling(self):
        glm = sealed.shape_body(self.payload("private/glm-5-3"), "private/glm-5-3",
                                "glm-5.3")
        self.assertEqual(glm["reasoning_effort"], "low")
        self.assertNotIn("reasoning", glm, "an unknown field must not reach the router")
        self.assertTrue(glm["chat_template_kwargs"]["thinking"])

        kimi = sealed.shape_body({"messages": [], "reasoning": {"effort": "high"},
                                  "temperature": 1}, "private/kimi-k3", "kimi-k3")
        self.assertEqual(kimi["reasoning_effort"], "high")
        self.assertNotIn("temperature", kimi)

    def test_effort_never_switches_thinking_on_in_the_sealed_body(self):
        deepseek = sealed.shape_body({"messages": [], "reasoning": {"effort": "high"}},
                                     "private/deepseek-v4-1-flash", "deepseek-v4.1-flash")
        self.assertFalse(deepseek["chat_template_kwargs"]["thinking"])
        self.assertNotIn("reasoning_effort", deepseek["chat_template_kwargs"])
        gemma = sealed.shape_body({"messages": [], "reasoning": {"effort": "high"}},
                                  "private/gemma4-31b", "gemma-4-31b")
        self.assertFalse(gemma["chat_template_kwargs"]["enable_thinking"])


class FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        yield from self._lines

    def close(self):
        pass


class HowAReplyEnds(CatalogueCase):
    def stream_with(self, chunks):
        lines = [b"data: " + json.dumps(c).encode() for c in chunks] + [b"data: [DONE]"]
        client = LLMClient(self.config)
        client._post = lambda payload, stream, cancel=None: FakeResponse(lines)
        return client.stream("TEE/glm-5.3", [{"role": "user", "content": "translate"}])

    def test_a_reply_that_was_all_thinking_says_so_and_how_to_fix_it(self):
        with self.assertRaises(LLMError) as caught:
            self.stream_with([
                {"id": "c1", "choices": [{"delta": {"reasoning": "Let me think..."}}]},
                {"id": "c1", "choices": [{"delta": {}, "finish_reason": "length"}]}])
        message = str(caught.exception)
        self.assertEqual(caught.exception.model, "TEE/glm-5.3")
        self.assertIn("never began its answer", message)
        self.assertIn("Reasoning effort", message)
        self.assertIn(str(self.config.max_tokens + REASONING_ROOM["low"]), message)

    def test_a_reply_cut_off_at_the_limit_is_marked(self):
        text = self.stream_with([
            {"id": "c1", "choices": [{"delta": {"content": "Half an answ"}}]},
            {"id": "c1", "choices": [{"delta": {}, "finish_reason": "length"}]}])
        self.assertTrue(text.endswith(CUT_OFF_NOTE))

    def test_a_whole_reply_is_left_as_it_is(self):
        text = self.stream_with([
            {"id": "c1", "choices": [{"delta": {"content": "Done."}}]},
            {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]}])
        self.assertEqual(text, "Done.")


if __name__ == "__main__":
    unittest.main()


class EveryWayAReplyEnds(CatalogueCase):
    """Only a reply that finished passes for a whole one; and when nothing came, the
    provider's own words are the error rather than "an empty response"."""

    def raw(self, lines, model="TEE/glm-5.3", **kwargs):
        client = LLMClient(self.config)
        client._post = lambda payload, stream, cancel=None: FakeResponse(lines)
        return client.stream(model, [{"role": "user", "content": "translate"}], **kwargs)

    @staticmethod
    def data(*chunks):
        return [b"data: " + json.dumps(c).encode() for c in chunks]

    def test_an_error_part_way_keeps_what_came_and_says_what_went_wrong(self):
        text = self.raw(self.data(
            {"id": "c1", "choices": [{"delta": {"content": "Der Hund"}}]},
            {"id": "c1", "error": {"code": 502, "message": "Provider disconnected"},
             "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]}))
        self.assertTrue(text.startswith("Der Hund"))
        self.assertIn("Cut off", text)
        self.assertIn("Provider disconnected", text)

    def test_an_error_before_any_text_is_the_providers_own_message(self):
        with self.assertRaises(LLMError) as caught:
            self.raw(self.data({"error": {"message": "Rate limit exceeded: free tier"}}))
        self.assertIn("Rate limit exceeded: free tier", str(caught.exception))

    def test_the_content_filter_is_named(self):
        text = self.raw(self.data(
            {"choices": [{"delta": {"content": "Half"}}]},
            {"choices": [{"delta": {}, "finish_reason": "content_filter"}]})
            + [b"data: [DONE]"])
        self.assertTrue(text.endswith(FILTERED_NOTE))
        with self.assertRaises(LLMError) as caught:
            self.raw(self.data({"choices": [{"delta": {},
                                             "finish_reason": "content_filter"}]}))
        self.assertIn("content filter", str(caught.exception))

    def test_a_stream_that_just_ends_is_marked_unfinished(self):
        text = self.raw(self.data({"choices": [{"delta": {"content": "Half a sente"}}]}))
        self.assertTrue(text.endswith(UNFINISHED_NOTE))

    def test_a_finish_reason_without_done_is_a_whole_reply(self):
        text = self.raw(self.data(
            {"choices": [{"delta": {"content": "Whole."}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}))
        self.assertEqual(text, "Whole.")

    def test_a_connection_that_breaks_keeps_what_came(self):
        import requests

        def lines():
            yield from self.data({"choices": [{"delta": {"content": "So far"}}]})
            raise requests.exceptions.ChunkedEncodingError("Connection broken")

        text = self.raw(lines())
        self.assertTrue(text.startswith("So far"))
        self.assertIn("connection broke", text)

    def test_an_error_sent_with_http_200_is_reported(self):
        with self.assertRaises(LLMError) as caught:
            self.raw([b'{"error": {"message": "Insufficient balance"}}'])
        self.assertIn("Insufficient balance", str(caught.exception))

    def test_a_server_that_does_not_stream_is_still_answered(self):
        body = json.dumps({"choices": [{"message": {"content": "Plain answer"},
                                        "finish_reason": "stop"}]}, indent=2)
        self.assertEqual(self.raw([line.encode() for line in body.splitlines()]),
                         "Plain answer")

    def test_a_page_that_is_not_an_answer_is_said_to_be_one(self):
        with self.assertRaises(LLMError) as caught:
            self.raw([b"<html><body>Captive portal</body></html>"])
        self.assertIn("not an answer", str(caught.exception))

    def test_content_in_parts_is_read_and_thinking_left_out(self):
        text = self.raw(self.data(
            {"choices": [{"delta": {"content": [{"type": "thinking", "text": "hmm"},
                                                {"type": "text", "text": "Answer"}]}}]},
            {"choices": [None]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}) + [b"data: [DONE]"])
        self.assertEqual(text, "Answer")

    def test_sse_comments_are_not_mistaken_for_a_body(self):
        text = self.raw([b": OPENROUTER PROCESSING", b"event: message"] + self.data(
            {"choices": [{"delta": {"content": "Fine"}, "finish_reason": "stop"}]})
            + [b"data: [DONE]"])
        self.assertEqual(text, "Fine")

    def test_a_completion_with_an_error_body_says_so(self):
        client = LLMClient(self.config)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"error": {"message": "Model is overloaded"}}
        client._post = lambda payload, stream, cancel=None: response
        with self.assertRaises(LLMError) as caught:
            client.complete("TEE/glm-5.3", [{"role": "user", "content": "hi"}])
        self.assertIn("Model is overloaded", str(caught.exception))

    def test_a_failed_web_search_is_said_in_the_answer(self):
        self.config.web_search_craft_query = False
        said = []
        with mock.patch.object(websearch, "run",
                               side_effect=websearch.SearchError("HTTP 503: busy")):
            text = self.raw(self.data(
                {"choices": [{"delta": {"content": "From memory."},
                              "finish_reason": "stop"}]}) + [b"data: [DONE]"],
                model="deepseek/deepseek-v4-pro", web_search=True, status=said.append)
        self.assertTrue(text.startswith("From memory."))
        self.assertIn("Not searched", text)
        self.assertIn("HTTP 503: busy", text)
        self.assertTrue(any("web search failed" in s for s in said))


class TheFirstRequestKnowsItsModel(unittest.TestCase):
    """The catalogue was fetched only at startup, once setup was complete; requests
    made after a key was first entered went without it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        for patch in (mock.patch.object(models, "CACHE_DIR", self.dir),
                      mock.patch.object(models, "CACHE_FILE",
                                        os.path.join(self.dir, "models.json")),
                      mock.patch.dict(models._asked, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_with_no_list_at_all_it_is_fetched_before_the_payload(self):
        def fetch(config, provider=None, timeout=None, force=False):
            models.save_cache([models.normalise(e) for e in CATALOGUE],
                              models.models_url(API_URL))
            return [], False

        with mock.patch.object(models, "fetch", side_effect=fetch) as fetched:
            payload = LLMClient(self.config)._payload(
                "TEE/glm-5.3", [{"role": "user", "content": "translate"}])
        fetched.assert_called_once()
        self.assertEqual(payload["reasoning"], {"effort": "low"})

    def test_a_list_already_there_is_not_fetched_again(self):
        models.save_cache([models.normalise(e) for e in CATALOGUE], models.models_url(API_URL))
        with mock.patch.object(models, "fetch") as fetched:
            models.ensure_listed(self.config, "TEE/glm-5.3")
        fetched.assert_not_called()

    def test_a_list_that_cannot_be_had_is_asked_for_once_a_minute(self):
        with mock.patch.object(models, "fetch", side_effect=RuntimeError("offline")) as fetched:
            models.ensure_listed(self.config, "TEE/glm-5.3")
            models.ensure_listed(self.config, "TEE/glm-5.3")
        self.assertEqual(fetched.call_count, 1)
