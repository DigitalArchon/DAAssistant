"""We run the search ourselves, against POST /api/web.

Handing the search to the chat endpoint via `:online` cost three rounds of debugging:
the query is derived server-side from the latest user message, so every instruction left
in that message became part of what was searched for, and the sources never come back -
a request that demonstrably searched (17,763 prompt tokens against 2,755 without)
carried none of the documented x_nanogpt_search_* fields.

These pin the two things that buys: the query is ours, and so are the citations.
"""

import gzip
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import websearch
from assistant.config import DEFAULTS
from assistant.llm import LLMClient


def a_config(**overrides):
    class Cfg:
        pass
    cfg = Cfg()
    for key, value in DEFAULTS.items():
        setattr(cfg, key, value)
    cfg.api_url = "https://nano-gpt.com/api/v1/chat/completions"
    cfg.api_key = "k"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class TheEndpoint(unittest.TestCase):
    def test_it_is_derived_from_the_configured_api_url(self):
        self.assertEqual(
            websearch.endpoint("https://nano-gpt.com/api/v1/chat/completions"),
            "https://nano-gpt.com/api/web")

    def test_a_self_hosted_prefix_is_preserved(self):
        self.assertEqual(websearch.endpoint("https://gw.internal/proxy/api/v1/chat/completions"),
                         "https://gw.internal/proxy/api/web")

    def test_an_unrecognisable_url_falls_back_rather_than_guessing(self):
        self.assertEqual(websearch.endpoint("https://example.test/v1/chat"),
                         "https://nano-gpt.com/api/web")


class TheQuery(unittest.TestCase):
    def test_it_comes_from_the_latest_user_message(self):
        self.assertEqual(websearch.query_from([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "what song is this?"},
        ]), "what song is this?")

    def test_the_text_of_a_message_that_also_carries_an_image(self):
        self.assertEqual(websearch.query_from([
            {"role": "user", "content": [{"type": "text", "text": "which song?"},
                                         {"type": "image_url", "image_url": {}}]},
        ]), "which song?")

    def test_a_long_message_is_cut_on_a_word_boundary(self):
        text = " ".join(["lyric"] * 200)
        query = websearch.query_from([{"role": "user", "content": text}])
        self.assertLessEqual(len(query), websearch.MAX_QUERY_CHARS)
        self.assertTrue(query.endswith("lyric"), "a query cut mid-word searches for a "
                                                 "word nobody wrote")

    def test_an_explicit_query_beats_reading_it_out_of_the_prompt(self):
        """The prompt carries labels the model needs and a search engine trips over.
        Measured live, `Can you identify this song? --- Text visible in the image: …`
        returned two on-target results out of five, the rest generic "identify any song"
        tools; the same lyrics without the scaffolding returned five out of five."""
        query = websearch.query_from([{
            "role": "user",
            "content": "Can you identify this song?\n\n---\nText visible in the image:"
                       "\n\nひたすら セイヤ ソイヤ",
            "search_query": "Can you identify this song? ひたすら セイヤ ソイヤ",
        }])
        self.assertEqual(query, "Can you identify this song? ひたすら セイヤ ソイヤ")
        self.assertNotIn("Text visible in the image", query)

    def test_the_prompts_own_separators_stay_out_of_the_fallback(self):
        query = websearch.query_from([
            {"role": "user", "content": "what does this mean?\n\n---\nsome text"}])
        self.assertEqual(query, "what does this mean? some text")

    def test_no_user_message_means_no_query(self):
        self.assertEqual(websearch.query_from([{"role": "assistant", "content": "hi"}]), "")


