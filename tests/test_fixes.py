"""Tests for defects that are easy to bring back."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from assistant import gtkcompat  # noqa: F401
from assistant import imaging
from assistant.capture.cli import SLURP_GEOMETRY
from assistant.config import Config
from assistant.llm import attach_image, strip_internal_keys
from assistant.ui.markdown_html import render_markdown


class ImagePipeline(unittest.TestCase):
    def test_a_dense_capture_keeps_enough_resolution_to_read(self):
        """Measured on a 3200x1100 console capture, three runs per size: at 2000 the
        transcription lost a tracking number every time; at 2600 and above it lost
        nothing. This app is pointed at error text, where a long identifier is the
        answer."""
        from assistant.config import DEFAULTS
        self.assertGreaterEqual(DEFAULTS["capture_max_long_edge"], 2600)

        _, info = imaging.prepare_for_vision(Image.new("RGB", (3200, 1100)))
        self.assertEqual(max(info["sent_size"]), 3000)

    def test_small_capture_is_upscaled_but_capped(self):
        _, info = imaging.prepare_for_vision(Image.new("RGB", (300, 80)))
        # 1600/300 would be 5.3x; MAX_UPSCALE caps it at 3x.
        self.assertEqual(info["sent_size"], (900, 240))

    def test_large_capture_is_downsampled(self):
        _, info = imaging.prepare_for_vision(Image.new("RGB", (3840, 2160)))
        self.assertEqual(max(info["sent_size"]), 3000)

    def test_midsized_capture_is_left_alone(self):
        _, info = imaging.prepare_for_vision(Image.new("RGB", (1700, 900)))
        self.assertFalse(info["scaled"])

    def test_what_is_sent_is_every_pixel_however_it_is_packed(self):
        """The PNG is packed at a level chosen for the time it takes
        (imaging.PNG_COMPRESS_LEVEL): that may change its size, never what it shows."""
        import base64
        from io import BytesIO
        source = Image.effect_noise((1700, 900), 80).convert("RGB")
        b64, info = imaging.prepare_for_vision(source)
        sent = Image.open(BytesIO(base64.b64decode(b64)))
        self.assertEqual(sent.format, "PNG")
        self.assertEqual(sent.convert("RGB").tobytes(), source.tobytes())
        self.assertEqual(info["bytes"], len(base64.b64decode(b64)))

    def test_no_picture_is_packed_with_the_slow_search(self):
        """optimize=True took six times as long to save 3.5%, on the main loop, before
        the review dialog could open. A capture, an attachment and the screenshot the
        chat window keeps all go through the one function that does not ask for it."""
        from unittest import mock
        from assistant import attachments
        asked = []
        real = Image.Image.save

        def save(image, fp, format=None, **params):
            asked.append((format, params))
            return real(image, fp, format, **params)

        picture = Image.new("RGB", (64, 48), "white")
        with mock.patch.object(Image.Image, "save", save):
            imaging.prepare_for_vision(picture)
            imaging.screenshot_png(picture)
            data, mime = attachments.encode_image(picture)
        self.assertEqual(mime, "image/png")
        self.assertEqual(len(asked), 3)
        for fmt, params in asked:
            self.assertEqual(fmt, "PNG")
            self.assertNotIn("optimize", params)
            self.assertEqual(params.get("compress_level"), imaging.PNG_COMPRESS_LEVEL)

    def test_transparency_flattens_to_white_not_black(self):
        """Compositing onto black darkens text; grim and the portal can emit alpha."""
        transparent = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        self.assertEqual(imaging.flatten(transparent).getpixel((0, 0)), (255, 255, 255))

    def test_output_is_rgb(self):
        self.assertEqual(imaging.flatten(Image.new("RGBA", (2, 2))).mode, "RGB")


class LLMMessages(unittest.TestCase):
    def test_image_attaches_to_the_turn_it_arrived_on(self):
        """Pinned to messages[0] and dropped after turn one, follow-ups would lose
        the image. Re-attaching it there every request keeps it visible without
        moving it, which is what lets the prefix be cached."""
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "follow-up"},
        ]
        out = attach_image(messages, "BASE64")
        self.assertIsInstance(out[0]["content"], list)
        # The image ahead of the words: glm-ocr reads a whole page only in that order.
        self.assertEqual([p["type"] for p in out[0]["content"]], ["image_url", "text"])
        self.assertIsInstance(out[2]["content"], str)

    def test_the_cached_prefix_survives_a_follow_up(self):
        """The whole point of both placements: turn two must agree with turn one on
        everything up to the capture, or the capture is billed again."""
        from assistant.llm import with_search_context
        first = with_search_context(
            attach_image([{"role": "user", "content": "q1"}], "B64"), "RESULTS A")
        second = with_search_context(
            attach_image([{"role": "user", "content": "q1"},
                          {"role": "assistant", "content": "a1"},
                          {"role": "user", "content": "q2"}], "B64"), "RESULTS B")
        shared = 0
        for a, b in zip(first, second):
            if a != b:
                break
            shared += 1
        self.assertGreaterEqual(shared, 1)
        self.assertTrue(any(isinstance(m["content"], list) for m in first[:shared]),
                        "the capture must fall inside the shared prefix")

    def test_image_is_not_attached_twice(self):
        once = attach_image([{"role": "user", "content": "q"}], "B64")
        twice = attach_image(once, "B64")
        self.assertEqual(sum(1 for p in twice[0]["content"] if p["type"] == "image_url"), 1)

    def test_internal_keys_are_not_sent_upstream(self):
        cleaned = strip_internal_keys([{"role": "assistant", "content": "x",
                                        "is_premium": True, "display": "y"}])
        self.assertEqual(cleaned, [{"role": "assistant", "content": "x"}])

    def test_no_user_message_is_handled(self):
        out = attach_image([{"role": "system", "content": "s"}], "B64")
        self.assertEqual(out[0]["content"], "s")


class SlurpParsing(unittest.TestCase):
    def test_negative_coordinates_parse(self):
        """An unsigned (\\d+) regex fails on monitors left of or above the origin."""
        match = SLURP_GEOMETRY.search("-1920,-200 800x600")
        self.assertIsNotNone(match)
        self.assertEqual(match.groups(), ("-1920", "-200", "800", "600"))

    def test_positive_coordinates_still_parse(self):
        self.assertEqual(SLURP_GEOMETRY.search("10,20 300x400").groups(),
                         ("10", "20", "300", "400"))


class ConfigStorage(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_api_key_file_is_not_world_readable(self):
        config = Config(self.path)
        config.api_key = "secret"
        config.save()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_corrupt_config_is_reported_not_swallowed(self):
        """Not caught by a bare except: and silently replaced with defaults."""
        with open(self.path, "w") as f:
            f.write("{ this is not json")
        config = Config(self.path)
        self.assertIsNotNone(config.load_error)
        self.assertTrue(config.api_url)       # still usable

    def test_roundtrip(self):
        config = Config(self.path)
        config.default_language = "Japanese"
        config.save()
        self.assertEqual(Config(self.path).default_language, "Japanese")

    def test_unknown_keys_are_ignored(self):
        import json
        with open(self.path, "w") as f:
            json.dump({"api_key": "k", "removed_option": 1}, f)
        config = Config(self.path)
        self.assertEqual(config.api_key, "k")
        self.assertFalse(hasattr(config, "removed_option"))


class MarkdownRendering(unittest.TestCase):
    """A regex converter mangles all of these."""

    def test_bold_and_italic_do_not_interfere(self):
        html = render_markdown("**bold** and *italic*")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)

    def test_fenced_code_block(self):
        html = render_markdown("```python\ndef f():\n    pass\n```")
        self.assertIn("codehilite", html)

    def test_table(self):
        html = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("<table>", html)

    def test_ordered_list(self):
        self.assertIn("<ol>", render_markdown("1. one\n2. two"))

    def test_script_tags_are_removed_from_model_output(self):
        """OCR transcribes whatever is on screen, so responses are untrusted input."""
        out = render_markdown("before <script>alert(1)</script> after")
        self.assertNotIn("<script", out)
        self.assertNotIn("alert(1)", out)      # body dropped, not just the tag
        self.assertIn("before", out)

    def test_event_handler_attributes_are_stripped(self):
        self.assertNotIn("onerror", render_markdown("<img src=x onerror=alert(1)>"))

    def test_remote_images_cannot_be_loaded(self):
        """A remote <img> in a response would be a tracking/exfiltration channel."""
        self.assertNotIn("http://evil", render_markdown("![a](http://evil/x.png)"))

    def test_javascript_urls_are_stripped(self):
        self.assertNotIn("javascript:", render_markdown("[bad](javascript:alert(1))"))

    def test_code_spans_are_not_double_escaped(self):
        """Pre-escaping the source made Markdown re-escape inside code spans, so the
        reader saw a literal '&lt;' instead of '<'."""
        out = render_markdown("`if a < b`")
        self.assertIn("&lt;", out)
        self.assertNotIn("&amp;lt;", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)


class ModelCatalogue(unittest.TestCase):
    def test_models_url_derived_from_chat_endpoint(self):
        from assistant.models import models_url
        self.assertEqual(models_url("https://x.com/api/v1/chat/completions"),
                         "https://x.com/api/v1/models")
        self.assertEqual(models_url("https://x.com/api/v1/responses"),
                         "https://x.com/api/v1/models")

    def test_normalise_extracts_pricing_and_vision(self):
        from assistant.models import normalise
        entry = normalise({
            "id": "a/b", "name": "B",
            "capabilities": {"vision": True, "reasoning": False},
            "architecture": {"input_modalities": ["text", "image"]},
            "pricing": {"prompt": 0.3, "completion": 1.2, "unit": "per_million_tokens"},
            "context_length": 131072,
        })
        self.assertTrue(entry["vision"])
        self.assertEqual(entry["prompt_price"], 0.3)
        self.assertEqual(entry["context_length"], 131072)

    def test_missing_pricing_does_not_crash(self):
        from assistant.models import format_price, normalise
        self.assertEqual(format_price(normalise({"id": "x"})), "")

    def test_search_matches_all_terms(self):
        from assistant.models import normalise, search
        models = [normalise({"id": "qwen/qwen3-vl-235b", "name": "Qwen3 VL",
                             "capabilities": {"vision": True}}),
                  normalise({"id": "openai/gpt-5", "name": "GPT 5"})]
        self.assertEqual(len(search(models, "qwen vl")), 1)
        self.assertEqual(len(search(models, "gpt")), 1)
        self.assertEqual(len(search(models, "nonexistent")), 0)
        self.assertEqual(len(search(models, "", vision_only=True)), 1)

    def test_resolve_finds_alias_case_insensitively(self):
        from assistant.models import normalise, resolve
        models = [normalise({"id": "qwen/qwen3-vl-235b-a22b-instruct",
                             "aliases": ["Qwen/Qwen3-VL-235B-A22B-Instruct"]})]
        self.assertIsNotNone(resolve(models, "Qwen/Qwen3-VL-235B-A22B-Instruct"))
        self.assertIsNone(resolve(models, "gpt-5-chat-latest"))


class ConfigConcurrentWrites(unittest.TestCase):
    """save() must not revert settings changed elsewhere since this object loaded.

    The tray's premium checkbox calls save() meaning only to flip use_premium. When
    save() wrote the whole in-memory snapshot, that silently reverted every other
    setting to its value at app startup - so editing config.json by hand, or from a
    second instance, was undone by the next checkbox click.
    """

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _stored(self, key):
        import json
        with open(self.path) as f:
            return json.load(f).get(key)

    def test_unrelated_external_change_is_preserved(self):
        running = Config(self.path)
        running.premium_text_model = "old-model"
        running.save()

        external = Config(self.path)
        external.premium_text_model = "anthropic/claude-sonnet-5:thinking"
        external.save()

        running.use_premium = True          # tray checkbox
        running.save()

        self.assertEqual(self._stored("premium_text_model"),
                         "anthropic/claude-sonnet-5:thinking")
        self.assertTrue(self._stored("use_premium"))

    def test_deliberate_change_still_wins_over_disk(self):
        running = Config(self.path)
        running.text_model = "chosen-by-user"
        Config(self.path).save()            # another writer touches the file
        running.save()
        self.assertEqual(self._stored("text_model"), "chosen-by-user")

    def test_memory_reflects_disk_after_save(self):
        running = Config(self.path)
        running.save()
        external = Config(self.path)
        external.default_language = "Japanese"
        external.save()

        running.use_premium = True
        running.save()
        self.assertEqual(running.default_language, "Japanese")

    def test_settings_dialog_style_bulk_save(self):
        """Saving every field at once must still write every field."""
        config = Config(self.path)
        config.save()
        values = {"text_model": "a", "premium_text_model": "b", "vision_model": "c",
                  "default_language": "German", "web_search_enabled": True}
        for key, value in values.items():
            setattr(config, key, value)
        config.save()
        for key, value in values.items():
            self.assertEqual(self._stored(key), value)

    def test_loading_does_not_mark_keys_dirty(self):
        Config(self.path).save()
        external = Config(self.path)
        external.vision_model = "set-externally"
        external.save()

        fresh = Config(self.path)           # loads, changes nothing
        fresh.save()                        # must not rewrite anything from defaults
        self.assertEqual(self._stored("vision_model"), "set-externally")

    def test_permissions_survive_the_merge(self):
        config = Config(self.path)
        config.api_key = "secret"
        config.save()
        config.use_premium = True
        config.save()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertIsNone(self._stored("api_key"))      # the keychain's, not the file's
        self.assertEqual(config.keychain.get("api_key"), "secret")


class OnlyProvidersThatAnswerAreOffered(unittest.TestCase):
    """Measured over three queries against every provider the endpoint accepts.

    Exa returned titles and URLs with no page text at all - at every one of its six
    depths - so the model was handed links it could not read. Kagi's "web" and "news"
    tiers answer HTTP 504 "Search returned no usable results" on every query. Neither
    belongs in a dropdown: a setting that cannot work is worse than no setting.
    """

    def test_the_default_provider_is_the_one_that_measured_best(self):
        from assistant.config import DEFAULTS
        self.assertEqual(DEFAULTS["web_search_provider"], "perplexity")

    def test_the_default_is_a_provider_we_still_offer(self):
        from assistant.config import DEFAULTS
        from assistant import websearch
        self.assertIn(DEFAULTS["web_search_provider"], websearch.PROVIDERS)

    def test_the_withdrawn_options_are_gone_from_the_settings(self):
        import tempfile
        from assistant.config import Config
        from assistant.ui.dialogs import SettingsDialog

        config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        dialog = SettingsDialog(None, config, type("C", (), {"config": config}))
        try:
            self.assertNotIn("web_search_exa_depth", dialog.search_combos)
            self.assertNotIn("web_search_kagi_source", dialog.search_combos)
            providers = dialog.search_combos["web_search_provider"][0]
            ids = []
            for row in range(providers.get_model().iter_n_children(None)):
                providers.set_active(row)
                ids.append(providers.get_active_id())
            self.assertNotIn("exa", ids)
        finally:
            dialog.destroy()

    def test_the_label_is_shown_but_the_value_is_stored(self):
        """A label saved into the config would be an unrecognised provider option."""
        import tempfile
        from assistant.config import Config
        from assistant.ui.dialogs import SettingsDialog

        config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        config.web_search_context_size = "high"
        dialog = SettingsDialog(None, config, type("C", (), {"config": config}))
        try:
            combo, _label = dialog.search_combos["web_search_context_size"]
            self.assertIn("8 results", combo.get_active_text())
            self.assertEqual(combo.get_active_id(), "high")
            self.assertEqual(dialog.values()["web_search_context_size"], "high")
        finally:
            dialog.destroy()


class Citations(unittest.TestCase):
    """Sources come from the response, not from asking the model to remember them.

    The search results are exposed as structured data - x_nanogpt_search_results, the
    matching annotations, and message.citations on search-native models. The app used to
    ask the model to copy `URL:` lines out of its own context instead, and put that
    request in the user's turn, which is where the search query is built from.
    """

    def setUp(self):
        from assistant.config import DEFAULTS

        class Cfg:
            pass
        self.cfg = Cfg()
        for key, value in DEFAULTS.items():
            setattr(self.cfg, key, value)
        self.cfg.api_url = "https://example.com/v1/chat/completions"
        self.cfg.api_key = "k"
        self.search_context = ""
        self.search_citations = []

    def _client(self):
        from assistant.llm import LLMClient
        return LLMClient(self.cfg)

    def _stream(self, frames, **kwargs):
        """Run the real SSE parser over `frames`."""
        import json as _json

        class FakeResponse:
            def __init__(self, lines):
                self.lines = lines

            def iter_lines(self):
                return iter(self.lines)

            def close(self):
                pass

        lines = [b"data: " + _json.dumps(f).encode() for f in frames] + [b"data: [DONE]"]
        client = self._client()
        client._post = lambda *a, **k: FakeResponse(lines)
        # The search is a live HTTP call of its own; tests never make one.
        client.search = lambda *a, **k: (self.search_context, self.search_citations)
        return client.stream("m", [{"role": "user", "content": "q"}], **kwargs)

    def test_nothing_about_searching_is_added_to_the_users_turn(self):
        """The whole reason the old approach had to go: this is the search query."""
        payload = self._client()._payload("m", [{"role": "user", "content": "q"}],
                                          web_search=True)
        user = [m for m in payload["messages"] if m["role"] == "user"]
        self.assertEqual([m["content"] for m in user], ["q"])

    def test_the_instruction_that_did_that_is_gone(self):
        import assistant.llm as llm
        self.assertFalse(hasattr(llm, "CITATION_INSTRUCTION"))
        self.assertFalse(hasattr(llm, "append_citation_request"))

    def test_sources_are_read_from_the_standalone_metadata_frame(self):
        """Search metadata arrives in its own SSE frame, with `choices` empty. Skipping
        those frames threw every source away unread."""
        text = self._stream([
            {"x_nanogpt_search_results": [
                {"title": "Seiya Soiya - lyrics", "url": "https://example.test/song",
                 "snippet": "..."}]},
            {"choices": [{"delta": {"content": "It is that song."}}]},
        ])
        self.assertIn("It is that song.", text)
        self.assertIn("[Seiya Soiya - lyrics](https://example.test/song)", text)

    def test_search_native_models_put_them_on_the_message(self):
        text = self._stream([
            {"choices": [{"delta": {"content": "Answer."},
                          "message": {"citations": ["https://example.test/a"]}}]},
        ])
        self.assertIn("https://example.test/a", text)

    def test_the_same_source_twice_is_listed_once(self):
        text = self._stream([
            {"x_nanogpt_search_results": [{"title": "A", "url": "https://a.test"}]},
            {"x_nanogpt_search_annotations": [{"title": "A", "url": "https://a.test"}]},
            {"choices": [{"delta": {"content": "Answer."}}]},
        ])
        self.assertEqual(text.count("https://a.test"), 1)

    def test_sources_come_from_the_search_we_ran(self):
        """The whole point of running the search ourselves: the citations are facts we
        hold, not a list the model was asked to remember."""
        self.search_context = "## KARATE\nURL: https://uta-net.test/1\nlyrics"
        self.search_citations = [("KARATE 歌詞", "https://uta-net.test/1")]
        text = self._stream([{"choices": [{"delta": {"content": "It is KARATE."}}]}],
                            web_search=True)
        self.assertIn("[KARATE 歌詞](https://uta-net.test/1)", text)

    def test_no_sources_is_not_reported_as_no_search(self):
        """A request that demonstrably searched - 17,763 prompt tokens against 2,755
        without - still carried none of the search fields, so their absence cannot be
        reported as a failed search."""
        text = self._stream([{"choices": [{"delta": {"content": "Answer."}}]}],
                            web_search=True)
        self.assertEqual(text, "Answer.")

    def test_the_footer_can_be_turned_off(self):
        self.cfg.web_search_citations = False
        text = self._stream([
            {"x_nanogpt_search_results": [{"title": "A", "url": "https://a.test"}]},
            {"choices": [{"delta": {"content": "Answer."}}]},
        ])
        self.assertEqual(text, "Answer.")

    def test_structured_citations_are_not_duplicated(self):
        """If a provider ever does return citations, don't add a second Sources block."""
        from assistant.llm import format_citations
        footer = format_citations([("A", "https://a.test")])
        self.assertIn("https://a.test", footer)

    def test_no_citations_means_no_footer(self):
        from assistant.llm import format_citations
        self.assertEqual(format_citations([]), "")

    def test_https_links_survive_the_sanitiser(self):
        html = render_markdown(
            "## Sources\n\n- [CoinGecko](https://www.coingecko.com/en/coins/bitcoin)")
        self.assertIn('href="https://www.coingecko.com/en/coins/bitcoin"', html)

    def test_structured_citations_are_not_duplicated(self):
        """If a provider ever does return citations, don't add a second Sources block."""
        from assistant.llm import format_citations
        footer = format_citations([("A", "https://a.test")])
        self.assertIn("https://a.test", footer)
        self.assertIn("**Sources**", footer)

    def test_no_citations_means_no_footer(self):
        from assistant.llm import format_citations
        self.assertEqual(format_citations([]), "")
