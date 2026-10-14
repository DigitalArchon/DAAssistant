"""Providers by name.

There used to be one API URL and one key, for every model. Now each of the three model
settings names its provider - NanoGPT, OpenRouter, an Ollama server, or another
OpenAI-compatible endpoint - so OCR can run on the machine next door while the answers
come from elsewhere. These guard what differs between them: where a request goes and
with what key, that only NanoGPT is sent a gzipped body (Ollama answers one with HTTP
400), how each one's model list is read, and that a private conversation still goes
nowhere but NanoGPT.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import llm, models, providers, websearch
from assistant.config import Config
from assistant.keychain import MemoryKeychain
from assistant.llm import LLMClient, LLMError

OLLAMA = "http://192.168.1.20:11434"

# As Ollama 0.40 lists them at /api/tags.
TAGS = {"models": [
    {"name": "glm-ocr:latest", "model": "glm-ocr:latest", "size": 2221706947,
     "details": {"family": "glm4", "parameter_size": "1.1B", "quantization_level": "F16",
                 "context_length": 131072},
     "capabilities": ["completion", "vision", "tools"]},
    {"name": "qwen3:1.7b", "model": "qwen3:1.7b", "size": 1359293444,
     "details": {"family": "qwen3", "parameter_size": "2.0B",
                 "quantization_level": "Q4_K_M", "context_length": 40960},
     "capabilities": ["completion", "tools", "thinking"]},
]}

# As OpenRouter lists them at /api/v1/models (Oct 2026): prices per token, as strings.
OPENROUTER = {"data": [
    {"id": "google/gemini-4-argon", "name": "Gemini 4 Argon", "context_length": 1000000,
     "architecture": {"input_modalities": ["text", "image"]},
     "pricing": {"prompt": "0.0000025", "completion": "0.0000075"},
     "top_provider": {"max_completion_tokens": 65536},
     "supported_parameters": ["max_tokens", "reasoning", "tools"],
     "reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"],
                   "default_effort": "medium"}},
    {"id": "acme/thinks-unless-told", "name": "Thinks", "context_length": 32000,
     "architecture": {"input_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"},
     "supported_parameters": ["max_tokens", "reasoning"],
     "reasoning": {"mandatory": False, "default_enabled": True,
                   "supported_efforts": ["high", "low", "none"]}},
    {"id": "acme/thinks-when-asked", "name": "Asked", "context_length": 32000,
     "architecture": {"input_modalities": ["text"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000002"},
     "supported_parameters": ["max_tokens"],
     "reasoning": {"mandatory": False, "default_enabled": False,
                   "supported_efforts": ["high", "low", "none"]}},
]}


def reply(body, status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = body
    return response


def answer(text="hello", finish="stop"):
    return reply({"choices": [{"message": {"content": text}, "finish_reason": finish}]})


def streamed(text, finish="stop", pieces=None):
    """The same reply as a stream, which is how a model that has to be watched is read."""
    response = mock.Mock(status_code=200)
    chunks = [{"id": "c1", "choices": [{"delta": {"content": piece}}]}
              for piece in (pieces or [text])]
    chunks.append({"id": "c1", "choices": [{"delta": {}, "finish_reason": finish}]})
    response.iter_lines.return_value = iter(
        [f"data: {json.dumps(chunk)}".encode() for chunk in chunks] + [b"data: [DONE]"])
    return response


PROSE = ("Quarterly maintenance report\n\nThe pump on line 4 was replaced on 12 February "
         "after two bearing failures in one week. Part number KX-2291-B.\nFlow returned to "
         "310 litres per minute within the hour.")


class Case(unittest.TestCase):
    """A config of its own, and a model cache of its own - never the user's."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, value in (("CACHE_DIR", self.dir),
                            ("CACHE_FILE", os.path.join(self.dir, "models.json"))):
            patch = mock.patch.object(models, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "sk-nano"
        self.config.ollama_url = OLLAMA
        self.config.vision_provider = "ollama"
        self.config.vision_model = "glm-ocr"

    def client(self, *responses):
        client = LLMClient(self.config)
        client.session = mock.Mock()
        client.session.post.side_effect = list(responses) or [answer()]
        return client

    @staticmethod
    def sent(client, call=0):
        """(url, headers, body as sent) of one of the client's requests."""
        args, kwargs = client.session.post.call_args_list[call]
        return args[0], kwargs["headers"], kwargs["data"]


class WhereAnAddressLeads(unittest.TestCase):
    def test_an_ollama_address_is_its_server_whatever_path_was_typed(self):
        kind = providers.KINDS["ollama"]
        for typed in ("http://192.168.1.20:11434", "http://192.168.1.20:11434/",
                      "http://192.168.1.20:11434/v1", "http://192.168.1.20:11434/api/chat",
                      "http://192.168.1.20:11434/v1/chat/completions"):
            self.assertEqual(providers.chat_url(kind, typed),
                             "http://192.168.1.20:11434/v1/chat/completions", typed)

    def test_another_endpoint_may_be_given_as_its_base_or_in_full(self):
        kind = providers.KINDS["custom"]
        for typed, used in (
                ("https://api.example.com", "https://api.example.com/v1/chat/completions"),
                ("https://api.example.com/v1", "https://api.example.com/v1/chat/completions"),
                ("https://api.example.com/v1/", "https://api.example.com/v1/chat/completions"),
                ("https://x.example/api/v1/chat/completions",
                 "https://x.example/api/v1/chat/completions"),
                ("http://localhost:1234/v1", "http://localhost:1234/v1/chat/completions")):
            self.assertEqual(providers.chat_url(kind, typed), used)

    def test_the_scheme_and_host_are_never_touched(self):
        """netguard judges an address as written: http to anywhere but the local
        network is refused, not turned into https."""
        config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        config.custom_url = "http://api.example.com/v1"
        found = providers.get(config, "custom").url_problem()
        self.assertIn("not https", found)
        config.custom_url = "api.example.com/v1"
        self.assertIn("Not a web address", providers.get(config, "custom").url_problem())

    def test_the_model_lists(self):
        config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        config.ollama_url, config.custom_url = OLLAMA + "/v1", "https://api.example.com/v1"
        self.assertEqual(providers.get(config, "ollama").models_url, OLLAMA + "/api/tags")
        self.assertEqual(providers.get(config, "custom").models_url,
                         "https://api.example.com/v1/models")
        self.assertEqual(providers.get(config, "openrouter").models_url,
                         "https://openrouter.ai/api/v1/models")
        self.assertEqual(providers.get(config, "nanogpt").models_url,
                         "https://nano-gpt.com/api/v1/models")

    def test_a_provider_nobody_knows_is_nanogpt(self):
        config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        config.text_provider = "somewhere-else"
        self.assertEqual(providers.role_provider(config, "text"), "nanogpt")
        self.assertEqual(providers.get(config, "somewhere-else").id, "nanogpt")


class WhichProviderAModelIsAskedOf(Case):
    def test_each_role_has_its_own(self):
        self.assertEqual(self.config.model("vision").provider, "ollama")
        self.assertEqual(self.config.model("text").provider, "nanogpt")
        self.assertEqual(self.config.model("vision"), "glm-ocr")        # still a str

    def test_a_plain_id_is_looked_up_among_the_model_settings(self):
        self.assertEqual(providers.provider_id(self.config, "glm-ocr"), "ollama")
        self.assertEqual(providers.provider_id(self.config, self.config.text_model),
                         "nanogpt")

    def test_anything_else_is_nanogpts(self):
        for model in ("TEE/glm-5.3", "private/glm-5-3", "never-heard-of-it", ""):
            self.assertEqual(providers.provider_id(self.config, model), "nanogpt", model)

    def test_a_model_keeps_the_provider_it_was_picked_from(self):
        picked = providers.Model("llava", "ollama")
        self.config.vision_provider = "nanogpt"
        self.assertEqual(providers.provider_id(self.config, picked), "ollama")

    def test_the_request_goes_where_its_model_does(self):
        client = self.client(streamed("read"), answer("answered"))
        client.complete(self.config.model("vision"), [{"role": "user", "content": "x"}])
        client.complete(self.config.model("text"), [{"role": "user", "content": "x"}])
        self.assertEqual(self.sent(client, 0)[0], OLLAMA + "/v1/chat/completions")
        self.assertEqual(self.sent(client, 1)[0],
                         "https://nano-gpt.com/api/v1/chat/completions")

    def test_each_provider_is_sent_its_own_key_and_ollama_none(self):
        self.config.openrouter_api_key = "sk-or"
        self.config.premium_text_provider = "openrouter"
        client = self.client(streamed("read"), answer(), answer())
        for role in ("vision", "text", "premium"):
            client.complete(self.config.model(role), [{"role": "user", "content": "x"}])
        self.assertNotIn("Authorization", self.sent(client, 0)[1])
        self.assertEqual(self.sent(client, 1)[1]["Authorization"], "Bearer sk-nano")
        self.assertEqual(self.sent(client, 2)[1]["Authorization"], "Bearer sk-or")
        self.assertEqual(self.sent(client, 2)[0],
                         "https://openrouter.ai/api/v1/chat/completions")

    def test_a_provider_with_no_key_is_said_so_and_nothing_is_sent(self):
        self.config.text_provider = "openrouter"
        client = self.client()
        with self.assertRaises(LLMError) as refused:
            client.complete(self.config.model("text"), [{"role": "user", "content": "x"}])
        self.assertIn("No API key is set for OpenRouter", str(refused.exception))
        client.session.post.assert_not_called()

    def test_another_endpoint_with_no_address_is_said_so(self):
        self.config.text_provider = "custom"
        client = self.client()
        with self.assertRaisesRegex(LLMError, "No address is set"):
            client.complete(self.config.model("text"), [{"role": "user", "content": "x"}])
        client.session.post.assert_not_called()

    def test_a_private_conversation_goes_to_nanogpt_whatever_the_settings_name(self):
        """Its models are TEE/ ones, NanoGPT's; a model setting pointing the same id at
        another provider does not get to carry the conversation there."""
        self.config.text_provider, self.config.text_model = "ollama", "TEE/glm-5.3"
        client = self.client()
        stream = mock.Mock(status_code=200)
        stream.iter_lines.return_value = [
            b'data: {"id": "c1", "choices": [{"delta": {"content": "hi"}}]}',
            b"data: [DONE]"]
        client.session.post.side_effect = [stream]
        session = mock.Mock()
        session.allows.return_value = True
        session.ensure.return_value = mock.Mock(headline="Attested")
        client.stream(self.config.model("text"), [{"role": "user", "content": "x"}],
                      privacy=session)
        self.assertEqual(self.sent(client)[0],
                         "https://nano-gpt.com/api/v1/chat/completions")
        self.assertEqual(self.sent(client)[1]["Authorization"], "Bearer sk-nano")


class WhatEachIsSent(Case):
    BIG = [{"role": "user", "content": "x" * (llm.GZIP_THRESHOLD_BYTES * 2)}]

    def test_only_nanogpt_is_sent_a_gzipped_body(self):
        """Ollama answers one with HTTP 400, "invalid character '\\x1f' looking for
        beginning of value" - and every capture is over the threshold."""
        client = self.client(streamed("read"), answer())
        client.complete(self.config.model("vision"), self.BIG)
        client.complete(self.config.model("text"), self.BIG)
        _url, headers, body = self.sent(client, 0)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(json.loads(body)["model"], "glm-ocr")
        self.assertEqual(self.sent(client, 1)[1]["Content-Encoding"], "gzip")

    def test_the_effort_is_sent_to_ollama(self):
        with mock.patch.object(models.requests, "get", return_value=reply(TAGS)):
            models.fetch(self.config, force=True, provider="ollama")
        self.config.reasoning_effort = "none"
        client = self.client()
        client.complete(providers.Model("qwen3:1.7b", "ollama"),
                        [{"role": "user", "content": "x"}])
        self.assertEqual(json.loads(self.sent(client)[2])["reasoning"], {"effort": "none"})


class AModelOllamaDoesNotStop(Case):
    """glm-ocr on Ollama 0.40.2 transcribes the image and carries on to the token limit:
    from `ollama run` as much as through the API, the full model as much as q8_0. What
    follows the transcription differs from one run to the next, so it is told where to
    stop for the endings that have a start, and watched for the one that has none."""

    def ask(self, client, model="glm-ocr"):
        return client.complete(providers.Model(model, "ollama"),
                               [{"role": "user", "content": "x"}])

    def test_it_is_told_where_to_stop_and_others_are_not(self):
        client = self.client(streamed("a"), streamed("b"), answer())
        for model in ("glm-ocr", "glm-ocr:q8_0", "qwen3:1.7b"):
            self.ask(client, model)
        self.assertIn("```markdown", json.loads(self.sent(client, 0)[2])["stop"])
        self.assertIn("```markdown", json.loads(self.sent(client, 1)[2])["stop"])
        self.assertNotIn("stop", json.loads(self.sent(client, 2)[2]))

    def test_no_stop_is_the_start_of_a_reply(self):
        """A bare fence would be: a screenshot of code is transcribed inside one, and
        stopping there returned nothing at all."""
        for _family, stops in providers.OLLAMA_RUNAWAYS:
            for stop in stops:
                for opening in ("```python\ncode", "<table><tr>", "| a | b |", "Plain"):
                    self.assertFalse(opening.startswith(stop), (stop, opening))

    def test_the_same_model_anywhere_else_is_left_alone(self):
        client = self.client()
        client.complete(providers.Model("glm-ocr", "nanogpt"),
                        [{"role": "user", "content": "x"}])
        body = json.loads(self.sent(client)[2])
        self.assertNotIn("stop", body)
        self.assertFalse(body["stream"])

    def test_it_is_read_as_a_stream_even_when_nothing_is_shown(self):
        """complete() is how a capture is transcribed, and only a stream can be cut
        short. No web search rides along with it."""
        self.config.web_search_enabled = True
        client = self.client(streamed("read"))
        self.assertEqual(self.ask(client), "read")
        self.assertTrue(json.loads(self.sent(client)[2])["stream"])
        client.session.post.assert_called_once()

    def test_a_code_block_the_stop_left_open_is_closed(self):
        """Ollama drops the stop from the reply, and for code the stop is the block's
        own closing fence followed by the one that should not be there."""
        client = self.client(streamed("```python\nprint(1)\n"), streamed("Just prose."))
        self.assertEqual(self.ask(client), "```python\nprint(1)\n```")
        self.assertEqual(self.ask(client), "Just prose.")

    def test_where_it_starts_over_it_is_cut_off_and_no_more_is_read(self):
        copies = [PROSE + "\n"] * 40
        response = streamed(None, finish="length", pieces=copies)
        lines = response.iter_lines.return_value
        client = self.client(response)
        self.assertEqual(self.ask(client), PROSE)
        self.assertGreater(len(list(lines)), 30, "it read on to the end")
        response.close.assert_called_once()         # which is what stops the model

    def test_the_copies_need_not_be_spaced_alike_or_joined_alike(self):
        other = PROSE.replace("after two bearing", "after two\nbearing")
        for between in ("\n", "\n---\n", "\n\n\n", "\n```markdown\n\n"):
            text = PROSE + between + other + between + PROSE[:60]
            cut = llm.repeat_start(text)
            self.assertIsNotNone(cut, between)
            self.assertEqual(llm.tidy_end(text[:cut]), PROSE, between)

    def test_saying_something_twice_is_not_starting_over(self):
        """A window's title is often its first heading too."""
        self.assertIsNone(llm.repeat_start(PROSE + "\n" + PROSE))
        title = "Quarterly maintenance report"
        self.assertIsNone(llm.repeat_start(
            f"{title}\n{title}\n\nThe pump on line 4 was replaced, and the report says "
            f"no more than that about it."))
        self.assertIsNone(llm.repeat_start("Short."))
        self.assertIsNone(llm.repeat_start(""))

    def test_one_line_said_over_and_over_is_kept_once(self):
        """Seen under a table it had read correctly: "NO TEXT FOUND", 660 times, three
        and a half minutes of it."""
        table = "| Item | Qty |\n| :--- | :--- |\n| Seal ring | 12 |\n"
        text = table + "NO TEXT FOUND\n" * llm.REPEAT_LINES
        self.assertEqual(text[:llm.runaway_start(text)], table + "NO TEXT FOUND")
        self.assertIsNone(llm.runaway_start(table + "NO TEXT FOUND\n" * 3))
        response = streamed(None, finish="length",
                            pieces=[table] + ["NO TEXT FOUND\n"] * 300)
        client = self.client(response)
        self.assertEqual(self.ask(client), table + "NO TEXT FOUND")
        self.assertGreater(len(list(response.iter_lines.return_value)), 200)

    def test_a_few_lines_said_round_and_round_are_kept_once(self):
        """Seen from glm-ocr:q8_0 under the same table: three lines, 79 times round,
        with a rule between the first round and the rest."""
        table = '<table border="1"><tr><td>Item</td><td>Qty</td></tr></table>\n'
        cycle = ("<p>PREVIOUS RESPONDING</p>\n<p>SUBMISSING VISUAL CONTENT</p>\n"
                 "<p>NO TEXT FOUND</p>\n")
        start = table + "<p>NO TEXT FOUND</p>\n" + cycle + "\n---\n"
        text = start + cycle * llm.REPEAT_CYCLES
        self.assertEqual(text[:llm.runaway_start(text)], start + cycle.rstrip("\n"))
        self.assertIsNone(llm.runaway_start(start + cycle * (llm.REPEAT_CYCLES - 1)))

    def test_rows_that_differ_are_not_lines_repeated(self):
        rows = "".join(f"| Seal ring | {n} |\n" for n in range(40))
        self.assertIsNone(llm.runaway_start(rows))
        pairs = "".join(f"Item {n}\n| ok |\n" for n in range(40))
        self.assertIsNone(llm.runaway_start(pairs))

    def test_a_finished_code_block_keeps_its_fence(self):
        self.assertEqual(llm.tidy_end("```python\nx = 1\n```"), "```python\nx = 1\n```")
        self.assertEqual(llm.tidy_end("| a |\n| b |\n"), "| a |\n| b |")


class TheModelLists(Case):
    def fetch(self, provider, *bodies):
        with mock.patch.object(models.requests, "get",
                               side_effect=[reply(b) for b in bodies]) as get:
            listed, cached = models.fetch(self.config, force=True, provider=provider)
        return listed, get

    def test_ollama_is_asked_for_its_own_list_which_says_what_sees(self):
        listed, get = self.fetch("ollama", TAGS)
        self.assertEqual(get.call_args[0][0], OLLAMA + "/api/tags")
        by_id = {m["id"]: m for m in listed}
        self.assertTrue(by_id["glm-ocr:latest"]["vision"])
        self.assertFalse(by_id["qwen3:1.7b"]["vision"])
        self.assertEqual(by_id["glm-ocr:latest"]["context_length"], 131072)
        self.assertEqual(models.format_price(by_id["qwen3:1.7b"]), "free in / free out")

    def test_a_model_is_found_with_or_without_latest(self):
        self.fetch("ollama", TAGS)
        self.assertTrue(models.accepts_images(self.config, self.config.model("vision")))
        self.assertTrue(models.accepts_images(
            self.config, providers.Model("glm-ocr:latest", "ollama")))
        self.assertFalse(models.accepts_images(
            self.config, providers.Model("qwen3:1.7b", "ollama")))

    def test_an_older_ollama_is_asked_about_each_model(self):
        """Before /api/tags carried capabilities, /api/show did."""
        bare = {"models": [{"name": "llava:13b", "details": {}}]}
        with mock.patch.object(models.requests, "post", return_value=reply(
                {"capabilities": ["completion", "vision"]})) as post:
            listed, _get = self.fetch("ollama", bare)
        self.assertEqual(post.call_args[0][0], OLLAMA + "/api/show")
        self.assertEqual(post.call_args[1]["json"], {"model": "llava:13b"})
        self.assertTrue(listed[0]["vision"])

    def test_openrouter_prices_are_per_token_and_shown_per_million(self):
        self.config.openrouter_api_key = "sk-or"
        listed, get = self.fetch("openrouter", OPENROUTER)
        self.assertEqual(get.call_args[0][0], "https://openrouter.ai/api/v1/models")
        self.assertEqual(get.call_args[1]["headers"], {"Authorization": "Bearer sk-or"})
        gemini = models.resolve(listed, "google/gemini-4-argon")
        self.assertEqual(models.format_price(gemini), "$2.50 in / $7.50 out")
        self.assertTrue(gemini["vision"])
        self.assertTrue(gemini["tool_calling"])
        self.assertEqual(gemini["efforts"], ["high", "medium", "low"])
        self.assertEqual(gemini["max_output_tokens"], 65536)

    def test_each_provider_has_a_cache_of_its_own(self):
        self.fetch("ollama", TAGS)
        self.fetch("nanogpt", {"data": [{"id": "a/b"}]}, {"data": []})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "models-ollama.json")))
        ollama, cached = models.fetch(self.config, provider="ollama")
        self.assertTrue(cached)
        self.assertEqual(len(ollama), 2)
        self.assertEqual([m["id"] for m in models.fetch(self.config)[0]], ["a/b"])

    def test_a_server_moved_is_not_answered_from_the_old_ones_list(self):
        self.fetch("ollama", TAGS)
        self.config.ollama_url = "http://192.168.1.99:11434"
        self.assertIsNone(models.accepts_images(self.config, self.config.model("vision")))

    def test_an_address_that_may_not_be_used_is_not_asked(self):
        self.config.custom_url = "http://api.example.com/v1"
        with mock.patch.object(models.requests, "get") as get:
            with self.assertRaisesRegex(RuntimeError, "not https"):
                models.fetch(self.config, force=True, provider="custom")
            get.assert_not_called()


