"""Anthropic, Chutes and Tinfoil as providers beside the first four (providers.py,
config.py, llm.py, models.py, privacy.py, the windows): each one's address filled in and
its key to type, the right protocol, sealing, key origin and privacy level read from the
address however it got into the settings - a preset, typed by hand as Other, an older
config - Privacy Mode able to use Tinfoil's models (and Chutes' -TEE ones) beside
NanoGPT's, a conversation's moves held to its level or higher, and the level shown
where models are chosen."""

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

from assistant import gtkcompat  # noqa: E402,F401  (pins GI versions)
from assistant import (levels, models, privacy, providers, services,  # noqa: E402
                       tinfoil_direct)
from assistant.config import Config  # noqa: E402
from assistant.keychain import MemoryKeychain  # noqa: E402
from assistant.llm import LLMClient, LLMError  # noqa: E402
from assistant.privacy import PrivacyError, PrivacySession  # noqa: E402

ANTHROPIC_STREAM = [
    b'data: {"type": "message_start", "message": {"id": "msg_1", "usage": '
    b'{"input_tokens": 5}}}',
    b'data: {"type": "content_block_delta", "delta": {"type": "text_delta", '
    b'"text": "ok"}}',
    b'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, '
    b'"usage": {"output_tokens": 1}}',
    b'data: {"type": "message_stop"}']


def streamed_lines(lines):
    response = mock.Mock(status_code=200, headers={})
    response.iter_content.return_value = iter([line + b"\n" for line in lines])
    return response


