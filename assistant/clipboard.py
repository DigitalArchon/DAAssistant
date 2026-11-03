"""Clipboard reads that work on both X11 and Wayland.

The logic is the same either way - ask GTK first, then shell out - so it is written once
here.

Why the fallbacks matter more on Wayland than they look: a Wayland client is only offered
the clipboard while it holds keyboard focus. This app is a tray icon driven by a hotkey
and has no focused window at the moment it reads, so Gtk.Clipboard.wait_for_text() comes
back empty every time - not because the clipboard is empty, but because nobody is allowed
to tell us. Everything below exists to get an answer anyway:

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
the wrong text. wl-paste and Klipper ask the compositor afresh each time.
"""

import shutil
import subprocess
import threading

from . import gtkcompat  # noqa: F401  (pins GI versions)
from . import hostenv, session
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


def _run(argv, timeout=3, binary=False):
    """Run a reader and return its CompletedProcess, or None.

    The main loop is kept turning while it runs: the clipboard may be the app's own - an
    answer's Copy button - and on Wayland the reader then has to be served it by this
    very process, which, waiting with its loop stopped, would serve nothing until the
    time limit ran out."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=hostenv.host_env())
    except (OSError, ValueError):
        return None
    done = {}

    def collect():
        try:
            done["out"] = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        except (OSError, ValueError):
            proc.kill()

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    context = GLib.MainContext.default()
    serve = threading.current_thread() is threading.main_thread()
    while reader.is_alive():
        if not (serve and context.iteration(False)):
            reader.join(0.01)
    if "out" not in done:
        return None
    out, err = done["out"]
    if not binary:
        try:
            # Not text: neither of the readers' own failures, and it used to escape
            # from here and take the "clipboard holds an image" notice with it.
            out, err = out.decode("utf-8"), err.decode("utf-8", errors="replace")
        except ValueError:
            return None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


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


def _gtk_text(board):
    text = board.wait_for_text()
    return text if text and text.strip() else None


def get_text():
    """Return (text, kind) where kind is text/image/file/empty."""
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

    text = _klipper_text()
    if text:
        return text, "text"

    if wayland:
        # Last, with no tool to ask: right while the app has focus, stale otherwise.
        text = _gtk_text(clipboard)
        if text:
            return text, "text"
        if clipboard.wait_is_image_available():
            return None, "image"

    if clipboard.wait_is_uris_available():
        return None, "file"
    return None, "empty"


def get_selection():
    """The highlighted text, or None: GTK's primary selection, then the command-line
    tools, which on Wayland can read it without focus as they read the clipboard - and
    on Wayland the tools first, GTK's being stale whenever the app lacks focus."""
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
    return _gtk_text(primary) if wayland else None


def unreadable_hint(selection=False):
    """Why 'empty' may mean 'unreadable', and what the user can do about it.

    Only worth saying on Wayland without wl-paste or Klipper: everywhere else an empty
    clipboard really is empty. Klipper answers for the clipboard and not for the
    highlighted text, so for the `selection` only wl-paste counts: on KDE without
    wl-clipboard, Translate selected text found "nothing highlighted" with text
    highlighted, and never said why (seen on the CachyOS test VM).
    """
    if session.session_type() != "wayland":
        return ""
    if shutil.which("wl-paste") or (not selection and _klipper_text() is not None):
        return ""
    what = "the highlighted text" if selection else "the clipboard"
    return (f"On Wayland an app can only read {what} while it has focus, which a "
            "tray app never does. Install wl-clipboard (wl-paste) to read it anyway.")


def get_image():
    """PIL image from the clipboard, or None. On Wayland wl-paste first, as for text."""
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
    return from_gtk() if wayland else None


def set_text(text):
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    clipboard.store()
