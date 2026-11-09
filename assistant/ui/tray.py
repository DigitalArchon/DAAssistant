"""System tray icon with a backend cascade.

Mint 22.3 - and most current distros - ship only AyatanaAppIndicator3, so a hardcoded
`gi.require_version('AppIndicator3', '0.1')` would raise at startup and the app would
never run. Here the namespace is resolved at runtime and there are three
fallbacks behind it, including XApp.StatusIcon which is native to Cinnamon/MATE/XFCE and
works under Wayland.
"""

from .. import gtkcompat
from gi.repository import Gio, GLib, Gtk

from .. import icon as app_icon, session

# What shows StatusNotifierItem icons - an indicator's - on the session bus: Plasma, the
# GNOME Shell AppIndicator extension, Cinnamon's and XFCE's panels, waybar, quickshell.
WATCHER = "org.kde.StatusNotifierWatcher"

# Said when nothing will show the icon, and with it the menu, which is otherwise the way
# in: stock GNOME has no tray at all, and Ubuntu 26.04 ships its extension switched off.
NO_TRAY = ("DA Assistant is running, but nothing on this desktop shows tray icons, so "
           "its icon and menu cannot appear. On GNOME, turn on the AppIndicator "
           "extension in the Extensions app (on Fedora, install "
           "gnome-shell-extension-appindicator first); on Sway or another compositor, "
           "add a tray to its bar (swaybar's tray, waybar's tray module). Your "
           "shortcuts work meanwhile, and running DA Assistant with --action=settings "
           "opens Settings.")


def watcher_present():
    """Whether anything on the session bus shows indicator icons; None when the bus
    cannot be asked."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (WATCHER,)), GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        return None
    return bool(reply.unpack()[0])


def xembed_tray_present():
    """Whether an XEmbed system tray is on this X display: a client that owns the
    _NET_SYSTEM_TRAY_S<screen> selection, which is how the System Tray Protocol finds
    one. None when that cannot be asked - no python-xlib, no X display. Asked over a
    connection of its own, which opens no window and leaves nothing behind: an atom no
    client has named is not made by asking, and was never any tray's."""
    try:
        from Xlib import X, display, error
    except ImportError:
        return None
    try:
        connection = display.Display()
    except (error.DisplayError, error.ConnectionClosedError):
        return None
    try:
        name = f"_NET_SYSTEM_TRAY_S{connection.get_default_screen()}"
        atom = connection.intern_atom(name, only_if_exists=True)
        return atom != X.NONE and connection.get_selection_owner(atom) != X.NONE
    except (error.XError, error.ConnectionClosedError):
        return None
    finally:
        try:
            connection.close()
        except error.ConnectionClosedError:
            pass


class Tray:
    def __init__(self, app_id, title, menu, icon=None):
        # app_icon.name() also registers the bundled icons with the GTK icon theme,
        # which is what makes the name resolvable for every backend below.
        icon = icon or app_icon.name()
        self.menu = menu
        self.kind = None
        self._impl = None

        module, kind = gtkcompat.load_indicator()
        self.kind = kind

        if kind == "appindicator" and module is not None:
            # An indicator is drawn by the panel, in another process, which resolves the
            # icon name against its own icon theme - appending to ours here would be
            # invisible to it. set_icon_theme_path publishes the directory over D-Bus
            # (the StatusNotifierItem IconThemePath property) so the panel can find an
            # icon that is not installed system-wide, which is the normal case for a
            # checkout and the only case for an AppImage.
            category = module.IndicatorCategory.APPLICATION_STATUS
            with_path = getattr(module.Indicator, "new_with_path", None)
            if with_path is not None:
                self._impl = with_path(app_id, icon, category, app_icon.THEME_DIR)
            else:
                # Older libappindicator: same property, set after construction.
                self._impl = module.Indicator.new(app_id, icon, category)
                self._impl.set_icon_theme_path(app_icon.THEME_DIR)
            self._impl.set_status(module.IndicatorStatus.ACTIVE)
            self._impl.set_title(title)
            self._impl.set_menu(menu)
        elif kind == "xapp" and module is not None:
            self._impl = module.StatusIcon()
            self._impl.set_icon_name(icon)
            self._impl.set_tooltip_text(title)
            self._impl.set_secondary_menu(menu)
            self._impl.set_primary_menu(menu)
        else:
            # Deprecated since GTK 3.14 but still functional, and the only option left
            # if no indicator library is present at all.
            self._impl = Gtk.StatusIcon.new_from_icon_name(icon)
            self._impl.set_tooltip_text(title)
            self._impl.connect("popup-menu", self._on_popup)
            self._impl.connect("activate", self._on_activate)

    def unseen(self):
        """Whether nothing will show this icon. Only known for an indicator on Wayland:
        on X11 one with no watcher falls back to the X11 tray, and the other backends
        are that tray themselves."""
        return (self.kind == "appindicator" and session.session_type() == "wayland"
                and watcher_present() is False)

    def _on_popup(self, icon, button, activate_time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon,
                        button, activate_time)

    def _on_activate(self, icon):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, 0,
                        Gtk.get_current_event_time())
