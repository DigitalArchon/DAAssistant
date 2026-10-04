"""Python's cycle collector, run on the main loop and nowhere else.

WebKit aborts the whole process when one of its objects - a WebView, a JavaScript
result - is finalized on any thread but the main one. Python's cycle collector runs on
whichever thread happens to allocate when a generation fills, and a closed chat window
is a cycle (its signal handlers refer back to it), so the collector could free one's
WebView from a worker thread - a lookup, a search, an answer streaming in - and take
the app down with it. Seen in the test suite: SIGABRT from libwebkit2gtk under
g_object_unref, on the thread indexing a dictionary.

So automatic collection is switched off, and the main loop collects instead, every few
seconds. Objects freed by reference counting are unaffected; only cycles wait.
"""

import gc

from gi.repository import GLib

INTERVAL = 10       # seconds between collections

_installed = False


def install(interval=INTERVAL):
    """Collect cycles on the main loop only. Safe to call more than once."""
    global _installed
    if _installed:
        return
    gc.disable()
    GLib.timeout_add_seconds(interval, _collect)
    _installed = True


def _collect():
    gc.collect()
    return True
