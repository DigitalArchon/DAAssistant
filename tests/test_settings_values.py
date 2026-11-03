"""What Settings writes back into config.json.

A save made while the dictionaries folder cannot be read must not throw away what was
set for the dictionaries in it.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402

from assistant.config import Config  # noqa: E402


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")


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
