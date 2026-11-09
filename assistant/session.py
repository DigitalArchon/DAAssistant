"""Display-server / desktop detection and runtime capability probing.

A Cinnamon Wayland session that falls through to 'unknown' gets gnome-screenshot, which
needs a GNOME Shell D-Bus interface that is not there, and every Wayland screenshot
fails. So this module names Cinnamon, MATE and XFCE explicitly and, more importantly,
probes the xdg-desktop-portal interfaces that are actually running rather than guessing
from environment variables alone.
"""

import os
import shutil
import subprocess
import time

from . import hostenv

X11 = "x11"
WAYLAND = "wayland"
UNKNOWN = "unknown"

# Tools each backend needs, for --probe reporting and the README.
KNOWN_TOOLS = ["grim", "slurp", "spectacle", "gnome-screenshot", "wl-paste", "xclip",
               "xsel", "tesseract", "notify-send"]


def session_type():
    t = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if t in (X11, WAYLAND):
        return t
    if os.environ.get("WAYLAND_DISPLAY"):
        return WAYLAND
    if os.environ.get("DISPLAY"):
        return X11
    return UNKNOWN


def desktop():
    """Normalised desktop name: cinnamon, kde, gnome, hyprland, sway, mate, xfce, unknown."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if os.environ.get("SWAYSOCK"):
        return "sway"

    raw = (os.environ.get("XDG_CURRENT_DESKTOP", "") or
           os.environ.get("XDG_SESSION_DESKTOP", "") or
           os.environ.get("DESKTOP_SESSION", "")).lower()
    # XDG_CURRENT_DESKTOP is colon-separated and Cinnamon reports "X-Cinnamon".
    for part in raw.replace("x-", "").split(":"):
        part = part.strip()
        if not part:
            continue
        if "cinnamon" in part:
            return "cinnamon"
        if "kde" in part or "plasma" in part:
            return "kde"
        if "gnome" in part:
            return "gnome"
        if "hyprland" in part:
            return "hyprland"
        if "sway" in part:
            return "sway"
        if "mate" in part:
            return "mate"
        if "xfce" in part:
            return "xfce"
    return UNKNOWN


def _dbus_introspect(dest, path):
    try:
        out = subprocess.run(["gdbus", "introspect", "--session", "--dest", dest,
                              "--object-path", path],
                             capture_output=True, text=True, timeout=5,
                             env=hostenv.host_env())
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# Seconds a probe of the portal that got no answer is believed for, before the portal
# is asked again.
PROBE_RETRY = 10.0

_portal_xml = None          # the portal's introspection, once it has answered
_portal_names = None        # the interfaces in it
_portal_unanswered = None   # time.monotonic() of the last probe that got no answer


def _portal_introspection():
    """The running xdg-desktop-portal's introspection, or "" while it does not answer.

    Kept once it has answered, for the rest of the run - and not when it has not. Asked
    at login, the portal may still be starting, and gdbus gives up after 5 s: that "" was
    kept as the answer, and the app went the whole run without the portal's screenshots
    or its shortcuts. Now it is asked again, PROBE_RETRY seconds on.
    """
    global _portal_xml, _portal_names, _portal_unanswered
    if _portal_xml is not None:
        return _portal_xml
    if _portal_unanswered is not None and \
            time.monotonic() - _portal_unanswered < PROBE_RETRY:
        return ""
    xml = _dbus_introspect("org.freedesktop.portal.Desktop",
                           "/org/freedesktop/portal/desktop")
    if not xml:
        _portal_unanswered = time.monotonic()
        return ""
    names = set()
    for line in xml.splitlines():
        line = line.strip()
        if line.startswith("interface org.freedesktop.portal."):
            names.add(line.split()[1].rstrip(" {"))
    _portal_xml, _portal_names, _portal_unanswered = xml, names, None
    return xml


def portal_answered():
    """Whether the portal has answered a probe this run: until it has, what
    portal_has says is only "not yet", and may change."""
    _portal_introspection()
    return _portal_xml is not None


def portal_interfaces():
    """Interfaces the running xdg-desktop-portal actually exposes - none while it has
    not answered (see _portal_introspection).

    Probed rather than assumed: on this Cinnamon box Screenshot is present (v2, so it
    supports the interactive region picker) but GlobalShortcuts is not, which is exactly
    the split that decides whether shortcuts are written into the desktop instead.
    """
    _portal_introspection()
    return set(_portal_names or ())


def portal_has(interface):
    return f"org.freedesktop.portal.{interface}" in portal_interfaces()


_backends = {}              # portal backend name -> whether it was running


def portal_backend_running(name):
    """Whether the portal backend xdg-desktop-portal-`name` ("wlr", "hyprland") is on
    the session bus. The portal starts its backends as it starts itself, so once the
    portal has answered (portal_answered) the answer is kept; before that a "no" may only
    mean "not yet", and is asked again next time."""
    if name in _backends:
        return _backends[name]
    answered = portal_answered()
    try:
        out = subprocess.run(["gdbus", "call", "--session",
                              "--dest", "org.freedesktop.DBus",
                              "--object-path", "/org/freedesktop/DBus", "--method",
                              "org.freedesktop.DBus.NameHasOwner",
                              f"org.freedesktop.impl.portal.desktop.{name}"],
                             capture_output=True, text=True, timeout=5,
                             env=hostenv.host_env())
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    running = "true" in out.stdout
    if running or answered:
        _backends[name] = running
    return running


def portal_version(interface):
    """Screenshot v2+ supports the `interactive` option; v1 does not. None while the
    portal has not answered, or does not say."""
    xml = _portal_introspection()
    in_iface = False
    for line in xml.splitlines():
        s = line.strip()
        if s.startswith(f"interface org.freedesktop.portal.{interface}"):
            in_iface = True
        elif in_iface:
            if s.startswith("interface "):
                break
            if "version" in s and "=" in s:
                try:
                    return int(s.split("=")[1].strip().rstrip(";").strip())
                except ValueError:
                    return None
    return None


def has_cinnamon_screenshot():
    return "org.Cinnamon" in _dbus_introspect("org.Cinnamon", "/org/Cinnamon")


def available_tools():
    return {name: shutil.which(name) for name in KNOWN_TOOLS}


def summary():
    """Human-readable capability report for `--probe`."""
    st, de = session_type(), desktop()
    lines = [
        f"Session type   : {st}",
        f"Desktop        : {de}",
        f"Portal ifaces  : {', '.join(sorted(i.split('.')[-1] for i in portal_interfaces())) or 'none (portal not running?)'}",
        f"  Screenshot   : {'yes, version ' + str(portal_version('Screenshot')) if portal_has('Screenshot') else 'NO'}",
        # Whether the portal is there, not what binds the shortcuts: on X11 the app grabs
        # them itself either way. --probe and Diagnostics name the mechanism after this.
        f"  GlobalShortcuts: {'yes, version ' + str(portal_version('GlobalShortcuts')) if portal_has('GlobalShortcuts') else 'no'}",
        f"Cinnamon D-Bus : {'yes' if has_cinnamon_screenshot() else 'no'}",
    ]
    tools = available_tools()
    found = [t for t, p in tools.items() if p]
    missing = [t for t, p in tools.items() if not p]
    lines.append(f"Tools present  : {', '.join(found) or 'none'}")
    lines.append(f"Tools missing  : {', '.join(missing) or 'none'}")
    return "\n".join(lines)
