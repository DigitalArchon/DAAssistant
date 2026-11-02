"""Where the app keeps what it writes, by the XDG base directory spec.

Decided once, when the app is first imported: the error log, the WebKit breadcrumb, the
model lists, the dictionary index and debug captures all go under CACHE_DIR, so that
setting XDG_CACHE_HOME moves every one of them - which is how the test suite keeps out
of the cache of whoever runs it (tests/support.py).
"""

import os

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "da-assistant")
