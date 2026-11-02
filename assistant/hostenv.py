"""Hand spawned host binaries back the environment they were started with.

Inside the AppImage, a few variables are set for the app alone: SSL_CERT_FILE by
AppRun, GI_TYPELIB_PATH by bundle.py where the host lacks a typelib. Everything this app
spawns - grim, spectacle, gnome-screenshot, xclip, gdbus - is a host program, and should
see the environment it would have had anyway. AppRun stashes the caller's original
values as APPIMAGE_HOST_<NAME>, set_for_self() does the same, and host_env() undoes the
substitution for children.

Outside an AppImage there is nothing to undo, so it returns None, which subprocess
reads as "inherit my environment".

The AppImage runtime sets a few variables of its own, naming the image and its mount:
APPIMAGE, APPDIR, OWD, ARGV0. They describe this app, not the host program being run,
and an AppImage-packaged browser that inherited them would take them for its own, so
children never see them.

open_uri() is the same promise for links: the browser a link opens in is a host program
too, started with the environment it would have had anyway.
"""

import os

PREFIX = "APPIMAGE_HOST_"
KEYS = "APPIMAGE_HOST_ENV_KEYS"
RUNTIME = ("APPIMAGE", "APPDIR", "OWD", "ARGV0")


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
    for key in RUNTIME:
        env.pop(key, None)
    return env


def open_uri(uri):
    """Open `uri` with the desktop's handler for it - a link in the browser, mailto: in
    the mail program - started with the host's environment. Raises GLib.Error when
    nothing can open it (no mail program for mailto:, say)."""
    from gi.repository import Gio
    env = host_env()
    context = None
    if env is not None:
        context = Gio.AppLaunchContext()
        for key in set(os.environ) - set(env):
            context.unsetenv(key)
        for key, value in env.items():
            if os.environ.get(key) != value:
                context.setenv(key, value)
    Gio.AppInfo.launch_default_for_uri(uri, context)
