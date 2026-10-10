"""Shortcuts kept by the desktop itself, for sessions where the app cannot listen for keys.

A Wayland client cannot grab global keys. Where the GlobalShortcuts portal is missing,
or does not bind keys by itself (Hyprland), the shortcuts are written into the desktop's
own keybindings instead, each running `run-assistant --action=NAME`, which hands the
action to the running instance over D-Bus (ipc.py). This replaces
tools/install-hotkeys.sh, and writes what Settings > Shortcuts says rather than a fixed
set of keys:

  GNOME, Cinnamon   custom keybindings, through the host's gsettings
  Hyprland, Sway    a marked block in the compositor's config file, a backup kept

Every entry is ours by name - da-assistant-<action>, or the block between the marker
lines - and nothing else is touched. Cinnamon and GNOME each keep one list naming every
custom shortcut, which is the user's list: ours are added to it and taken out of it,
and the rest stays as it was. The names and markers are the ones the old script used,
so the bindings it installed are taken over, not duplicated.

KDE Plasma 6 has the portal. What the old script wrote there - a .desktop file per
action and its kglobalshortcutsrc entry - would fire beside the portal's, so it is
removed when found (remove_kde_legacy).
"""

import ast
import glob
import os
import shlex
import shutil
import subprocess

from . import hostenv, shortcuts

PREFIX = "da-assistant"
NAME = "DA Assistant"
BEGIN = "# --- DA Assistant hotkeys"
END = "# --- end DA Assistant hotkeys ---"


def run_host(args):
    """Run a host program - gsettings, kwriteconfig6, hyprctl - as the desktop would:
    with the environment the app was started with, not the AppImage's. (returncode,
    stdout); 127 when it is not installed."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=15,
                              env=hostenv.host_env())
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return done.returncode, done.stdout


def launcher():
    """What a binding runs. It outlives this process, so it must name something that
    does too: the .AppImage file itself, not the mount the app is running from."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return appimage
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "run-assistant")


def command(action):
    return f"{shlex.quote(launcher())} --action={action}"


# ---- GNOME and Cinnamon ------------------------------------------------------------

class GSettingsKeybindings:
    """Custom keybindings in GNOME or Cinnamon, through the host's gsettings."""

    SCHEMAS = {
        # list schema, list key, item schema, item base path, list holds paths, binding
        # is a list
        "gnome": ("org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings",
                  "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
                  "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings",
                  True, False),
        # Cinnamon builds base/<entry>/ itself, so its list holds names: a full path
        # there pointed at nothing and the keys never fired. And it stores the binding
        # as a list of strings, where GNOME stores one string.
        "cinnamon": ("org.cinnamon.desktop.keybindings", "custom-list",
                     "org.cinnamon.desktop.keybindings.custom-keybinding",
                     "/org/cinnamon/desktop/keybindings/custom-keybindings",
                     False, True),
    }

    def __init__(self, desktop, run=run_host):
        self.desktop = desktop
        (self.list_schema, self.list_key, self.item_schema, self.base, self.paths,
         self.binding_list) = self.SCHEMAS[desktop]
        self.run = run

    def describe(self):
        return (f"{'GNOME' if self.desktop == 'gnome' else 'Cinnamon'}'s custom "
                f"keyboard shortcuts")

    def _entry(self, action):
        name = f"{PREFIX}-{action}"
        return f"{self.base}/{name}/" if self.paths else name

    @staticmethod
    def _ours(entry):
        return f"{PREFIX}-" in entry

    def _current(self):
        code, out = self.run(["gsettings", "get", self.list_schema, self.list_key])
        if code == 127:
            raise OSError("gsettings is not installed")
        text = out.strip()
        if text.startswith("@as "):
            text = text[4:]
        try:
            listed = ast.literal_eval(text) if text else []
        except (ValueError, SyntaxError):
            raise OSError(f"could not read {self.list_key}: {text[:80]!r}")
        return [str(entry) for entry in listed]

    @staticmethod
    def _gvariant(strings):
        quoted = (s.replace("\\", "\\\\").replace("'", "\\'") for s in strings)
        return "[" + ", ".join(f"'{s}'" for s in quoted) + "]"

    def _set(self, *args):
        code, out = self.run(["gsettings", "set", *args])
        if code != 0:
            raise OSError(f"gsettings set {args[0]} failed {out.strip()}".strip())

    def apply(self, bindings, labels, actions):
        """Make ours exactly `bindings` ({action: accel}). `actions` is every action
        there is, so that one no longer bound is taken out too."""
        current = self._current()
        for action in actions:
            path = f"{self.item_schema}:{self.base}/{PREFIX}-{action}/"
            accel = bindings.get(action)
            if accel:
                self._set(path, "name", f"{NAME}: {labels[action]}")
                self._set(path, "command", command(action))
                self._set(path, "binding", self._gvariant([accel]) if self.binding_list
                          else accel)
            elif self._entry(action) in current:
                self.run(["gsettings", "reset-recursively", path])
        wanted = [e for e in current if not self._ours(e)] + [
            self._entry(action) for action in actions if bindings.get(action)]
        if wanted != current:
            self._set(self.list_schema, self.list_key, self._gvariant(wanted))

    def remove(self, actions):
        """Take ours out, leaving the user's own. Nothing is written if none is there."""
        if any(self._ours(e) for e in self._current()):
            self.apply({}, {}, actions)


