"""A capture leaves no note of itself behind in the desktop's records (capture/traces.py):
GNOME's screenshot portal and Spectacle add the file they write to the recent files list,
and Spectacle keeps where it last saved in its settings - the lab found both, outliving the
file, on GNOME and KDE (Nov 14 2026). Only that entry and that setting go; nothing else in
either file changes.

Every file here is in a folder of the test's own (XDG_DATA_HOME, XDG_CONFIG_HOME,
XDG_STATE_HOME, XDG_RUNTIME_DIR), and no screenshot tool or portal is run: stand-ins write
what each was seen to write.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: E402,F401  (pins GI versions)
from PIL import Image  # noqa: E402

from assistant.capture import cli, traces  # noqa: E402
from assistant.capture.portal import PortalBackend  # noqa: E402

HEAD = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<xbel version="1.0"\n'
        b'      xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"\n'
        b'      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info"\n'
        b'>\n')
TAIL = b'</xbel>'


def bookmark(href, added="2026-09-01T10:00:00.000000Z", extra=b""):
    """A recent file's entry, as GLib writes one (with what KDE adds, `extra`)."""
    return (b'  <bookmark href="' + href.encode() + b'" added="' + added.encode()
            + b'" modified="' + added.encode() + b'" visited="' + added.encode() + b'">\n'
            b'    <info>\n'
            b'      <metadata owner="http://freedesktop.org">\n'
            b'        <mime:mime-type type="image/png"/>\n'
            b'        <bookmark:applications>\n'
            b'          <bookmark:application name="org.gnome.Shell" exec="&apos;gio open'
            b' %u&apos;" modified="' + added.encode() + b'" count="1"/>\n'
            b'        </bookmark:applications>\n'
            b'      </metadata>\n' + extra
            + b'    </info>\n'
            b'  </bookmark>\n')


def png_file(path):
    with open(path, "wb") as f:
        Image.new("RGB", (4, 4), "red").save(f, "PNG")


class FolderCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.data = os.path.join(self.home, "data")
        self.config = os.path.join(self.home, "config")
        self.state = os.path.join(self.home, "state")
        self.runtime = os.path.join(self.home, "runtime")
        for folder in (self.data, self.config, self.state, self.runtime):
            os.mkdir(folder, 0o700)
        patch = mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": self.data, "XDG_CONFIG_HOME": self.config,
            "XDG_STATE_HOME": self.state, "XDG_RUNTIME_DIR": self.runtime,
            "HOME": self.home})
        patch.start()
        self.addCleanup(patch.stop)
        self.recent = os.path.join(self.data, "recently-used.xbel")

    def write_recent(self, *entries, mode=0o600):
        data = HEAD + b"".join(entries) + TAIL
        with open(self.recent, "wb") as f:
            f.write(data)
        os.chmod(self.recent, mode)
        return data

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()


