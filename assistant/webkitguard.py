"""Notice when the WebKit view takes the whole app down, and stop using it.

The AppImage carries its own GTK but borrows the host's WebKit, because WebKit spawns
its web process from a path compiled into the library and cannot be relocated. Where the
host's WebKit was built against a different GTK 3 than the one in the bundle, loading it
puts two copies of the same library in one process. Sometimes that fails cleanly at
import, which gtkcompat already turns into the plain-text view; sometimes it imports and
then kills the process the moment a WebView is realised. The second kind is the one that
looked like "the window just closes and nothing happens": no traceback, no reply, because
there is no process left to report anything.

A crash cannot be caught from inside the process it kills, so this works by breadcrumb.
Before a WebView is built the attempt is counted on disk; the count is cleared when the
view has actually rendered, and again on a clean exit. An attempt that is still counted
when the next one starts means the last one never reached a rendered frame.

It takes two of those to give up on WebKit, and the reason is that not every abrupt end
is WebKit's fault: a session that logs out, an OOM kill, a SIGKILL while a window is
still loading would each leave the same breadcrumb, and one of those must not cost the
user the rendered view for good. A real mismatch fails every single time, so it reaches
two immediately, while a one-off is forgiven by the next load that works.

The count belongs to the version that recorded it, for the same reason: an update is the
likeliest fix for whatever WebKit could not do, and it must not inherit the old build's
verdict.

Delete the file named by state_path(), or unset DA_ASSISTANT_NO_WEBKIT, to try again -
after installing a matching webkit2gtk, say.
"""

import os
import time

from . import __version__
from .errors import LOG_DIR      # same per-user cache directory as the error log

STATE_FILE = os.path.join(LOG_DIR, "webkit-crash")
FORCE_OFF = "DA_ASSISTANT_NO_WEBKIT"
STRIKES = 2


def state_path():
    return STATE_FILE


def _strikes():
    """Strikes recorded against the build that is running, and no other.

    A verdict about one build says nothing about the next one: an update is the most
    likely thing to have fixed whatever WebKit was tripping over, and it is exactly how
    the first version of this guard stranded a user on the plain-text view - the build
    that could not load WebKit left two strikes behind, and the build that fixed it
    believed them.
    """
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            count, _stamp, version = (fh.read().split() + ["", "", ""])[:3]
        return int(count) if version == __version__ else 0
    except (OSError, ValueError):
        return 0


def blocked():
    """(bool, reason) - whether to avoid WebKit, and what to tell the user."""
    if os.environ.get(FORCE_OFF, "").strip() not in ("", "0"):
        return True, f"disabled by {FORCE_OFF}"
    if _strikes() >= STRIKES:
        return True, (f"the WebKit view ended this app {STRIKES} times running; using "
                      f"plain text. Delete {STATE_FILE} to try it again")
    return False, ""


def note_attempt():
    """Count a WebView about to be built, in case building it is the last thing we do."""
    count = _strikes() + 1      # read before the open below truncates the file
    try:
        os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
        with open(os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "w", encoding="utf-8") as fh:
            fh.write(f"{count} {time.time():.0f} {__version__}\n")
    except OSError:
        pass


def note_success():
    """The view rendered, so whatever happens later was not it."""
    try:
        os.unlink(STATE_FILE)
    except OSError:
        pass


# A clean shutdown while a view was still loading is not a crash, so the app clears the
# breadcrumb on its way out too. That is what keeps quitting mid-load from costing the
# user their WebKit view.
note_clean_exit = note_success
