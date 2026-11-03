"""A config.json that cannot be read, wholly or in part, keeps captures private.

Up to 3.4.0 a file broken while the app ran - by hand, mid-edit - was written over at
the next save with the defaults, which the running app then adopted as its own: Privacy
Mode off and every model back to NanoGPT's, there and then, without a word. And a file
broken before the app started, or a privacy_mode it could not read as true or false,
started it with Privacy Mode off.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant.config import Config  # noqa: E402

OWN = {"privacy_mode": True, "text_provider": "ollama", "text_model": "qwen3:32b",
       "ollama_url": "http://192.168.1.201:11434", "default_language": "German"}


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)
        os.chmod(self.path, 0o600)


class BrokenWhileTheAppRuns(TempDir):
    def test_a_save_keeps_what_the_app_holds(self):
        self.write(json.dumps(OWN))
        config = Config(self.path)
        self.write('{"privacy_mode": true,, "text_model": "qwen3:32b"}')    # mid-edit
        config.chat_zoom = 1.1                                               # Ctrl+plus
        config.save()
        for key, value in OWN.items():
            self.assertEqual(getattr(config, key), value, key)
        again = Config(self.path)
        self.assertIsNone(again.load_error)
        self.assertEqual({key: getattr(again, key) for key in OWN}, OWN)
        self.assertEqual(again.chat_zoom, 1.1)
        with open(self.path + ".corrupt") as f:
            self.assertIn(",,", f.read())


class BrokenBeforeItStarted(TempDir):
    def test_privacy_mode_stays_on_when_the_file_says_so(self):
        self.write('{"privacy_mode": true, "privacy_strict": "yes", "max_tokens": 4096,}')
        config = Config(self.path)
        self.assertIsNotNone(config.load_error)
        self.assertTrue(config.privacy_mode)
        self.assertTrue(config.privacy_strict)
        self.assertTrue(any("privacy_mode stays on" in w for w in config.load_warnings))

    def test_and_off_when_it_does_not(self):
        for text in ('{"max_tokens": 4096,}', '{"privacy_mode": false,}', "\x00\x01"):
            self.write(text)
            self.assertFalse(Config(self.path).privacy_mode, text)

    def test_a_value_neither_true_nor_false_is_read_as_on(self):
        for value in ("enabled", 2, "sometimes", [True]):
            self.write(json.dumps({"privacy_mode": value, "privacy_strict": value}))
            config = Config(self.path)
            self.assertTrue(config.privacy_mode, value)
            self.assertTrue(config.privacy_strict, value)
            self.assertTrue(any("privacy_mode" in w and "using true" in w
                                for w in config.load_warnings), value)
        for value in (False, "off", 0, "no"):
            self.write(json.dumps({"privacy_mode": value}))
            self.assertFalse(Config(self.path).privacy_mode, value)

    def test_and_so_is_null(self):
        """null passed as "undecided", which a setting may be until it is decided - and
        Privacy Mode undecided was off. It still is that for one that may be."""
        self.write(json.dumps({"privacy_mode": None, "privacy_strict": None,
                               "hotkeys": None}))
        config = Config(self.path)
        self.assertIs(config.privacy_mode, True)
        self.assertIs(config.privacy_strict, True)
        self.assertIn("privacy_mode in config.json is null, which is not true or false; "
                      "using true.", config.load_warnings)
        self.assertIsNone(config.hotkeys)


if __name__ == "__main__":
    unittest.main()
