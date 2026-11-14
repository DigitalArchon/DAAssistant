"""An API key goes only to the origin - scheme, host and port - of the address it was
saved for. Changing the address, in Settings or by hand in config.json, does not take
the key with it: Settings asks, when it is saved, whether to use the key there (No the
default), and until it is told so nothing that needs the key is sent there, with words
that say why. A key kept before origins were recorded takes its address's on the first
start that reads it, with no question. No key is ever deleted for it.

Every Config here has a file of its own and a MemoryKeychain: nothing reads or writes the
real keyring, and no request leaves the test.
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

from assistant import gtkcompat  # noqa: E402,F401  (pins GI versions)
from gi.repository import Gtk  # noqa: E402

from assistant import models, netguard, privacy, providers, websearch  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.keychain import MemoryKeychain  # noqa: E402
from assistant.llm import LLMClient, LLMError  # noqa: E402

OURS = "https://llm.example/v1/chat/completions"
ELSEWHERE = "https://llm.elsewhere.example/v1/chat/completions"
NANOGPT = providers.NANOGPT_URL


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")
        self.keychain = MemoryKeychain()

    def fresh(self):
        """The config as the next start reads it."""
        return Config(self.path, keychain=self.keychain)

    def saved(self, **values):
        config = self.fresh()
        for key, value in values.items():
            setattr(config, key, value)
        config.save()
        return self.fresh()

    def stored(self):
        with open(self.path) as f:
            return json.load(f)

    def edit_file(self, **values):
        """config.json edited by hand, the app not running."""
        on_disk = self.stored()
        on_disk.update(values)
        with open(self.path, "w") as f:
            json.dump(on_disk, f)


class AKeyGoesWhereItWasSavedFor(Case):
    def test_it_is_recorded_with_its_origin_and_sent_there(self):
        config = self.saved(custom_url=OURS, custom_api_key="sk-ours",
                            text_provider="custom")
        self.assertEqual(self.stored()["key_origins"],
                         {"custom_api_key": "https://llm.example:443"})
        self.assertNotIn("sk-ours", json.dumps(self.stored()), "the key is the keyring's")
        provider = providers.get(config, providers.CUSTOM)
        self.assertEqual(provider.api_key, "sk-ours")
        self.assertIsNone(provider.problem())

    def test_an_address_changed_without_its_key_is_sent_no_key(self):
        self.saved(custom_url=OURS, custom_api_key="sk-ours", text_provider="custom")
        config = self.saved(custom_url=ELSEWHERE)
        provider = providers.get(config, providers.CUSTOM)
        self.assertEqual(provider.api_key, "")
        refused = provider.problem()
        self.assertIn("was saved for https://llm.example:443", refused)
        self.assertIn("https://llm.elsewhere.example:443", refused)
        self.assertIn("the key is not sent there", refused)
        self.assertIn("open Settings and save", refused)
        client = LLMClient(config)
        client.session = mock.Mock()
        with self.assertRaisesRegex(LLMError, "Nothing was sent. The Other OpenAI-"
                                              "compatible endpoint API key was saved"):
            client.complete(config.model("text"), [{"role": "user", "content": "hi"}])
        client.session.post.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "was saved for"):
            models.fetch(config, force=True, provider=providers.CUSTOM)
        # Never deleted: still kept, and sent again once the address is back.
        self.assertEqual(self.keychain.get("custom_api_key"), "sk-ours")
        back = self.saved(custom_url="https://LLM.example:443/v1")
        self.assertEqual(providers.get(back, providers.CUSTOM).api_key, "sk-ours")

    def test_a_key_entered_with_the_new_address_goes_with_it(self):
        self.saved(custom_url=OURS, custom_api_key="sk-ours")
        config = self.saved(custom_url=ELSEWHERE, custom_api_key="sk-theirs")
        self.assertEqual(providers.get(config, providers.CUSTOM).api_key, "sk-theirs")
        self.assertEqual(self.stored()["key_origins"]["custom_api_key"],
                         "https://llm.elsewhere.example:443")
        # And typed in Settings, not saved yet - its connection test, say - it goes with
        # the address typed beside it.
        tried = config.detached(custom_url=OURS, custom_api_key="sk-new")
        self.assertEqual(providers.get(tried, providers.CUSTOM).api_key, "sk-new")
        kept = config.detached(custom_url=OURS)
        self.assertEqual(providers.get(kept, providers.CUSTOM).api_key, "")

    def test_another_port_or_scheme_is_another_place(self):
        self.saved(custom_url="https://llm.example/v1", custom_api_key="sk-ours")
        for moved in ("https://llm.example:8443/v1", "http://localhost/v1"):
            config = self.saved(custom_url=moved)
            self.assertIsNotNone(config.key_withheld("custom_api_key"), moved)
        for same in ("https://LLM.EXAMPLE/v2/chat/completions", "https://llm.example:443"):
            config = self.saved(custom_url=same)
            self.assertIsNone(config.key_withheld("custom_api_key"), same)


class EditedByHand(Case):
    def test_nanogpts_address_edited_in_takes_no_key_and_nothing_goes_there(self):
        self.saved(api_key="sk-nano")
        self.assertEqual(self.stored()["key_origins"], {"api_key": "https://nano-gpt.com:443"})
        # Another port of the same host: Private Mode still takes it for nano-gpt's.
        self.edit_file(api_url="https://nano-gpt.com:8443/api/v1/chat/completions")
        config = self.fresh()
        self.assertEqual(config.api_key, "sk-nano", "the key is still kept")
        refused = config.key_withheld("api_key")
        self.assertIn("the address in config.json is now https://nano-gpt.com:8443",
                      refused)
        client = LLMClient(config)
        client.session = mock.Mock()
        with self.assertRaisesRegex(LLMError, "Nothing was sent"):
            client.complete("some/model", [{"role": "user", "content": "hi"}])
        client.session.post.assert_not_called()
        # The web search, which would carry the query with it.
        self.assertIn("was saved for", websearch.unavailable(config))
        session = mock.Mock()
        with self.assertRaisesRegex(websearch.SearchError, "was saved for"):
            websearch.run("a question", config, session=session)
        session.post.assert_not_called()
        # Privacy Mode: every TEE check and the relay are at that address.
        config.privacy_mode = True
        with self.assertRaisesRegex(privacy.PrivacyError, "was saved for.*Nothing was "
                                                          "sent"):
            privacy.PrivacySession.start(config)

    def test_a_key_kept_before_origins_were_takes_its_addresss_without_a_question(self):
        with open(self.path, "w") as f:
            json.dump({"custom_url": OURS, "text_provider": "custom"}, f)
        self.keychain.set("custom_api_key", "sk-old")
        self.keychain.set("api_key", "sk-nano")
        config = self.fresh()
        self.assertEqual(self.stored()["key_origins"],
                         {"api_key": "https://nano-gpt.com:443",
                          "custom_api_key": "https://llm.example:443"})
        self.assertEqual(providers.get(config, providers.CUSTOM).api_key, "sk-old")
        # From then on, an edit by hand takes no key with it.
        self.edit_file(custom_url=ELSEWHERE)
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "")

    def test_a_config_with_no_key_records_nothing(self):
        config = self.saved(custom_url=OURS)
        self.assertEqual(self.stored().get("key_origins"), {})
        self.assertIsNone(config.key_withheld("custom_api_key"))

    def test_an_origin_made_nonsense_by_hand_is_one_the_key_goes_to_nowhere(self):
        self.saved(custom_url=OURS, custom_api_key="sk-ours")
        self.edit_file(key_origins={"custom_api_key": 5})
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "")


class SettingsAsks(Case):
    """Assistant.show_settings, with a stand-in for the dialog and for the question."""

    def show(self, config, values, answer):
        from assistant import app as app_module
        app = app_module.Assistant.__new__(app_module.Assistant)
        app.config, app.client, app.encyclopedia = config, None, None
        app.hotkeys, app._settings_dialog = mock.Mock(), None
        app.notices = []
        app.notify = lambda message, urgency="low": app.notices.append(message)
        for name in ("_warm_catalogue", "_refresh_reviews", "_settings_changed_in_chats",
                     "_sync_privacy_item", "_sync_encyclopedia_items", "_sync_custom_items",
                     "_refresh_menu_keys", "_release_privacy_holds",
                     "_make_open_windows_private"):
            setattr(app, name, lambda *a, **k: None)
        dialog = mock.Mock()
        dialog.run.return_value = Gtk.ResponseType.OK
        dialog.values.return_value = values
        asked = []

        def ask(parent, name, saved_for, going_to):
            asked.append((parent, name, saved_for, going_to))
            return answer

        with mock.patch.object(app_module, "SettingsDialog", return_value=dialog), \
                mock.patch.object(app_module, "use_saved_key_there", ask), \
                mock.patch.object(app_module.proxy, "apply"), \
                mock.patch.object(app_module, "action_labels", return_value=[]):
            app.show_settings()
        return asked, dialog

    def kept(self):
        config = self.saved(custom_url=OURS, custom_api_key="sk-ours",
                            text_provider="custom")
        return config, {"custom_url": OURS, "custom_api_key": "sk-ours",
                        "text_provider": "custom"}

    def test_no_the_default_keeps_the_key_from_the_new_address(self):
        config, values = self.kept()
        asked, dialog = self.show(config, dict(values, custom_url=ELSEWHERE), False)
        self.assertEqual(asked, [(dialog, "Other OpenAI-compatible endpoint",
                                  "https://llm.example:443",
                                  "https://llm.elsewhere.example:443")])
        self.assertEqual(self.fresh().custom_url, ELSEWHERE, "the address is saved")
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "")
        self.assertEqual(self.keychain.get("custom_api_key"), "sk-ours")
        # Asked again at the next Save, while the address stays where it went: that is
        # how the refusal says to send it there.
        asked, _dialog = self.show(self.fresh(), dict(values, custom_url=ELSEWHERE), True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "sk-ours")

    def test_yes_sends_it_there_from_then_on(self):
        config, values = self.kept()
        asked, _dialog = self.show(config, dict(values, custom_url=ELSEWHERE), True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.stored()["key_origins"]["custom_api_key"],
                         "https://llm.elsewhere.example:443")
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "sk-ours")

    def test_nothing_is_asked_for_a_new_key_an_address_kept_or_one_edited_back(self):
        config, values = self.kept()
        for changed in ({}, {"custom_url": "https://llm.example/v2"},
                        {"custom_url": ELSEWHERE, "custom_api_key": "sk-new"}):
            asked, _dialog = self.show(self.fresh(), dict(values, **changed), True)
            self.assertEqual(asked, [], changed)
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "sk-new")

    def test_an_address_edited_by_hand_is_asked_about_at_the_next_save(self):
        self.saved(api_key="sk-nano")
        moved = "https://nano-gpt.example.net/api/v1/chat/completions"
        self.edit_file(api_url=moved)
        config = self.fresh()
        asked, _dialog = self.show(config, {"api_key": "sk-nano"}, False)
        self.assertEqual([a[1:] for a in asked],
                         [("NanoGPT", "https://nano-gpt.com:443",
                           "https://nano-gpt.example.net:443")])
        self.assertEqual(self.fresh().key_for("api_key"), "")


class TheQuestion(unittest.TestCase):
    def ask(self, response):
        from assistant.ui import dialogs
        seen = {}

        def run(dialog):
            no = dialog.get_widget_for_response(Gtk.ResponseType.NO)
            seen["default is No"] = dialog.get_default_widget() is no
            seen["text"] = dialog.get_property("text")
            seen["secondary"] = dialog.get_property("secondary-text")
            return response

        with mock.patch.object(Gtk.MessageDialog, "run", run):
            answer = dialogs.use_saved_key_there(None, "NanoGPT", "https://nano-gpt.com:443",
                                                 "https://nano-gpt.example.net:443")
        return answer, seen

    def test_only_yes_sends_it_and_no_is_the_default(self):
        answer, seen = self.ask(Gtk.ResponseType.YES)
        self.assertIs(answer, True)
        self.assertTrue(seen["default is No"])
        self.assertEqual(seen["text"], "Use the saved key with nano-gpt.example.net?")
        self.assertIn("saved for https://nano-gpt.com:443", seen["secondary"])
        self.assertIn("whoever runs that server can use it", seen["secondary"])
        for response in (Gtk.ResponseType.NO, Gtk.ResponseType.DELETE_EVENT):
            self.assertIs(self.ask(response)[0], False)


class Origins(unittest.TestCase):
    def test_an_origin_is_scheme_host_and_port_and_nothing_else(self):
        self.assertEqual(netguard.origin("https://u:p@LLM.Example/v1?x#y"),
                         "https://llm.example:443")
        self.assertEqual(netguard.origin("http://[::1]:8080/v1"), "http://[::1]:8080")
        self.assertEqual(netguard.origin("http://192.168.1.5/v1"), "http://192.168.1.5:80")
        for nothing in ("", "not an address", "ftp://host/x", "http://[fd00::5",
                        "https://host:99999/"):
            self.assertEqual(netguard.origin(nothing), "", nothing)


if __name__ == "__main__":
    unittest.main()