class HowHardToThink(Case):
    """effort_for's rule was made for NanoGPT, whose list marks a model that thinks only
    when asked by "none" among its levels. Elsewhere "none" can also mean a model that
    thinks unless told not to, and the provider says which."""

    def setUp(self):
        super().setUp()
        self.config.openrouter_api_key = "sk-or"
        with mock.patch.object(models.requests, "get",
                               side_effect=[reply(TAGS), reply(OPENROUTER)]):
            models.fetch(self.config, force=True, provider="ollama")
            models.fetch(self.config, force=True, provider="openrouter")

    def effort(self, model, provider, wanted):
        self.config.reasoning_effort = wanted
        return models.effort_for(self.config, providers.Model(model, provider))

    def test_a_thinking_model_on_ollama_can_be_switched_off(self):
        """Measured on Ollama 0.40.2, qwen3:1.7b: 64s and 2,936 characters of thinking
        at its default; under a second with reasoning.effort "none"."""
        self.assertEqual(self.effort("qwen3:1.7b", "ollama", "none"), "none")
        self.assertEqual(self.effort("qwen3:1.7b", "ollama", "low"), "low")

    def test_one_that_does_not_think_is_asked_for_nothing(self):
        self.assertIsNone(self.effort("glm-ocr", "ollama", "low"))
        self.assertIsNone(self.effort("glm-ocr", "ollama", "none"))

    def test_openrouter_says_which_models_think_unless_told_not_to(self):
        self.assertEqual(self.effort("acme/thinks-unless-told", "openrouter", "low"), "low")
        self.assertIsNone(self.effort("acme/thinks-when-asked", "openrouter", "low"))
        self.assertEqual(self.effort("acme/thinks-when-asked", "openrouter", "none"), "none")
        self.assertEqual(self.effort("google/gemini-4-argon", "openrouter", "none"), "low")


