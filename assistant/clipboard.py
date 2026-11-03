"""Clipboard reads that work on both X11 and Wayland.

The logic is the same either way - ask GTK first, then shell out - so it is written once
here.

Why the fallbacks matter more on Wayland than they look: a Wayland client is only offered
the clipboard while it holds keyboard focus. This app is a tray icon driven by a hotkey
and has no focused window at the moment it reads, so Gtk.Clipboard.wait_for_text() has
nothing current to give - not because the clipboard is empty, but because nobody is
allowed to tell us. Everything below exists to get an answer anyway:

  wl-paste  - wl-clipboard's own protocol (wlr/ext-data-control), which is exempt from
              the focus rule. Needs the wl-clipboard package.
  klipper   - Plasma's clipboard manager, over D-Bus. Already running on any KDE desktop,
              so it is the one that needs nothing installed.

The highlighted text - X11's PRIMARY selection, what a middle click pastes, which
Wayland keeps too - is read the same way (get_selection), for Translate selected text:
nothing has to be copied first.

On Wayland GTK is asked last, not first. It hears of a new clipboard only while the app
has focus, and otherwise answers with what it last heard - so once one of the app's
windows had been focused, the text copied after it closed came back as the text before
(measured on KDE, GNOME 50 and Hyprland, Nov 2026): Translate clipboard text translated
the wrong text. wl-paste and Klipper ask the compositor afresh each time. And GTK is
asked at all only while one of the app's windows has focus, when what it heard is
current. Asked at any other time, with neither tool to read it, it gave back the app's
own Copy from an hour before as the clipboard - text, picture or file - with nothing
to say it was not; now the read comes back empty, and unreadable_hint says why.
"""

import shutil
import subprocess
import threading

from . import gtkcompat  # noqa: F401  (pins GI versions)
from . import APP_NAME, hostenv, session
from gi.repository import Gdk, GLib, Gtk

FALLBACKS = [
    # --type text: left to choose, wl-paste hands back whatever is on offer, and for a
    # copied image that is the PNG itself.
    (["wl-paste", "-n", "--type", "text"], "wl-paste"),
    (["xclip", "-selection", "clipboard", "-o"], "xclip"),
    (["xsel", "--clipboard", "--output"], "xsel"),
]

SELECTION_FALLBACKS = [
    (["wl-paste", "-n", "--primary", "--type", "text"], "wl-paste"),
    (["xclip", "-selection", "primary", "-o"], "xclip"),
    (["xsel", "--primary", "--output"], "xsel"),
]

KLIPPER = ["gdbus", "call", "--session", "--dest", "org.kde.klipper",
           "--object-path", "/klipper", "--method",
           "org.kde.klipper.klipper.getClipboardContents"]

# The most a read takes in, of text and of a picture: twice the text of the longest
# context any model takes, and more than a PNG of a whole 4K screen can come to.
TEXT_LIMIT = 8 * 1024 * 1024
IMAGE_LIMIT = 64 * 1024 * 1024


class _TooLarge(Exception):
    """A reader had more to give than its limit, the number of bytes in args[0]."""


# What the last read found too large to take in, for unreadable_hint: ("the clipboard"
# or "the highlighted text", the limit), or None.
_too_large = None