def openai_stream(text="ok"):
    return streamed_lines([
        b'data: {"id": "c1", "choices": [{"delta": {"content": "' + text.encode() + b'"}}]}',
        b'data: {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]}',
        b"data: [DONE]"])


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, value in (("CACHE_DIR", self.dir),
                            ("CACHE_FILE", os.path.join(self.dir, "models.json"))):
            patch = mock.patch.object(models, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.config = Config(os.path.join(self.dir, "c.json"), keychain=MemoryKeychain())
        self.config.api_key = "sk-nano"

    def client(self, *responses):
        client = LLMClient(self.config)
        client.session = mock.Mock()
        client.session.post.side_effect = list(responses)
        return client

    @staticmethod
    def sent(client, call=0):
        args, kwargs = client.session.post.call_args_list[call]
        return args[0], kwargs["headers"], json.loads(kwargs["data"])


class EachProviderIsFilledIn(Case):
    def test_the_three_are_beside_the_first_four_with_their_addresses_filled_in(self):
        self.assertEqual(providers.ORDER, ("nanogpt", "openrouter", "anthropic", "chutes",
                                           "tinfoil", "ollama", "custom"))
        for kind, url, protocol, service in (
                ("anthropic", "https://api.anthropic.com/v1/messages", "anthropic",
                 services.ANTHROPIC),
                ("chutes", "https://llm.chutes.ai/v1/chat/completions", "openai",
                 services.CHUTES),
                ("tinfoil", "https://inference.tinfoil.sh/v1/chat/completions", "tinfoil",
                 services.TINFOIL)):
            with self.subTest(kind=kind):
                provider = providers.get(self.config, kind)
                self.assertEqual((provider.chat_url, provider.protocol, provider.service),
                                 (url, protocol, service))
                self.assertIn("No API key is set", provider.problem())
                self.assertTrue(providers.KINDS[kind].needs_key)

    def test_chutes_proxy_of_anthropics_api_speaks_it(self):
        self.config.chutes_url = "https://claude.chutes.ai"
        provider = providers.get(self.config, "chutes")
        self.assertEqual((provider.chat_url, provider.protocol, provider.service),
                         ("https://claude.chutes.ai/v1/messages", "anthropic", "chutes"))
        self.config.chutes_api_key = "cpk"
        self.assertEqual(providers.get(self.config, "chutes").headers()["authorization"],
                         "Bearer cpk")

    def test_the_keys_are_kept_in_the_keyring_and_bound_to_their_origins(self):
        from assistant.config import KEYED, SECRETS
        for key in ("anthropic_api_key", "chutes_api_key", "tinfoil_api_key"):
            self.assertIn(key, SECRETS)
            self.assertIn(key, KEYED)
        self.config.tinfoil_api_key = "tk"
        self.config.save()
        with open(self.config.config_file) as f:
            self.assertNotIn("tk", json.load(f).get("tinfoil_api_key", ""))
        self.assertEqual(self.config.key_saved_for("tinfoil_api_key"),
                         services.TINFOIL_KEY_ORIGIN)
        self.config.tinfoil_url = "https://router-0.tinfoil.sh/v1"
        self.assertIn("not sent there", self.config.key_withheld("tinfoil_api_key"))
        self.assertIn("not Tinfoil's API", providers.get(self.config, "tinfoil").problem())


class AddressesAreReadHoweverTheyGetIn(Case):
    def test_tinfoils_address_typed_as_other_is_tinfoils(self):
        self.config.custom_url = "https://inference.tinfoil.sh/v1"
        self.config.custom_api_key = "tk"
        other = providers.get(self.config, "custom")
        self.assertEqual((other.service, other.protocol, other.sealed),
                         (services.TINFOIL, "tinfoil", True))
        self.assertEqual(other.listing, "tinfoil")
        self.assertEqual(levels.privacy_at("glm-5-3", other.chat_url, other.protocol).level,
                         levels.HIGHEST)
        self.config.text_provider = "custom"
        client = self.client()
        sealed_client = mock.MagicMock()
        sealed_client.request.return_value.__enter__.return_value = "r"
        sealed_client.lines.return_value = openai_stream("sealed").iter_content()
        with mock.patch.object(client, "_direct_client", return_value=sealed_client):
            text = client.complete(providers.Model("glm-5-3", "custom"),
                                   [{"role": "user", "content": "hi"}])
        self.assertEqual(text, "sealed")
        client.session.post.assert_not_called()

    def test_chutes_and_anthropics_typed_as_other_are_theirs(self):
        for url, protocol, service in (("https://llm.chutes.ai/v1", "openai", "chutes"),
                                       ("https://CLAUDE.chutes.ai/", "anthropic", "chutes"),
                                       ("https://api.anthropic.com", "anthropic",
                                        "anthropic")):
            with self.subTest(url=url):
                self.config.custom_url = url
                other = providers.get(self.config, "custom")
                self.assertEqual((other.protocol, other.service), (protocol, service))

    def test_an_older_config_naming_one_of_them_moves_to_its_own_provider(self):
        path = os.path.join(self.dir, "old.json")
        for url, kind, url_key in (
                ("https://inference.tinfoil.sh/v1/chat/completions", "tinfoil",
                 "tinfoil_url"),
                ("https://llm.chutes.ai/v1/chat/completions", "chutes", "chutes_url"),
                ("https://api.anthropic.com/v1/messages", "anthropic", "anthropic_url")):
            with self.subTest(kind=kind):
                with open(path, "w") as f:
                    json.dump({"api_url": url}, f)
                keychain = MemoryKeychain()
                keychain.set("api_key", "the-key")
                config = Config(path, keychain=keychain)
                self.assertEqual(config.text_provider, kind)
                self.assertEqual(getattr(config, url_key), url)
                self.assertEqual(getattr(config, providers.KINDS[kind].key_key), "the-key")
                self.assertEqual(config.api_key, "")

    def test_a_plain_request_to_a_sealed_address_is_never_made(self):
        self.config.custom_url = "https://router-0.tinfoil.sh/v1"
        client = self.client()
        with self.assertRaisesRegex(LLMError, "Nothing was sent"):
            client._post({"model": "x", "messages": []}, True,
                         provider=providers.get(self.config, "custom"))
        client.session.post.assert_not_called()


class EachIsSentItsOwnWay(Case):
    def test_anthropic_is_sent_its_messages_api_with_its_key(self):
        self.config.anthropic_api_key = "sk-ant"
        self.config.text_provider = "anthropic"
        self.config.text_model = "claude-opus-5-5"
        client = self.client(streamed_lines(ANTHROPIC_STREAM),
                             streamed_lines(ANTHROPIC_STREAM))
        model = providers.role_model(self.config, "text")
        self.assertEqual(client.stream(model, [{"role": "user", "content": "hi"}],
                                       web_search=False), "ok")
        self.assertEqual(client.complete(model, [{"role": "user", "content": "hi"}]), "ok")
        url, headers, body = self.sent(client)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "sk-ant")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(body["messages"], [{"role": "user", "content": [
            {"type": "text", "text": "hi"}]}])
        self.assertTrue(body["stream"])

    def test_chutes_ordinary_models_go_in_the_clear_with_thinking_off_when_least(self):
        self.config.chutes_api_key = "cpk"
        self.config.text_provider = "chutes"
        self.config.text_model = "zai-org/GLM-5.3"
        listing = {"data": [{"id": "zai-org/GLM-5.3",
                             "chute_id": "9ecb73cc-909b-5334-9cd8-74985945cff5",
                             "supported_features": ["reasoning"]}]}
        with mock.patch.object(models, "_fetch_listing",
                               return_value=[models.normalise_chutes(
                                   {**listing["data"][0], "capabilities":
                                    {"reasoning": True}})]):
            models.fetch(self.config, force=True, provider="chutes")
        whole = mock.Mock(status_code=200)
        whole.json.return_value = {"choices": [{"message": {"content": "ok"},
                                                "finish_reason": "stop"}]}
        client = self.client(whole)
        self.assertEqual(client.complete(providers.role_model(self.config, "text"),
                                         [{"role": "user", "content": "hi"}]), "ok")
        url, headers, body = self.sent(client)
        self.assertEqual(url, "https://llm.chutes.ai/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer cpk")
        self.assertNotIn("reasoning", body)
        self.assertEqual(body["chat_template_kwargs"],
                         {"enable_thinking": False, "thinking": False})

    def test_a_chutes_tee_model_is_only_ever_sent_sealed(self):
        self.config.chutes_api_key = "cpk"
        self.config.text_provider = "chutes"
        self.config.text_model = "Qwen/Qwen3.8-27B-TEE"
        client = self.client()
        made = {}

        def factory(url, key, key_saved_for="", request_timeout=180):
            made.update(url=url, key=key, saved=key_saved_for)
            raise PrivacyError("attestation refused for the test")

        with mock.patch("assistant.chutes.ChutesClient", factory):
            with self.assertRaisesRegex(LLMError, "nothing was sent in the clear"):
                client.stream(providers.role_model(self.config, "text"),
                              [{"role": "user", "content": "secret"}])
        client.session.post.assert_not_called()
        self.assertEqual(made, {"url": "https://llm.chutes.ai/v1/chat/completions",
                                "key": "cpk", "saved": "https://llm.chutes.ai:443"})

    def test_a_tinfoil_model_is_sealed_by_tinfoils_own_client(self):
        self.config.tinfoil_api_key = "tk"
        client = LLMClient(self.config)
        provider = providers.get(self.config, "tinfoil")
        with mock.patch.object(tinfoil_direct, "available",
                               return_value=(True, "tinfoil")):
            made = client._direct_client(provider)
        self.assertIsInstance(made, tinfoil_direct.TinfoilDirectClient)
        self.assertEqual((made.base, made.api_key), (services.TINFOIL_BASE_URL, "tk"))
        self.assertIs(client._direct_client(provider), made, "one attestation, kept")

    def test_the_lists_are_read_as_each_lists_them(self):
        for kind, listing in (("chutes", "chutes"), ("tinfoil", "tinfoil"),
                              ("anthropic", "anthropic")):
            provider = providers.get(self.config, kind)
            self.assertEqual(provider.listing, listing)
        self.assertEqual(providers.get(self.config, "chutes").models_url,
                         "https://llm.chutes.ai/v1/models")
        self.assertEqual(providers.get(self.config, "tinfoil").models_url,
                         "https://inference.tinfoil.sh/v1/models")
        older = models.normalise_anthropic({"id": "claude-sonnet-4-5",
                                            "reasoning_efforts": ["low", "high"]})
        self.assertEqual(older["efforts"][0], "none", "thinks only when asked")
        always = models.normalise_anthropic({"id": "claude-opus-5-5",
                                             "reasoning_efforts": ["low", "high"]})
        self.assertNotIn("none", always["efforts"])


