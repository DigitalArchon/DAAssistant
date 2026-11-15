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
And a chooser opened through the guard fits the screen's work area: on a 640x480 screen
GTK's own size for one is 1096x822, placed off the screen's top left.
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


STAMP_NOW = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(NOW))


class WritesBeforeTheAnswer(Case):
    """A name typed into a Save dialog (or a place into its location bar) reaches the app a
    quarter of a second after GTK took the file in: GtkButton holds the default button pressed
    for 250 ms. A write of GTK's already on its way - its "changed" timer started by another
    program's change to the list, or by the list put back after the app's previous choice -
    lands first, with the file in it."""

    def theirs(self):
        theirs = self.seed.replace(b"</xbel>", entry("file:///srv/theirs.odt") + b"</xbel>")
        self.write(theirs, 0o640)               # another program's write, GTK re-reads it
        self.manager.changed()
        return theirs

    def gtk_writes_first(self, *uris):
        for uri in uris:
            self.manager.model[uri] = NOW
        gtk_writes(self.recent, xbel(entry("file:///srv/theirs.odt"),
                                     *(entry(uri, STAMP_NOW, APP) for uri in uris)))
        self.manager.changed()

    def test_a_write_landing_before_the_answer_is_put_back_byte_for_byte(self):
        self.write(self.seed)
        keeper = self.keeper()
        keeper.opened()
        theirs = self.theirs()
        self.gtk_writes_first("file:///home/u/new.pdf")
        keeper.chose(["file:///home/u/new.pdf"])
        keeper.closed()
        self.assertEqual(self.read(), theirs)
        self.assertEqual(stat.S_IMODE(os.lstat(self.recent).st_mode), 0o640)
        self.assertFalse(keeper.pending)
        self.assertEqual(self.timers.waiting, [])

    def test_the_files_in_the_write_that_came_first_are_taken_in_whatever_the_manager_says(
            self):
        """The write held them: nothing more is awaited, even where the manager's model is
        not yet heard from (DA Media Browser's Keeper is told of writes by a file watcher)."""
        self.write(self.seed)
        keeper = self.keeper()
        keeper.opened()
        gtk_writes(self.recent, xbel(entry("file:///home/u/new.pdf", STAMP_NOW, APP)))
        keeper.chose(["file:///home/u/new.pdf"])
        keeper.closed()
        self.assertEqual(self.read(), self.seed)
        self.assertFalse(keeper.pending)
        self.assertEqual(self.timers.waiting, [])

    def test_without_the_chooser_open_the_write_that_came_first_stays(self):
        """What opened() is for: told only of the answer, the list holding the file already
        is taken for the clean copy."""
        self.write(self.seed)
        keeper = self.keeper()
        self.theirs()
        self.gtk_writes_first("file:///home/u/new.pdf")
        keeper.chose(["file:///home/u/new.pdf"])
        self.assertIn(b"new.pdf", self.read())

    def test_another_programs_write_while_it_is_open_stands_when_gtk_writes_after(self):
        self.write(self.seed)
        keeper = self.keeper()
        keeper.opened()
        theirs = self.theirs()
        self.choose(keeper, "file:///home/u/report.pdf")
        keeper.closed()
        self.assertEqual(self.read(), theirs)
        self.assertFalse(keeper.pending)

    def test_a_file_the_list_already_held_from_before_counts_only_as_added_since(self):
        """notes.txt is in the list from September: the list as the answer comes is clean."""
        self.write(self.seed)
        keeper = self.keeper()
        keeper.opened()
        theirs = self.theirs()
        keeper.chose(["file:///srv/notes.txt"])
        keeper.closed()
        self.assertEqual(keeper.clean, recentguard.look(self.recent))
        self.assertEqual(self.read(), theirs)

    def test_a_list_that_was_not_there_is_taken_away_again(self):
        keeper = self.keeper()
        keeper.opened()
        self.gtk_writes_first("file:///home/u/new.pdf")
        keeper.chose(["file:///home/u/new.pdf"])
        keeper.closed()
        self.assertFalse(os.path.lexists(self.recent))

    def test_the_list_it_opened_on_and_the_newest_are_kept(self):
        self.write(self.seed)
        keeper = self.keeper()
        keeper.opened()
        for n in range(recentguard.SEEN * 2):
            self.write(self.seed + str(n).encode())
            self.manager.changed()
        self.assertEqual(len(keeper._seen), recentguard.SEEN)
        self.assertEqual(keeper._seen[0][1], self.seed)
        self.assertEqual(keeper._seen[-1][1], self.seed + str(recentguard.SEEN * 2 - 1).encode())
        keeper.closed()
        self.assertEqual(keeper._seen, [])
        self.assertFalse(keeper.watching)

    def test_two_choosers_open_keep_the_lists_until_both_have_answered(self):
        self.write(self.seed)
        keeper = self.keeper()
        keeper.opened()
        keeper.opened()
        keeper.closed()
        self.assertTrue(keeper.watching)
        theirs = self.theirs()
        self.gtk_writes_first("file:///home/u/new.pdf")
        keeper.chose(["file:///home/u/new.pdf"])
        keeper.closed()
        self.assertEqual(self.read(), theirs)

    def test_what_a_list_holds_is_read_from_gtks_layout(self):
        data = xbel(entry("file:///srv/a%20b.txt", STAMP_NOW, APP),
                    entry("file:///srv/old.txt"), entry("https://example.com/x", STAMP_NOW))
        found = recentguard.added(data)
        self.assertEqual(found["/srv/a b.txt"], NOW)
        self.assertLess(found["/srv/old.txt"], NOW)
        self.assertEqual(found["https://example.com/x"], NOW)
        listing = (recentguard.FILE, data, 0o600)
        self.assertTrue(recentguard.holds(listing, ["file:///srv/a b.txt"], NOW))
        self.assertFalse(recentguard.holds(listing, ["file:///srv/old.txt"], NOW))
        self.assertFalse(recentguard.holds((recentguard.ABSENT,), ["file:///srv/a"], NOW))
        self.assertEqual(recentguard.added(b"not a list"), {})


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

    def test_run_tells_the_keeper_the_chooser_opened_before_it_runs_and_closed_after(self):
        keeper = mock.Mock()
        with mock.patch.object(recentguard, "keeper", return_value=keeper):
            recentguard.run(Chooser(-3, keeper))
        self.assertEqual([name for name, *_ in keeper.method_calls],
                         ["opened", "chose", "closed"])

    def test_a_chooser_that_fails_is_still_closed(self):
        keeper = mock.Mock()
        chooser = mock.Mock(spec=["run", "connect"])
        chooser.run.side_effect = RuntimeError("gone")
        with mock.patch.object(recentguard, "keeper", return_value=keeper), \
                self.assertRaises(RuntimeError):
            recentguard.run(chooser)
        self.assertEqual([name for name, *_ in keeper.method_calls], ["opened", "closed"])

    def test_a_pages_file_input_is_open_from_show_until_it_answers(self):
        keeper, calls, made = mock.Mock(), [], []

        class Native:
            def __init__(self):
                self.handlers = []

            def connect(self, name, handler):
                self.handlers.append(handler)

            def show(self):
                calls.append("show")

            def get_uris(self):
                return ["file:///a.txt"]

            def get_filenames(self):
                return ["/a.txt"]

            def destroy(self):
                calls.append("destroy")

            def __getattr__(self, name):
                return lambda *a: None

        class FileChooserNative:
            @staticmethod
            def new(*_args):
                made.append(Native())
                return made[-1]

        gtk = mock.Mock(FileChooserNative=FileChooserNative)
        gtk.Window, gtk.OffscreenWindow = type("W", (), {}), type("O", (), {})
        gtk.ResponseType.ACCEPT = -3
        view = mock.Mock()
        keeper.opened.side_effect = lambda: calls.append("opened")
        keeper.closed.side_effect = lambda: calls.append("closed")
        keeper.chose.side_effect = lambda uris: calls.append("chose")
        request = mock.Mock()
        request.get_select_multiple.return_value = False
        request.get_mime_types_filter.return_value = None
        request.get_selected_files.return_value = None
        with mock.patch.object(recentguard, "keeper", return_value=keeper), \
                mock.patch.object(recentguard, "_gtk", return_value=(None, gtk)), \
                mock.patch.object(recentguard, "_fitting", return_value=lambda: None):
            self.assertTrue(recentguard.answer_file_input(view, request))
            self.assertEqual(calls, ["opened", "show"])
            made[0].handlers[0](made[0], -3)
        self.assertEqual(calls, ["opened", "show", "chose", "closed", "destroy"])

    def test_pywebviews_own_dialog_is_open_while_it_runs(self):
        keeper, calls = mock.Mock(), []
        keeper.opened.side_effect = lambda: calls.append("opened")
        keeper.closed.side_effect = lambda: calls.append("closed")

        class BrowserView:
            def create_file_dialog(self, *args, **kwargs):
                calls.append("dialog")
                return ("/a.txt",)

        backend = mock.Mock(BrowserView=BrowserView)
        platforms = mock.Mock(gtk=backend)
        modules = {"webview": mock.Mock(platforms=platforms), "webview.platforms": platforms,
                   "webview.platforms.gtk": backend}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(recentguard, "keeper", return_value=keeper), \
                mock.patch.object(recentguard, "_hook_responses", return_value=lambda: None):
            recentguard._wrap_pywebview_dialogs()
            self.assertEqual(BrowserView().create_file_dialog(), ("/a.txt",))
        self.assertEqual(calls, ["opened", "dialog", "closed"])

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



