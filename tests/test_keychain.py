"""The API key lives in the keychain, never in config.json.

Every Config here has a file of its own, so its secrets go to a MemoryKeychain - or to
one handed in, standing in for the desktop's keyring across two runs. Nothing here
reads or writes the real keyring.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant.config import Config
from assistant.keychain import KeychainError, MemoryKeychain, SystemKeychain


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


class KeyInTheKeychain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")

    def _file(self):
        with open(self.path) as f:
            return json.load(f)

    def _write(self, values):
        with open(self.path, "w") as f:
            json.dump(values, f)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def test_a_config_with_its_own_file_never_reaches_the_keyring(self):
        self.assertIsInstance(Config(self.path).keychain, MemoryKeychain)

    def test_the_key_is_saved_to_the_keychain_and_not_the_file(self):
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        config.api_key = "sk-secret"
        config.default_language = "Japanese"
        config.save()
        self.assertNotIn("api_key", self._file())
        with open(self.path) as f:
            self.assertNotIn("sk-secret", f.read())
        self.assertEqual(keychain.get("api_key"), "sk-secret")
        # The next run reads it back from there.
        again = Config(self.path, keychain=keychain)
        self.assertEqual(again.api_key, "sk-secret")
        self.assertEqual(again.default_language, "Japanese")

    def test_a_key_left_in_the_file_moves_to_the_keychain(self):
        self._write({"api_key": "sk-old", "max_tokens": 2048})
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        self.assertEqual(config.api_key, "sk-old")
        self.assertEqual(keychain.get("api_key"), "sk-old")
        self.assertEqual(self._file()["max_tokens"], 2048)
        self.assertNotIn("api_key", self._file())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_the_keychains_key_wins_over_a_stale_one_in_the_file(self):
        keychain = MemoryKeychain()
        keychain.set("api_key", "sk-current")
        self._write({"api_key": "sk-stale"})
        self.assertEqual(Config(self.path, keychain=keychain).api_key, "sk-current")
        self.assertNotIn("api_key", self._file())

    def test_saving_other_settings_does_not_touch_the_keychain(self):
        keychain = CountingKeychain()
        config = Config(self.path, keychain=keychain)
        config.api_key = "sk-secret"
        config.save()
        self.assertEqual(keychain.writes, 1)
        # Settings writes every field back on OK, the key unchanged among them.
        config.api_key = "sk-secret"
        config.use_premium = True
        config.save()
        self.assertEqual(keychain.writes, 1)
        self.assertEqual(keychain.get("api_key"), "sk-secret")

    def test_clearing_the_key_removes_it_from_the_keychain(self):
        keychain = MemoryKeychain()
        config = Config(self.path, keychain=keychain)
        config.api_key = "sk-secret"
        config.save()
        config.api_key = ""
        config.save()
        self.assertIsNone(keychain.get("api_key"))
        self.assertEqual(Config(self.path, keychain=keychain).api_key, "")

    def test_a_locked_keyring_keeps_the_files_key_rather_than_losing_it(self):
        self._write({"api_key": "sk-old"})
        config = Config(self.path, keychain=LockedKeychain())
        self.assertEqual(config.api_key, "sk-old")
        self.assertIn("locked", config.keychain_error)
        config.use_premium = True
        config.save()
        self.assertEqual(self._file()["api_key"], "sk-old")

    def test_a_new_key_the_keyring_refuses_is_used_but_not_written(self):
        config = Config(self.path, keychain=LockedKeychain())
        config.api_key = "sk-typed"
        config.default_language = "French"
        with self.assertRaises(KeychainError):
            config.save()
        self.assertEqual(config.api_key, "sk-typed")          # for this run
        self.assertNotIn("api_key", self._file())             # never in plain text
        self.assertEqual(self._file()["default_language"], "French")

    def test_a_detached_copy_shares_the_keychain_but_saves_nothing(self):
        keychain = CountingKeychain()
        config = Config(self.path, keychain=keychain)
        trial = config.detached(api_key="sk-trial")
        self.assertEqual(trial.api_key, "sk-trial")
        self.assertIs(trial.keychain, keychain)
        self.assertEqual(keychain.writes, 0)
        self.assertEqual(config.api_key, "")


class SystemKeychainFailures(unittest.TestCase):
    def test_no_secure_backend_is_a_problem_not_a_crash(self):
        import keyring
        from keyring.backends import fail
        before = keyring.get_keyring()
        keyring.set_keyring(fail.Keyring())
        try:
            chain = SystemKeychain()
            self.assertIn("keyring", chain.problem())
            with self.assertRaises(KeychainError):
                chain.set("api_key", "x")
            with self.assertRaises(KeychainError):
                chain.get("api_key")
        finally:
            keyring.set_keyring(before)


if __name__ == "__main__":
    unittest.main()
