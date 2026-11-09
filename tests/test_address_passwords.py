"""A password written into an address - the proxy's, an endpoint's - is kept in the
keychain, as an API key is, and never in config.json.

Every Config here has a file of its own, and a MemoryKeychain - or a keychain handed in,
standing in for the desktop's keyring across two runs. Nothing here reads or writes the
real keyring.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import netguard  # noqa: E402
from assistant.config import ADDRESS_SECRET, ADDRESSES, Config  # noqa: E402
from assistant.keychain import KeychainError, MemoryKeychain  # noqa: E402

PROXY = "https://alice:s3cret@proxy.example:3128"
ENDPOINT = "https://bob:hunter2@llm.example/v1/chat/completions"
OLLAMA = "https://carol:pw@ollama.example:11434"


class CountingKeychain(MemoryKeychain):
    def __init__(self):
        super().__init__()
        self.writes = 0

    def set(self, name, value):
        self.writes += 1
        super().set(name, value)


class LockedKeychain(MemoryKeychain):
    """A keyring that is there but refuses: locked, or its unlock prompt dismissed."""

    def get(self, name):
        raise KeychainError("Could not read the keyring: locked")

    def set(self, name, value):
        raise KeychainError("Could not write to the keyring: locked")


class WithoutPassword(unittest.TestCase):
    def test_the_user_name_and_password_go_and_the_address_stays(self):
        for url, plain in ((PROXY, "https://proxy.example:3128"),
                           ("socks5h://u:p@127.0.0.1:9050", "socks5h://127.0.0.1:9050"),
                           ("https://token@host.example/v1?a=b#c",
                            "https://host.example/v1?a=b#c"),
                           ("https://u:p@ss@host.example/", "https://host.example/"),
                           ("http://u:p@[fd00::5]:11434", "http://[fd00::5]:11434"),
                           ("u:p@proxy:3128", "proxy:3128"),
                           ("https://u:p@host:port/", "https://host:port/")):
            self.assertEqual(netguard.without_password(url), plain, url)

    def test_an_address_without_one_is_left_as_it_is(self):
        for url in ("", "http://localhost:11434", "HTTPS://Proxy.Example:3128/",
                    "https://host.example/a@b", "https://host.example/?to=a@b",
                    "socks5h://127.0.0.1:9050"):
            self.assertEqual(netguard.without_password(url), url, url)


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")

    def text(self):
        with open(self.path) as f:
            return f.read()

    def file(self):
        return json.loads(self.text())

    def write(self, values):
        with open(self.path, "w") as f:
            json.dump(values, f)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def assert_nowhere_in_the_file(self, *passwords):
        for password in passwords:
            self.assertNotIn(password, self.text())


class KeptInTheKeychain(Case):
    def test_an_address_typed_with_a_password_is_written_without_it(self):
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        config.proxy, config.custom_url, config.ollama_url = PROXY, ENDPOINT, OLLAMA
        config.save()
        self.assertEqual(self.file()["proxy"], "https://proxy.example:3128")
        self.assertEqual(self.file()["custom_url"],
                         "https://llm.example/v1/chat/completions")
        self.assertEqual(self.file()["ollama_url"], "https://ollama.example:11434")
        self.assert_nowhere_in_the_file("s3cret", "hunter2", "alice", "bob", "carol")
        self.assertEqual(keychain.get(ADDRESS_SECRET.format("proxy")), PROXY)
        # In use as typed, now and at the next start.
        self.assertEqual((config.proxy, config.custom_url, config.ollama_url),
                         (PROXY, ENDPOINT, OLLAMA))
        again = Config(self.path, keychain=keychain)
        self.assertEqual((again.proxy, again.custom_url, again.ollama_url),
                         (PROXY, ENDPOINT, OLLAMA))

    def test_one_left_in_the_file_moves_to_the_keychain(self):
        self.write({"proxy": PROXY, "custom_url": ENDPOINT, "max_tokens": 2048})
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        self.assertEqual((config.proxy, config.custom_url), (PROXY, ENDPOINT))
        self.assertEqual(keychain.get(ADDRESS_SECRET.format("custom_url")), ENDPOINT)
        self.assertEqual(self.file()["proxy"], "https://proxy.example:3128")
        self.assertEqual(self.file()["max_tokens"], 2048)
        self.assert_nowhere_in_the_file("s3cret", "hunter2")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_one_the_keychain_already_holds_goes_from_the_file(self):
        keychain = MemoryKeychain()
        keychain.set(ADDRESS_SECRET.format("proxy"), PROXY)
        self.write({"proxy": PROXY})
        self.assertEqual(Config(self.path, keychain=keychain).proxy, PROXY)
        self.assert_nowhere_in_the_file("s3cret")

    def test_the_password_goes_only_to_the_address_it_was_typed_for(self):
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        config.proxy = PROXY
        config.save()
        # config.json edited by hand, or by another copy of the app, to another proxy.
        values = self.file()
        values["proxy"] = "https://other-proxy.example:3128"
        self.write(values)
        self.assertEqual(Config(self.path, keychain=keychain).proxy,
                         "https://other-proxy.example:3128")

    def test_taking_the_password_out_takes_it_out_of_the_keychain(self):
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        config.proxy = PROXY
        config.save()
        config.proxy = "https://proxy.example:3128"
        config.save()
        self.assertIsNone(keychain.get(ADDRESS_SECRET.format("proxy")))
        self.assertEqual(Config(self.path, keychain=keychain).proxy,
                         "https://proxy.example:3128")

    def test_saving_other_settings_does_not_touch_the_keychain(self):
        keychain = CountingKeychain()
        config = Config(self.path, keychain=keychain)
        config.proxy = PROXY
        config.save()
        self.assertEqual(keychain.writes, 1)
        config.proxy = PROXY                    # as the Settings dialog sets every field
        config.use_premium = True
        config.save()
        self.assertEqual(keychain.writes, 1)
        self.assertEqual(config.proxy, PROXY)
        self.assert_nowhere_in_the_file("s3cret")

    def test_an_address_without_a_password_is_written_as_it_is(self):
        keychain = CountingKeychain()
        config = Config(self.path, keychain=keychain)
        config.ollama_url = "http://192.168.1.201:11434"
        config.save()
        self.assertEqual(self.file()["ollama_url"], "http://192.168.1.201:11434")
        self.assertEqual(keychain.writes, 0)

    def test_a_file_set_aside_as_unreadable_is_replaced_without_the_password(self):
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        config.proxy = PROXY
        config.save()
        with open(self.path, "w") as f:
            f.write("{ not json")
        config.default_language = "German"
        config.save()
        self.assertEqual(self.file()["default_language"], "German")
        self.assertEqual(self.file()["proxy"], "https://proxy.example:3128")
        self.assert_nowhere_in_the_file("s3cret")
        self.assertEqual(config.proxy, PROXY)


class WithNoKeyring(Case):
    """As an API key is: one in the file stays there and is used until the keychain can
    take it; one typed with no keychain to take it is used until the app quits, and
    never written."""

    def test_one_in_the_file_is_used_and_kept_there(self):
        self.write({"proxy": PROXY})
        config = Config(self.path, keychain=LockedKeychain())
        self.assertEqual(config.proxy, PROXY)
        self.assertIn("locked", config.keychain_error)
        config.default_language = "French"
        config.proxy = config.proxy                 # as the dialog's values() did
        with self.assertRaises(KeychainError):
            config.save()
        self.assertEqual(self.file()["proxy"], PROXY)
        self.assertEqual(Config(self.path, keychain=LockedKeychain()).proxy, PROXY)

    def test_one_typed_is_used_but_not_written(self):
        config = Config(self.path, keychain=LockedKeychain())
        config.custom_url = ENDPOINT
        config.default_language = "Spanish"
        with self.assertRaises(KeychainError):
            config.save()
        self.assertEqual(config.custom_url, ENDPOINT)               # for this run
        self.assertEqual(self.file()["custom_url"],
                         "https://llm.example/v1/chat/completions")
        self.assert_nowhere_in_the_file("hunter2")
        config.use_premium = True                                   # a later save
        config.save()
        self.assert_nowhere_in_the_file("hunter2")
        self.assertEqual(config.custom_url, ENDPOINT)

    def test_it_moves_once_the_keyring_is_back(self):
        self.write({"ollama_url": OLLAMA})
        Config(self.path, keychain=LockedKeychain())
        self.assertEqual(self.file()["ollama_url"], OLLAMA)
        keychain = MemoryKeychain()
        self.assertEqual(Config(self.path, keychain=keychain).ollama_url, OLLAMA)
        self.assert_nowhere_in_the_file("carol", "pw@")


class EveryAddressSetting(unittest.TestCase):
    def test_they_are_the_settings_that_hold_an_address(self):
        from assistant.config import DEFAULTS
        self.assertEqual(set(ADDRESSES), {"proxy", "custom_url", "ollama_url"})
        self.assertTrue(set(ADDRESSES) <= set(DEFAULTS))


if __name__ == "__main__":
    unittest.main()
