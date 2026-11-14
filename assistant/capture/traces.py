"""What the desktop notes of a capture, taken out again.

The capture itself goes nowhere: the file a screenshot tool or the portal writes is read
into memory and deleted at once (portal._load_uri, cli._discard). But the desktop notes the
file down, and the note outlived it (measured on the lab's desktops, Nov 14 2026):

  - GNOME's screenshot portal adds the screenshot it saves to the recent files list,
    $XDG_DATA_HOME/recently-used.xbel, as Spectacle on KDE does with the file it writes in
    the app's own folder (/run/user/UID/da-assistant-.../capture.png);
  - and Spectacle keeps where it last saved, in its settings: lastImageSaveLocation in
    $XDG_CONFIG_HOME/spectaclerc.

forget_recent takes the capture's own entry out of the recent files: only that one, found
by its URI, and every byte of the rest left as it was. Not through Gtk.RecentManager's
remove_item: GTK writes the whole list back through its own housekeeping, which drops every
entry older than gtk-recent-files-max-age (30 days by default), and with recent files
switched off empties the file (measured with GTK 3.24 on a folder of its own, Nov 14 2026).
Where the entry is not there, nothing is written.

spectacle_settings_kept puts back, once Spectacle has run, each of its settings that now
names the capture's folder: as it was before, or taken out where it was not there. Every
other line stays as Spectacle left it: the user's own settings, and anything changed
meanwhile. Starting Spectacle with a settings folder of its own instead (XDG_CONFIG_HOME)
would have run it without the user's settings, kdeglobals among them. The same is done for
spectaclestaterc, where Plasma 6 programs keep what they remember of their own state.

Neither ever fails a capture: a file that cannot be read, understood or written is left as
it is, and said on the console.
"""

import os
import stat
import urllib.parse
from contextlib import contextmanager
from xml.parsers import expat

from .. import atomic, hostenv

RECENT_FILES = "recently-used.xbel"
# (where, under $HOME if it is not set, which file): Spectacle's settings and its state.
SPECTACLE_FILES = (("XDG_CONFIG_HOME", ".config", "spectaclerc"),
                   ("XDG_STATE_HOME", os.path.join(".local", "state"), "spectaclestaterc"))
# The most of any of these read: GTK keeps a thousand recent files, a few hundred KB.
MOST = 32 * 1024 * 1024


def _env(env):
    """The environment the desktop's programs run with: a screenshot tool is started
    with it (hostenv.host_env)."""
    return env if env is not None else (hostenv.host_env() or os.environ)


def _base(env, name, under_home):
    """An XDG base directory: $name if it is an absolute path, else ~/under_home."""
    value = env.get(name) or ""
    if os.path.isabs(value):
        return value
    return os.path.join(env.get("HOME") or os.path.expanduser("~"), under_home)


def recent_files(env=None):
    """The recent files list, where GTK, GNOME Shell and KDE all keep it."""
    return os.path.join(_base(_env(env), "XDG_DATA_HOME", os.path.join(".local", "share")),
                        RECENT_FILES)


def _path_of(named):
    """The local path a file:// URI - or a path - names, or None for anything else."""
    text = str(named)
    if text.startswith("/"):
        return os.path.normpath(text)
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
        return None
    return os.path.normpath(urllib.parse.unquote(parts.path))


def _read(path):
    """A file's bytes, or None where there is no such file, it is no ordinary file (a
    link to /dev/null, say), or it is past MOST."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as f:
            data = f.read(MOST + 1)
    except OSError:
        return None
    return data if len(data) <= MOST else None


def _write_over(path, data):
    """`data` in `path`'s place, whole (atomic), with the permissions it had."""
    atomic.write_bytes(path, data, mode=stat.S_IMODE(os.stat(path).st_mode))


def _tag_end(data, at):
    """Where the tag that begins at `at` ends: past its '>', a quoted one not counting."""
    quote = None
    for i in range(at, len(data)):
        byte = data[i]
        if quote is not None:
            if byte == quote:
                quote = None
        elif byte in b"\"'":
            quote = byte
        elif byte == ord(">"):
            return i + 1
    raise ValueError("a tag with no end")


