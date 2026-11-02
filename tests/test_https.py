"""https, or nothing.

A plain http API URL used to be warned about in Settings and then used anyway: the API
key, and whatever was captured, went out unencrypted. Now such an address is refused -
by Settings, which will not save it, and again at each place a request leaves from, so a
config.json edited by hand sends nothing either. Plain http stays allowed for this
machine and its local network, which is how a model server of one's own is reached.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gtk

from assistant import models, netguard, privacy, websearch
from assistant.config import Config
from assistant.llm import LLMClient, LLMError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def config(self, **values):
        config = Config(os.path.join(self.dir, "c.json"))
        for key, value in values.items():
            setattr(config, key, value)
        return config


class WhichAddressesMayBeUsed(unittest.TestCase):
    def test_https_is_accepted_anywhere(self):
        for url in ("https://nano-gpt.com/api/v1/chat/completions",
                    "https://api.example.com:8443/v1", "HTTPS://Example.com/x",
                    "https://192.0.2.7/v1", "  https://nano-gpt.com/api  "):
            self.assertIsNone(netguard.problem(url), url)

    def test_plain_http_is_accepted_only_on_this_machine_and_its_network(self):
        for url in ("http://localhost:11434/v1/chat/completions", "http://LOCALHOST/v1",
                    "http://127.0.0.1:8080/v1", "http://127.8.9.10/v1", "http://[::1]:1234/v1",
                    "http://10.0.0.5/v1", "http://172.16.0.1/v1", "http://172.31.255.254/v1",
                    "http://192.168.1.20:5000/v1", "http://169.254.10.10/v1",
                    "http://[fe80::1]/v1", "http://[fd12:3456:789a::1]/v1",
                    "http://[fc00::1]/v1", "http://ollama.local:11434/v1",
                    "http://box.localhost/v1"):
            self.assertIsNone(netguard.problem(url), url)

    def test_plain_http_to_anywhere_else_is_refused_with_the_reason(self):
        for url in ("http://api.example.com/v1/chat/completions", "http://nano-gpt.com/api",
                    "http://8.8.8.8/v1", "http://172.15.0.1/v1", "http://172.32.0.1/v1",
                    "http://192.169.0.1/v1", "http://11.0.0.1/v1", "http://169.255.0.1/v1",
                    "http://[2001:db8::1]/v1", "http://[fec0::1]/v1",
                    # Ranges Python calls "private" that are not the local network:
                    "http://192.0.2.1/v1", "http://198.18.0.1/v1", "http://100.64.0.1/v1",
                    # Names that only look local:
                    "http://localhost.example.com/v1", "http://local/v1",
                    "http://10.0.0.5.example.com/v1", "http://mylocal/v1"):
            found = netguard.problem(url)
            self.assertIsNotNone(found, url)
            self.assertIn("not https", found)
            self.assertIn("https://", found)

    def test_an_ipv4_address_dressed_as_ipv6_is_judged_as_what_it_is(self):
        self.assertIsNone(netguard.problem("http://[::ffff:192.168.1.5]/v1"))
        self.assertIsNotNone(netguard.problem("http://[::ffff:8.8.8.8]/v1"))

    def test_what_is_not_a_web_address_at_all_is_refused(self):
        for url in ("", "   ", "nano-gpt.com/api", "ftp://example.com/x", "file:///etc/passwd",
                    "http://", "https://", "javascript:alert(1)"):
            self.assertIsNotNone(netguard.problem(url), url)

    def test_check_raises_and_never_rewrites(self):
        with self.assertRaises(netguard.InsecureURL) as refused:
            netguard.check("http://api.example.com/v1")
        self.assertIn("http://api.example.com/v1", str(refused.exception))
        self.assertEqual(netguard.check(" https://a.example/v1 "), "https://a.example/v1")
        self.assertEqual(netguard.check("http://localhost:1/v1"), "http://localhost:1/v1")

    def test_an_address_two_parsers_read_differently_is_refused(self):
        """urllib's parser saw localhost in these, and requests connected to evil.com."""
        for url in ("http://evil.com\\@localhost/v1", "http://evil.com\\.local/v1",
                    "http://evil.com\t.local/v1", "http://evil.com\n.local/v1",
                    "http://local host/v1", "https://api.example.com/v1\\x",
                    "http://localhost/v1\x00"):
            found = netguard.problem(url)
            self.assertIsNotNone(found, repr(url))
            self.assertIn("Not a web address", found)

    def test_requests_connects_to_the_host_that_was_judged(self):
        from urllib3.util import parse_url
        for url in ("http://localhost:11434/v1", "http://[::1]:8080/v1",
                    "http://user:pw@192.168.1.5/v1", "http://[fe80::1%25eth0]:1/v1",
                    "http://LOCALHOST./v1"):
            self.assertIsNone(netguard.problem(url), url)
            self.assertTrue(netguard.is_local(parse_url(url).host.strip("[]").split("%")[0])
                            or "%" in url, url)

    def test_a_half_typed_address_is_explained_not_raised(self):
        for url in ("http://[fd00::5", "http://[", "https://[::1"):
            found = netguard.problem(url)
            self.assertIn("Not a web address", found)

    def test_a_tor_onion_service_may_be_plain_http(self):
        onion = "http://" + "a" * 56 + ".onion:8080/v1/chat/completions"
        self.assertIsNone(netguard.problem(onion))
        self.assertIsNone(netguard.problem("http://Example.ONION./v1"))
        self.assertIsNotNone(netguard.problem("http://onion.example.com/v1"))
        self.assertIsNotNone(netguard.problem("http://example.onion.evil.com/v1"))

    def test_a_missing_scheme_is_told_what_will_do(self):
        found = netguard.problem("localhost:11434")
        self.assertIn("https://", found)
        self.assertIn("http:// for this machine", found)