class TheRequest(unittest.TestCase):
    def test_the_provider_settings_come_along(self):
        body = websearch.params("q", a_config(web_search_provider="kagi"))
        self.assertEqual(body["provider"], "kagi")
        self.assertEqual(body["query"], "q")

    def test_kagi_always_gets_the_tier_that_actually_searches(self):
        """The other two 504 on every query, so they are not offered."""
        body = websearch.params("q", a_config(web_search_provider="kagi"))
        self.assertEqual(body["kagiSource"], "search")

    def test_a_withdrawn_provider_in_a_saved_config_falls_back(self):
        """Exa is gone, but it can still be sitting in someone's config.json."""
        body = websearch.params("q", a_config(web_search_provider="exa"))
        self.assertEqual(body["provider"], websearch.DEFAULT_PROVIDER)
        self.assertIn(body["provider"], websearch.PROVIDERS)

    def test_exa_is_not_offered(self):
        """It returns titles and URLs and no page text, at every depth it has."""
        self.assertNotIn("exa", websearch.PROVIDERS)

    def test_deep_mode_reaches_providers_that_have_one(self):
        body = websearch.params("q", a_config(web_search_provider="linkup",
                                              web_search_mode="deep"))
        self.assertEqual(body["depth"], "deep")

    def test_context_size_now_means_how_many_results(self):
        for size, count in (("low", 3), ("medium", 5), ("high", 8)):
            body = websearch.params("q", a_config(web_search_context_size=size))
            self.assertEqual(body["maxResults"], count)