class TheRecentFiles(FolderCase):
    CAPTURE = "/run/user/1000/da-assistant-k2x9_a1/capture.png"

    def test_only_the_captures_entry_goes_and_every_other_byte_stays(self):
        # Another program's, a month old (GTK's own writing drops those), and one with
        # metadata of KDE's that GLib does not keep when it writes the list back.
        kde = (b'      <metadata owner="http://www.kde.org">\n'
               b'        <kde:activity xmlns:kde="http://www.kde.org">x</kde:activity>\n'
               b'      </metadata>\n')
        others = (bookmark("file:///home/u/old.odt", added="2026-01-02T03:04:05.000000Z"),
                  bookmark("file:///home/u/notes.txt", extra=kde))
        self.write_recent(others[0], bookmark("file://" + self.CAPTURE), others[1])
        self.assertEqual(traces.forget_recent(self.CAPTURE), 1)
        self.assertEqual(self.read(self.recent), HEAD + others[0] + others[1] + TAIL)

    def test_by_its_uri_however_it_is_written(self):
        quoted = "file://localhost" + urllib.parse.quote(self.CAPTURE, safe="/").replace(
            "-", "%2D")
        self.write_recent(bookmark(quoted))
        self.assertEqual(traces.forget_recent("file://" + self.CAPTURE), 1)
        self.assertEqual(self.read(self.recent), HEAD + TAIL)

    def test_where_it_is_not_there_nothing_is_written(self):
        data = self.write_recent(bookmark("file:///home/u/notes.txt"))
        before = os.stat(self.recent)
        self.assertEqual(traces.forget_recent(self.CAPTURE), 0)
        after = os.stat(self.recent)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(self.read(self.recent), data)
        # Nor where there is no list at all, or the entry names another folder's file.
        os.unlink(self.recent)
        self.assertEqual(traces.forget_recent(self.CAPTURE), 0)
        self.assertFalse(os.path.exists(self.recent))

    def test_the_list_keeps_its_permissions_and_a_link_to_it_stays_a_link(self):
        elsewhere = os.path.join(self.home, "dotfiles.xbel")
        self.write_recent(bookmark("file://" + self.CAPTURE), mode=0o640)
        os.replace(self.recent, elsewhere)
        os.symlink(elsewhere, self.recent)
        self.assertEqual(traces.forget_recent(self.CAPTURE), 1)
        self.assertTrue(os.path.islink(self.recent))
        self.assertEqual(self.read(elsewhere), HEAD + TAIL)
        self.assertEqual(os.stat(elsewhere).st_mode & 0o777, 0o640)

    def test_a_list_kept_from_being_written_is_left_alone(self):
        os.symlink(os.devnull, self.recent)              # recent files switched off so
        self.assertEqual(traces.forget_recent(self.CAPTURE), 0)
        self.assertEqual(os.readlink(self.recent), os.devnull)
        os.unlink(self.recent)
        with open(self.recent, "wb") as f:
            f.write(HEAD + bookmark("file://" + self.CAPTURE))   # cut off: not XML
        with mock.patch("builtins.print"):
            self.assertEqual(traces.forget_recent(self.CAPTURE), 0)
        self.assertEqual(self.read(self.recent), HEAD + bookmark("file://" + self.CAPTURE))

    def test_an_empty_element_and_one_on_a_line_of_its_own(self):
        inline = (b'  <bookmark href="file:///a.png" added="x"/><bookmark href="file://'
                  + self.CAPTURE.encode() + b'" added="y>z"/>\n')
        self.write_recent(inline)
        self.assertEqual(traces.forget_recent(self.CAPTURE), 1)
        self.assertEqual(self.read(self.recent),
                         HEAD + b'  <bookmark href="file:///a.png" added="x"/>\n' + TAIL)
        for last in (b'<bookmark href="file://' + self.CAPTURE.encode() + b'"/>',
                     b'<bookmark href="file://' + self.CAPTURE.encode() + b'"></bookmark>'):
            with self.subTest(last=last):
                self.write_recent(b"  <bookmark href=\"file:///a.png\"/>" + last)
                self.assertEqual(traces.forget_recent(self.CAPTURE), 1)
                self.assertEqual(self.read(self.recent),
                                 HEAD + b'  <bookmark href="file:///a.png"/>' + TAIL)

    def test_a_list_glib_wrote_reads_as_glib_would_write_it_without_the_entry(self):
        from gi.repository import GLib

        def written(*paths):
            """The list as GLib writes it, every date fixed (GLib stamps the time)."""
            when = GLib.DateTime.new_utc(2026, 1, 2, 3, 4, 5)
            bookmarks = GLib.BookmarkFile.new()
            for path in paths:
                uri = GLib.filename_to_uri(path, None)
                bookmarks.set_mime_type(uri, "image/png")
                bookmarks.set_application_info(uri, "org.kde.spectacle", "spectacle %u", 1,
                                               when)
                for stamp in (bookmarks.set_added_date_time,
                              bookmarks.set_modified_date_time,
                              bookmarks.set_visited_date_time):
                    stamp(uri, when)
            data = bookmarks.to_data()
            return data[0] if isinstance(data, tuple) else data

        with open(self.recent, "wb") as f:
            f.write(written("/home/u/a b.png", self.CAPTURE, "/home/u/c.png"))
        self.assertEqual(traces.forget_recent(self.CAPTURE), 1)
        self.assertEqual(self.read(self.recent), written("/home/u/a b.png", "/home/u/c.png"))

    def test_the_portals_screenshot_goes_with_its_entry(self):
        """GNOME's portal saves the screenshot into ~/Pictures/Screenshots and adds it to
        the recent files before it answers; the app reads it and deletes it."""
        shot = os.path.join(self.home, "Screenshot From 2026-11-14 10-00-00.png")
        png_file(shot)
        self.write_recent(bookmark("file:///home/u/notes.txt"),
                          bookmark("file://" + urllib.parse.quote(shot)))
        image = PortalBackend._load_uri("file://" + urllib.parse.quote(shot))
        self.assertEqual(image.size, (4, 4))
        self.assertFalse(os.path.exists(shot))
        self.assertEqual(self.read(self.recent),
                         HEAD + bookmark("file:///home/u/notes.txt") + TAIL)


