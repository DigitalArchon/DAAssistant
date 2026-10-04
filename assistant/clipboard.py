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
"""

import shutil
import subprocess

from . import gtkcompat, hostenv, session
from gi.repository import Gdk, GLib, Gtk

FALLBACKS = [
    # --type text: left to choose, wl-paste hands back whatever is on offer, and for a
    # copied image that is the PNG itself.
    (["wl-paste", "-n", "--type", "text"], "wl-paste"),
    (["xclip", "-selection", "clipboard", "-o"], "xclip"),
    (["xsel", "--clipboard", "--output"], "xsel"),
]

KLIPPER = ["gdbus", "call", "--session", "--dest", "org.kde.klipper",
           "--object-path", "/klipper", "--method",
           "org.kde.klipper.klipper.getClipboardContents"]


def _run(argv, timeout=3, binary=False):
    try:
        return subprocess.run(argv, capture_output=True, text=not binary,
                              timeout=timeout, env=hostenv.host_env())
    except (OSError, ValueError, subprocess.SubprocessError):
        # ValueError: output that is not text. Decoding it raises UnicodeDecodeError,
        # which is neither of the other two, and used to escape from here and take
        # the "clipboard holds an image" notice with it.
        return None


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


def get_text():
    """Return (text, kind) where kind is text/image/file/empty."""
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    text = clipboard.wait_for_text()
    if text and text.strip():
        return text, "text"

    for argv, name in FALLBACKS:
        if not shutil.which(argv[0]):
            continue
        result = _run(argv)
        if result is None:
            continue
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            return result.stdout, "text"

    text = _klipper_text()
    if text:
        return text, "text"

    if clipboard.wait_is_image_available() or get_image() is not None:
        return None, "image"
    if clipboard.wait_is_uris_available():
        return None, "file"
    return None, "empty"


def unreadable_hint():
    """Why 'empty' may mean 'unreadable', and what the user can do about it.

    Only worth saying on Wayland without wl-paste or Klipper: everywhere else an empty
    clipboard really is empty.
    """
    if session.session_type() != "wayland":
        return ""
    if shutil.which("wl-paste") or _klipper_text() is not None:
        return ""
    return ("On Wayland an app can only read the clipboard while it has focus, which a "
            "tray app never does. Install wl-clipboard (wl-paste) to read it anyway.")


def get_image():
    """PIL image from the clipboard, or None."""
    from PIL import Image

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    pixbuf = clipboard.wait_for_image()
    if pixbuf is not None:
        width, height = pixbuf.get_width(), pixbuf.get_height()
        mode = "RGBA" if pixbuf.get_has_alpha() else "RGB"
        return Image.frombytes(mode, (width, height), pixbuf.get_pixels(),
                               "raw", mode, pixbuf.get_rowstride())

    # Same focus rule as the text above, and the same way out of it.
    if not shutil.which("wl-paste"):
        return None
    result = _run(["wl-paste", "--type", "image/png"], binary=True)
    if result is None or result.returncode != 0 or not result.stdout:
        return None
    import io
    try:
        with Image.open(io.BytesIO(result.stdout)) as img:
            img.load()
            return img.copy()
    except Exception:
        return None


def set_text(text):
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    clipboard.store()
