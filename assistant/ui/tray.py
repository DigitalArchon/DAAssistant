"""System tray icon with a backend cascade.

Mint 22.3 - and most current distros - ship only AyatanaAppIndicator3, so a hardcoded
`gi.require_version('AppIndicator3', '0.1')` would raise at startup and the app would
never run. Here the namespace is resolved at runtime and there are three
fallbacks behind it, including XApp.StatusIcon which is native to Cinnamon/MATE/XFCE and
works under Wayland.
"""

from .. import gtkcompat
from gi.repository import Gtk

from .. import icon as app_icon


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

    def _on_popup(self, icon, button, activate_time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon,
                        button, activate_time)

    def _on_activate(self, icon):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, 0,
                        Gtk.get_current_event_time())
