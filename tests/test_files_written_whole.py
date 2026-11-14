"""Every file that matters is written whole or not at all, and every folder of the app's
own is its owner's alone (the DA apps' security standard, Nov 2026).

Written whole: to a temp file of a name of its own beside it (O_CREAT|O_EXCL, 0600),
flushed to the disk, os.replace'd into place, and the folder flushed - never a fixed temp
name two writers share, never a rename of bytes not yet on the disk. Folders: 0700, also
when they were there already.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: E402,F401  (pins GI versions)
from assistant import atomic, errors, keybindings, models, paths, webkitguard  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.keychain import MemoryKeychain  # noqa: E402


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Flushed:
    """os.fsync, as atomic.py calls it: what was flushed, a file or a folder, by name."""

    def __init__(self, test):
        self.flushed = []
        real = os.fsync

        def fsync(fd):
            self.flushed.append(os.readlink(f"/proc/self/fd/{fd}"))
            real(fd)

        patch = mock.patch.object(atomic.os, "fsync", fsync)
        patch.start()
        test.addCleanup(patch.stop)


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)


class WrittenWhole(Case):
    def test_a_file_is_flushed_put_in_place_and_its_folder_flushed_after(self):
        path = os.path.join(self.dir, "x.json")
        flushed = Flushed(self)
        atomic.write_json(path, {"a": 1})
        self.assertEqual(json.load(open(path)), {"a": 1})
        self.assertEqual(mode_of(path), 0o600)
        # The temp file of its own, then the folder; nothing left beside it.
        self.assertEqual(len(flushed.flushed), 2)
        self.assertTrue(os.path.basename(flushed.flushed[0]).startswith(".x.json."))
        self.assertEqual(flushed.flushed[1], self.dir)
        self.assertEqual(os.listdir(self.dir), ["x.json"])

    def test_one_that_fails_leaves_what_was_there_and_no_temp_file(self):
        path = os.path.join(self.dir, "x.json")
        atomic.write_json(path, {"a": 1})
        with mock.patch.object(atomic.os, "replace", side_effect=OSError("full")), \
                self.assertRaises(OSError):
            atomic.write_json(path, {"a": 2})
        self.assertEqual(json.load(open(path)), {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["x.json"])

    def test_one_for_someone_else_has_a_new_files_permissions(self):
        path = os.path.join(self.dir, "x.desktop")
        atomic.write_text(path, "[Desktop Entry]\n", mode=atomic.AS_A_NEW_FILE)
        self.assertEqual(mode_of(path), 0o666 & ~atomic.umask())


class TheAppsFiles(Case):
    def test_config_json_is_written_whole_and_its_folder_made_its_owners(self):
        os.chmod(self.dir, 0o755)           # as an older version, or by hand, left it
        path = os.path.join(self.dir, "config.json")
        config = Config(path, keychain=MemoryKeychain())
        flushed = Flushed(self)
        config.default_language = "German"
        config.save()
        self.assertEqual(mode_of(self.dir), 0o700)
        self.assertEqual(mode_of(path), 0o600)
        self.assertIn(self.dir, flushed.flushed)
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_the_model_cache_is_written_whole_into_a_folder_of_its_owners(self):
        cache = os.path.join(self.dir, "cache")
        os.makedirs(cache, mode=0o755)
        os.chmod(cache, 0o755)
        flushed = Flushed(self)
        with mock.patch.object(models, "CACHE_DIR", cache), \
                mock.patch.object(models, "CACHE_FILE", os.path.join(cache, "models.json")):
            models.save_cache([{"id": "a/b"}], "https://one.example/v1/models")
        self.assertEqual(mode_of(cache), 0o700)
        self.assertIn(cache, flushed.flushed)
        listed = os.listdir(cache)
        self.assertEqual(len(listed), 1)
        self.assertEqual(mode_of(os.path.join(cache, listed[0])), 0o600)

    def test_the_webkit_breadcrumb_and_a_trimmed_log_are_written_whole(self):
        log_dir = os.path.join(self.dir, "logs")
        state = os.path.join(log_dir, "webkit-crash")
        log = os.path.join(log_dir, "errors.log")
        flushed = Flushed(self)
        with mock.patch.object(webkitguard, "LOG_DIR", log_dir), \
                mock.patch.object(webkitguard, "STATE_FILE", state), \
                mock.patch.object(webkitguard, "_loading", set()):
            webkitguard.note_attempt("view")
        self.assertIn(os.path.realpath(log_dir), flushed.flushed)
        self.assertEqual(mode_of(log_dir), 0o700)
        self.assertEqual(mode_of(state), 0o600)
        with open(log, "w") as f:
            f.write("x" * (errors.MAX_BYTES + 10))
        flushed.flushed.clear()
        with mock.patch.object(errors, "LOG_DIR", log_dir), \
                mock.patch.object(errors, "LOG_FILE", log):
            errors.record("and then")
        self.assertIn(os.path.realpath(log_dir), flushed.flushed)
        self.assertLess(os.path.getsize(log), errors.MAX_BYTES)

    def test_a_desktop_file_has_a_temp_file_of_its_own_never_a_fixed_name(self):
        flushed = Flushed(self)
        with mock.patch.object(keybindings, "launcher", return_value="/usr/bin/x"):
            path = keybindings.app_entry(data_home=self.dir)
        self.assertTrue(os.path.basename(flushed.flushed[0]).startswith("."))
        self.assertNotEqual(flushed.flushed[0], path + ".tmp")
        self.assertEqual(mode_of(path), 0o666 & ~atomic.umask())

    def test_a_folder_of_the_apps_own_is_made_its_owners_if_it_was_not(self):
        folder = os.path.join(self.dir, "made")
        paths.own_folder(folder)
        self.assertEqual(mode_of(folder), 0o700)
        os.chmod(folder, 0o775)
        paths.own_folder(folder)
        self.assertEqual(mode_of(folder), 0o700)


class AnAnswerSaved(Case):
    def test_is_on_the_disk_before_it_replaces_what_was_there(self):
        from assistant.ui.chat import write_whole
        path = os.path.join(self.dir, "answer.md")
        with open(path, "w") as f:
            f.write("old")
        flushed = Flushed(self)
        write_whole(path, b"new")
        self.assertEqual(open(path).read(), "new")
        self.assertEqual(len(flushed.flushed), 2)
        self.assertTrue(flushed.flushed[0].endswith(".part"))
        self.assertEqual(flushed.flushed[1], self.dir)


if __name__ == "__main__":
    unittest.main()
