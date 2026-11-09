"""The status colours of the GTK widgets - privacy badges, warnings in Settings -
readable in the light and the dark theme.

style.css gave them one colour each, for both: the red of a refused address or a failed
check was 2.6:1 on the dark theme's grey, and the badges' green and amber were 3.4:1 and
3:1 on a light window, small and bold. These are the chat page's own pairs
(markdown_html), each above 4.5:1 on its theme, put in front of style.css by a provider
of their own, which is rewritten when the desktop's theme changes.
"""

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, Gtk

LIGHT = {"ok": "#1d7f47", "warn": "#94650a", "bad": "#c4271f"}
DARK = {"ok": "#5cc98a", "warn": "#e0b04a", "bad": "#ff7b72"}

_provider = None


def is_dark():
    settings = Gtk.Settings.get_default()
    if settings is None:
        return False
    if settings.get_property("gtk-application-prefer-dark-theme"):
        return True
    return "dark" in (settings.get_property("gtk-theme-name") or "").lower()


def css(dark):
    tone = DARK if dark else LIGHT
    return (f".privacy-ok {{ color: {tone['ok']}; }}\n"
            f".privacy-warn {{ color: {tone['warn']}; }}\n"
            f".privacy-bad, .warning-text {{ color: {tone['bad']}; }}\n"
            f".chat-bubble.chat-private {{ border-left-color: {tone['ok']}; }}\n")


def install():
    """Put the colours for the theme in force in front of style.css, and keep them so
    as the theme changes. Once, at startup."""
    global _provider
    screen = Gdk.Screen.get_default()
    settings = Gtk.Settings.get_default()
    if screen is None or settings is None or _provider is not None:
        return
    _provider = Gtk.CssProvider()
    Gtk.StyleContext.add_provider_for_screen(
        screen, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    for name in ("gtk-theme-name", "gtk-application-prefer-dark-theme"):
        settings.connect(f"notify::{name}", lambda *_a: _load())
    _load()


def _load():
    _provider.load_from_data(css(is_dark()).encode())
