"""Somewhere for a failure to go when there is no terminal to print it to.

PyGObject routes an exception raised inside a GTK callback through sys.excepthook and
then carries on, so the app stays alive and the user is told nothing at all: a menu item
that opens no window, a send button that produces no reply. Launched from a .desktop file
or a hotkey, stderr is not attached to anything either, so the traceback is simply lost.

Both are fixed here: the traceback is written to a log file that can be asked for, and
the first line of it is put in front of the user as a notification.
"""

import os
import sys
import traceback

LOG_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "da-assistant")
LOG_FILE = os.path.join(LOG_DIR, "errors.log")
MAX_BYTES = 256 * 1024

_notify = None
_previous = None


def log_path():
    return LOG_FILE


def record(text):
    """Append to the log, trimming it first so it cannot grow without bound."""
    try:
        os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_BYTES:
            with open(LOG_FILE, "rb") as fh:
                fh.seek(-MAX_BYTES // 2, os.SEEK_END)
                tail = fh.read()
            with open(os.open(LOG_FILE, os.O_WRONLY | os.O_TRUNC, 0o600), "wb") as fh:
                fh.write(tail)
        with open(os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
                  "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass        # a log that cannot be written must never become the failure itself


def _handle(exc_type, value, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        (_previous or sys.__excepthook__)(exc_type, value, tb)
        return
    text = "".join(traceback.format_exception(exc_type, value, tb))
    print(text, file=sys.stderr, end="")
    from datetime import datetime
    record(f"\n----- {datetime.now().isoformat(timespec='seconds')}\n{text}")
    if _notify is not None:
        try:
            _notify(f"{exc_type.__name__}: {value}\nDetails in {LOG_FILE}",
                    urgency="normal")
        except Exception:
            pass


def install(notify=None):
    """Route unhandled exceptions to the log and to `notify(message, urgency=...)`."""
    global _notify, _previous
    _notify = notify
    if _previous is None:
        _previous = sys.excepthook
    sys.excepthook = _handle