class FittingTheScreen(unittest.TestCase):
    """What a chooser may take of the monitor's work area, and where it goes in it."""

    def test_the_room_is_the_work_area_less_a_margin_on_each_side(self):
        self.assertEqual(recentguard.fit_room(1280, 800), (1152, 720))
        self.assertEqual(recentguard.fit_room(640, 480), (576, 432))
        self.assertEqual(recentguard.fit_room(1920, 1080), (1728, 972))
        self.assertEqual(recentguard.fit_room(200, 100), (168, 68))     # 16 px at least
        self.assertEqual(recentguard.fit_room(20, 20), (1, 1))

    def test_a_window_off_the_area_is_moved_in_as_little_as_it_takes(self):
        area = (0, 26, 1280, 774)               # a bar of 26 px at the top
        self.assertEqual(recentguard.placed(100, 100, 600, 400, area), (100, 100))
        self.assertEqual(recentguard.placed(-228, -171, 600, 400, area), (0, 26))
        self.assertEqual(recentguard.placed(1000, 600, 600, 400, area), (680, 400))
        # larger than the area: its top left on the area, where its header bar is
        self.assertEqual(recentguard.placed(-50, -50, 2000, 1000, area), (0, 26))
        self.assertEqual(recentguard.placed(10, 30, 600, 400, (1280, 0, 1920, 1080)),
                         (1280, 30))         # a second monitor's area

    def test_on_hyprland_the_room_is_what_stays_on_the_area_centred_on_the_parent(self):
        """Hyprland centres a dialog on its parent and keeps neither on the screen: a parent
        tiled to the right half of 1280x800 leaves 614 px across (seen: the chooser 874 px
        wide, Choose off the screen), one in the middle the whole room."""
        room, area = (1152, 720), (0, 0, 1280, 800)
        self.assertEqual(recentguard.around_centre(room, (646, 38, 622, 750), area), (614, 720))
        self.assertEqual(recentguard.around_centre(room, (12, 38, 1256, 750), area), (1152, 720))
        self.assertEqual(recentguard.around_centre(room, (1100, 600, 180, 200), area),
                         (148, 168))         # what is free there, however little
        self.assertEqual(recentguard.within_parent(room, (874, 700)), (874, 700))
        self.assertEqual(recentguard.within_parent(room, (300, 200)), (480, 360))
        self.assertEqual(recentguard.within_parent(room, (2000, 900)), (1152, 720))

    def test_only_a_hyprland_session_on_wayland_centres_on_the_parent(self):
        def window(name):
            made = mock.Mock()
            made.get_display.return_value.__gtype__ = mock.Mock()
            made.get_display.return_value.__gtype__.name = name
            return made

        for kind, env, centres in (("GdkWaylandDisplay", {"XDG_CURRENT_DESKTOP": "Hyprland"}, True),
                                   ("GdkWaylandDisplay", {"HYPRLAND_INSTANCE_SIGNATURE": "a_1"},
                                    True),
                                   ("GdkWaylandDisplay", {"XDG_CURRENT_DESKTOP": "GNOME"}, False),
                                   ("GdkWaylandDisplay", {"XDG_CURRENT_DESKTOP": "KDE"}, False),
                                   ("GdkX11Display", {"XDG_CURRENT_DESKTOP": "Hyprland"}, False)):
            with self.subTest(kind=kind, env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(recentguard._centres_on_parent(window(kind)), centres)

    def test_hyprlands_answer_names_the_parents_place_and_nothing_else(self):
        import socket
        import threading
        runtime = tempfile.mkdtemp(dir="/tmp")         # a socket's path must be short
        self.addCleanup(shutil.rmtree, runtime, True)
        os.makedirs(os.path.join(runtime, "hypr", "abc_1"))
        path = os.path.join(runtime, "hypr", "abc_1", ".socket.sock")
        answers = []

        def serve(listening):
            while answers:
                connection, _ = listening.accept()
                with connection:
                    asked = connection.recv(100)
                    self.assertEqual(asked, b"j/clients")
                    connection.sendall(answers.pop(0))

        def place(answer, title="Settings"):
            answers.append(answer)
            listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            if os.path.exists(path):
                os.unlink(path)
            listening.bind(path)
            listening.listen(1)
            server = threading.Thread(target=serve, args=(listening,))
            server.start()
            parent = mock.Mock()
            parent.get_title.return_value = title
            env = {"XDG_RUNTIME_DIR": runtime, "HYPRLAND_INSTANCE_SIGNATURE": "abc_1"}
            try:
                with mock.patch.dict(os.environ, env):
                    return recentguard._hyprland_place(parent)
            finally:
                server.join(5)
                listening.close()

        ours = {"pid": os.getpid(), "title": "Settings", "at": [646, 38], "size": [622, 750]}
        others = [{"pid": 1, "title": "Settings", "at": [0, 0], "size": [10, 10]},
                  {"pid": os.getpid(), "title": "Ask anything", "at": [12, 38],
                   "size": [622, 750]}]
        self.assertEqual(place(json.dumps(others + [ours]).encode()), (646, 38, 622, 750))
        self.assertIsNone(place(json.dumps(others).encode()))
        self.assertIsNone(place(json.dumps([dict(ours, hidden=True)]).encode()))
        self.assertIsNone(place(json.dumps([dict(ours, at=["1", 2])]).encode()))
        self.assertIsNone(place(json.dumps([dict(ours, at=[True, 2])]).encode()))
        self.assertIsNone(place(b"[" * 40 + b"]" * 40))          # nested past any answer's
        self.assertIsNone(place(b"not json"))
        self.assertIsNone(place(json.dumps({"error": "no"}).encode()))
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime,
                                          "HYPRLAND_INSTANCE_SIGNATURE": "gone_2"}):
            self.assertIsNone(recentguard._hyprland_place(mock.Mock()))     # no socket
        for instance in ("../x", "", ".."):
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime,
                                              "HYPRLAND_INSTANCE_SIGNATURE": instance}):
                self.assertIsNone(recentguard._hyprland_place(mock.Mock()))
        self.assertEqual(recentguard._deepest('{"a": "[[[", "b": [[1], {"c": "\\""}]}'), 3)

    def test_fit_on_hyprland_holds_the_chooser_to_the_room_around_its_parent(self):
        window = mock.Mock()
        window.get_default_size.return_value = (-1, -1)
        window.get_transient_for.return_value.get_size.return_value = (874, 750)
        with mock.patch.object(recentguard, "_area", return_value=(0, 0, 1280, 800)), \
                mock.patch.object(recentguard, "_centres_on_parent", return_value=True), \
                mock.patch.object(recentguard, "_hyprland_place",
                                  return_value=(646, 38, 622, 750)):
            recentguard.fit(window)
        hints = window.set_geometry_hints.call_args[0][1]
        self.assertEqual((hints.max_width, hints.max_height), (614, 720))
        with mock.patch.object(recentguard, "_area", return_value=(0, 0, 1280, 800)), \
                mock.patch.object(recentguard, "_centres_on_parent", return_value=True), \
                mock.patch.object(recentguard, "_hyprland_place", return_value=None):
            recentguard.fit(window)            # the place not known: the parent's size
        hints = window.set_geometry_hints.call_args[0][1]
        self.assertEqual((hints.max_width, hints.max_height), (874, 720))

    def test_a_save_chooser_too_wide_for_its_room_has_its_name_entry_narrowed(self):
        """The real dialog, with a header bar as GNOME and Omarchy have it: a Save chooser's
        name, 45 characters, makes it wider than an Open one. Given a room between the two -
        the room around a parent tiled to half a 1280 px screen, on Omarchy - its name entry is
        narrowed until the chooser fits; an Open chooser, which fits, is left as it is."""
        Gtk = recentguard._gtk()[1]
        parent = Gtk.Window()
        self.addCleanup(parent.destroy)

        def made(action):
            dialog = Gtk.FileChooserDialog(title="x", transient_for=parent, action=action,
                                           use_header_bar=True)   # as GNOME and Omarchy have it
            dialog.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Save", Gtk.ResponseType.ACCEPT)
            self.addCleanup(dialog.destroy)
            return dialog

        save, folder = made(Gtk.FileChooserAction.SAVE), made(Gtk.FileChooserAction.SELECT_FOLDER)
        wide, narrow = recentguard._least(save)[0], recentguard._least(folder)[0]
        self.assertGreater(wide, narrow + 20)
        names = recentguard._name_entries(save)
        self.assertEqual([entry.get_width_chars() for entry in names], [45])
        self.assertEqual(recentguard._name_entries(folder), [])
        room = (wide + narrow) // 2
        free = room + 2 * recentguard.FIT_MARGIN_MIN
        tile = (int(1280 - free / 2 - 300), 38, 600, 750)       # centred `free` from the edge
        for dialog in (save, folder):
            with mock.patch.object(recentguard, "_area", return_value=(0, 0, 1280, 800)), \
                    mock.patch.object(recentguard, "_centres_on_parent", return_value=True), \
                    mock.patch.object(recentguard, "_hyprland_place", return_value=tile):
                recentguard.fit(dialog)
            self.assertLessEqual(recentguard._least(dialog)[0], room)
            self.assertIs(dialog.get_transient_for(), parent)
        self.assertLess(names[0].get_width_chars(), 45)
        self.assertGreaterEqual(names[0].get_width_chars(), recentguard.NAME_CHARS_LEAST)
        self.assertEqual(recentguard._least(folder)[0], narrow)

    def test_the_name_is_narrowed_no_further_than_it_takes_nor_than_its_least(self):
        class Entry:
            def __init__(self, chars):
                self.chars = chars

            def get_width_chars(self):
                return self.chars

            def set_width_chars(self, chars):
                self.chars = chars

            def get_preferred_width(self):
                return self.chars * 8, self.chars * 8

        window, entry = mock.Mock(), Entry(45)

        def least(_window):
            return 300 + entry.chars * 8, 400

        with mock.patch.object(recentguard, "_least", side_effect=least), \
                mock.patch.object(recentguard, "_name_entries", return_value=[entry]):
            recentguard._narrow_name(window, 700)       # wide enough: untouched
            self.assertEqual(entry.chars, 45)
            recentguard._narrow_name(window, 600)       # 60 px too wide: 8 characters
            self.assertEqual(entry.chars, 37)
            recentguard._narrow_name(window, 100)       # no room at all: as narrow as it goes
            self.assertEqual(entry.chars, recentguard.NAME_CHARS_LEAST)

    def test_run_fits_choosers_while_it_runs_and_stops_after(self):
        calls = []
        chooser = mock.Mock(spec=["run", "connect"])
        chooser.run.side_effect = lambda: calls.append("run") or -6
        with mock.patch.object(recentguard, "keeper", return_value=mock.Mock()), \
                mock.patch.object(recentguard, "_fitting",
                                  side_effect=lambda: calls.append("fit") or
                                  (lambda: calls.append("unfit"))):
            recentguard.run(chooser)
            self.assertEqual(calls, ["fit", "run", "unfit"])
            calls.clear()
            chooser.run.side_effect = RuntimeError("gone")
            with self.assertRaises(RuntimeError):
                recentguard.run(chooser)
            self.assertEqual(calls, ["fit", "unfit"])
        calls.clear()
        chooser.run.side_effect = lambda: calls.append("run") or -6
        with mock.patch.object(recentguard, "keeper", return_value=None), \
                mock.patch.object(recentguard, "_fitting",
                                  side_effect=lambda: calls.append("fit") or
                                  (lambda: calls.append("unfit"))):
            recentguard.run(chooser)        # the list not kept: still fitted
        self.assertEqual(calls, ["fit", "run", "unfit"])

    def test_a_hook_that_cannot_be_had_never_stops_the_chooser(self):
        with mock.patch.object(recentguard, "_fit_choosers", side_effect=RuntimeError("no")), \
                mock.patch.object(recentguard, "_say") as said:
            unfit = recentguard._fitting()
        unfit()
        said.assert_called_once()

    def test_fit_holds_the_window_to_the_room_and_keeps_it_on_the_area(self):
        """fit(): geometry hints of the room, a default size no larger, and a map-event
        handler that keeps it on the area; anything failing is said, never raised."""
        from gi.repository import Gdk
        window = mock.Mock()
        window.get_default_size.return_value = (1096, 822)
        with mock.patch.object(recentguard, "_area", return_value=(0, 0, 640, 480)), \
                mock.patch.object(recentguard, "_centres_on_parent", return_value=False):
            recentguard.fit(window)
        hints, flags = window.set_geometry_hints.call_args[0][1:]
        self.assertEqual((hints.max_width, hints.max_height), (576, 432))
        self.assertEqual(flags, Gdk.WindowHints.MAX_SIZE)
        window.set_default_size.assert_called_once_with(576, 432)
        window.connect.assert_called_once_with("map-event", recentguard._keep_on)
        window = mock.Mock()
        window.get_default_size.return_value = (-1, -1)     # GTK's own: left to the hints
        with mock.patch.object(recentguard, "_area", return_value=(0, 0, 640, 480)), \
                mock.patch.object(recentguard, "_centres_on_parent", return_value=False):
            recentguard.fit(window)
        window.set_default_size.assert_not_called()
        with mock.patch.object(recentguard, "_area", side_effect=RuntimeError("no screen")), \
                mock.patch.object(recentguard, "_say") as said:
            recentguard.fit(mock.Mock())
        said.assert_called_once()

    def test_a_pages_file_input_and_pywebviews_dialog_are_fitted_until_they_answer(self):
        calls = []
        fitting = mock.patch.object(recentguard, "_fitting", side_effect=lambda: calls.append(
            "fit") or (lambda: calls.append("unfit")))
        made = []

        class Native:
            def __init__(self):
                self.handlers = []

            def connect(self, name, handler):
                self.handlers.append(handler)

            def show(self):
                calls.append("show")

            def __getattr__(self, name):
                return lambda *a: None

        class FileChooserNative:
            @staticmethod
            def new(*_args):
                made.append(Native())
                return made[-1]

        gtk = mock.Mock(FileChooserNative=FileChooserNative)
        gtk.Window, gtk.OffscreenWindow = type("W", (), {}), type("O", (), {})
        gtk.ResponseType.ACCEPT = -3
        request = mock.Mock()
        request.get_select_multiple.return_value = False
        request.get_mime_types_filter.return_value = None
        request.get_selected_files.return_value = None
        with fitting, mock.patch.object(recentguard, "keeper", return_value=None), \
                mock.patch.object(recentguard, "_gtk", return_value=(None, gtk)):
            recentguard.answer_file_input(mock.Mock(), request)
            self.assertEqual(calls, ["fit", "show"])
            made[0].handlers[0](made[0], -6)
        self.assertEqual(calls, ["fit", "show", "unfit"])

        calls.clear()

        class BrowserView:
            def create_file_dialog(self, *args, **kwargs):
                calls.append("dialog")

        backend = mock.Mock(BrowserView=BrowserView)
        platforms = mock.Mock(gtk=backend)
        modules = {"webview": mock.Mock(platforms=platforms), "webview.platforms": platforms,
                   "webview.platforms.gtk": backend}
        for kept in (mock.Mock(), None):
            BrowserView.create_file_dialog = lambda self, *a, **k: calls.append("dialog")
            with mock.patch.dict(sys.modules, modules), fitting, \
                    mock.patch.object(recentguard, "keeper", return_value=kept), \
                    mock.patch.object(recentguard, "_hook_responses", return_value=lambda: None):
                recentguard._wrap_pywebview_dialogs()
                BrowserView().create_file_dialog()
            self.assertEqual(calls, ["fit", "dialog", "unfit"])
            calls.clear()

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
                if ready and how == "race":
                    # another program writes the list (the same bytes): GTK's monitor (up
                    # to some 100 ms late: GLib's inotify reads at that pace when idle)
                    # starts its 250 ms timer, and the Save, answered 200 ms later, reaches
                    # the app only after that timer's write, the file in it
                    pressed.append(window)
                    with open(recent, "rb") as f:
                        data = f.read()
                    with open(recent + ".theirs", "wb") as f:
                        f.write(data)
                    os.chmod(recent + ".theirs", 0o644)
                    os.replace(recent + ".theirs", recent)
                    GLib.timeout_add(200, lambda answer=window: answer.response(
                        Gtk.ResponseType.ACCEPT))
                elif ready:
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
        elif how in ("save", "race"):
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
        if how == "race":
            chosen = os.path.join(env["HOME"], "new-notes.txt")   # a name typed: not there
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

    def test_a_save_to_a_new_name_just_after_another_programs_write_leaves_the_list_as_it_was(
            self):
        """GTK's write, its timer started by the other program's change, lands before the
        answer reaches the app: the list kept while the dialog was open is put back."""
        seed, seen = self.choose("race", guarded=True)
        self.assertEqual(seen["after_write"], seed)
        self.assertEqual(seen["after_loop"], seed)
        self.assertEqual(seen["mode"], 0o644)

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



