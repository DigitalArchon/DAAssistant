"""Pin every GI namespace version in one place.

Importing Gdk without pinning it loads 4.0 (both typelibs are installed on Mint 22.3),
which then makes every later `require_version('Gdk','3.0')` fail. Any module that touches
GTK imports this first.

This module also resolves the tray indicator namespace. Hardcoding
`gi.require_version('AppIndicator3','0.1')` would stop the app starting at all on Mint
22.3, which ships only AyatanaAppIndicator3-0.1.typelib.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")


def _load_webkit():
    """True only if WebKit2 can actually be used, not merely imported.

    The two can come apart on a host whose WebKit is broken or half-installed. There
    are two ways that goes wrong, and only one of them looks like a failure:

      - the typelib is missing, or importing it raises ImportError. Easy to see.
      - the typelib loads but its shared library does not, because something it needs
        on the host cannot be resolved. gi prints "Failed to load shared library ...
        referenced by the typelib" as a *warning* and hands back a module whose every
        class is an unclassed 'void' type. The import succeeds. The first attempt to
        use any of it then raises TypeError: could not get a reference to type class -
        by which point we are in the middle of building a window, so the user gets no
        chat window at all and no reason for it. A KDE/Arch host did this when an
        AppImage carried a GTK stack of its own and shadowed a library WebKit needed.

    So the check is a real instantiation. WebKitSettings is a plain GObject: no web
    process, no display, nothing but the type lookup that is the thing in doubt. It is
    deferred until something asks, because building one initialises enough of WebKit to
    be worth avoiding in the processes that never open a window: --version, --probe's
    caller, and the short-lived --action that every hotkey press spawns.
    """
    for version in ("4.1", "4.0"):
        try:
            gi.require_version("WebKit2", version)
        except ValueError:
            continue
        try:
            from gi.repository import WebKit2
            return WebKit2
        except ImportError:
            return None
    return None


def webkit_is_usable(module):
    """Does this namespace's first real use work, or only its import?"""
    try:
        module.Settings()
    except Exception:
        return False
    return True


_WEBKIT = _load_webkit()
_USABLE_WEBKIT = None

_HAS_NOTIFY = False
try:
    gi.require_version("Notify", "0.7")
    _HAS_NOTIFY = True
except ValueError:
    _HAS_NOTIFY = False


def has_webkit():
    """Whether the chat window can use WebKit. Answered once, on first ask."""
    global _USABLE_WEBKIT
    if _USABLE_WEBKIT is None:
        _USABLE_WEBKIT = _WEBKIT is not None and webkit_is_usable(_WEBKIT)
    return _USABLE_WEBKIT


def webkit_failure_detail():
    """Why the host's WebKit will not load, in the dynamic loader's own words.

    gi reports this as a warning on stderr and then carries on, so by the time anything
    notices, the message is long gone. Asking the loader directly gets it back, and it
    is the message that matters: it names the library and the missing symbol.
    """
    import ctypes

    first = ""
    for soname in ("libwebkit2gtk-4.1.so.0", "libwebkit2gtk-4.0.so.37"):
        try:
            ctypes.CDLL(soname)
            return ""                      # it loads; the trouble is elsewhere
        except OSError as e:
            first = first or str(e)
    return first


def webkit_advice():
    """What to install for the rendered chat view, when WebKit will not load; else "".
    The AppImage uses the host's WebKitGTK too, so the answer is the same either way."""
    if has_webkit():
        return ""
    return ("  -> for the rendered view install WebKitGTK: sudo apt install "
            "gir1.2-webkit2-4.1 (Ubuntu, Mint, Debian), sudo dnf install webkit2gtk4.1 "
            "(Fedora), sudo pacman -S webkit2gtk-4.1 (Arch, CachyOS)")


def webkit_status():
    """One line for --probe and the diagnostics window."""
    from . import webkitguard

    if not has_webkit():
        kind = "typelib missing" if _WEBKIT is None else "library unusable"
        detail = webkit_failure_detail()
        return f"no ({kind}; plain text)" + (f" - {detail}" if detail else "")
    blocked, reason = webkitguard.blocked()
    return f"no ({reason})" if blocked else "yes"


def has_notify():
    return _HAS_NOTIFY


def load_indicator():
    """Return (module, kind) for the best available tray backend.

    Order: Ayatana (Mint, Debian, most of Arch) -> legacy AppIndicator3 (older distros)
    -> XApp.StatusIcon (Cinnamon/MATE/XFCE native, works on Wayland) -> Gtk.StatusIcon.
    """
    for namespace, version in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
        try:
            gi.require_version(namespace, version)
            module = __import__("gi.repository", fromlist=[namespace])
            return getattr(module, namespace), "appindicator"
        except (ValueError, ImportError, AttributeError):
            continue
    try:
        gi.require_version("XApp", "1.0")
        from gi.repository import XApp
        return XApp, "xapp"
    except (ValueError, ImportError):
        pass
    return None, "gtk"