def _run(argv, timeout=3, binary=False):
    """Run a reader and return its CompletedProcess, or None; _TooLarge, the reader
    stopped, once it has given more than a read takes in - IMAGE_LIMIT bytes of a
    `binary` read, a picture's, and TEXT_LIMIT of any other.

    The main loop is kept turning while it runs: the clipboard may be the app's own - an
    answer's Copy button - and on Wayland the reader then has to be served it by this
    very process, which, waiting with its loop stopped, would serve nothing until the
    time limit ran out.

    Its output is taken a piece at a time, up to the limit. Taken whole, as it once was,
    100 MB on the clipboard took the app from 85 MB to 372 MB, and 300 MB had it killed
    under a 900 MB limit. What it says on stderr is not wanted, and not kept."""
    limit = IMAGE_LIMIT if binary else TEXT_LIMIT
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=hostenv.host_env())
    except (OSError, ValueError):
        return None
    out, ended = bytearray(), {}

    def collect():
        try:
            while len(out) <= limit:
                chunk = proc.stdout.read1(1 << 16)
                if not chunk:
                    return
                out.extend(chunk)
        except (OSError, ValueError):
            ended["failed"] = True

    def too_slow():
        ended["late"] = True
        proc.kill()

    reader = threading.Thread(target=collect, daemon=True)
    timer = threading.Timer(timeout, too_slow)
    timer.daemon = True
    reader.start()
    timer.start()
    context = GLib.MainContext.default()
    serve = threading.current_thread() is threading.main_thread()
    while reader.is_alive():
        if not (serve and context.iteration(False)):
            reader.join(0.01)
    timer.cancel()
    if len(out) > limit:
        proc.kill()
    proc.stdout.close()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if len(out) > limit:
        raise _TooLarge(limit)
    if ended:
        return None
    out = bytes(out)
    if not binary:
        try:
            # Not text: neither of the readers' own failures, and it used to escape
            # from here and take the "clipboard holds an image" notice with it.
            out = out.decode("utf-8")
        except ValueError:
            return None
    return subprocess.CompletedProcess(argv, proc.returncode, out, b"" if binary else "")


def _bounded(text):
    """`text`, which GTK has read in whole, unless it is more than a read takes in."""
    if text and len(text) > TEXT_LIMIT:
        raise _TooLarge(TEXT_LIMIT)
    return text


def _klipper_text():
    """Plasma's clipboard manager, which answers regardless of who has focus.

    Klipper is part of plasmashell, so on KDE this path needs nothing installed - which
    is what makes it the one that rescues a stock Plasma Wayland session.
    """
    if not shutil.which("gdbus"):
        return None
    result = _run(KLIPPER)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    # gdbus prints a GVariant tuple: ('the text',). GLib parses its own format, and
    # doing it that way keeps escapes and quotes in the text intact.
    try:
        value = GLib.Variant.parse(None, result.stdout.strip(), None, None).unpack()
    except GLib.Error:
        return None
    text = value[0] if isinstance(value, tuple) and value else value
    return text if isinstance(text, str) and text.strip() else None


def _wayland():
    return session.session_type() == "wayland"


def _focused():
    """Whether one of the app's windows has the keyboard now: on Wayland, the only time
    what GTK says of the clipboard is current (see the module docstring)."""
    return any(window.is_active() for window in Gtk.Window.list_toplevels())


def _gtk_text(board):
    text = _bounded(board.wait_for_text())
    return text if text and text.strip() else None


def _limited(what, read, nothing):
    """`read()`, or `nothing` once it found more than it takes in - which is kept, for
    unreadable_hint to say, until the next read."""
    global _too_large
    _too_large = None
    try:
        return read()
    except _TooLarge as e:
        _too_large = (what, e.args[0])
        return nothing


def get_text():
    """Return (text, kind) where kind is text/image/file/empty - "empty" too for a
    clipboard holding more than a read takes in, as unreadable_hint then says."""
    return _limited("the clipboard", _get_text, (None, "empty"))


def _get_text():
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    wayland = _wayland()

    if not wayland:
        text = _gtk_text(clipboard)         # X11: GTK is always current
        if text:
            return text, "text"

    for argv, _name in FALLBACKS:
        if not shutil.which(argv[0]):
            continue
        result = _run(argv)
        if result is None:
            continue
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            return result.stdout, "text"

    # A picture before Klipper: asked about a copied image, Klipper answers with a line
    # describing it, which read as the text to translate.
    if (not wayland and clipboard.wait_is_image_available()) or get_image() is not None:
        return None, "image"
    if _too_large is not None:              # a picture, larger than is taken in
        raise _TooLarge(_too_large[1])

    text = _klipper_text()
    if text:
        return text, "text"

    # On Wayland GTK last, and only while it is current: with no window of ours focused
    # it is whatever was copied when one last was, and the read is "empty" instead.
    current = not wayland or _focused()
    if wayland and current:
        text = _gtk_text(clipboard)
        if text:
            return text, "text"
        if clipboard.wait_is_image_available():
            return None, "image"

    if current and clipboard.wait_is_uris_available():
        return None, "file"
    return None, "empty"