FIT_SCRIPT = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, {repo!r})
    from assistant import gtkcompat, recentguard
    from gi.repository import Gdk, GLib, Gtk
    from assistant.ui import chat

    how, folder, guarded = sys.argv[1], sys.argv[2], sys.argv[3] == "guarded"
    if not guarded:
        recentguard._fitting = lambda: (lambda: None)
    seen = {{}}

    def measure():
        # once the chooser is on the screen: where, and how large, then Cancel
        for window in Gtk.Window.list_toplevels():
            if isinstance(window, Gtk.FileChooserDialog) and window.get_mapped():
                frame = window.get_window().get_frame_extents()
                area = Gdk.Display.get_default().get_monitor(0).get_workarea()
                seen["frame"] = [frame.x, frame.y, frame.width, frame.height]
                seen["area"] = [area.x, area.y, area.width, area.height]
                window.response(Gtk.ResponseType.CANCEL)
                return False
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
            Gtk.main_quit()
        def cancel(self):
            Gtk.main_quit()

    window = Window()
    window.set_default_size(300, 200)
    window.move(500, 380)              # near the screen's bottom right
    window.show()

    def choose():
        GLib.timeout_add(400, measure)
        if how == "attach":
            chat.ChatWindow._choose_attachments(window)
            Gtk.main_quit()
        elif how == "save":
            chat.ChatWindow._last_save_folder = folder
            chat.ChatWindow._ask_save_path(window, "Save", "notes.txt", "Text", "*.txt",
                                           "text/plain")
            Gtk.main_quit()
        else:
            view = type("View", (), {{"get_toplevel": lambda self: window}})()
            recentguard.answer_file_input(view, Request())
        return False

    GLib.idle_add(choose)
    Gtk.main()
    print("RESULT", json.dumps(seen), flush=True)