class RedirectsKeepToTheRule(unittest.TestCase):
    """requests follows a redirect by itself, and a 307 carries the body along."""

    def session(self, location, status=307):
        import requests
        from requests.adapters import BaseAdapter
        sent = []

        class Fake(BaseAdapter):
            def send(self, request, **_kwargs):
                sent.append((request.url, request.body, request.headers.get("Authorization")))
                response = requests.models.Response()
                response.request, response.url = request, request.url
                response._content = b"{}"
                if len(sent) == 1:
                    response.status_code = status
                    response.headers["Location"] = location
                else:
                    response.status_code = 200
                return response

            def close(self):
                pass

        session = netguard.session()
        session.mount("https://", Fake())
        session.mount("http://", Fake())
        return session, sent

    def test_a_redirect_to_plain_http_elsewhere_is_not_followed(self):
        session, sent = self.session("http://collector.example/steal")
        with self.assertRaises(netguard.RedirectRefused) as refused:
            session.post("https://api.example/v1/chat/completions", data=b"the conversation",
                         headers={"Authorization": "Bearer k"})
        self.assertEqual(len(sent), 1, "the body went on to the redirect")
        self.assertIn("http://collector.example/steal", str(refused.exception))

    def test_https_is_never_carried_on_over_plain_http_even_locally(self):
        session, sent = self.session("http://192.168.1.5/v1/chat/completions")
        with self.assertRaises(netguard.RedirectRefused):
            session.post("https://api.example/v1/chat/completions", data=b"x")
        self.assertEqual(len(sent), 1)

    def test_a_redirect_that_keeps_to_https_is_followed(self):
        session, sent = self.session("https://api2.example/v1/chat/completions", status=308)
        response = session.post("https://api.example/v1/chat/completions", data=b"x")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent[1][0], "https://api2.example/v1/chat/completions")

    def test_a_relative_redirect_is_judged_where_it_lands(self):
        session, sent = self.session("/v2/chat/completions")
        session.post("http://localhost:11434/v1/chat/completions", data=b"x")
        self.assertEqual(sent[1][0], "http://localhost:11434/v2/chat/completions")

    def test_a_chat_request_stops_at_a_refused_redirect_without_retrying(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        config = Config(os.path.join(directory, "c.json"))
        config.api_key = "k"
        client = LLMClient(config)
        client.session, sent = self.session("http://collector.example/steal")
        with self.assertRaises(LLMError) as failed:
            client.complete("m", [{"role": "user", "content": "the conversation"}])
        self.assertEqual(len(sent), 1)
        self.assertIn("not followed", str(failed.exception))

    def test_every_request_the_app_makes_itself_follows_the_rule(self):
        """No plain requests.get/post/Session where the app makes its own requests."""
        for name in ("llm.py", "websearch.py", "models.py"):
            with open(os.path.join(ROOT, "assistant", name), encoding="utf-8") as f:
                source = f.read()
            for forbidden in ("requests.get(", "requests.post(", "requests.Session("):
                self.assertNotIn(forbidden, source, f"{name}: {forbidden}")


class NothingIsSentInTheClear(TempDir):
    """The check sits where each request leaves from, so no setting, no config.json
    edited by hand and no caller can send the API key over plain http."""

    URL = "http://api.example.com/v1/chat/completions"

    def test_a_chat_request_is_refused_before_it_is_made(self):
        client = LLMClient(self.config(api_url=self.URL, api_key="sk-secret"))
        client.session = mock.Mock()
        with self.assertRaises(LLMError) as refused:
            client.complete("m", [{"role": "user", "content": "hi"}])
        client.session.post.assert_not_called()
        self.assertIn("Nothing was sent", str(refused.exception))
        self.assertIn("not https", str(refused.exception))

    def test_a_stream_is_refused_too(self):
        client = LLMClient(self.config(api_url=self.URL, api_key="sk-secret"))
        client.session = mock.Mock()
        with self.assertRaises(LLMError):
            client.stream("m", [{"role": "user", "content": "hi"}])
        client.session.post.assert_not_called()

    def test_the_connection_test_says_why_and_sends_nothing(self):
        client = LLMClient(self.config(api_url=self.URL, api_key="sk-secret"))
        client.session = mock.Mock()
        ok, message = client.test_connection()
        self.assertFalse(ok)
        self.assertIn("not https", message)
        client.session.post.assert_not_called()

    def test_the_model_list_is_not_fetched(self):
        config = self.config(api_url=self.URL, api_key="sk-secret")
        with mock.patch.object(models, "_get") as get:
            with self.assertRaisesRegex(RuntimeError, "not https"):
                models.fetch(config, force=True)
            self.assertEqual(models.fetch_private_ids(self.URL), [])
            get.assert_not_called()

    def test_a_web_search_is_not_made(self):
        config = self.config(api_url="http://nano-gpt.example/api/v1/chat/completions",
                             api_key="sk-secret")
        session = mock.Mock()
        with self.assertRaisesRegex(websearch.SearchError, "not https"):
            websearch.run("a query", config, session=session)
        session.post.assert_not_called()

    def test_privacy_mode_will_not_start(self):
        config = self.config(api_url=self.URL, api_key="sk-secret", privacy_mode=True)
        self.assertTrue(any("not https" in found for found in privacy.problems(config)))
        with self.assertRaisesRegex(privacy.PrivacyError, "not https"):
            privacy.PrivacySession.start(config)

    def test_a_local_server_over_http_still_works(self):
        client = LLMClient(self.config(api_url="http://localhost:11434/v1/chat/completions",
                                       api_key="k"))
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "hello"},
                                                   "finish_reason": "stop"}]}
        client.session = mock.Mock()
        client.session.post.return_value = response
        self.assertEqual(client.complete("m", [{"role": "user", "content": "hi"}]), "hello")
        client.session.post.assert_called_once()

    def test_certificates_are_never_switched_off(self):
        """Nowhere in the app is a request made with verification disabled."""
        for folder, _dirs, files in os.walk(os.path.join(ROOT, "assistant")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    source = f.read()
                for forbidden in ("verify=False", "verify = False", "CERT_NONE",
                                  "check_hostname = False", "_create_unverified_context",
                                  "disable_warnings"):
                    self.assertNotIn(forbidden, source, f"{name}: {forbidden}")


class SettingsRefuseAnInsecureAddress(TempDir):
    def dialog(self, **values):
        from assistant.ui.dialogs import SettingsDialog
        config = self.config(**values)
        dialog = SettingsDialog(None, config, type("C", (), {"config": config}))
        self.addCleanup(dialog.destroy)
        return dialog

    def test_save_is_held_and_the_reason_shown(self):
        dialog = self.dialog()
        self.assertTrue(dialog.save_button.get_sensitive())
        self.assertFalse(dialog.url_warning.get_visible())
        dialog.entries["custom_url"].set_text("http://api.example.com/v1/chat/completions")
        self.assertFalse(dialog.save_button.get_sensitive())
        self.assertTrue(dialog.url_warning.get_visible())
        self.assertIn("not https", dialog.url_warning.get_text())
        dialog.entries["custom_url"].set_text("https://api.example.com/v1/chat/completions")
        self.assertTrue(dialog.save_button.get_sensitive())
        self.assertFalse(dialog.url_warning.get_visible())

    def test_a_response_got_out_of_the_dialog_anyway_saves_nothing(self):
        """Save is insensitive, so nobody can click it. Should the dialog answer OK
        regardless, the app does not take its word for the address."""
        from assistant import app as app_module
        config = self.config(api_url="https://nano-gpt.com/api/v1/chat/completions")
        app = app_module.Assistant.__new__(app_module.Assistant)
        app.config, app.client, app.encyclopedia = config, None, None
        app.hotkeys, app._settings_dialog = mock.Mock(), None
        app.notices = []
        app.notify = lambda message, urgency="low": app.notices.append(message)
        dialog = mock.Mock()
        dialog.run.return_value = Gtk.ResponseType.OK
        dialog.values.return_value = {"custom_url": "http://api.example.com/v1",
                                      "text_provider": "custom",
                                      "default_language": "French"}
        with mock.patch.object(app_module, "SettingsDialog", return_value=dialog):
            app.show_settings()
        self.assertEqual(config.custom_url, "")
        self.assertEqual(config.text_provider, "nanogpt")
        self.assertEqual(config.default_language, "English")
        self.assertFalse(os.path.exists(config.config_file))
        self.assertIn("Settings not saved", app.notices[-1])
        self.assertIn("not https", app.notices[-1])
        dialog.destroy.assert_called_once()

    def test_a_local_address_over_http_can_be_saved(self):
        dialog = self.dialog()
        dialog.entries["ollama_url"].set_text("http://192.168.1.20:11434")
        dialog.provider_combos["vision_model"][0].set_active_id("ollama")
        self.assertTrue(dialog.save_button.get_sensitive())
        self.assertFalse(dialog.url_warning.get_visible())
        self.assertEqual(dialog.values()["ollama_url"], "http://192.168.1.20:11434")
        self.assertEqual(dialog.values()["vision_provider"], "ollama")

    def test_the_address_is_not_rewritten_for_the_user(self):
        dialog = self.dialog()
        dialog.entries["custom_url"].set_text("http://api.example.com/v1")
        self.assertEqual(dialog.values()["custom_url"], "http://api.example.com/v1")

    def test_a_config_edited_by_hand_shows_its_fault_when_settings_open(self):
        dialog = self.dialog(api_url="http://api.example.com/v1")
        self.assertFalse(dialog.save_button.get_sensitive())
        self.assertTrue(dialog.url_warning.get_visible())

    def test_privacy_mode_being_fine_does_not_let_a_bad_address_through(self):
        dialog = self.dialog()
        dialog.privacy_enabled.set_active(False)
        dialog.entries["custom_url"].set_text("http://api.example.com/v1")
        self.assertFalse(dialog.save_button.get_sensitive())
        dialog.entries["private_text_model"].set_text("TEE/another")   # re-syncs privacy
        self.assertFalse(dialog.save_button.get_sensitive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
