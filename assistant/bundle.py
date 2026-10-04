"""Filling gaps in the host's GTK stack, from the fallbacks the AppImage carries.

The AppImage brings its own Python and PyGObject, and uses the host's GLib, GTK and
WebKitGTK: those should get the distribution's security updates, and a GTK stack from
another distribution is what used to break the host's WebKit. A few pieces are packaged
apart from GTK, though, and not every host has them:

  - girepository-2.0, which PyGObject is built against: Debian and Ubuntu package it
    apart from GLib (libgirepository-2.0-0). Loaded under its own soname, so PyGObject's
    import finds it already loaded.
  - typelibs: GLib's own (Debian's gir1.2-glib-2.0), the base ones GTK's refer to
    (xlib, cairo, freetype: Arch ships them apart), GTK's own (Debian's gir1.2-gtk-3.0),
    the tray indicator's and libnotify's. Only those the host lacks are added to the
    search path, for this process only.
  - the libraries behind the indicator's and libnotify's typelibs. Without the
    indicator, Plasma on Wayland shows no tray icon at all. Each is loaded only where the
    host has no copy of its own, and a typelib whose libraries will not load is left out,
    so the app falls back as it would have without it.

The host's own always win. Outside the AppImage there are no fallbacks and this does
nothing. It has to run before gi is first imported: assistant/__init__ calls it.
"""

import ctypes
import os
import sys

HOST_TYPELIB_DIRS = ("/usr/lib64/girepository-1.0",
                     "/usr/lib/x86_64-linux-gnu/girepository-1.0",
                     "/usr/lib/girepository-1.0")

# The libraries behind a bundled typelib, by soname in the order they load.
LIBRARIES = {
    "AyatanaAppIndicator3-0.1": ("libayatana-ido3-0.4.so.0", "libdbusmenu-glib.so.4",
                                 "libdbusmenu-gtk3.so.4", "libayatana-indicator3.so.7",
                                 "libayatana-appindicator3.so.1"),
    "Notify-0.7": ("libnotify.so.4",),
}

_used = None


def fallback_dir():
    """usr/lib/fallback in the AppImage: its Python is usr/python/bin/python3."""
    usr = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(sys.executable))))
    return os.path.join(usr, "lib", "fallback")


def _in_appimage():
    return bool(os.environ.get("APPIMAGE_HOST_ENV_KEYS"))


def _load(soname, directory):
    """The host's copy of a library if it has one, else ours. False if neither loads."""
    try:
        ctypes.CDLL(soname)
        return True
    except OSError:
        pass
    try:
        ctypes.CDLL(os.path.join(directory, "libs", soname), mode=ctypes.RTLD_GLOBAL)
        return True
    except OSError:
        return False


def fill_gaps(directory=None, host_dirs=HOST_TYPELIB_DIRS):
    """Put the fallbacks the host needs in use; returns their names. Runs once."""
    global _used
    if _used is not None:
        return _used
    _used = []
    if directory is None:
        if not _in_appimage():
            return _used
        directory = fallback_dir()
    if not os.path.isdir(directory):
        return _used

    try:
        ctypes.CDLL("libgirepository-2.0.so.0")
    except OSError:
        try:
            ctypes.CDLL(os.path.join(directory, "libgirepository-2.0.so.0"),
                        mode=ctypes.RTLD_GLOBAL)
            _used.append("girepository-2.0")
        except OSError:
            pass                        # gi's own import error will say what is wrong

    typelibs = os.path.join(directory, "typelibs")
    added = []
    for name in sorted(os.listdir(typelibs)) if os.path.isdir(typelibs) else ():
        if any(os.path.exists(os.path.join(host, f"{name}.typelib")) for host in host_dirs):
            continue
        if all(_load(soname, directory) for soname in LIBRARIES.get(name, ())):
            added.append(os.path.join(typelibs, name))
            _used.append(name)
    if added:
        from . import hostenv
        current = os.environ.get("GI_TYPELIB_PATH")
        hostenv.set_for_self("GI_TYPELIB_PATH", ":".join(added + ([current] if current else [])))
    return _used


def status():
    """One line for --probe and Diagnostics; "" outside the AppImage."""
    if not _in_appimage():
        return ""
    used = fill_gaps()
    return "bundled " + ", ".join(used) if used else "none needed - all the host's own"