""")


@unittest.skipUnless(shutil.which("xvfb-run") and _gi(), "needs Xvfb and PyGObject")
class AChooserFitsTheScreen(unittest.TestCase):
    """On a 640x480 screen, a parent window near its bottom right: GTK's own chooser is
    1096x822, its top left off the screen; through the guard it lies inside the work area."""

    def show(self, how, guarded):
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
        script = os.path.join(top, "fit.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(FIT_SCRIPT.format(repo=REPO))
        command = ["xvfb-run", "-a", "-s", "-screen 0 640x480x24"]
        if shutil.which("dbus-run-session"):
            command += ["dbus-run-session", "--"]
        command += [sys.executable, "-I", script, how, env["HOME"],
                    "guarded" if guarded else "unguarded"]
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180,
                              check=False)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        results = [line[len("RESULT "):] for line in done.stdout.splitlines()
                   if line.startswith("RESULT ")]
        self.assertEqual(len(results), 1, done.stdout[-3000:] + done.stderr[-3000:])
        seen = json.loads(results[0])
        self.assertIn("frame", seen, done.stderr[-3000:])
        return seen["frame"], seen["area"]

    def assertInside(self, frame, area):
        x, y, width, height = frame
        left, top, across, down = area
        self.assertGreaterEqual(x, left, frame)
        self.assertGreaterEqual(y, top, frame)
        self.assertLessEqual(x + width, left + across, frame)
        self.assertLessEqual(y + height, top + down, frame)

    def test_the_attach_dialog_lies_inside_the_work_area(self):
        self.assertInside(*self.show("attach", guarded=True))

    def test_the_save_dialog_lies_inside_the_work_area(self):
        self.assertInside(*self.show("save", guarded=True))

    def test_a_pages_file_input_lies_inside_the_work_area(self):
        self.assertInside(*self.show("input", guarded=True))

    def test_gtks_own_chooser_on_this_screen_does_not(self):
        """What the guard changes: unfitted, the chooser is larger than the screen."""
        frame, area = self.show("attach", guarded=False)
        self.assertGreater(frame[2] * frame[3], area[2] * area[3])

if __name__ == "__main__":
    unittest.main()