class SpectaclesRecord(FolderCase):
    """Spectacle on KDE, run for a region or the whole screen: it adds the file it writes
    to the recent files, and keeps where it last saved in spectaclerc."""

    USERS = (b"# the user's own\n"
             b"[General]\n"
             b"launchAction=UseLastUsedCapturemode\n"
             b"\n"
             b"[ImageSave]\n"
             b"imageSaveLocation=file:///home/u/Pictures/Screenshots\n"
             b"lastImageSaveLocation=file:///home/u/Pictures/Screenshots/mine.png\n"
             b"translatedScreenshotsFolder=Screenshots\n")

    def setUp(self):
        super().setUp()
        self.rc = os.path.join(self.config, "spectaclerc")
        self.staterc = os.path.join(self.state, "spectaclestaterc")
        self.seen = []

    def spectacle(self, also=None):
        """A Spectacle that writes the capture, notes it in the recent files and records
        where it saved - and `also`, anything else it changes in its settings."""
        def run(cmd, timeout=120):
            path = cmd[-1]
            self.seen.append(path)
            png_file(path)
            uri = "file://" + path
            try:
                listed = self.read(self.recent)[:-len(TAIL)]
            except FileNotFoundError:
                listed = HEAD
            with open(self.recent, "wb") as f:
                f.write(listed + bookmark(uri) + TAIL)
            try:
                settings = self.read(self.rc)
            except FileNotFoundError:
                settings = b"[ImageSave]\n"
            # In its place, as KConfig writes a group's keys (in order).
            saved = b"lastImageSaveLocation=" + uri.encode() + b"\n"
            lines = settings.splitlines(keepends=True)
            places = [n for n, line in enumerate(lines)
                      if line.startswith(b"lastImageSaveLocation=")]
            if places:
                lines[places[0]] = saved
            else:
                lines.insert(lines.index(b"[ImageSave]\n") + 1, saved)
            with open(self.rc, "wb") as f:
                f.write(b"".join(lines) + (also or b""))
            # Not seen on the lab: any setting naming the capture, in either file, goes.
            with open(self.staterc, "wb") as f:
                f.write(b"[MainWindow]\nlastCapture=" + uri.encode() + b"\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        for patch in (mock.patch.object(cli, "_run", side_effect=run),
                      mock.patch.object(cli, "_spectacle_args",
                                        side_effect=lambda path, region: ["spectacle", path])):
            patch.start()
            self.addCleanup(patch.stop)

    def backend(self):
        backend = cli.CliBackend.__new__(cli.CliBackend)
        backend.desktop, backend.tool = "kde", "spectacle"
        return backend

    def test_a_region_leaves_the_users_settings_and_recent_files_as_they_were(self):
        with open(self.rc, "wb") as f:
            f.write(self.USERS)
        os.chmod(self.rc, 0o644)
        mine = self.write_recent(bookmark("file:///home/u/notes.txt"))
        self.spectacle()
        image = self.backend()._select_blocking()
        self.assertEqual(image.size, (4, 4))
        self.assertTrue(self.seen[0].startswith(self.runtime))
        self.assertEqual(os.listdir(self.runtime), [])         # the file, and its folder
        self.assertEqual(self.read(self.rc), self.USERS)
        self.assertEqual(os.stat(self.rc).st_mode & 0o777, 0o644)
        self.assertEqual(self.read(self.recent), mine)
        self.assertEqual(self.read(self.staterc), b"[MainWindow]\n")

    def test_the_whole_screen_too_and_what_else_it_changed_stays(self):
        """A setting of Spectacle's own that names no capture - one the user changed in
        its window meanwhile, say - stays as it is now."""
        with open(self.rc, "wb") as f:
            f.write(self.USERS)
        self.spectacle(also=b"[GuiConfig]\nquitAfterSaveCopyExport=true\n")
        self.assertEqual(self.backend().capture_fullscreen().size, (4, 4))
        self.assertEqual(self.read(self.rc),
                         self.USERS + b"[GuiConfig]\nquitAfterSaveCopyExport=true\n")

    def test_first_run_takes_out_only_the_captures_setting(self):
        """No spectaclerc before: what Spectacle wrote of its own stays, the capture's
        place goes."""
        self.spectacle(also=b"[General]\nlaunchAction=TakeFullscreenScreenshot\n")
        self.backend()._select_blocking()
        self.assertEqual(self.read(self.rc),
                         b"[ImageSave]\n[General]\nlaunchAction=TakeFullscreenScreenshot\n")

    def test_a_cancelled_region_leaves_nothing_either(self):
        with open(self.rc, "wb") as f:
            f.write(self.USERS)

        def cancelled(cmd, timeout=120):
            # Esc: no file, but Spectacle had written where it would have saved.
            with open(self.rc, "ab") as f:
                f.write(b"[ImageSave]\nlastImageSaveAsLocation=file://"
                        + os.path.dirname(cmd[-1]).encode() + b"\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(cli, "_run", side_effect=cancelled), \
                mock.patch.object(cli, "_spectacle_args",
                                  side_effect=lambda path, region: ["spectacle", path]):
            self.assertIsNone(self.backend()._select_blocking())
        self.assertEqual(self.read(self.rc), self.USERS + b"[ImageSave]\n")


if __name__ == "__main__":
    unittest.main()