def get_selection():
    """The highlighted text, or None: GTK's primary selection, then the command-line
    tools, which on Wayland can read it without focus as they read the clipboard - and
    on Wayland the tools first, and GTK only while one of the app's windows has focus,
    its selection being stale at any other time. None too for more than a read takes
    in, as unreadable_hint then says."""
    return _limited("the highlighted text", _get_selection, None)


def _get_selection():
    primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
    wayland = _wayland()
    if not wayland:
        text = _gtk_text(primary)
        if text:
            return text
    for argv, _name in SELECTION_FALLBACKS:
        if not shutil.which(argv[0]):
            continue
        result = _run(argv)
        if result is not None and result.returncode == 0 and result.stdout \
                and result.stdout.strip():
            return result.stdout
    return _gtk_text(primary) if wayland and _focused() else None


def unreadable_hint(selection=False):
    """Why 'empty' may mean 'unreadable', and what the user can do about it.

    Only worth saying on Wayland without wl-paste or Klipper: everywhere else an empty
    clipboard really is empty. Klipper answers for the clipboard and not for the
    highlighted text, so for the `selection` only wl-paste counts: on KDE without
    wl-clipboard, Translate selected text found "nothing highlighted" with text
    highlighted, and never said why (seen on the CachyOS test VM). Or, in any session,
    the read just made found more than it takes in (see _run).
    """
    what = "the highlighted text" if selection else "the clipboard"
    if _too_large is not None and _too_large[0] == what:
        size = f"more than {_too_large[1] // (1024 * 1024)} MB"
        return (f"{'The highlighted text is' if selection else 'It holds'} {size}, more "
                f"than {APP_NAME} reads at once.")
    if session.session_type() != "wayland":
        return ""
    if shutil.which("wl-paste") or (not selection and _klipper_answers()):
        return ""
    return (f"On Wayland an app can only read {what} while it has focus, which a "
            "tray app never does. Install wl-clipboard (wl-paste) to read it anyway.")


def _klipper_answers():
    try:
        return _klipper_text() is not None
    except _TooLarge:
        return True


def get_image():
    """PIL image from the clipboard, or None. On Wayland wl-paste first, and GTK only
    while one of the app's windows has focus, as for text. None too for a picture of
    more than IMAGE_LIMIT bytes, as unreadable_hint then says."""
    return _limited("the clipboard", _get_image, None)


def _get_image():
    from PIL import Image

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    wayland = _wayland()

    def from_gtk():
        pixbuf = clipboard.wait_for_image()
        if pixbuf is None:
            return None
        width, height = pixbuf.get_width(), pixbuf.get_height()
        mode = "RGBA" if pixbuf.get_has_alpha() else "RGB"
        return Image.frombytes(mode, (width, height), pixbuf.get_pixels(),
                               "raw", mode, pixbuf.get_rowstride())

    if not wayland:
        image = from_gtk()
        if image is not None:
            return image

    # Same focus rule as the text above, and the same way out of it.
    if shutil.which("wl-paste"):
        result = _run(["wl-paste", "--type", "image/png"], binary=True)
        if result is not None and result.returncode == 0 and result.stdout:
            import io
            try:
                with Image.open(io.BytesIO(result.stdout)) as img:
                    img.load()
                    return img.copy()
            except Exception:
                pass
    return from_gtk() if wayland and _focused() else None


def set_text(text):
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    clipboard.store()