class ASettingsFileFromBeforeProvidersHadNames(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")

    def load(self, on_disk, key="sk-old"):
        with open(self.path, "w") as f:
            json.dump(on_disk, f)
        keychain = MemoryKeychain()
        if key:
            keychain.set("api_key", key)
        return Config(self.path, keychain=keychain), keychain

    def stored(self):
        with open(self.path) as f:
            return json.load(f)

    def test_nanogpts_is_left_as_it_was(self):
        config, keychain = self.load({"api_url": providers.NANOGPT_URL, "max_tokens": 2048})
        self.assertEqual(config.text_provider, "nanogpt")
        self.assertEqual(config.api_key, "sk-old")
        self.assertNotIn("text_provider", self.stored())        # nothing was rewritten

    def test_an_endpoint_of_ones_own_becomes_the_other_endpoint_with_its_key(self):
        config, keychain = self.load({"api_url": "https://api.example.com/v1/chat/completions",
                                      "text_model": "their/model"})
        for key in ("text_provider", "premium_text_provider", "vision_provider"):
            self.assertEqual(getattr(config, key), "custom", key)
        self.assertEqual(config.custom_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(config.custom_api_key, "sk-old")
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.api_url, providers.NANOGPT_URL)
        self.assertEqual(keychain.get("custom_api_key"), "sk-old")
        self.assertIsNone(keychain.get("api_key"))
        stored = self.stored()
        self.assertEqual(stored["text_provider"], "custom")
        self.assertNotIn("sk-old", json.dumps(stored))          # still never in the file
        # and the request goes where it always did
        self.assertEqual(providers.of(config, config.model("text")).chat_url,
                         "https://api.example.com/v1/chat/completions")

    def test_an_ollama_server_is_recognised_by_its_port(self):
        config, _keychain = self.load(
            {"api_url": "http://192.168.1.20:11434/v1/chat/completions"}, key=None)
        self.assertEqual(config.vision_provider, "ollama")
        self.assertEqual(config.ollama_url, "http://192.168.1.20:11434")
        self.assertIsNone(providers.unready(config))            # no key wanted

    def test_it_is_done_once(self):
        self.load({"api_url": "https://api.example.com/v1/chat/completions"})
        again = Config(self.path, keychain=MemoryKeychain())
        again.text_provider = "nanogpt"
        again.save()
        self.assertEqual(Config(self.path, keychain=MemoryKeychain()).text_provider,
                         "nanogpt")


class WhatMayBeSavedAndUsed(Case):
    def test_only_the_providers_in_use_have_to_be_ready(self):
        self.assertIsNone(providers.unready(self.config))       # no OpenRouter key: unused
        self.config.premium_text_provider = "openrouter"
        self.assertIn("OpenRouter", providers.unready(self.config))

    def test_with_everything_on_ollama_no_key_is_wanted(self):
        self.config.api_key = ""
        self.assertIn("NanoGPT", providers.unready(self.config))
        for key in ("text_provider", "premium_text_provider"):
            setattr(self.config, key, "ollama")
        self.assertIsNone(providers.unready(self.config))
        self.config.privacy_mode = True                         # which is NanoGPT's
        self.assertIn("NanoGPT", providers.unready(self.config))

    def test_an_insecure_address_is_refused_even_before_it_is_used(self):
        self.assertIsNone(providers.refused(self.config))
        self.config.custom_url = "http://api.example.com/v1"
        self.assertIn("not https", providers.refused(self.config))

    def test_a_missing_key_does_not_stop_settings_being_saved(self):
        self.config.text_provider = "openrouter"
        self.assertIsNone(providers.refused(self.config))

    def test_the_web_search_is_nanogpts_and_says_so_without_its_key(self):
        """With an Ollama URL as the one API URL, the query used to be posted to
        nano-gpt.com with whatever key the other server had been given."""
        self.config.api_key = ""
        session = mock.Mock()
        with self.assertRaisesRegex(websearch.SearchError, "NanoGPT"):
            websearch.run("a query", self.config, session=session)
        session.post.assert_not_called()


class TheConnectionTest(Case):
    def test_it_looks_for_the_vision_model_on_its_own_provider(self):
        client = self.client(answer("ok"))
        with mock.patch.object(models.requests, "get", return_value=reply(TAGS)) as get:
            ok, message = client.test_connection()
        self.assertTrue(ok, message)
        self.assertEqual(get.call_args[0][0], OLLAMA + "/api/tags")
        self.assertIn("Ollama has glm-ocr", message)
        self.assertIn("NanoGPT", message)
        client.session.post.assert_called_once()        # the OCR model is not questioned

    def test_a_model_the_server_does_not_have_fails_it(self):
        self.config.vision_model = "llava"
        client = self.client(answer("ok"))
        with mock.patch.object(models.requests, "get", return_value=reply(TAGS)):
            ok, message = client.test_connection()
        self.assertFalse(ok)
        self.assertIn("does not list the vision model, llava", message)

    def test_a_server_that_is_not_there_fails_it(self):
        client = self.client(answer("ok"))
        with mock.patch.object(models.requests, "get", side_effect=models.requests.exceptions
                               .ConnectionError("refused")):
            ok, message = client.test_connection()
        self.assertFalse(ok)
        self.assertIn("Ollama", message)

    def test_one_provider_for_everything_reads_as_it_always_did(self):
        self.config.vision_provider = "nanogpt"
        ok, message = self.client(answer("ok")).test_connection()
        self.assertEqual((ok, message), (True, "Model replied: ok"))


class InTheWindows(Case):
    """The Settings dialog, the model browser and a conversation's model menu."""

    def setUp(self):
        super().setUp()
        from assistant import gtkcompat  # noqa: F401
        from gi.repository import Gtk
        self.Gtk = Gtk

    def settings(self):
        from assistant.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(None, self.config, LLMClient(self.config))
        self.addCleanup(dialog.destroy)
        return dialog

    def test_each_model_has_its_provider_beside_it(self):
        dialog = self.settings()
        combo, key = dialog.provider_combos["vision_model"]
        self.assertEqual((combo.get_active_id(), key), ("ollama", "vision_provider"))
        self.assertEqual(dialog.provider_combos["text_model"][0].get_active_id(), "nanogpt")
        combo.set_active_id("openrouter")
        values = dialog.values()
        self.assertEqual(values["vision_provider"], "openrouter")
        self.assertEqual(values["text_provider"], "nanogpt")

    def test_nanogpts_address_is_not_a_setting_to_type(self):
        dialog = self.settings()
        self.assertNotIn("api_url", dialog.entries)
        self.assertNotIn("api_url", dialog.values())
        for key in ("api_key", "openrouter_api_key", "ollama_url", "custom_url",
                    "custom_api_key"):
            self.assertIn(key, dialog.entries)
        for key in ("api_key", "openrouter_api_key", "custom_api_key"):
            self.assertFalse(dialog.entries[key].get_visibility(), key)

    def test_a_provider_chosen_without_its_address_holds_save(self):
        dialog = self.settings()
        self.assertTrue(dialog.save_button.get_sensitive())
        dialog.provider_combos["text_model"][0].set_active_id("custom")
        self.assertFalse(dialog.save_button.get_sensitive())
        self.assertIn("No address is set", dialog.url_warning.get_text())
        dialog.entries["custom_url"].set_text("https://api.example.com/v1")
        self.assertTrue(dialog.save_button.get_sensitive())

    def test_browsing_lists_the_provider_beside_the_model_with_what_is_typed(self):
        """An address typed a moment ago can be browsed with before it is saved, and a
        model picked from another provider's list brings that provider with it."""
        from assistant.ui import dialogs
        dialog = self.settings()
        dialog.entries["ollama_url"].set_text("http://192.168.1.77:11434")
        browser = mock.Mock(selected_id="llava:13b", provider="ollama")
        with mock.patch.object(dialogs, "ModelBrowser", return_value=browser) as opened:
            dialog._on_browse(None, "text_model", False)
        staged = opened.call_args[0][1]
        self.assertEqual(staged.ollama_url, "http://192.168.1.77:11434")
        self.assertEqual(self.config.ollama_url, OLLAMA)               # not the real one
        self.assertEqual(opened.call_args[1]["provider"], "nanogpt")
        self.assertEqual(dialog.entries["text_model"].get_text(), "llava:13b")
        self.assertEqual(dialog.provider_combos["text_model"][0].get_active_id(), "ollama")

    def browser(self, **kwargs):
        from assistant.ui.model_browser import ModelBrowser
        with mock.patch.object(ModelBrowser, "_load", lambda *a, **kw: None):  # no network
            browser = ModelBrowser(None, self.config, **kwargs)
        self.addCleanup(browser.destroy)
        return browser

    def test_a_list_that_does_not_say_what_sees_is_not_filtered_on_it(self):
        """A plain /v1/models has names and nothing else. Vision only would hide every
        model on the server, the right one among them."""
        browser = self.browser(vision_only=True, provider="custom")
        plain = [models.normalise({"id": name}) for name in ("llava", "phi")]
        browser._on_loaded(plain, False, "custom")
        self.assertFalse(browser.vision_check.get_active())
        self.assertEqual(len(browser.filtered), 2)
        self.assertIn("does not say", browser.status.get_text())

    def test_one_that_does_say_is(self):
        browser = self.browser(vision_only=True, provider="ollama")
        browser._on_loaded([models.normalise_ollama(tag) for tag in TAGS["models"]],
                           False, "ollama")
        self.assertTrue(browser.vision_check.get_active())
        self.assertEqual([row[0] for row in browser.filtered], ["glm-ocr:latest"])

    def test_a_list_that_arrives_for_a_provider_since_left_is_dropped(self):
        browser = self.browser(provider="ollama")
        browser._on_loaded([models.normalise({"id": "from/nanogpt"})], False, "nanogpt")
        self.assertEqual(len(browser.store), 0)

    def test_tee_models_are_browsed_at_nanogpt_and_nowhere_else(self):
        browser = self.browser(private_only=True, provider="ollama")
        self.assertEqual(browser.provider, "nanogpt")
        self.assertFalse(browser.provider_combo.get_sensitive())

    def window(self):
        from assistant.ui import chat
        app = mock.Mock(config=self.config, client=LLMClient(self.config))
        window = chat.ChatWindow(app, "Ask anything", [])
        self.addCleanup(window.destroy)
        return window

    def test_the_model_menu_carries_each_models_provider(self):
        window = self.window()
        choices = {key: model for key, _label, model in window._choices()}
        self.assertEqual(choices["vision"].provider, "ollama")
        self.assertEqual(choices["text"].provider, "nanogpt")
        self.assertEqual(window._where(choices["vision"]), " · Ollama")
        self.assertEqual(window._where(choices["text"]), "")

    def test_a_model_picked_from_another_provider_goes_to_that_provider(self):
        from assistant.ui import chat
        window = self.window()
        picked = []
        browser = mock.Mock(selected_id="qwen3:1.7b", provider="ollama")
        with mock.patch("assistant.ui.model_browser.ModelBrowser", return_value=browser):
            window._choose_other(picked.append)
        self.assertEqual(picked, ["model:ollama:qwen3:1.7b"])
        window._choose(picked[0])
        self.assertEqual(window.current_model, "qwen3:1.7b")
        self.assertEqual(providers.provider_id(self.config, window.current_model), "ollama")
        self.assertIn("Ollama", window.header.get_subtitle())
        # picked again, it is the same entry, not a second one
        with mock.patch("assistant.ui.model_browser.ModelBrowser", return_value=browser):
            window._choose_other(picked.append)
        self.assertEqual(len(window._chosen), 1)

    def test_picking_a_role_model_by_name_is_that_role(self):
        window = self.window()
        picked = []
        browser = mock.Mock(selected_id="glm-ocr", provider="ollama")
        with mock.patch("assistant.ui.model_browser.ModelBrowser", return_value=browser):
            window._choose_other(picked.append)
        self.assertEqual(picked, ["vision"])
        # the same name at another provider is another model
        browser = mock.Mock(selected_id="glm-ocr", provider="openrouter")
        with mock.patch("assistant.ui.model_browser.ModelBrowser", return_value=browser):
            window._choose_other(picked.append)
        self.assertEqual(picked[-1], "model:openrouter:glm-ocr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