class PrivacyModeOnTinfoilToo(Case):
    def setUp(self):
        super().setUp()
        self.config.tinfoil_api_key = "tk"
        self.config.chutes_api_key = "cpk"
        self.config.privacy_mode = True
        self.config.private_text_model = "Tinfoil::glm-5-3"
        self.config.private_premium_model = "private/glm-5-3"
        self.config.private_vision_model = "Tinfoil::gemma4-31b"

    def test_a_tinfoil_model_is_private_at_the_highest_level(self):
        for model, kind, service, level in (
                ("Tinfoil::glm-5-3", privacy.E2EE, "tinfoil", levels.HIGHEST),
                (" tinfoil :: x", privacy.E2EE, "tinfoil", levels.HIGHEST),
                ("Chutes::a/b-TEE", privacy.E2EE, "chutes", levels.PARTIAL),
                ("Chutes::zai-org/GLM-5.3", None, None, levels.NOT_PRIVATE),
                ("private/x", privacy.E2EE, "nanogpt", levels.HIGHEST),
                ("TEE/x", privacy.TEE, "nanogpt", levels.PARTIAL)):
            with self.subTest(model=model):
                self.assertEqual(privacy.kind_of(model), kind)
                self.assertEqual(privacy.service_of(model), service)
                self.assertEqual(privacy.level_of(model).level, level)
        self.assertTrue(privacy.sealed_only("Tinfoil::glm-5-3"))

    def test_the_session_seals_tinfoils_models_with_the_key_it_started_with(self):
        with mock.patch.object(privacy, "problems", return_value=[]):
            session = PrivacySession.start(self.config)
        self.assertEqual(session.services["tinfoil"],
                         ("https://inference.tinfoil.sh/v1/chat/completions", "tk",
                          services.TINFOIL_KEY_ORIGIN))
        with mock.patch.object(tinfoil_direct, "available",
                               return_value=(True, "tinfoil")):
            client = session.sealed_client("Tinfoil::glm-5-3")
        self.assertIsInstance(client, tinfoil_direct.TinfoilDirectClient)
        self.assertIs(session.sealed_client("Tinfoil::gemma4-31b"), client)
        self.assertTrue(session.all_sealed)
        self.config.tinfoil_api_key = "tk-changed"
        self.assertFalse(session.matches(self.config))

    def test_privacy_mode_says_what_a_tinfoil_model_lacks(self):
        self.config.tinfoil_api_key = ""
        found = " ".join(privacy.problems(self.config, wait=False))
        self.assertIn("Tinfoil::glm-5-3 is Tinfoil's: No API key is set for Tinfoil", found)
        self.config.tinfoil_api_key = "tk"
        self.config.tinfoil_url = "https://llm.chutes.ai/v1"
        self.assertIn("not Tinfoil's", " ".join(privacy.problems(self.config, wait=False)))

    def test_a_conversation_moves_only_to_its_level_or_higher(self):
        session = PrivacySession("https://nano-gpt.com/api/v1/chat/completions", "k",
                                 "private/glm-5-3", "private/glm-5-3", uses=("text",),
                                 services={"tinfoil": ("u", "tk", ""),
                                           "chutes": ("u", "cpk", "")})
        session.enlist("Tinfoil::glm-5-3", moving_from="private/glm-5-3")
        for lower in ("TEE/glm-5.3", "Chutes::a/b-TEE"):
            with self.subTest(lower=lower):
                with self.assertRaisesRegex(PrivacyError, "kept to the highest privacy"):
                    session.enlist(lower, moving_from="private/glm-5-3")
                self.assertNotIn(lower, session.models())
        partial = PrivacySession("https://nano-gpt.com/api/v1/chat/completions", "k",
                                 "TEE/glm-5.3", "TEE/glm-5.3", uses=("text",),
                                 services={"chutes": ("u", "cpk", "")})
        partial.enlist("Chutes::a/b-TEE", moving_from="TEE/glm-5.3")
        partial.enlist("private/glm-5-3", moving_from="Chutes::a/b-TEE")
        with self.assertRaisesRegex(PrivacyError, "isn't|is not a TEE"):
            partial.enlist("zai-org/GLM-5.3", moving_from="TEE/glm-5.3")

    def test_privacy_mode_fetches_only_its_own_providers_lists(self):
        from assistant.app import Assistant
        stub = type("Stub", (), {"config": self.config,
                                 "_catalogue_providers": Assistant._catalogue_providers})()
        self.assertEqual([p.id for p in stub._catalogue_providers()],
                         ["tinfoil", "nanogpt"])