# ---- Hyprland and Sway -------------------------------------------------------------

class ConfigFileKeybindings:
    """A block of binds in Hyprland's or Sway's config file, between marker lines."""

    def __init__(self, desktop, run=run_host, path=None):
        self.desktop = desktop
        self.run = run
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        self.path = path or (os.path.join(config, "hypr", "hyprland.conf")
                             if desktop == "hyprland"
                             else os.path.join(config, "sway", "config"))

    def describe(self):
        return self.path.replace(os.path.expanduser("~"), "~", 1)

    def block(self, bindings, labels, actions):
        """The lines for `bindings`, markers included; "" when there are none."""
        lines = []
        for action in actions:
            accel = bindings.get(action)
            if not accel:
                continue
            if self.desktop == "hyprland":
                mods, key = shortcuts.hyprland(accel)
                lines.append(f"bind = {mods}, {key}, exec, {command(action)}"
                             f"  # {labels[action]}")
            else:
                lines.append(f"bindsym {shortcuts.sway(accel)} exec {command(action)}"
                             f"  # {labels[action]}")
        if not lines:
            return ""
        return "\n".join([f"{BEGIN} (managed by {NAME}: Settings > Shortcuts) ---",
                          *lines, END])

    @staticmethod
    def strip(text):
        """`text` without our block - or blocks, should an old script have left two."""
        out, inside = [], False
        for line in text.splitlines(keepends=True):
            if line.startswith(BEGIN):
                inside = True
            elif inside and line.rstrip("\n") == END:
                inside = False
            elif not inside:
                out.append(line)
        return "".join(out).rstrip("\n") + ("\n" if out else "")

    def apply(self, bindings, labels, actions):
        if not os.path.exists(self.path):
            if not any(bindings.values()):
                return
            raise OSError(f"{self.describe()} does not exist; add the lines from "
                          f"Settings > Shortcuts to your config yourself")
        with open(self.path, encoding="utf-8") as f:
            before = f.read()
        block = self.block(bindings, labels, actions)
        after = self.strip(before)
        if block:
            after = after + ("\n" if after else "") + block + "\n"
        if after == before:
            return
        shutil.copy2(self.path, self.path + ".da-assistant.bak")
        tmp = self.path + ".da-assistant.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(after)
        os.chmod(tmp, os.stat(self.path).st_mode & 0o7777)
        os.replace(tmp, self.path)
        if self.desktop == "hyprland":
            self.run(["hyprctl", "reload"])
        else:
            self.run(["swaymsg", "reload"])

    def remove(self, actions):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                if BEGIN not in f.read():
                    return
            self.apply({}, {}, actions)


def writer(desktop, run=run_host):
    """The desktop's own keybindings for `desktop`, or None where there is no way to
    write them and the user binds the commands by hand."""
    if desktop in GSettingsKeybindings.SCHEMAS:
        return GSettingsKeybindings(desktop, run)
    if desktop in ("hyprland", "sway"):
        return ConfigFileKeybindings(desktop, run)
    return None


# ---- what the old script left on KDE -----------------------------------------------

def _kde_tool(name):
    return f"{name}6" if shutil.which(f"{name}6") else f"{name}5"


def remove_kde_legacy(run=run_host, data_home=None):
    """Remove the .desktop launchers and kglobalshortcutsrc entries install-hotkeys.sh
    wrote on KDE. True if there were any."""
    data_home = data_home or os.environ.get("XDG_DATA_HOME") or \
        os.path.expanduser("~/.local/share")
    found = sorted(glob.glob(os.path.join(data_home, "applications",
                                          f"{PREFIX}-*.desktop")))
    if not found:
        return False
    kwrite = _kde_tool("kwriteconfig")
    for path in found:
        os.unlink(path)
        run([kwrite, "--file", "kglobalshortcutsrc", "--group", "services",
             "--group", os.path.basename(path), "--key", "_launch", "--delete"])
    run([_kde_tool("kbuildsycoca"), "--noincremental"])
    return True
