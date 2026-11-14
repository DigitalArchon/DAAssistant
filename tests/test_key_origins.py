"""An API key goes only to the origin - scheme, host and port - of the address it was
saved for. Changing the address, in Settings or by hand in config.json, does not take
the key with it: Settings asks, when it is saved, whether to use the key there (No the
default), and until it is told so nothing that needs the key is sent there, with words
that say why. A No is remembered for that place, not asked again at the next Save; the
Providers page shows the key held back, with Ask again.... What sends the key at once -
Test connection, a model list, Check attestation - asks every time, a place said No to
included. A key kept before origins were recorded takes its address's on the first start
that reads it, with no question. No key is ever deleted for it. And a key or a password
that would go over plain http to another machine on the network is asked about first.

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

    def show(self, config, values, answer, answers=None):
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
        if answers is not None:
            dialog.key_answers = answers
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
        # The No is remembered for that place, the key itself in the keyring only.
        self.assertEqual(self.stored()["key_declined"],
                         {"custom_api_key": "https://llm.elsewhere.example:443"})
        self.assertIn("You said No to sending it there",
                      self.fresh().key_withheld("custom_api_key"))
        # Not asked again at the next Save: a question repeated at every Save is soon
        # answered Yes without reading (the DA apps' standard, Nov 2026).
        asked, _dialog = self.show(self.fresh(), dict(values, custom_url=ELSEWHERE), True)
        self.assertEqual(asked, [])
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "")
        # Asked again from the Providers page (the dialog's answer): it goes there.
        answers = {("custom_api_key", "https://llm.elsewhere.example:443"): True}
        asked, _dialog = self.show(self.fresh(), dict(values, custom_url=ELSEWHERE), False,
                                   answers=answers)
        self.assertEqual(asked, [])
        self.assertEqual(providers.get(self.fresh(), providers.CUSTOM).api_key, "sk-ours")
        self.assertEqual(self.stored()["key_declined"], {})

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


class ANoIsLetGo(Case):
    def test_by_a_key_entered_for_the_new_place_or_the_address_put_back(self):
        self.saved(custom_url=OURS, custom_api_key="sk-ours")
        config = self.saved(custom_url=ELSEWHERE,
                            key_declined={"custom_api_key": netguard.origin(ELSEWHERE)})
        self.assertEqual(config.key_declined_for("custom_api_key"),
                         "https://llm.elsewhere.example:443")
        back = self.saved(custom_url=OURS)
        self.assertEqual(back.key_declined_for("custom_api_key"), "")
        self.assertEqual(self.stored()["key_declined"], {})
        self.saved(custom_url=ELSEWHERE,
                   key_declined={"custom_api_key": netguard.origin(ELSEWHERE)})
        theirs = self.saved(custom_api_key="sk-theirs")
        self.assertEqual(theirs.key_declined_for("custom_api_key"), "")
        self.assertEqual(providers.get(theirs, providers.CUSTOM).api_key, "sk-theirs")
        # Typed in Settings and not saved yet, the same.
        held = self.saved(key_declined={"custom_api_key": "https://x.example:443"})
        self.assertEqual(held.detached(custom_api_key="sk-new").key_declined_for(
            "custom_api_key"), "")

    def test_nonsense_written_there_by_hand_holds_back_nothing_more(self):
        self.saved(custom_url=OURS, custom_api_key="sk-ours")
        self.edit_file(key_declined={"custom_api_key": 5})
        config = self.fresh()
        self.assertEqual(config.key_declined_for("custom_api_key"), "")
        self.assertEqual(providers.get(config, providers.CUSTOM).api_key, "sk-ours")


class FakeClient:
    """A stand-in for LLMClient in Settings: the config each test connection is made
    with (FakeClient.made), and nothing sent."""

    def __init__(self, config):
        FakeClient.made.append(config)
        self.config = config

    def test_connection(self):
        return True, "ok"


FakeClient.made = []


class TheDialogAsks(Case):
    """Settings itself, on a virtual display: what it asks before a Test connection, a
    model list, Check attestation and Save send a key, and what it does with the
    answers."""

    def dialog(self, config):
        from assistant.ui.dialogs import SettingsDialog
        FakeClient.made = []
        dialog = SettingsDialog(None, config, FakeClient(config))
        FakeClient.made = []
        self.addCleanup(dialog.destroy)
        return dialog

    def asking(self, moved=None, plain=None):
        """The two questions, answered `moved` and `plain`; what each was asked."""
        from assistant.ui import dialogs
        asked = {"moved": [], "plain": []}

        def moved_question(parent, name, saved_for, going_to):
            asked["moved"].append((name, saved_for, going_to))
            return moved

        def plain_question(parent, entries, after_no):
            asked["plain"].append(([(what, address) for what, address, _s in entries],
                                   after_no))
            return plain

        patches = (mock.patch.object(dialogs, "use_saved_key_there", moved_question),
                   mock.patch.object(dialogs, "send_in_the_clear", plain_question))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return asked

    def saved_moved(self):
        """A custom endpoint's key saved for OURS, its address since moved by hand."""
        self.saved(custom_url=OURS, custom_api_key="sk-ours", text_provider="custom",
                   text_model="m")
        self.edit_file(custom_url=ELSEWHERE)
        return self.fresh()

    def test_an_action_asks_every_time_a_place_said_no_to_included(self):
        config = self.saved_moved()
        config.key_declined = {"custom_api_key": netguard.origin(ELSEWHERE)}
        config.save()
        config = self.fresh()
        asked = self.asking(moved=True)
        dialog = self.dialog(config)
        dialog._on_test(None)
        self.assertEqual(asked["moved"], [("Other OpenAI-compatible endpoint",
                                           "https://llm.example:443",
                                           "https://llm.elsewhere.example:443")])
        tried = FakeClient.made[-1]
        self.assertEqual(providers.get(tried, providers.CUSTOM).api_key, "sk-ours")
        # The answer stands for the dialog, Save included: asked once.
        dialog._on_test(None)
        self.assertEqual(len(asked["moved"]), 1)
        self.assertEqual(dialog.key_answers,
                         {("custom_api_key", "https://llm.elsewhere.example:443"): True})

    def test_a_no_to_an_action_sends_that_key_nowhere(self):
        config = self.saved_moved()
        self.asking(moved=False)
        dialog = self.dialog(config)
        dialog._on_test(None)
        self.assertEqual(providers.get(FakeClient.made[-1], providers.CUSTOM).api_key, "")

    def test_save_does_not_ask_again_about_a_place_said_no_to_and_ask_again_does(self):
        config = self.saved_moved()
        config.key_declined = {"custom_api_key": netguard.origin(ELSEWHERE)}
        config.save()
        asked = self.asking(moved=True)
        dialog = self.dialog(self.fresh())
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(asked["moved"], [])
        self.assertEqual(dialog.key_answers, {})
        words, again = dialog.held_back["custom_api_key"]
        self.assertIn("The saved Other OpenAI-compatible endpoint key is for "
                      "https://llm.example:443, not sent to "
                      "https://llm.elsewhere.example:443. You said No to sending it "
                      "there.", words.get_text())
        again.clicked()
        self.assertEqual(len(asked["moved"]), 1)
        self.assertEqual(words.get_text(), "With Save, the saved Other OpenAI-compatible "
                                           "endpoint key goes to "
                                           "https://llm.elsewhere.example:443.")
        self.assertEqual(dialog.key_answers,
                         {("custom_api_key", "https://llm.elsewhere.example:443"): True})

    def test_no_key_held_back_no_box(self):
        dialog = self.dialog(self.saved(custom_url=OURS, custom_api_key="sk-ours"))
        self.assertFalse(hasattr(dialog, "held_back"))