class TheResults(unittest.TestCase):
    KAGI = {"data": [
        {"type": "text", "title": "KARATE 歌詞", "url": "https://uta-net.test/1",
         "content": "  ひたすら   セイヤ  "},
        {"type": "text", "title": "KARATE", "url": "https://uta-net.test/1"},
        {"title": "Another", "url": "https://other.test/2", "content": "x" * 5000},
    ]}

    def test_a_providers_payload_becomes_title_url_content(self):
        results = websearch.parse(self.KAGI)
        self.assertEqual(results[0]["title"], "KARATE 歌詞")
        self.assertEqual(results[0]["url"], "https://uta-net.test/1")
        self.assertEqual(results[0]["content"], "ひたすら セイヤ")

    def test_the_same_url_twice_is_kept_once(self):
        urls = [r["url"] for r in websearch.parse(self.KAGI)]
        self.assertEqual(len(urls), len(set(urls)))

    def test_page_text_fits_a_budget_shared_across_the_results(self):
        results = websearch.parse(self.KAGI)
        total = sum(len(r["content"]) for r in results)
        self.assertLessEqual(total, websearch.TOTAL_CONTEXT_CHARS)

    def test_a_short_result_keeps_all_of_its_text(self):
        """The budget is shared, not rationed: nothing is trimmed to fit a quota it
        was never going to reach."""
        body = {"data": [{"title": "T", "url": "https://a.test", "content": "short"},
                         {"title": "U", "url": "https://b.test", "content": "x" * 50000}]}
        results = websearch.parse(body, limit=2)
        self.assertEqual(results[0]["content"], "short")

    def test_one_long_page_does_not_eat_the_whole_budget(self):
        body = {"data": [{"title": chr(65 + i), "url": f"https://{i}.test",
                          "content": "word " * 5000} for i in range(4)]}
        results = websearch.parse(body, limit=4)
        lengths = [len(r["content"]) for r in results]
        self.assertLessEqual(sum(lengths), websearch.TOTAL_CONTEXT_CHARS)
        self.assertEqual(len(set(lengths)), 1, "equal claims get equal shares")

    def test_fewer_results_means_deeper_text(self):
        body = {"data": [{"title": chr(65 + i), "url": f"https://{i}.test",
                          "content": "word " * 5000} for i in range(8)]}
        three = websearch.parse(body, limit=3)
        eight = websearch.parse(body, limit=8)
        self.assertGreater(len(three[0]["content"]), len(eight[0]["content"]))

    def test_markup_is_removed_before_the_budget_is_spent(self):
        """One provider sends whole raw HTML documents. Truncating those fed the model
        the contents of <head> and threw the article away."""
        page = ("<!DOCTYPE html><html><head><title>t</title>"
                "<script>var x=1;</script></head><body><nav>Home</nav>"
                "<p>The WinRE partition needs <strong>250 MB</strong> free "
                "&amp; a reboot.</p></body></html>")
        results = websearch.parse({"data": [{"title": "T", "url": "https://a.test",
                                             "content": page}]}, limit=1)
        text = results[0]["content"]
        self.assertNotIn("<", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("&amp;", text)
        self.assertIn("250 MB", text)
        self.assertIn("& a reboot", text)

    def test_results_nested_under_a_key_are_found_too(self):
        # Providers disagree about where the list lives; the shape is not ours to fix.
        results = websearch.parse({"data": {"results": [
            {"title": "T", "url": "https://a.test", "snippet": "s"}]}})
        self.assertEqual(results[0]["url"], "https://a.test")

    def test_entries_without_a_url_are_dropped(self):
        self.assertEqual(websearch.parse({"data": [{"title": "no link"}]}), [])

    def test_the_context_block_names_every_source(self):
        block = websearch.as_context("karate lyrics", websearch.parse(self.KAGI))
        self.assertIn('Web search results for "karate lyrics"', block)
        self.assertIn("## KARATE 歌詞", block)
        self.assertIn("URL: https://uta-net.test/1", block)


class TheCraftedQuery(unittest.TestCase):
    """Pasting the turn into a search box is how a stack trace finds nothing.

    Measured live: searching `What is going wrong here? Traceback (most recent call
    last): File "/srv/app/workers/ingest.py", line 142 …` returned HTTP 504, no usable
    results; the crafted `psycopg2.errors.DeadlockDetected deadlock detected` found the
    answer.
    """

    TURN = [{"role": "user", "content": "What is going wrong here?\n\n---\n"
                                        "psycopg2.errors.DeadlockDetected: deadlock"}]

    def _client(self, answer, **settings):
        client = LLMClient(a_config(**settings))
        self.asked = {}

        def complete(model, messages, **kwargs):
            self.asked = {"model": model, "content": messages[0]["content"],
                          "max_tokens": kwargs.get("max_tokens")}
            if isinstance(answer, Exception):
                raise answer
            return answer

        client.complete = complete
        client_search = []
        client.session = None
        return client

    def test_the_crafted_query_is_what_gets_searched(self):
        client = self._client("psycopg2.errors.DeadlockDetected deadlock detected")
        captured = {}
        websearch_run = websearch.run
        websearch.run = lambda q, *a, **k: captured.setdefault("query", q) and []
        try:
            client.search(self.TURN, web_search=True)
        finally:
            websearch.run = websearch_run
        self.assertEqual(captured["query"],
                         "psycopg2.errors.DeadlockDetected deadlock detected")

    def test_it_reads_the_material_not_the_trimmed_query(self):
        client = self._client("a query")
        client.craft_query(websearch.material_from(self.TURN))
        self.assertIn("psycopg2.errors.DeadlockDetected", self.asked["content"])
        self.assertIn("Write one web search query", self.asked["content"])

    def test_it_is_a_cheap_short_call(self):
        client = self._client("a query")
        client.craft_query("material")
        self.assertEqual(self.asked["model"], a_config().text_model)
        self.assertEqual(self.asked["max_tokens"], 120)

    def test_an_explanation_instead_of_a_query_is_refused(self):
        client = self._client("Sure! Here is a good query: " + "x" * 500)
        self.assertIsNone(client.craft_query("material"))

    def test_wrapping_quotes_and_extra_lines_are_stripped(self):
        # Quotes around the whole answer are the model presenting its query, not a
        # phrase to match word for word.
        client = self._client('"deadlock detected psycopg2"\nHope this helps!')
        self.assertEqual(client.craft_query("m"), "deadlock detected psycopg2")

    def test_a_stray_quote_is_removed_but_a_quoted_phrase_is_kept(self):
        """Seen live: the model answered `"0x80070643" KB5034441 Windows 11`, and the
        old stripping left `0x80070643" KB5034441` - a search for something nobody
        wrote. A balanced pair is how a search engine is told to match words together,
        so that stays."""
        client = self._client('"0x80070643 KB5034441 Windows 11')
        self.assertEqual(client.craft_query("m"), "0x80070643 KB5034441 Windows 11")

        client = self._client('"deadlock detected" psycopg2')
        self.assertEqual(client.craft_query("m"), '"deadlock detected" psycopg2')

    def test_a_failure_leaves_the_mechanical_query(self):
        from assistant.llm import LLMError
        client = self._client(LLMError("provider down"))
        self.assertIsNone(client.craft_query("material"))

    def test_it_can_be_turned_off(self):
        client = self._client("crafted", web_search_craft_query=False)
        captured = {}
        websearch_run = websearch.run
        websearch.run = lambda q, *a, **k: captured.setdefault("query", q) and []
        try:
            client.search(self.TURN, web_search=True)
        finally:
            websearch.run = websearch_run
        self.assertIn("What is going wrong here?", captured["query"])
        self.assertEqual(self.asked, {}, "no crafting call should have been made")


class TheRequestBody(unittest.TestCase):
    """What reaches the chat endpoint once the search has run."""

    def setUp(self):
        self.client = LLMClient(a_config())

    def _payload(self, **kwargs):
        return self.client._payload("anthropic/claude-sonnet-5",
                                    [{"role": "user", "content": "q"}], **kwargs)

    def test_results_are_context_for_the_model_not_words_from_the_user(self):
        payload = self._payload(system="Answer in English.",
                                search_context="## A\nURL: https://a.test\nbody")
        # Instructions at the front, the user's own words untouched in between.
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("Answer in English.", payload["messages"][0]["content"])
        self.assertNotIn("https://a.test", payload["messages"][0]["content"])
        user = [m for m in payload["messages"] if m["role"] == "user"]
        self.assertEqual(user[-1]["content"], "q")

    def test_results_go_last_so_the_rest_of_the_request_can_be_cached(self):
        """A fresh search runs every turn, so the results are the one part that always
        differs. Anything after them would be uncacheable; there is nothing after."""
        payload = self._payload(system="Answer in English.",
                                search_context="## A\nURL: https://a.test\nbody")
        last = payload["messages"][-1]
        self.assertEqual(last["role"], "system")
        self.assertIn("https://a.test", last["content"])

    def test_no_trailing_block_when_nothing_was_found(self):
        payload = self._payload(system="Answer in English.", search_context="")
        self.assertEqual(payload["messages"][-1]["role"], "user")

    def test_the_model_name_is_left_alone(self):
        # The suffix is what made the chat endpoint run its own search on a query we
        # did not choose. Nothing should be appending it any more.
        self.assertNotIn(":online", self._payload()["model"])

    def test_prompt_caching_is_not_requested(self):
        """Measured, it is a 24% surcharge here: the provider breaks the cache at the
        end of the whole prompt, so only a byte-identical repeat ever reads one back,
        and a conversation never sends the same request twice."""
        self.assertNotIn("promptCaching", self._payload())
        self.assertNotIn("promptCaching",
                         self.client._payload("deepseek/deepseek-v4-pro",
                                              [{"role": "user", "content": "q"}]))


class TheUpload(unittest.TestCase):
    def setUp(self):
        self.client = LLMClient(a_config())

    def test_a_small_body_is_sent_as_it_is(self):
        body, headers = self.client._encode({"model": "m", "messages": []},
                                            self.client._provider("m"))
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(json.loads(body)["model"], "m")

    def test_a_capture_sized_body_is_compressed(self):
        payload = {"model": "m", "image": "A" * 200_000}
        body, headers = self.client._encode(payload, self.client._provider("m"))
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertLess(len(body), 100_000, "gzip bought nothing")
        self.assertEqual(json.loads(gzip.decompress(body))["model"], "m")


if __name__ == "__main__":
    unittest.main()
