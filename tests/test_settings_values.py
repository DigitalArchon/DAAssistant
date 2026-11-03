"""What config.json may hold, and what Settings writes back into it.

A number edited in by hand has to be one its setting can take, not only a number; and
a save made while the dictionaries folder cannot be read must not throw away what was
set for the dictionaries in it.
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

from assistant import gtkcompat  # noqa: F401,E402

from assistant import config as config_module  # noqa: E402
from assistant.config import DEFAULTS, Config  # noqa: E402


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")


class HandEditedNumbersOutOfRange(TempDir):
    def load(self, text):
        with open(self.path, "w") as f:
            f.write(text)
        return Config(self.path)

    def test_a_negative_cap_is_the_default_and_said(self):
        config = self.load('{"capture_max_long_edge": "-3000"}')
        self.assertEqual(config.capture_max_long_edge, DEFAULTS["capture_max_long_edge"])
        [warning] = config.load_warnings
        self.assertIn("capture_max_long_edge", warning)
        self.assertIn("at least 64", warning)

    def test_and_a_capture_is_sent_at_its_size_not_as_one_pixel(self):
        from PIL import Image
        from assistant import imaging
        config = self.load('{"capture_max_long_edge": -3000}')
        _image, info = imaging.prepare_for_vision(Image.new("RGB", (2000, 100)), config)
        self.assertEqual(info["sent_size"], (2000, 100))

    def test_no_max_tokens_is_not_a_reply_without_a_limit(self):
        from assistant.llm import LLMClient
        config = self.load('{"max_tokens": 0}')
        self.assertEqual(config.max_tokens, DEFAULTS["max_tokens"])
        payload = LLMClient(config)._payload("m", [{"role": "user", "content": "q"}])
        self.assertGreaterEqual(payload["max_tokens"], DEFAULTS["max_tokens"])

    def test_a_text_size_of_nan_or_beyond_the_steps_is_the_default(self):
        for text in ('{"chat_zoom": NaN}', '{"chat_zoom": Infinity}',
                     '{"chat_zoom": "nan"}', '{"chat_zoom": 40}', '{"chat_zoom": 0}'):
            config = self.load(text)
            self.assertEqual(config.chat_zoom, 1.0, text)
            self.assertEqual(len(config.load_warnings), 1, text)

    def test_the_text_sizes_are_the_ones_the_window_steps_through(self):
        from assistant.ui import chat
        self.assertEqual(config_module.RANGES["chat_zoom"],
                         (chat.ZOOM_STEPS[0], chat.ZOOM_STEPS[-1]))

    def test_the_rest_are_held_to_theirs(self):
        config = self.load(json.dumps({"request_timeout": 0, "chat_prompt_height": -40,
                                       "capture_target_long_edge": 10}))
        for key in ("request_timeout", "chat_prompt_height", "capture_target_long_edge"):
            self.assertEqual(getattr(config, key), DEFAULTS[key], key)
        self.assertEqual(len(config.load_warnings), 3)

    def test_what_is_in_range_is_kept_as_it_is(self):
        config = self.load(json.dumps({"capture_max_long_edge": 64, "max_tokens": 1,
                                       "chat_zoom": 3.0, "chat_prompt_height": 0,
                                       "request_timeout": "600"}))
        self.assertEqual((config.capture_max_long_edge, config.max_tokens,
                          config.chat_zoom, config.chat_prompt_height,
                          config.request_timeout), (64, 1, 3.0, 0, 600))
        self.assertEqual(config.load_warnings, [])

    def test_every_range_is_a_setting_that_is_a_number(self):
        for key in config_module.RANGES:
            self.assertIsInstance(DEFAULTS[key], (int, float), key)
            self.assertEqual(config_module._in_range(key, DEFAULTS[key]), DEFAULTS[key])


def write_dsl(path, name):
    """A DSL dictionary with one entry, as Lingvo writes one: UTF-16LE, CRLF."""
    text = f'#NAME "{name}"\n\nglorp\n\t[m1]said by {name}'
    with open(path, "wb") as f:
        f.write(b"\xff\xfe" + text.replace("\n", "\r\n").encode("utf-16-le"))


class DictionariesNotListedNow(TempDir):
    """Settings lists the dictionaries in the folder; what was set for any it cannot
    list - the folder unmounted, renamed, a dictionary moved out for now - stays set."""

    def settings(self, config):
        from assistant.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(None, config, type("Client", (), {"config": config}))
        self.addCleanup(dialog.destroy)
        return dialog

    def test_a_folder_that_cannot_be_read_keeps_the_order_and_what_is_off(self):
        config = Config(self.path)
        config.dictionaries_dir = os.path.join(self.dir, "not-mounted")
        config.dictionaries_order = ["/d/b.dsl", "/d/a.dsl", "/d/c.dsl"]
        config.dictionaries_disabled = ["/d/a.dsl"]
        values = self.settings(config).values()
        self.assertEqual(values["dictionaries_order"], ["/d/b.dsl", "/d/a.dsl", "/d/c.dsl"])
        self.assertEqual(values["dictionaries_disabled"], ["/d/a.dsl"])

    def test_those_listed_are_as_set_here_and_the_rest_as_they_were(self):
        folder = os.path.join(self.dir, "dicts")
        os.makedirs(folder)
        a, c = os.path.join(folder, "a.dsl"), os.path.join(folder, "c.dsl")
        write_dsl(a, "Alpha")
        write_dsl(c, "Charlie")
        gone = os.path.join(self.dir, "elsewhere", "b.dsl")
        config = Config(self.path)
        config.dictionaries_dir = folder
        config.dictionaries_order = [c, gone, a]
        config.dictionaries_disabled = [c, gone]
        dialog = self.settings(config)
        self.assertEqual(dialog._dictionary_order(), [c, a])
        dialog._dictionary_checks[c].set_active(True)         # on again
        dialog._dictionary_checks[a].set_active(False)        # and this one off
        dialog._place_dictionary(a, 0)
        values = dialog.values()
        self.assertEqual(values["dictionaries_order"], [a, c, gone])
        self.assertEqual(values["dictionaries_disabled"], sorted([a, gone]))


if __name__ == "__main__":
    unittest.main()