def _bookmarks_of(data, path):
    """[(start, end)] of each <bookmark> in a recent files list whose href names `path`."""
    parser = expat.ParserCreate()
    found, under_way = [], []

    def started(name, attributes):
        if name == "bookmark":
            under_way.append((parser.CurrentByteIndex, attributes.get("href", "")))

    def ended(name):
        if name == "bookmark" and under_way:
            start, href = under_way.pop()
            if _path_of(href) == path:
                # Expat gives an end tag where it begins, and an element that is one
                # tag alone (<bookmark .../>) where that tag ends.
                opened = _tag_end(data, start)
                end = (opened if data[opened - 2:opened] == b"/>"
                       else _tag_end(data, parser.CurrentByteIndex))
                found.append((start, end))

    parser.StartElementHandler, parser.EndElementHandler = started, ended
    parser.Parse(data, True)
    return found


def _without(data, spans):
    """`data` less each span, and the line each stood on, where it stood alone."""
    kept, last = [], 0
    for start, end in sorted(spans):
        line = data.rfind(b"\n", 0, start) + 1
        if not data[line:start].strip(b" \t"):
            after = end
            while data[after:after + 1] in (b" ", b"\t"):
                after += 1
            for ending in (b"\r\n", b"\n"):
                if data.startswith(ending, after):
                    start, end = line, after + len(ending)
                    break
        kept.append(data[last:start])
        last = end
    kept.append(data[last:])
    return b"".join(kept)


def forget_recent(named, env=None):
    """Take the entry for `named` - a capture's file, by its path or its file:// URI -
    out of the recent files list. How many entries went: 0 where there was none, and
    then nothing is written."""
    path = _path_of(named)
    if path is None:
        return 0
    target = os.path.realpath(recent_files(env))    # through a link, the file it names
    data = _read(target)
    if not data:
        return 0
    try:
        spans = _bookmarks_of(data, path)
        if spans:
            _write_over(target, _without(data, spans))
    except (OSError, ValueError, expat.ExpatError) as e:
        print(f"[capture] the capture's entry in {target} not taken out: {e}")
        return 0
    return len(spans)


def _entries(lines):
    """{(group, key): (line, value)} of a KConfig file: a group by its header as written,
    a key by what stands before its '='."""
    group, found = b"", {}
    for number, line in enumerate(lines):
        text = line.strip()
        if text.startswith(b"["):
            group = text
        elif text and not text.startswith(b"#") and b"=" in text:
            key, value = text.split(b"=", 1)
            found[(group, key.strip())] = (number, value.strip())
    return found


def _names(value, folder):
    text = value.decode("utf-8", "replace")
    return folder in text or urllib.parse.quote(folder) in text


def _put_back(path, before, folder):
    """Each setting in the KConfig file at `path` that names `folder` now and did not
    `before` (its bytes, None where there was no file): as it was, or taken out."""
    now = _read(path)
    if now is None or now == before:
        return
    lines = now.splitlines(keepends=True)
    then = (before or b"").splitlines(keepends=True)
    was = _entries(then)
    changed = False
    for place, (number, value) in _entries(lines).items():
        if not _names(value, folder) or (place in was and was[place][1] == value):
            continue
        line = lines[number]
        ending = line[len(line.rstrip(b"\r\n")):]
        lines[number] = then[was[place][0]].rstrip(b"\r\n") + ending if place in was else b""
        changed = True
    if changed:
        _write_over(path, b"".join(lines))


@contextmanager
def spectacle_settings_kept(capture, env=None):
    """Spectacle runs inside the block, writing `capture`: afterwards, none of its
    settings names the capture's folder that did not before (see the module)."""
    folder = os.path.dirname(os.path.abspath(capture))
    env = _env(env)
    files = [os.path.realpath(os.path.join(_base(env, name, under_home), file))
             for name, under_home, file in SPECTACLE_FILES]
    before = {path: _read(path) for path in files}
    try:
        yield
    finally:
        for path, was in before.items():
            try:
                _put_back(path, was, folder)
            except (OSError, ValueError) as e:
                print(f"[capture] Spectacle's record of the capture in {path} not "
                      f"taken out: {e}")