class InTheWindows(Case):
    """Settings and the model browser, on a virtual display."""

    def test_the_providers_page_fills_each_address_in_and_leaves_the_key_to_type(self):
        from assistant.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(None, self.config, type("C", (), {"config": self.config}))
        self.addCleanup(dialog.destroy)
        for url_key, key_key, address in (
                ("anthropic_url", "anthropic_api_key", services.ANTHROPIC_URL),
                ("chutes_url", "chutes_api_key", services.CHUTES_BASE_URL),
                ("tinfoil_url", "tinfoil_api_key", services.TINFOIL_BASE_URL)):
            with self.subTest(url_key=url_key):
                self.assertEqual(dialog.entries[url_key].get_text(), address)
                self.assertEqual(dialog.entries[key_key].get_text(), "")
                self.assertFalse(dialog.entries[key_key].get_visibility())
        combo, _provider_key = dialog.provider_combos["text_model"]
        offered = [row[1] for row in combo.get_model()]
        self.assertEqual(offered, list(providers.ORDER))
        dialog.entries["tinfoil_api_key"].set_text("tk")
        dialog.entries["text_model"].set_text("glm-5-3")
        combo.set_active_id("tinfoil")
        self.assertEqual(dialog.model_levels["text_model"].get_text(), "🔐 Highest")
        combo.set_active_id("chutes")
        dialog.entries["text_model"].set_text("Qwen/Qwen3.8-27B-TEE")
        self.assertEqual(dialog.model_levels["text_model"].get_text(), "🔒 Partial")
        dialog.entries["text_model"].set_text("zai-org/GLM-5.3")
        self.assertEqual(dialog.model_levels["text_model"].get_text(), "Not private")
        values = dialog.values()
        self.assertEqual((values["tinfoil_api_key"], values["text_provider"]),
                         ("tk", "chutes"))

    def test_the_model_browser_shows_each_models_privacy_where_it_is_listed(self):
        from assistant.ui.model_browser import COL_PRIVACY, ModelBrowser
        with mock.patch.object(ModelBrowser, "_load", lambda *a, **kw: None):
            browser = ModelBrowser(None, self.config, provider="chutes")
        self.addCleanup(browser.destroy)
        browser._on_loaded([models.normalise({"id": "a/b-TEE"}),
                            models.normalise({"id": "zai-org/GLM-5.3"})], False, "chutes")
        shown = {row[0]: row[COL_PRIVACY] for row in browser.store}
        self.assertEqual(shown, {"a/b-TEE": "🔐 End-to-end encrypted (Chutes, partial)",
                                 "zai-org/GLM-5.3": "Not private"})
        browser.private_check.set_active(True)
        self.assertEqual([row[0] for row in browser.filtered], ["a/b-TEE"])

    def test_a_private_model_of_tinfoils_is_chosen_by_its_reference(self):
        from gi.repository import Gtk

        from assistant.ui.model_browser import COL_PRIVACY, ModelBrowser
        with mock.patch.object(ModelBrowser, "_load", lambda *a, **kw: None):
            browser = ModelBrowser(None, self.config, private_only=True,
                                   current="Tinfoil::glm-5-3")
        self.addCleanup(browser.destroy)
        browser._on_loaded([models.normalise({"id": "glm-5-3"})], False, "tinfoil")
        self.assertEqual(browser.store[0][COL_PRIVACY], "🔐 End-to-end encrypted (Tinfoil)")
        browser.tree.get_selection().select_path(Gtk.TreePath.new_first())
        with mock.patch.object(Gtk.Dialog, "run", return_value=Gtk.ResponseType.OK):
            browser.run()
        self.assertEqual(browser.selected_id, "Tinfoil::glm-5-3")


if __name__ == "__main__":
    unittest.main()
