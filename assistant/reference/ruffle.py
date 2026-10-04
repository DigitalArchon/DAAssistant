"""Where Ruffle is: the Flash Player emulator that plays the DVD's animations.

Installed from packaging/ruffle.lock - beside the assistant package inside the
AppImage, by packaging/appimage/build.sh, and under ~/.local/share when running from
source, by tools/install-ruffle.sh. Only file names Ruffle itself ships are served from
it (see assistant/ui/encscheme.py).
"""

import os
import re

DIRNAME = "ruffle"
USER_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or
                        os.path.expanduser("~/.local/share"), "da-assistant", DIRNAME)
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), DIRNAME)

_FILE = re.compile(r"^[\w.-]+\.(?:js|wasm)$")


def location():
    """The directory holding ruffle.js, or None when Ruffle is not installed."""
    for folder in (BUNDLED_DIR, USER_DIR):
        if os.path.isfile(os.path.join(folder, "ruffle.js")):
            return folder
    return None


def version():
    folder = location()
    if not folder:
        return None
    try:
        with open(os.path.join(folder, "VERSION")) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def file(name):
    """(bytes, MIME type) of one of Ruffle's own files, or None."""
    folder = location()
    if not folder or not _FILE.match(name or ""):
        return None
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    return data, "application/wasm" if name.endswith(".wasm") else "text/javascript"
