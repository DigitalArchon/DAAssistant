"""Where end-to-end encrypted Privacy Mode's dependencies live, and putting them first.

nano-gpt's `private/*` models are sealed to their enclave with Tinfoil's own SDK (see
assistant/sealed.py). That SDK is not packaged by any distribution, and what it pulls in
- cryptography 50, requests 2.34, urllib3 2.8 - is newer than Ubuntu 24.04 ships
(cryptography 41). So it is installed, pinned by hash from packaging/private-mode.lock,
into a directory of its own:

  - inside the AppImage, `private-mode/` beside the `assistant` package, put there by
    packaging/appimage/build.sh;
  - running from source, ~/.local/share/da-assistant/private-mode, put there by
    tools/install-private-mode.sh.

That directory has to be *ahead* of the system's packages on sys.path, and before
anything imports requests: appended behind them, `import cryptography` would find the
distribution's 41.0.7 and Tinfoil's pyOpenSSL would fail against it. assistant/__init__
calls activate() for that reason, before any other module of ours is imported.

The AppImage never reads the one under ~/.local: it runs Python with -I so that nothing
the user installed can shadow what was bundled, and this keeps to the same rule.
"""

import os
import sys

DIRNAME = "private-mode"
USER_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or
                        os.path.expanduser("~/.local/share"), "da-assistant", DIRNAME)
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           DIRNAME)

_active = None


def _in_appimage():
    return bool(os.environ.get("APPIMAGE_HOST_ENV_KEYS"))


def candidates():
    """Directories that may hold the dependencies, in the order they are preferred."""
    found = [BUNDLED_DIR]
    if not _in_appimage():
        found.append(USER_DIR)
    return found


def activate():
    """Put the first directory that exists at the front of sys.path. Idempotent."""
    global _active
    if _active is not None:
        return _active or None
    _active = ""
    for path in candidates():
        if os.path.isdir(os.path.join(path, "tinfoil")):
            if path not in sys.path:
                sys.path.insert(0, path)
            _active = path
            break
    return _active or None


def location():
    """The directory in use, or None."""
    return _active or None
