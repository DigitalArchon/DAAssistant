"""The app's file choosers leave GTK's recent files list as it was (recentguard.py): GTK's
chooser adds every file chosen to $XDG_DATA_HOME/recently-used.xbel, writes the whole list
from its model a quarter of a second later (an entry older than 30 days dropped) and again
as the main loop ends, and the guard puts back what those writes changed.

The logic is held here with a stand-in for GTK's manager and a list in a folder of the
test's own; then a real chooser, under Xvfb in a process of its own (HOME and the XDG
folders fresh temporary ones, no session bus of the user's): the attach dialog and a page's
<input type="file">, a list seeded with an entry older than 30 days and the chosen file's
own earlier entry, which must be byte for byte as seeded after GTK's write and after the
loop has ended - and, with the guard off, isn't (GTK's writes are what the guard undoes).
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: F401  (the suite's own cache and data folder, before the app)

from assistant import recentguard

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = 1_800_000_000
APP = "da-assistant"


def entry(href, stamp="2026-09-01T10:00:00.000000Z", app="org.gnome.TextEditor"):
    return (f'  <bookmark href="{href}" added="{stamp}" modified="{stamp}" visited="{stamp}">\n'
            f'    <info>\n'
            f'      <metadata owner="http://freedesktop.org">\n'
            f'        <mime:mime-type type="text/plain"/>\n'
            f'        <bookmark:applications>\n'
            f'          <bookmark:application name="{app}" exec="&apos;{app} %u&apos;" '
            f'modified="{stamp}" count="1"/>\n'
            f'        </bookmark:applications>\n'
            f'      </metadata>\n'
            f'    </info>\n'
            f'  </bookmark>\n').encode()


def xbel(*entries):
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n<xbel version="1.0"\n'
            b'      xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"\n'
            b'      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info"\n'
            b'>\n' + b"".join(entries) + b'</xbel>')


class Info:
    def __init__(self, stamp):
        self.stamp = stamp

    def has_application(self, name):
        return name == APP

    def get_application_info(self, name):
        return (f"'{name} %u'", 1, self.stamp)


class Manager:
    """GTK's recent manager as the guard sees it: "changed" handlers run after its own
    write, and its model (lookup_item)."""

    def __init__(self):
        self.handlers, self.model = [], {}

    def connect_after(self, name, handler):
        assert name == "changed"
        self.handlers.append(handler)

    def changed(self):
        for handler in self.handlers:
            handler(self)

    def lookup_item(self, uri):
        if uri not in self.model:
            raise LookupError(f"Unable to find an item with URI '{uri}'")
        return Info(self.model[uri])


def gtk_writes(path, data):
    """As GLib's g_file_set_contents writes the list: a file of its own renamed over it
    (a link in its place replaced, not followed), then 0600."""
    temp = path + ".XXXXXX"
    with open(temp, "wb") as f:
        f.write(data)
    os.replace(temp, path)
    os.chmod(path, 0o600)


class Timers:
    def __init__(self):
        self.waiting = []

    def later(self, seconds, call):
        item = [seconds, call]
        self.waiting.append(item)
        return lambda: self.waiting.remove(item)

    def run(self):
        for _seconds, call in list(self.waiting):
            self.waiting.remove([_seconds, call])
            call()


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.data = os.path.join(self.dir, "data")
        os.mkdir(self.data, 0o700)
        self.recent = os.path.join(self.data, "recently-used.xbel")
        self.manager, self.timers = Manager(), Timers()
        self.seed = xbel(entry("file:///srv/notes.txt"),
                         entry("file:///srv/old.txt", "2026-01-02T03:04:05.000000Z"))

    def keeper(self):
        return recentguard.Keeper(self.manager, self.recent, APP, clock=lambda: NOW,
                                  later=self.timers.later)

    def write(self, data, mode=0o600, path=None):
        path = path or self.recent
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode)

    def read(self, path=None):
        with open(path or self.recent, "rb") as f:
            return f.read()

    def choose(self, keeper, *uris, taken_in=True):
        """A chooser ends with `uris` chosen, and GTK takes them in and writes the list."""
        keeper.chose(list(uris))
        if taken_in:
            for uri in uris:
                self.manager.model[uri] = NOW
        gtk_writes(self.recent, xbel(entry("file:///srv/notes.txt"),
                                     *(entry(uri, app=APP) for uri in uris)))
        self.manager.changed()


class PendingWrites(Case):
    def test_the_list_gtk_writes_after_a_choice_is_put_back_byte_for_byte_with_its_mode(self):
        self.write(self.seed, 0o640)
        keeper = self.keeper()
        self.choose(keeper, "file:///home/u/report.pdf")
        self.assertEqual(self.read(), self.seed)
        self.assertEqual(stat.S_IMODE(os.lstat(self.recent).st_mode), 0o640)
        self.assertFalse(keeper.pending)
        self.assertEqual(self.timers.waiting, [])

    def test_another_programs_change_is_kept_and_is_what_a_later_choice_puts_back(self):
        self.write(self.seed)
        keeper = self.keeper()
        theirs = self.seed.replace(b"</xbel>", entry("file:///srv/theirs.odt") + b"</xbel>")
        gtk_writes(self.recent, theirs)         # another program's write, GTK re-reads it
        self.manager.changed()
        self.assertEqual(self.read(), theirs)
        self.choose(keeper, "file:///home/u/report.pdf")
        self.assertEqual(self.read(), theirs)

    def test_a_list_that_was_not_there_is_taken_away_again(self):
        keeper = self.keeper()
        self.choose(keeper, "file:///home/u/report.pdf")
        self.assertFalse(os.path.lexists(self.recent))

    def test_the_write_stays_pending_until_gtk_holds_every_file_chosen(self):
        """GTK adds the files one by one, as each one's details come: a write with some of
        them is followed by another."""
        self.write(self.seed)
        keeper = self.keeper()
        keeper.chose(["file:///a.txt", "file:///b.txt"])
        self.manager.model["file:///a.txt"] = NOW
        self.manager.model["file:///b.txt"] = NOW - 86400 * 9    # its earlier entry, not ours
        gtk_writes(self.recent, b"with a")
        self.manager.changed()
        self.assertEqual(self.read(), self.seed)
        self.assertTrue(keeper.pending)
        self.manager.model["file:///b.txt"] = NOW
        gtk_writes(self.recent, b"with a and b")
        self.manager.changed()
        self.assertEqual(self.read(), self.seed)
        self.assertFalse(keeper.pending)

    def test_where_gtk_never_takes_the_files_in_the_wait_ends_and_others_changes_count_again(
            self):
        """Recent files switched off by the user: GTK writes an empty list over theirs and adds
        nothing."""
        self.write(self.seed)
        keeper = self.keeper()
        keeper.chose(["file:///a.txt"])
        gtk_writes(self.recent, xbel())
        self.manager.changed()
        self.assertEqual(self.read(), self.seed)
        self.assertTrue(keeper.pending)
        self.assertEqual([seconds for seconds, _ in self.timers.waiting],
                         [recentguard.HOLD_SECONDS])
        self.timers.run()
        self.assertFalse(keeper.pending)
        gtk_writes(self.recent, b"theirs")
        self.manager.changed()
        self.assertEqual(self.read(), b"theirs")

    def test_the_list_gtk_writes_as_the_loop_ends_is_put_back(self):
        self.write(self.seed)
        keeper = self.keeper()
        self.choose(keeper, "file:///home/u/report.pdf")
        gtk_writes(self.recent, b"the model, as the loop ended")   # no signal: put_back
        keeper.put_back()
        self.assertEqual(self.read(), self.seed)

    def test_a_chooser_left_with_nothing_chosen_pends_nothing(self):
        self.write(self.seed)
        keeper = self.keeper()
        keeper.chose([])
        self.assertFalse(keeper.pending)


class WhatIsWritten(Case):
    def test_a_link_to_a_regular_file_is_made_again_and_written_through(self):
        target = os.path.join(self.data, "kept", "recent.xbel")
        os.mkdir(os.path.dirname(target))
        self.write(self.seed, 0o644, target)
        os.symlink("kept/recent.xbel", self.recent)
        keeper = self.keeper()
        self.choose(keeper, "file:///home/u/report.pdf")       # the link replaced by a file
        self.assertEqual(os.readlink(self.recent), "kept/recent.xbel")
        self.assertEqual(self.read(target), self.seed)
        keeper.chose(["file:///home/u/two.pdf"])
        self.write(b"written through the link", 0o600, target)
        self.manager.model["file:///home/u/two.pdf"] = NOW
        self.manager.changed()
        self.assertEqual(os.readlink(self.recent), "kept/recent.xbel")
        self.assertEqual(self.read(target), self.seed)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)

    def test_a_link_to_what_is_no_file_is_made_again_and_what_it_names_is_never_written(self):
        os.symlink(os.devnull, self.recent)
        keeper = self.keeper()
        with mock.patch.object(recentguard, "_write_whole", wraps=recentguard._write_whole) \
                as written:
            self.choose(keeper, "file:///home/u/report.pdf")
            keeper.put_back()
        self.assertEqual(os.readlink(self.recent), os.devnull)
        written.assert_not_called()

    def test_a_link_naming_a_file_outside_the_data_folder_is_never_written_through(self):
        outside = os.path.join(self.dir, "elsewhere.xbel")
        self.write(self.seed, path=outside)
        os.symlink(outside, self.recent)
        keeper = self.keeper()
        keeper.chose(["file:///home/u/report.pdf"])
        self.write(b"changed through the link", path=outside)
        self.manager.model["file:///home/u/report.pdf"] = NOW
        with mock.patch.object(recentguard, "_say") as said:
            self.manager.changed()
        self.assertEqual(self.read(outside), b"changed through the link")
        self.assertEqual(os.readlink(self.recent), outside)
        said.assert_called_once()

    def test_a_folder_in_the_lists_place_is_left_alone(self):
        os.mkdir(self.recent)
        keeper = self.keeper()
        with mock.patch.object(recentguard, "_say"):
            self.choose_into_folder(keeper)
        self.assertTrue(os.path.isdir(self.recent))

    def choose_into_folder(self, keeper):
        keeper.chose(["file:///a.txt"])
        self.manager.model["file:///a.txt"] = NOW
        self.manager.changed()
        keeper.put_back()

    def test_a_failure_is_said_on_stderr_and_never_raised(self):
        self.write(self.seed)
        keeper = self.keeper()
        with mock.patch.object(recentguard, "_write_whole", side_effect=OSError("disk full")), \
                mock.patch.object(recentguard, "_say") as said:
            self.choose(keeper, "file:///home/u/report.pdf")
            keeper.put_back()
        self.assertEqual(said.call_count, 2)
        self.assertIn("disk full", said.call_args[0][0])

    def test_a_write_leaves_no_temp_file_and_names_none_twice(self):
        self.write(self.seed)
        keeper = self.keeper()
        self.choose(keeper, "file:///home/u/report.pdf")
        self.assertEqual(os.listdir(self.data), ["recently-used.xbel"])


class TheProcesssKeeper(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, recentguard, "_keeper", recentguard._keeper)
        recentguard._keeper = None
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def gtk(self, data_dir, filename):
        manager = Manager()
        manager.props = type("Props", (), {"filename": filename})()
        GLib = type("GLib", (), {"get_user_data_dir": staticmethod(lambda: data_dir),
                                 "get_application_name": staticmethod(lambda: APP)})
        Gtk = type("Gtk", (), {"RecentManager": type("RM", (), {
            "get_default": staticmethod(lambda: manager)})})
        return mock.patch.object(recentguard, "_gtk", return_value=(GLib, Gtk))

    def test_the_list_is_kept_only_where_gtk_keeps_it_in_the_data_folder(self):
        data = os.path.join(self.dir, "data")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": data}), \
                self.gtk(data, os.path.join(data, "recently-used.xbel")):
            self.assertEqual(recentguard.keeper().path, os.path.join(data, "recently-used.xbel"))

    def test_a_list_elsewhere_or_another_data_folder_is_never_kept(self):
        data = os.path.join(self.dir, "data")
        for glib_dir, filename in ((data, os.path.join(self.dir, "recently-used.xbel")),
                                   (os.path.join(self.dir, "other"),
                                    os.path.join(data, "recently-used.xbel"))):
            recentguard._keeper = None
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": data}), \
                    self.gtk(glib_dir, filename), mock.patch.object(recentguard, "_say") as said:
                self.assertIsNone(recentguard.keeper())
                self.assertIsNone(recentguard.keeper())
            said.assert_called_once()

    def test_the_data_folder_is_xdg_data_home_where_absolute_else_under_home(self):
        self.assertEqual(recentguard.data_home({"XDG_DATA_HOME": "/x/data", "HOME": "/h"}),
                         "/x/data")
        self.assertEqual(recentguard.data_home({"XDG_DATA_HOME": "rel", "HOME": "/h"}),
                         "/h/.local/share")
        self.assertEqual(recentguard.data_home({"HOME": "/h"}), "/h/.local/share")


class Chooser:
    """A file chooser as run() sees one: run() emits "response" to what is connected, as GTK's
    do, then returns it - noting how often the keeper had been told by then."""

    def __init__(self, response, keeper):
        self.response, self.keeper, self.handlers, self.told_by_then = response, keeper, [], None

    def connect(self, name, handler):
        assert name == "response"
        self.handlers.append(handler)

    def run(self):
        for handler in self.handlers:
            handler(self, self.response)
        self.told_by_then = self.keeper.chose.call_count
        return self.response

    def get_uris(self):
        return ["file:///a.txt"]


class Choosers(unittest.TestCase):
    def test_run_tells_the_keeper_what_an_accepted_chooser_chose_as_it_answers(self):
        """Before run() comes back: GTK can write the list before then."""
        keeper = mock.Mock()
        with mock.patch.object(recentguard, "keeper", return_value=keeper):
            for response, chose in ((-3, True), (-5, True), (-6, False), (-4, False)):
                chooser = Chooser(response, keeper)
                self.assertEqual(recentguard.run(chooser), response)
                self.assertEqual(chooser.told_by_then, 1 if chose else 0)
                if chose:
                    keeper.chose.assert_called_once_with(["file:///a.txt"])
                else:
                    keeper.chose.assert_not_called()
                keeper.reset_mock()

    def test_a_stand_in_chooser_with_no_signals_is_told_once_it_has_answered(self):
        chooser = mock.Mock(spec=["run", "get_uris"])
        chooser.run.return_value = -3
        chooser.get_uris.return_value = ["file:///a.txt"]
        with mock.patch.object(recentguard, "keeper", return_value=mock.Mock()) as kept:
            self.assertEqual(recentguard.run(chooser), -3)
        kept.return_value.chose.assert_called_once_with(["file:///a.txt"])

    def test_every_file_chooser_of_the_app_goes_through_the_guard(self):
        """Each function that makes a Gtk file chooser runs it through recentguard.run, and
        every WebKit view's guard answers a page's <input type="file"> (webpolicy)."""
        import ast
        made = 0
        for folder, _dirs, files in os.walk(os.path.join(REPO, "assistant")):
            for name in files:
                path = os.path.join(folder, name)
                if not name.endswith(".py") or name == "recentguard.py":
                    continue
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                for node in ast.walk(ast.parse(text)):
                    if not isinstance(node, ast.FunctionDef):
                        continue
                    body = ast.get_source_segment(text, node)
                    if "FileChooserNative" in body or "FileChooserDialog" in body:
                        made += 1
                        self.assertIn("recentguard.run(dialog)", body, f"{path}: {node.name}")
                        self.assertNotIn("dialog.run()", body, f"{path}: {node.name}")
        self.assertEqual(made, 4)
        with open(os.path.join(REPO, "assistant", "ui", "webpolicy.py"), encoding="utf-8") as f:
            self.assertIn('view.connect("run-file-chooser", recentguard.answer_file_input)',
                          f.read())
        with open(os.path.join(REPO, "assistant", "app.py"), encoding="utf-8") as f:
            self.assertRegex(f.read(), r"Gtk\.main\(\)\n\s+finally:\n(\s+#.*\n)*\s+"
                                       r"recentguard\.put_back\(\)")


SCRIPT = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, {repo!r})
    from assistant import gtkcompat, recentguard
    from gi.repository import GLib, Gtk
    from assistant.ui import chat

    how, chosen, guarded = sys.argv[1], sys.argv[2], sys.argv[3] == "guarded"
    if not guarded:
        recentguard._keeper = recentguard._NONE
    recent = os.path.join(os.environ["XDG_DATA_HOME"], "recently-used.xbel")
    seen = {{}}

    def now():
        with open(recent, "rb") as f:
            return f.read().decode()

    pressed = []

    def accept():
        # chooses the file and accepts, and answers GTK's "Replace it?" with Replace
        if "chosen" in seen:
            return False
        for window in Gtk.Window.list_toplevels():
            if not window.get_visible():
                continue
            if isinstance(window, Gtk.MessageDialog):
                window.response(Gtk.ResponseType.ACCEPT)
            elif isinstance(window, Gtk.FileChooserDialog) and window not in pressed:
                if window.get_action() == Gtk.FileChooserAction.OPEN:
                    window.select_filename(chosen)
                    ready = window.get_filenames() == [chosen]
                else:
                    ready = window.get_filename() == chosen
                if ready:
                    pressed.append(window)
                    window.response(Gtk.ResponseType.ACCEPT)
        return True

    class Window(Gtk.Window):
        def attach_files(self, paths):
            seen["chosen"] = paths

    class Request:
        def get_select_multiple(self):
            return False
        def get_mime_types_filter(self):
            return None
        def get_selected_files(self):
            return None
        def select_files(self, paths):
            seen["chosen"] = list(paths)
            GLib.timeout_add(800, written)
        def cancel(self):
            seen["chosen"] = None
            Gtk.main_quit()

    window = Window()
    window.show()

    def choose():
        GLib.timeout_add(100, accept)
        if how == "attach":
            chat.ChatWindow._choose_attachments(window)
            GLib.timeout_add(800, written)
        elif how == "save":
            chat.ChatWindow._last_save_folder = os.path.dirname(chosen)
            seen["chosen"] = [chat.ChatWindow._ask_save_path(
                window, "Save", os.path.basename(chosen), "Text", "*.txt", "text/plain")]
            GLib.timeout_add(800, written)
        else:
            view = type("View", (), {{"get_toplevel": lambda self: window}})()
            seen["answered"] = recentguard.answer_file_input(view, Request())
        return False

    def written():
        seen["after_write"] = now()
        Gtk.main_quit()
        return False

    GLib.idle_add(choose)
    Gtk.main()
    recentguard.put_back()
    seen["after_loop"] = now()
    seen["mode"] = os.stat(recent).st_mode & 0o777
    print("RESULT", json.dumps(seen), flush=True)
""")