class InTheClear(Case):
    """A key, or a password written into an address, that would go over plain http to
    another machine on the local network - allowed, but anyone on that network reads it
    - is asked about first, No the default (SealedLore's keys_in_the_clear, the DA apps'
    standard, Nov 2026). To this machine itself nothing leaves it."""

    LAN = "http://192.168.1.50:8000/v1/chat/completions"

    def test_what_goes_unencrypted_to_another_machine_and_what_does_not(self):
        self.assertTrue(netguard.in_the_clear(self.LAN))
        self.assertTrue(netguard.in_the_clear("socks5h://10.0.0.2:1080"))
        for not_in_the_clear in ("http://127.0.0.1:11434/v1", "http://localhost/v1",
                                 "http://[::1]:8080/v1", "http://ai.localhost/v1",
                                 "https://192.168.1.50/v1", "https://api.example/v1",
                                 "http://192.168.1.50./v1", "not an address"):
            self.assertFalse(netguard.in_the_clear(not_in_the_clear), not_in_the_clear)
        self.assertTrue(netguard.is_loopback("127.8.9.10"))
        self.assertTrue(netguard.is_loopback("::ffff:127.0.0.1"))
        self.assertFalse(netguard.is_loopback("192.168.1.50"))
        self.assertFalse(netguard.is_loopback("127.0.0.1."))
        self.assertEqual(netguard.credentials("http://bob:hunter2@nas.local/v1"),
                         "bob:hunter2")
        self.assertEqual(netguard.credentials("http://nas.local/v1"), "")

    def test_a_key_and_passwords_going_there_are_listed_never_one_held_back(self):
        config = self.saved(custom_url=self.LAN, custom_api_key="sk-lan",
                            ollama_url="http://bob:hunter2@192.168.1.60:11434",
                            proxy="http://p:q@10.0.0.3:3128")
        found = {(what, address) for what, address, _s in config.keys_in_the_clear()}
        self.assertEqual(found, {
            ("The Other OpenAI-compatible endpoint API key", self.LAN),
            ("The password in Ollama's address", "http://bob:hunter2@192.168.1.60:11434"),
            ("The proxy's user name and password", "http://p:q@10.0.0.3:3128")})
        moved = self.saved(custom_url="http://192.168.1.99/v1/chat/completions")
        self.assertNotIn("The Other OpenAI-compatible endpoint API key",
                         {what for what, _a, _s in moved.keys_in_the_clear()})

    def asking(self, plain):
        from assistant.ui import dialogs
        asked = []

        def plain_question(parent, entries, after_no):
            asked.append(([what for what, _a, _s in entries], after_no))
            return plain

        patch = mock.patch.object(dialogs, "send_in_the_clear", plain_question)
        patch.start()
        self.addCleanup(patch.stop)
        return asked

    def dialog(self, config):
        from assistant.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(None, config, FakeClient(config))
        self.addCleanup(dialog.destroy)
        reached = []
        dialog.connect("response", lambda _d, response: reached.append(response))
        return dialog, reached

    def test_save_asks_and_no_keeps_the_dialog_open(self):
        asked = self.asking(False)
        dialog, reached = self.dialog(self.fresh())
        dialog.entries["custom_url"].set_text(self.LAN)
        dialog.entries["custom_api_key"].set_text("sk-lan")
        dialog.response(Gtk.ResponseType.OK)
        said = "No goes back to Settings, to change the address or the key."
        self.assertEqual(asked, [(["The Other OpenAI-compatible endpoint API key"], said)])
        self.assertEqual(reached, [], "Save did not go through")
        self.assertIn("would have gone unencrypted", dialog.save_held.get_text())
        # Yes, and it goes through; asked once for the dialog.
        asked[:] = []
        self.asking(True)
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(reached, [Gtk.ResponseType.OK])
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(reached, [Gtk.ResponseType.OK] * 2)

    def test_an_action_asks_and_no_sends_nothing(self):
        asked = self.asking(False)
        dialog, _reached = self.dialog(self.fresh())
        dialog.entries["custom_url"].set_text(self.LAN)
        dialog.entries["custom_api_key"].set_text("sk-lan")
        FakeClient.made = []
        dialog._on_test(None)
        self.assertEqual([no for _what, no in asked], ["No sends nothing."])
        self.assertEqual(FakeClient.made, [])
        self.assertEqual(dialog.test_result.get_text(), "Nothing was sent.")

    def test_not_asked_about_one_going_there_as_settings_opened_nor_to_this_machine(self):
        asked = self.asking(False)
        config = self.saved(custom_url=self.LAN, custom_api_key="sk-lan")
        dialog, reached = self.dialog(config)
        dialog.entries["custom_url"].set_text(self.LAN.replace("/chat/completions", ""))
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(asked, [])
        self.assertEqual(reached, [Gtk.ResponseType.OK])
        dialog, reached = self.dialog(self.fresh())
        dialog.entries["custom_url"].set_text("http://127.0.0.1:8000/v1")
        dialog.entries["custom_api_key"].set_text("sk-other")
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(asked, [])
        # A new key to the same machine is asked about.
        dialog, reached = self.dialog(self.fresh())
        dialog.entries["custom_url"].set_text(self.LAN)
        dialog.entries["custom_api_key"].set_text("sk-new")
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(len(asked), 1)

    def test_a_secret_already_going_there_is_held_back_until_confirmed(self):
        """Nothing is taken as confirmed (the author, Nov 14 2026): a key, or a password in
        an address, set to go over plain http to another machine before this was asked is
        not sent until it is confirmed once - refused in words naming the machine and
        where to confirm - and never deleted. To this machine itself nothing is held."""
        from assistant import proxy
        config = self.saved(custom_url=self.LAN, custom_api_key="sk-lan",
                            ollama_url="http://bob:hunter2@192.168.1.60:11434",
                            proxy="http://p:q@10.0.0.3:3128")
        held = providers.get(config, providers.CUSTOM)
        self.assertEqual(held.api_key, "")
        self.assertIn("would go over plain http, unencrypted, to http://192.168.1.50:8000",
                      held.problem())
        self.assertIn("Settings > Providers, under Keys held back, Confirm", held.problem())
        self.assertNotIn("sk-lan", held.problem())
        self.assertEqual(config.key_for("custom_api_key"), "")
        self.assertEqual(config.custom_api_key, "sk-lan", "never deleted")
        self.assertIn("password in Ollama's address", providers.get(config, providers.OLLAMA)
                      .problem())
        self.addCleanup(proxy.apply, Config(os.path.join(self.dir, "none.json"),
                                            keychain=MemoryKeychain()))
        self.assertFalse(proxy.apply(config))
        self.assertIn("proxy's user name and password", proxy.refusal())
        config.key_in_clear = {name: netguard.origin(address) for name, address in (
            ("custom_api_key", self.LAN), ("ollama_url", config.ollama_url),
            ("proxy", config.proxy))}
        config.save()
        config = self.fresh()
        self.assertEqual(providers.get(config, providers.CUSTOM).api_key, "sk-lan")
        self.assertIsNone(providers.get(config, providers.OLLAMA).problem())
        self.assertTrue(proxy.apply(config))
        here = self.saved(custom_url="http://127.0.0.1:8000/v1", custom_api_key="sk-here",
                          proxy="")
        self.assertEqual(providers.get(here, providers.CUSTOM).api_key, "sk-here")

    def test_settings_lists_one_held_back_with_confirm_and_save_keeps_the_yes(self):
        config = self.saved(custom_url=self.LAN, custom_api_key="sk-lan")
        asked = self.asking(False)
        dialog, reached = self.dialog(config)
        words, confirm = dialog.held_back["custom_api_key"]
        self.assertIn("would go over plain http", words.get_text())
        confirm.clicked()
        self.assertEqual(asked, [(["The Other OpenAI-compatible endpoint API key"],
                                  "Save keeps your answer.")])
        self.assertNotIn("custom_api_key", dialog.values()["key_in_clear"])
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(len(asked), 1, "Save does not ask about it again")
        self.assertEqual(reached, [Gtk.ResponseType.OK])
        asked = self.asking(True)
        dialog, _reached = self.dialog(config)
        dialog.held_back["custom_api_key"][1].clicked()
        self.assertEqual(dialog.values()["key_in_clear"],
                         {"custom_api_key": "http://192.168.1.50:8000"})

    def test_an_action_asks_about_one_held_back_and_sends_it_only_on_yes(self):
        config = self.saved(custom_url=self.LAN, custom_api_key="sk-lan")
        asked = self.asking(False)
        dialog, _reached = self.dialog(config)
        FakeClient.made = []
        dialog._on_test(None)
        self.assertEqual([no for _what, no in asked], ["No sends nothing."])
        self.assertEqual(FakeClient.made, [])
        self.asking(True)
        staged = dialog._ready_to_send(dialog._staged())
        self.assertEqual(staged.key_for("custom_api_key"), "sk-lan", "the Yes goes with it")

    def test_the_question_says_what_and_where_never_the_secret_and_no_is_the_default(self):
        from assistant.ui import dialogs
        seen = {}

        def run(dialog):
            no = dialog.get_widget_for_response(Gtk.ResponseType.NO)
            seen["default is No"] = dialog.get_default_widget() is no
            seen["secondary"] = dialog.get_property("secondary-text")
            return Gtk.ResponseType.DELETE_EVENT

        with mock.patch.object(Gtk.MessageDialog, "run", run):
            answer = dialogs.send_in_the_clear(None, [
                ("The proxy's user name and password", "http://p:q@10.0.0.3:3128", "p:q")],
                "No sends nothing.")
        self.assertIs(answer, False)
        self.assertTrue(seen["default is No"])
        self.assertIn("http://…@10.0.0.3:3128", seen["secondary"])
        self.assertNotIn("p:q", seen["secondary"])
        self.assertIn("No sends nothing.", seen["secondary"])


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
