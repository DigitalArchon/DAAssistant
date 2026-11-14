"""Where the app keeps what it writes, by the XDG base directory spec.

Decided once, when the app is first imported: the error log, the WebKit breadcrumb, the
model lists, the dictionary index and debug captures all go under CACHE_DIR, so that
setting XDG_CACHE_HOME moves every one of them - which is how the test suite keeps out
of the cache of whoever runs it (tests/support.py).
"""

import os

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "da-assistant")


def own_folder(folder):
    """`folder` made, as one its owner alone can enter (0700) - or made so, if it is
    there already: os.makedirs' mode is for a folder it makes, and one made by an older
    version, or by hand, kept what it had. Every folder of the app's own is: the cache
    names the models and endpoints used, the error log what went wrong, the encyclopedia's
    data what was read. One that cannot be changed - not ours, on a file system without
    permissions - is left as it is. Returns `folder`."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        mode = os.stat(folder).st_mode & 0o777
        if mode & 0o077:
            os.chmod(folder, mode & 0o700 or 0o700)
    except OSError:
        pass
    return folder