def _gi():
    return importlib.util.find_spec("gi") is not None


@unittest.skipUnless(shutil.which("xvfb-run") and _gi(), "needs Xvfb and PyGObject")
class ARealChooser(unittest.TestCase):
    def choose(self, how, guarded):
        top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, top, True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "GTK_USE_PORTAL": "0", "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory",
               "GIO_USE_VFS": "local"}
        for name, sub in (("HOME", "home"), ("XDG_DATA_HOME", "data"),
                          ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                          ("XDG_RUNTIME_DIR", "run")):
            env[name] = os.path.join(top, sub)
            os.mkdir(env[name], 0o700)
        chosen = os.path.join(env["HOME"], "notes.txt")
        with open(chosen, "w", encoding="utf-8") as f:
            f.write("notes\n")
        seed = xbel(entry("file:///srv/report-from-august.txt", "2026-08-01T09:00:00.000000Z"),
                    entry("file:///srv/agenda.txt",
                          time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())),
                    entry("file://" + chosen, "2026-11-05T12:00:00.000000Z"))
        recent = os.path.join(env["XDG_DATA_HOME"], "recently-used.xbel")
        with open(recent, "wb") as f:
            f.write(seed)
        os.chmod(recent, 0o644)
        script = os.path.join(top, "choose.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(SCRIPT.format(repo=REPO))
        command = ["xvfb-run", "-a"]
        if shutil.which("dbus-run-session"):
            command += ["dbus-run-session", "--"]
        command += [sys.executable, "-I", script, how, chosen,
                    "guarded" if guarded else "unguarded"]
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180,
                              check=False)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        results = [line[len("RESULT "):] for line in done.stdout.splitlines()
                   if line.startswith("RESULT ")]
        self.assertEqual(len(results), 1, done.stdout[-3000:] + done.stderr[-3000:])
        seen = json.loads(results[0])
        self.assertEqual(seen["chosen"], [chosen], done.stderr[-3000:])
        return seed.decode(), seen

    def test_the_attach_dialog_leaves_the_list_byte_for_byte_as_it_was(self):
        seed, seen = self.choose("attach", guarded=True)
        self.assertEqual(seen["after_write"], seed)
        self.assertEqual(seen["after_loop"], seed)
        self.assertEqual(seen["mode"], 0o644)

    def test_the_save_dialog_saving_over_a_file_leaves_the_list_byte_for_byte_as_it_was(self):
        """Save, over the file the list already names (GTK asks Replace it?): GTK adds it as the
        dialog answers, and run() comes back later than GTK's write would."""
        seed, seen = self.choose("save", guarded=True)
        self.assertEqual(seen["after_write"], seed)
        self.assertEqual(seen["after_loop"], seed)

    def test_a_pages_file_input_leaves_the_list_byte_for_byte_as_it_was(self):
        seed, seen = self.choose("input", guarded=True)
        self.assertTrue(seen["answered"])
        self.assertEqual(seen["after_write"], seed)
        self.assertEqual(seen["after_loop"], seed)

    def test_without_the_guard_gtk_changes_the_list_as_it_writes_and_as_the_loop_ends(self):
        """What the guard undoes: the old entry dropped, the file's own entry the app's."""
        seed, seen = self.choose("attach", guarded=False)
        for written in (seen["after_write"], seen["after_loop"]):
            self.assertNotEqual(written, seed)
            self.assertNotIn("report-from-august", written)
            self.assertIn("agenda.txt", written)
        self.assertEqual(seen["mode"], 0o600)


if __name__ == "__main__":
    unittest.main()
