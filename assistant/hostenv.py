"""Hand spawned host binaries back the environment they were started with.

Inside the AppImage, a few variables are set for the app alone: SSL_CERT_FILE by
AppRun, GI_TYPELIB_PATH by bundle.py where the host lacks a typelib. Everything this app
spawns - grim, spectacle, gnome-screenshot, xclip, gdbus - is a host program, and should
see the environment it would have had anyway. AppRun stashes the caller's original
values as APPIMAGE_HOST_<NAME>, set_for_self() does the same, and host_env() undoes the
substitution for children.

Outside an AppImage there is nothing to undo, so it returns None, which subprocess
reads as "inherit my environment".
"""

import os

PREFIX = "APPIMAGE_HOST_"
KEYS = "APPIMAGE_HOST_ENV_KEYS"


def set_for_self(name, value):
    """Set `name` for this process only: host_env() hands children the value it had
    before. Inside the AppImage only; outside, nothing is set for the app alone."""
    keys = os.environ.get(KEYS, "").split()
    if name not in keys:
        if name in os.environ:
            os.environ[PREFIX + name] = os.environ[name]
        os.environ[KEYS] = " ".join(keys + [name])
    os.environ[name] = value


def host_env():
    """Environment for a host binary, or None to inherit ours unchanged."""
    keys = os.environ.get(KEYS)
    if not keys:
        return None

    env = dict(os.environ)
    for key in keys.split():
        env.pop(key, None)
        # Only the variables AppRun actually overrode were stashed, so a missing
        # stash means the caller had it unset and it should stay unset.
        saved = env.pop(PREFIX + key, None)
        if saved is not None:
            env[key] = saved
    env.pop(KEYS, None)
    return env
