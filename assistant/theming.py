"""What GTK was asked to look like, and whether it could.

An app inside an AppImage carries its own GTK but is styled by the host: the theme name
comes from the desktop, and the theme itself from the host's /usr/share/themes. When the
second half is missing, GTK says nothing and quietly draws its built-in Adwaita, so the
app looks like neither the desktop around it nor a bug. That is the whole reason this
module exists - to turn "it looks wrong" into a line naming the theme, where it was
looked for, and what to install.

Wayland matters here. On X11 the desktop's settings reach GTK through the XSettings
daemon whatever else is true; on Wayland there is no such thing, and GTK3 reads
~/.config/gtk-3.0/settings.ini instead - written on KDE by kde-gtk-config, which is not
always installed. So the same app can be themed correctly on X11 and not on Wayland, on
the same machine.
"""

import os

# Compiled into libgtk-3 as resources rather than shipped as directories, so "no
# directory" is not a problem for these.
BUILTIN_THEMES = {"Adwaita", "Adwaita-dark", "HighContrast", "HighContrastInverse",
                  "Default", "Emacs", "Raleigh"}


def _data_dirs():
    raw = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [d for d in raw.split(":") if d]


def theme_dirs():
    """Every directory GTK 3 looks in for a named theme, in its own order."""
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, ".themes")]
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local/share")
    dirs.append(os.path.join(data_home, "themes"))
    dirs += [os.path.join(d, "themes") for d in _data_dirs()]
    return dirs


def find_theme(name):
    """The theme's own CSS, or None. GTK 3.24 accepts any gtk-3.N up to its own."""
    if not name:
        return None
    for directory in theme_dirs():
        base = os.path.join(directory, name)
        if not os.path.isdir(base):
            continue
        for minor in range(24, -2, -2):     # gtk-3.24, gtk-3.22, ... then gtk-3.0
            css = os.path.join(base, f"gtk-3.{minor}" if minor >= 0 else "gtk-3.0",
                               "gtk.css")
            if os.path.exists(css):
                return css
    return None


def available_themes(limit=12):
    found = set()
    for directory in theme_dirs():
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            if find_theme(entry):
                found.add(entry)
    return sorted(found)[:limit]


def _icon_theme_found(name):
    """Icons come from the host too, and can go missing on their own."""
    if not name:
        return False
    for directory in _data_dirs() + [os.path.expanduser("~/.local/share"),
                                     os.path.expanduser("~")]:
        for candidate in (os.path.join(directory, "icons", name),
                          os.path.join(directory, ".icons", name)):
            if os.path.isdir(candidate):
                return True
    return False


def settings():
    """What GTK has been asked for, as far as it will tell us."""
    from gi.repository import Gtk

    gtk_settings = Gtk.Settings.get_default()
    if gtk_settings is None:
        return {}
    wanted = ("gtk-theme-name", "gtk-icon-theme-name", "gtk-font-name",
              "gtk-application-prefer-dark-theme")
    values = {}
    for key in wanted:
        try:
            values[key] = gtk_settings.get_property(key)
        except (TypeError, ValueError):
            continue
    return values


def backend():
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None:
        return "none (no display)"
    name = type(display).__name__.lower()
    if "wayland" in name:
        return "wayland"
    if "x11" in name:
        return "x11"
    return type(display).__name__


def problem():
    """One line when the desktop's theme is named but not installed, else "".

    Worth saying unprompted at startup: it is the difference between an app that looks
    like the desktop and one that does not, and the only visible symptom is that it
    looks wrong - GTK itself reports nothing at all.
    """
    values = settings()
    name = (os.environ.get("GTK_THEME") or values.get("gtk-theme-name") or "").split(":")[0]
    if not name or name in BUILTIN_THEMES or find_theme(name):
        return ""
    return (f"theme '{name}' is not installed here; GTK is drawing its built-in Adwaita "
            f"(on KDE, install breeze-gtk)")


def theme_line():
    """The one line that says what GTK was asked for and whether it has it.

    Printed at every start, not only when something is wrong: "it does not look right"
    is otherwise unanswerable without asking the user to run a second command, and the
    answer is one string GTK already knows.
    """
    values = settings()
    theme = os.environ.get("GTK_THEME") or values.get("gtk-theme-name") or ""
    forced = " (forced by GTK_THEME)" if os.environ.get("GTK_THEME") else ""
    name = theme.split(":")[0]
    css = find_theme(name)

    if not name:
        # Nothing was asked for, so nothing is missing: no desktop settings reached
        # this process at all, and GTK is drawing its own default.
        return "(unset) - no theme setting reached this process; GTK default"
    if css:
        where = f"found: {css}"
    elif name in BUILTIN_THEMES:
        where = "built into GTK"
    else:
        where = "NOT FOUND - GTK is drawing its built-in Adwaita instead"
    dark = " + dark variant requested" if values.get(
        "gtk-application-prefer-dark-theme") else ""
    return f"{theme}{forced} - {where}{dark}"


def summary():
    """Lines for --probe and the diagnostics window."""
    values = settings()
    theme = os.environ.get("GTK_THEME") or values.get("gtk-theme-name") or ""
    name = theme.split(":")[0]
    css = find_theme(name)

    icons = values.get("gtk-icon-theme-name")
    icon_line = icons or "(unset)"
    if icons:
        icon_line += " - found" if _icon_theme_found(icons) else " - NOT FOUND"

    lines = [f"GTK theme      : {theme_line()}",
             f"Icon theme     : {icon_line}",
             f"Font           : {values.get('gtk-font-name') or '(unset)'}",
             f"GDK backend    : {backend()}"]
    if name and not css and name not in BUILTIN_THEMES:
        lines.append(f"  -> install the GTK 3 build of {name} "
                     f"(on KDE: breeze-gtk), or set GTK_THEME to one of: "
                     f"{', '.join(available_themes(6)) or 'none found'}")
    return lines
