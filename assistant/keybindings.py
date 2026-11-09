"""Shortcuts kept by the desktop itself, for sessions where the app cannot listen for keys.

A Wayland client cannot grab global keys. Where the GlobalShortcuts portal is missing,
or does not bind keys by itself (Hyprland), the shortcuts are written into the desktop's
own keybindings instead, each running `run-assistant --action=NAME`, which hands the
action to the running instance over D-Bus (ipc.py). This replaces
tools/install-hotkeys.sh, and writes what Settings > Shortcuts says rather than a fixed
set of keys:

  GNOME, Cinnamon   custom keybindings, through the host's gsettings
  Hyprland, Sway    a marked block in the compositor's config file, a backup kept

Every entry is ours by name - da-assistant-<action>, on Cinnamon custom-<number> (see
GSettingsKeybindings), or the block between the marker lines - and nothing else is
touched. Cinnamon and GNOME each keep one list naming every custom shortcut, which is
the user's list: ours are added to it and taken out of it, and the rest stays as it
was. The names and markers are the ones the old script used, or on Cinnamon followed
from them, so the bindings it installed are taken over, not duplicated.

Nor is a key taken from what has it already. One the desktop or the user's own config
binds would fire both - and on Sway ours, coming last, silently replaced the user's own
bindsym - so each writer's clashes() says which keys are bound there already, and to
what: hotkeys.HotkeyManager writes the rest only, and says so of those, as the X11
grabs say so of a key another program holds.

KDE Plasma 6 has the portal. What the old script wrote there - a .desktop file per
action and its kglobalshortcutsrc entry - would fire beside the portal's, so it is
removed when found (remove_kde_legacy).
"""

import ast
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import unicodedata
import zlib

from gi.repository import GLib

from . import hostenv, shortcuts

PREFIX = "da-assistant"
NAME = "DA Assistant"
BEGIN = "# --- DA Assistant hotkeys"
END = "# --- end DA Assistant hotkeys ---"
# The same markers in Hyprland's Lua configuration (0.55 and later), where -- comments.
LUA_BEGIN = "-- --- DA Assistant hotkeys"
LUA_END = "-- --- end DA Assistant hotkeys ---"

# A line of a compositor's config, with the "\n" that ends it: see
# ConfigFileKeybindings.strip.
_LINE = re.compile(r"[^\n]*\n|[^\n]+")


def run_host(args):
    """Run a host program - gsettings, kwriteconfig6, hyprctl - as the desktop would:
    with the environment the app was started with, not the AppImage's. (returncode,
    output): what it printed, or when it failed, what it said was wrong - gsettings
    says that on stderr and nothing on stdout, so stdout alone made every refusal a
    bare "failed". 127 when it is not installed."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=15,
                              env=hostenv.host_env())
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    if done.returncode != 0:
        return done.returncode, done.stderr or done.stdout
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


# The Desktop Entry spec's reserved characters: an argument holding one is quoted.
_EXEC_RESERVED = ' \t\n"\'\\><~|&;$*?#()`'


def _desktop_exec(path):
    r"""`path` as one argument of a .desktop Exec line (Desktop Entry spec, "Exec key").

    Two layers, which whatever reads the entry takes off again in the opposite order.
    An argument holding a reserved character goes in double quotes, with `"`, `` ` ``,
    `$` and `\` escaped by a backslash. And Exec is a string like any other value in
    the file, whose own escapes apply on top: every backslash is doubled, so a literal
    `$` reaches the file as `\\$` and a literal backslash as `\\\\`, and a tab or a
    newline is written `\t` or `\n`. A single backslash before `$`, `"` or a backtick,
    as written before, is an escape the key file format does not have, and GLib
    refused the whole value.

    A `%` is left as it is, though the spec would have it written `%%`. GLib will not
    load an entry whose program cannot be found, and looks for it in Exec as written,
    with no field code expanded: a path written with `%%` names a file that is not
    there, and the portal knows an app only through GLib's loading of its entry (KDE's
    says "App info not found" without one). Nothing ever launches this entry, so no
    field code in it is ever expanded.
    """
    if any(c in path for c in _EXEC_RESERVED):
        path = '"' + "".join("\\" + c if c in '"`$\\' else c for c in path) + '"'
    return (path.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
            .replace("\r", "\\r"))


def app_entry(data_home=None):
    """Install the hidden .desktop entry the GlobalShortcuts portal needs to accept the
    app's id (see hotkeys.PortalHotkeys); its path. Rewritten only when it would change
    - an AppImage moved elsewhere - and it also gives the desktop's shortcut settings a
    name to show the shortcuts under."""
    from . import APP_ID, APP_NAME
    data_home = data_home or os.environ.get("XDG_DATA_HOME") or \
        os.path.expanduser("~/.local/share")
    folder = os.path.join(data_home, "applications")
    path = os.path.join(folder, f"{APP_ID}.desktop")
    text = ("[Desktop Entry]\n"
            f"# Written by {APP_NAME}, so that the desktop's portal knows it by its app\n"
            "# id and can bind its shortcuts. Hidden from menus.\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Exec={_desktop_exec(launcher())}\n"
            "Icon=da-assistant\n"
            "Terminal=false\n"
            "NoDisplay=true\n")
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return path
    except OSError:
        pass
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


# ---- GNOME and Cinnamon ------------------------------------------------------------

class GSettingsKeybindings:
    """Custom keybindings in GNOME or Cinnamon, through the host's gsettings.

    Cinnamon takes a change in only one way (js/ui/keybindings.js, Cinnamon 6.6). It
    reads the shortcuts again when custom-list changes, and only then; on that rebuild
    it lets go only of the shortcuts whose names have "custom" in them; and a shortcut
    whose keys are the same as before keeps the command it was bound with. Ours were
    named da-assistant-<action> and the list written only when which were in it
    changed, so a key changed, a key cleared and an AppImage moved all reached Cinnamon
    only at the next login. Cinnamon's own settings make every change reach it by
    putting a "__dummy__" entry into the list, or taking it out; so does apply, and
    ours are named custom-<number>. Its settings read every name in the list as
    "custom" and a number, and failed on any other: its Add custom shortcut added
    nothing while one of the old names was in the list. Theirs are custom0, custom1 and
    on; ours have a dash, which reads as a minus, so neither is ever the other's.
    """

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

    # What Cinnamon's settings put into its list to change it, and take out again.
    DUMMY = "__dummy__"

    def __init__(self, desktop, run=run_host):
        self.desktop = desktop
        (self.list_schema, self.list_key, self.item_schema, self.base, self.paths,
         self.binding_list) = self.SCHEMAS[desktop]
        self.run = run
        # Cinnamon names from before kept in its list for one more rebuild: see apply.
        self.unfinished = False

    def describe(self):
        return (f"{'GNOME' if self.desktop == 'gnome' else 'Cinnamon'}'s custom "
                f"keyboard shortcuts")

    def _entry(self, action):
        """`action`'s entry in the list: GNOME's a path, Cinnamon's a name. Cinnamon's
        number is the CRC-32 of da-assistant-<action>, the name GNOME's path ends in, so
        an action keeps its entry whatever actions there are."""
        name = f"{PREFIX}-{action}"
        if self.desktop == "cinnamon":
            return f"custom-{zlib.crc32(name.encode())}"
        return f"{self.base}/{name}/" if self.paths else name

    def _item(self, entry):
        """gsettings' PATH for an entry of the list."""
        return f"{self.item_schema}:{entry if self.paths else f'{self.base}/{entry}/'}"

    def _ours(self, entry):
        return f"{PREFIX}-" in entry or (self.desktop == "cinnamon"
                                         and re.fullmatch(r"custom-\d+", entry) is not None)

    def _legacy(self, entry):
        """A Cinnamon entry under the name used before 4.0, which no rebuild lets go of."""
        return self.desktop == "cinnamon" and entry.startswith(f"{PREFIX}-")

    def _command(self, action):
        """What `action`'s shortcut runs. GNOME's media keys launch it through
        g_app_info_create_from_commandline, which makes it the Exec of a desktop entry,
        whose field codes are expanded on launch: in the launcher's path 50%off became
        50ff, pct%u became pct and 100%% became 100%. So GNOME is given each % doubled,
        which that makes one % again. Cinnamon splits the command as the shell would
        (GLib.shell_parse_argv), with no field codes, and is given it as it is."""
        run = command(action)
        return run.replace("%", "%%") if self.desktop == "gnome" else run

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

    @staticmethod
    def _string(value):
        """`value` as GVariant text, which is how gsettings reads every value it sets.

        It takes the text as a bare string only when parsing it fails and it does not
        begin with a quote. A command begins with one whenever the launcher's path holds
        a space, an apostrophe or anything outside ASCII, which shlex quotes - so
        "'/home/u/My Apps/DA.AppImage' --action=ocr_only" was refused ("expected end of
        input"), and no shortcut could be saved at all from an AppImage kept in ~/My Apps
        or ~/Téléchargements."""
        return GLib.Variant("s", value).print_(False)

    def _set(self, *args):
        """gsettings set PATH KEY VALUE, VALUE already GVariant text (_string, _gvariant)."""
        code, out = self.run(["gsettings", "set", *args])
        if code != 0:
            # What gsettings said, less the caret it draws under a parse error.
            said = " ".join(line.strip() for line in out.splitlines()
                            if line.strip(" \t^~"))
            raise OSError(f"gsettings set {args[0]} {args[1]} failed"
                          + (f": {said}" if said else ""))

    def apply(self, bindings, labels, actions, settled=False):
        """Make ours exactly `bindings` ({action: accel}). `actions` is every action
        there is, so that one no longer bound is taken out too.

        An entry Cinnamon holds under a name from before 4.0 it lets go of only when a
        rebuild finds it in the list with its keys gone. So it is emptied and stays
        listed, ahead of ours - Cinnamon would not bind a key the old name still held -
        and `unfinished` is set: it leaves the list only on a later call made with
        `settled`, the rebuild long done by then. Taken out at once, as before, the old
        shortcut stayed bound, and fired its old command, until the next login."""
        current = self._current()
        ours = [entry for entry in current if self._ours(entry)]
        written = [self._entry(action) for action in actions if bindings.get(action)]
        for action in actions:
            accel = bindings.get(action)
            if accel:
                path = self._item(self._entry(action))
                self._set(path, "name", self._string(f"{NAME}: {labels[action]}"))
                self._set(path, "command", self._string(self._command(action)))
                self._set(path, "binding", self._gvariant([accel]) if self.binding_list
                          else self._string(accel))
        for entry in ours:
            if entry not in written:
                self.run(["gsettings", "reset-recursively", self._item(entry)])
        kept = [] if settled else [entry for entry in ours if self._legacy(entry)]
        self.unfinished = bool(kept)
        wanted = [entry for entry in current if not self._ours(entry)] + kept + written
        if self.desktop == "cinnamon" and (ours or written):
            if self.DUMMY in wanted:
                wanted.remove(self.DUMMY)
            else:
                wanted.append(self.DUMMY)
        if wanted != current:
            self._set(self.list_schema, self.list_key, self._gvariant(wanted))

    def remove(self, actions, settled=False):
        """Take ours out, leaving the user's own. Nothing is written if none is there."""
        if any(self._ours(e) for e in self._current()):
            self.apply({}, {}, actions, settled)

    # The desktop's own keybindings: its window manager's, its shell's, its media keys.
    BUILT_IN = {
        "gnome": ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
                  "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings",
                  "org.gnome.settings-daemon.plugins.media-keys"),
        "cinnamon": ("org.cinnamon.desktop.keybindings",
                     "org.cinnamon.desktop.keybindings.wm",
                     "org.cinnamon.desktop.keybindings.media-keys",
                     "org.cinnamon.muffin.keybindings",
                     "org.cinnamon.muffin.wayland.keybindings"),
    }

    def _listed(self, schema):
        """[(key, value)] of `schema` - a schema, or SCHEMA:PATH - each value unpacked
        from gsettings' GVariant text; [] when gsettings cannot list it, as for a schema
        this version of the desktop does not have."""
        code, out = self.run(["gsettings", "list-recursively", schema])
        if code != 0:
            return []
        found = []
        for line in out.splitlines():
            fields = line.split(" ", 2)
            if len(fields) != 3:
                continue
            try:
                found.append((fields[1], GLib.Variant.parse(None, fields[2], None,
                                                            None).unpack()))
            except GLib.Error:
                continue
        return found

    @staticmethod
    def _keys(value):
        """The key combinations in a keybinding's value - one string, or a list of them -
        in the config's spelling. Anything else a value holds, a path or a name, is no
        usable combination and reads as none."""
        values = [value] if isinstance(value, str) else \
            value if isinstance(value, list) else []
        return {shortcuts.canonical(item) for item in values
                if isinstance(item, str)} - {None}

    def clashes(self, bindings):
        """{action: what has its key already} for the keys in `bindings` ({action:
        accel}) that the desktop binds itself, or that a custom shortcut of the user's
        has. Only read; a gsettings that cannot be asked finds none."""
        wanted = {accel: action for action, accel in bindings.items()}
        found = {}
        who = "GNOME" if self.desktop == "gnome" else "Cinnamon"

        def note(keys, what):
            for key in keys:
                action = wanted.get(key)
                if action and action not in found:
                    found[action] = what

        for schema in self.BUILT_IN[self.desktop]:
            for key, value in self._listed(schema):
                note(self._keys(value), f"already {who}'s shortcut for “{key}”")
        try:
            current = self._current()
        except OSError:
            current = []
        for entry in current:
            if self._ours(entry) or entry == self.DUMMY:
                continue
            item = dict(self._listed(self._item(entry)))
            name = _comment(str(item.get("name") or "")) or entry
            note(self._keys(item.get("binding")), f"already your shortcut “{name}”")
        return found


# ---- Hyprland and Sway -------------------------------------------------------------

def _lua(text):
    r"""`text` as a Lua string literal, in Lua's own escapes: a backslash and a quote
    each behind a backslash, a line break as \n, and any other control character, and
    the line and paragraph separators U+2028 and U+2029, as the decimal escapes of its
    UTF-8 bytes - the same string to Lua. Written as they were, U+0085, U+2028 and
    U+2029 were characters like any other to Lua and to Hyprland, but line breaks to
    the str.splitlines that read the file back: see ConfigFileKeybindings.strip."""
    out = []
    for char in text:
        if char in '\\"':
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif unicodedata.category(char) in ("Cc", "Zl", "Zp"):
            out.append("".join(f"\\{byte:03d}" for byte in char.encode("utf-8")))
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _comment(text):
    """`text` - an action's name, as typed or pasted into Settings > Actions - as the
    rest of a comment line: on one line, every line break and control character a
    space. A name pasted with a line break in it put its second line into the config
    as a line of its own, where `exec = notify-send injected` was a command Hyprland
    ran at every reload. A backslash at its end goes too, for any reader that takes a
    line ending in one to go on to the next."""
    flat = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text)
    return " ".join(flat.split()).rstrip("\\").rstrip()


def _config_command(action, where):
    """command(action) as it goes into a line of Hyprland's hyprland.conf or Sway's
    config (`where`, for the error), each of which reads its lines before any shell
    does. The launcher's path is always in single quotes, written so that neither
    reader finds anything of its own in it:

      ;  ,   Sway splits a key's command into several at either, wherever it is not in
             quotes - and shlex leaves a path whose only odd character is a comma bare,
             so /home/u/Apps,old/DA.AppImage ran /home/u/Apps.
      $      Both put the value of a variable of the user's config, $mod or $mainMod,
             in place of its name, quotes or none. Each $ is followed by the end of the
             quoted part, which begins again after it: '$''mod', which no variable's
             name can match, and which the shell reads as $mod.

    Nothing can stand for a line break or another control character in a line of
    either file. Nor for a backslash: Sway reads one as an escape before the shell is
    reached, and Hyprland's config language, which in some versions escapes a $ with
    one, may too. A path with any of these is refused, and nothing is written."""
    path = launcher()
    control = any(unicodedata.category(c) == "Cc" for c in path)
    if control or "\\" in path:
        what = "a line break or another control character" if control else "a backslash"
        raise OSError(f"{where} cannot hold a line that runs {path!r}: its path has "
                      f"{what} in it. Nothing was changed; move DA Assistant somewhere "
                      f"whose path has none")
    quoted = "'" + path.replace("'", "'\"'\"'").replace("$", "$''") + "'"
    return f"{quoted} --action={action}"


class ConfigFileKeybindings:
    """A block of binds in Hyprland's or Sway's config file, between marker lines.

    Hyprland's configuration is Lua from 0.55: hyprland.lua, and the old hyprland.conf
    is not read at all once there is one (measured on 0.56). A new install has only
    hyprland.lua, so that is the file written where it exists, in its own syntax -
    hl.bind(keys, hl.dsp.exec_cmd(command), options) - and hyprland.conf otherwise."""

    def __init__(self, desktop, run=run_host, path=None):
        self.desktop = desktop
        self.run = run
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        if path is None:
            if desktop == "hyprland":
                lua = os.path.join(config, "hypr", "hyprland.lua")
                path = lua if os.path.exists(lua) else os.path.join(config, "hypr",
                                                                    "hyprland.conf")
            else:
                path = os.path.join(config, "sway", "config")
        self.path = path
        self.lua = desktop == "hyprland" and path.endswith(".lua")
        self.begin, self.end = (LUA_BEGIN, LUA_END) if self.lua else (BEGIN, END)

    def describe(self):
        return self.path.replace(os.path.expanduser("~"), "~", 1)

    def block(self, bindings, labels, actions):
        """The lines for `bindings`, markers included; "" when there are none.

        Hyprland's config language starts a comment at any # that is not doubled, inside
        shell quotes or not, so a # in the launcher's path is written ## - or the bind
        stopped there, and ran a command cut off in the middle of the path. Sway takes
        only a whole line that begins with # for a comment: the rest of a bindsym line
        is the command, and an action's name after it was run as part of it - a name
        with a ; in it ran what followed as a command of Sway's own, exit among them. So
        Sway is given the name on a comment line of its own, before the bind. See
        _comment and _config_command for the rest."""
        lines = []
        for action in actions:
            accel = bindings.get(action)
            if not accel:
                continue
            if self.lua:
                mods, key = shortcuts.hyprland(accel)
                keys = " + ".join(mods.split() + [key])
                lines.append(f"hl.bind({_lua(keys)}, hl.dsp.exec_cmd({_lua(command(action))}), "
                             f"{{ description = {_lua(f'{NAME}: {labels[action]}')} }})")
            elif self.desktop == "hyprland":
                mods, key = shortcuts.hyprland(accel)
                run = _config_command(action, self.describe()).replace("#", "##")
                lines.append(f"bind = {mods}, {key}, exec, {run}"
                             f"  # {_comment(labels[action])}")
            else:
                # "DA Assistant: " first, so that no name makes the line a marker.
                lines.append(f"# {NAME}: {_comment(labels[action])}")
                lines.append(f"bindsym {shortcuts.sway(accel)} exec "
                             f"{_config_command(action, self.describe())}")
        if not lines:
            return ""
        return "\n".join([f"{self.begin} (managed by {NAME}: Settings > Shortcuts) ---",
                          *lines, self.end])

    @staticmethod
    def strip(text, where="the config", begin=BEGIN, end=END):
        r"""`text` without our block - or blocks, should an old script have left two.

        Only a whole block is taken out: a BEGIN line and the END line after it, END
        known whatever space an editor has left around it. A BEGIN with no END after it
        raises OSError. Where that block stops is anybody's guess, and taking everything
        after BEGIN, as this once did, took the user's own lines with it - after which
        the next save backed the damaged file up over the only good copy.

        Text with no block in it comes back as it was, to the byte. The blank lines at
        its end were once trimmed whatever it held, so that a first start with no
        shortcuts chosen rewrote any config that did not end in exactly one newline -
        its backup overwritten, the compositor reloaded - with nothing of the app's in
        it before or after.

        A line ends at a "\n" and nowhere else, as Hyprland's config language and Sway
        end one, and Lua too, but for a "\r" on its own, which nothing the app writes
        holds unescaped. str.splitlines also ends one at U+0085, U+2028, U+2029 and a
        few control characters, which to the compositor are characters like any other:
        an end marker after one, in an action's name in hyprland.lua or the launcher's
        path in Sway's config, ended the block there at the next rewrite, and the rest
        of that line was kept as one of the user's own, for the compositor to run at
        every reload. A begin marker so hidden made the block one that never ended.
        """
        out, begun, found = [], None, False
        for number, line in enumerate(_LINE.findall(text), 1):
            if line.lstrip().startswith(begin):
                if begun is not None:
                    break                   # a second BEGIN before the first one's END
                begun, found = number, True
            elif begun is None:
                out.append(line)
            elif line.strip() == end:
                begun = None
        if begun is not None:
            raise OSError(f"{where}: the {NAME} block that begins on line {begun} has no "
                          f'"{end}" line to end it, so nothing was changed. Put that '
                          f"line back where the block ends, or delete the block")
        if not found:
            return text
        return "".join(out).rstrip("\n") + ("\n" if out else "")

    def _read(self):
        """The file's text, every byte of it. A config need not be UTF-8 throughout - a
        comment written in Latin-1 years ago - and such bytes are carried through to the
        file written back unchanged (surrogateescape). Read strictly, one raised
        UnicodeDecodeError, which nothing expected, and the app stopped at every start."""
        with open(self.path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    def _write(self, text):
        """Make `text` the file's contents, keeping what it held as the backup.

        Through a symbolic link, not over it: a config kept in a dotfiles repository -
        GNU Stow, chezmoi, home-manager - is a link to the real file, and replacing the
        link with a file of its own cut the config off from the repository. So the new
        copy is written beside the real file, and the rename that swaps it in stays in
        that one directory; the backup is kept beside the link, where it always was. A
        file that cannot be written - home-manager's are in the read-only Nix store -
        is said to be so, and nothing is written at all.
        """
        real = os.path.realpath(self.path)
        where = self.describe()
        if os.path.islink(self.path):
            where += f", a link to {real},"
        if not os.access(real, os.W_OK):
            raise OSError(f"{where} cannot be written; nothing was changed")
        tmp = real + ".da-assistant.tmp"
        try:
            try:
                with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
                    f.write(text)
                os.chmod(tmp, os.stat(real).st_mode & 0o7777)
            except OSError as e:
                raise OSError(f"{where} cannot be written ({e.strerror or e}); nothing "
                              f"was changed") from None
            shutil.copy2(self.path, self.path + ".da-assistant.bak")
            os.replace(tmp, real)
        finally:
            try:
                os.unlink(tmp)              # left over only if something failed
            except OSError:
                pass

    def apply(self, bindings, labels, actions, settled=False):
        """Make the block exactly `bindings`, and reload the compositor if that changed
        the file. `settled` is for GSettingsKeybindings, and means nothing here: a
        reload reads the file as it is."""
        written = self._rewrite(bindings, labels, actions)
        if self.lua:
            written = self._leave_conf(actions) or written
        if not written:
            return
        if self.desktop == "hyprland":
            self.run(["hyprctl", "reload"])
        else:
            self.run(["swaymsg", "reload"])

    def _rewrite(self, bindings, labels, actions):
        """Put the block for `bindings` into the file, in place of the one there. True
        if the file was written; nothing is written when nothing would change."""
        if not os.path.exists(self.path):
            if not any(bindings.values()):
                return False
            raise OSError(f"{self.describe()} does not exist; add the lines from "
                          f"Settings > Shortcuts to your config yourself")
        before = self._read()
        block = self.block(bindings, labels, actions)
        after = self.strip(before, self.describe(), self.begin, self.end)
        if block:
            body = after.rstrip("\n")
            after = (body + "\n\n" if body else "") + block + "\n"
        if after == before:
            return False
        self._write(after)
        return True

    def _leave_conf(self, actions):
        """Take the block out of hyprland.conf, beside the hyprland.lua being written:
        the one written there before the user moved to Lua, which would come back
        into force, keys and launcher as they were then, with the hyprland.lua gone -
        or at once, from a Lua config that loads the old file. True if it was there.
        Not being able to is said and passed over: the keys are in hyprland.lua."""
        conf = ConfigFileKeybindings(self.desktop, self.run,
                                     os.path.join(os.path.dirname(self.path),
                                                  "hyprland.conf"))
        try:
            return conf._rewrite({}, {}, actions)
        except OSError as e:
            print(f"[hotkeys] the shortcuts in {conf.describe()} were left there: {e}")
            return False

    def remove(self, actions, settled=False):
        """Take the block out, and from beside a hyprland.lua the hyprland.conf one;
        nothing is written where there is none."""
        self.apply({}, {}, actions, settled)

    def clashes(self, bindings):
        """{action: where its key is bound already} for the keys in `bindings` that the
        user's own config binds: the file outside our block, and the files it brings in
        - Hyprland's `source`, Sway's `include`, the modules hyprland.lua requires from
        the config directory. Only read; a file that cannot be read binds nothing.

        Hyprland is asked too, for the binds in force (_hyprland_live), and where it
        answers, that is what decides: a Lua config is a program, and what it binds
        cannot all be read off the page. Omarchy's, measured on 0.56, binds every key
        through a helper of its own (o.bind), from modules found through a search path
        it sets, loaded by a dofile whose path is built at run time - the files gave
        none of its 225 binds, and a key it held was written and fired both. The files
        still name the line, where they find it."""
        if not bindings or not os.path.exists(self.path):
            return {}
        own = self._outside_block(self._read())
        if self.lua:
            bound = _lua_binds(self.path, own)
        elif self.desktop == "hyprland":
            bound = _hyprland_binds(self.path, own)
        else:
            bound = _sway_binds(self.path, own)
        live = _hyprland_live(self.run) if self.desktop == "hyprland" else None
        found = {}
        for action, accel in bindings.items():
            mods, key = shortcuts.parts(accel)
            combination = (frozenset(mods), key.lower())
            where = bound.get(combination)
            if live is not None and combination not in live:
                continue                    # in force it is not: the files misread
            if where:
                path, number, line = where
                line = " ".join(line.split())
                if len(line) > 70:
                    line = line[:69] + "…"
                found[action] = (f"already bound on line {number} of "
                                 f"{path.replace(os.path.expanduser('~'), '~', 1)}: {line}")
            elif live is not None:
                found[action] = f"already bound in Hyprland: {live[combination]}"
        return found

    def _outside_block(self, text):
        """`text` with the lines of our block - or blocks - left blank, the rest keeping
        their numbers, which is how a clash names them. Unlike strip, a block with no end
        is not refused here: apply refuses it, and says so."""
        out, inside = [], False
        for line in text.split("\n"):
            if not inside and line.lstrip().startswith(self.begin):
                inside = True
            if inside:
                out.append("")
                inside = line.strip() != self.end
            else:
                out.append(line)
        return "\n".join(out)


# ---- what the user's own Hyprland or Sway config binds -----------------------------
#
# Read as the compositor reads it, as far as a key goes: its variables put in, the
# files it brings in read too, and an unbind taking a bind back out. Each reader finds
# {(modifiers, key's name in lower case): (file, line number, line)} for every key
# bound, at its first bind still in force; the name is compared in lower case, as both
# compositors look a key up. A modifier the app never binds with - Caps Lock, Mod2,
# Mod3, Mod5 - makes a combination that is none of the app's ("other").

_INCLUDE_DEPTH = 8          # files brought in by files brought in, at most


def _config_lines(path, text=None):
    """[(number, line)] of the config at `path` - of `text`, when that has been read
    already - or [] when it cannot be read."""
    if text is None:
        try:
            with open(path, encoding="utf-8", errors="surrogateescape") as f:
                text = f.read()
        except OSError:
            return []
    return list(enumerate(text.split("\n"), 1))


def _brought_in(pattern, folder):
    """The files a `source`, `include` or `dofile` names: ~ and $VARIABLES read, relative
    to `folder`, and a glob as both compositors take one, in order."""
    pattern = os.path.expanduser(os.path.expandvars(pattern.strip().strip("\"'")))
    if not pattern:
        return []
    if not os.path.isabs(pattern):
        pattern = os.path.join(folder, pattern)
    return sorted(path for path in glob.glob(pattern) if os.path.isfile(path))


def _substitute(text, variables):
    """`text` with each $variable in it put in, the longest names first, as both
    compositors do, so that $mod2 is not read as $mod and a 2."""
    for name in sorted(variables, key=len, reverse=True):
        text = text.replace("$" + name, variables[name])
    return text


def _visit(path, depth, seen):
    """Whether `path` is to be read: not one read already - a file sourcing itself, or
    two each other - and not brought in too deep."""
    real = os.path.realpath(path)
    if real in seen or depth > _INCLUDE_DEPTH:
        return False
    seen.add(real)
    return True


# Hyprland finds its modifiers anywhere in the field (CKeybindManager::stringToModMask),
# so that "SUPER_SHIFT", "$mainMod SHIFT" and "CTRLALT" all read.
_HYPRLAND_MODS = (("SHIFT", "shift"), ("CTRL", "control"), ("CONTROL", "control"),
                  ("ALT", "alt"), ("MOD1", "alt"), ("SUPER", "super"), ("WIN", "super"),
                  ("LOGO", "super"), ("MOD4", "super"), ("META", "super"),
                  ("CAPS", "other"), ("MOD2", "other"), ("MOD3", "other"),
                  ("MOD5", "other"))

_HYPRLAND_LINE = re.compile(r"(\$?[A-Za-z_][\w:.-]*)\s*=(.*)")


def _hyprland_mods(field):
    field = field.upper()
    return frozenset(mod for name, mod in _HYPRLAND_MODS if name in field)


def _hyprland_binds(path, text=None, bound=None, variables=None, depth=0, seen=None):
    """What hyprland.conf at `path`, and every file it sources, binds: see above."""
    bound = {} if bound is None else bound
    variables = {} if variables is None else variables
    seen = set() if seen is None else seen
    if not _visit(path, depth, seen):
        return bound
    for number, raw in _config_lines(path, text):
        # A # begins a comment, and ## is a # of its own.
        line = raw.replace("##", "\0").split("#", 1)[0].replace("\0", "#").strip()
        found = _HYPRLAND_LINE.fullmatch(line)
        if not found:
            continue
        keyword, value = found.group(1), found.group(2).strip()
        if keyword.startswith("$"):
            variables[keyword[1:]] = _substitute(value, variables)
            continue
        if keyword == "source":
            for other in _brought_in(_substitute(value, variables), os.path.dirname(path)):
                _hyprland_binds(other, None, bound, variables, depth + 1, seen)
            continue
        if keyword != "unbind" and not re.fullmatch(r"bind[a-z]*", keyword):
            continue
        fields = [field.strip() for field in _substitute(value, variables).split(",")]
        if len(fields) < 2 or (keyword != "unbind" and "m" in keyword[4:]):
            continue                        # a mouse bind's "key" is a button
        key = fields[1]
        if not key or key.lower().startswith(("code:", "mouse")):
            continue
        combination = (_hyprland_mods(fields[0]), key.lower())
        if keyword == "unbind":
            bound.pop(combination, None)
        else:
            bound.setdefault(combination, (path, number, raw.strip()))
    return bound


# Hyprland's modifier mask (hyprctl binds), bit by bit.
_HYPRLAND_MASK = ((1, "shift"), (2, "other"), (4, "control"), (8, "alt"), (16, "other"),
                  (32, "other"), (64, "super"), (128, "other"))


def _hyprland_live(run):
    """{(modifiers, key's name in lower case): what it does} for every key bind Hyprland
    has in force outside a submap - its own description where it has one - from
    `hyprctl binds -j`; None when Hyprland cannot be asked, or answers with something
    that is not a list of binds. Ours are left out: in a Lua config they are described
    "DA Assistant: ...", in hyprland.conf they run the app with --action=. Mouse binds
    and keys given by code are left out too: none is one of the app's."""
    code, output = run(["hyprctl", "binds", "-j"])
    if code != 0:
        return None
    try:
        binds = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(binds, list):
        return None
    live = {}
    for bind in binds:
        if not isinstance(bind, dict) or bind.get("mouse") or bind.get("submap"):
            continue
        key = str(bind.get("key") or "")
        description = str(bind.get("description") or "")
        argument = str(bind.get("arg") or "")
        if not key or description.startswith(f"{NAME}:") or "--action=" in argument:
            continue
        try:
            mask = int(bind.get("modmask") or 0)
        except (TypeError, ValueError):
            continue
        mods = frozenset(name for bit, name in _HYPRLAND_MASK if mask & bit)
        dispatcher = str(bind.get("dispatcher") or "")
        what = description or (f"{dispatcher} {argument}".strip()
                               if dispatcher and dispatcher != "__lua"
                               else "a bind in its config")
        live.setdefault((mods, key.lower()), what)
    return live


def _lua_code(lines):
    """[(number, line)] with Lua's comments taken out: from -- to the end of the line,
    and --[[ ... ]] - or --[==[ ... ]==] - however many lines it takes. A -- inside a
    string is none."""
    out, closing = [], None
    for number, line in lines:
        code, i = [], 0
        while i < len(line):
            if closing is not None:
                end = line.find(closing, i)
                if end < 0:
                    break
                i, closing = end + len(closing), None
                continue
            char = line[i]
            if char in "\"'":
                j = i + 1
                while j < len(line) and line[j] != char:
                    j += 2 if line[j] == "\\" else 1
                code.append(line[i:j + 1])
                i = j + 1
                continue
            if line.startswith("--", i):
                long = re.match(r"--\[(=*)\[", line[i:])
                if not long:
                    break
                closing = "]" + long.group(1) + "]"
                i += long.end()
                continue
            code.append(char)
            i += 1
        out.append((number, "".join(code)))
    return out


_LUA_STRING = r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
_LUA_BIND = re.compile(r"\bhl\.(bind|unbind)\s*\(\s*" + _LUA_STRING)
_LUA_REQUIRE = re.compile(r"\brequire\s*\(?\s*" + _LUA_STRING)
_LUA_DOFILE = re.compile(r"\bdofile\s*\(?\s*" + _LUA_STRING)


def _lua_binds(path, text=None, bound=None, depth=0, seen=None, folder=None):
    """What hyprland.lua at `path` binds with a written-out key - hl.bind("SUPER + Q",
    ...) - and the modules it requires from the config directory, which is where the
    user's own are: require("hypr.bindings") is ~/.config/hypr/bindings.lua. A module
    from anywhere else, and a key put together at run time, are not read."""
    bound = {} if bound is None else bound
    seen = set() if seen is None else seen
    if not _visit(path, depth, seen):
        return bound
    folder = folder or os.path.dirname(path)            # ~/.config/hypr
    for number, line in _lua_code(_config_lines(path, text)):
        for found in _LUA_BIND.finditer(line):
            keys = [part.strip() for part in
                    (found.group(2) if found.group(2) is not None
                     else found.group(3)).split("+")]
            if not keys[-1]:
                continue
            combination = (_hyprland_mods(" ".join(keys[:-1])), keys[-1].lower())
            if found.group(1) == "unbind":
                bound.pop(combination, None)
            else:
                bound.setdefault(combination, (path, number, line.strip()))
        for found in _LUA_REQUIRE.finditer(line):
            module = (found.group(1) if found.group(1) is not None else found.group(2))
            parts = module.split(".")
            for base in (os.path.dirname(folder), folder):
                for candidate in (os.path.join(base, *parts) + ".lua",
                                  os.path.join(base, *parts, "init.lua")):
                    if os.path.isfile(candidate):
                        _lua_binds(candidate, None, bound, depth + 1, seen, folder)
        for found in _LUA_DOFILE.finditer(line):
            named = found.group(1) if found.group(1) is not None else found.group(2)
            for other in _brought_in(named, folder):
                _lua_binds(other, None, bound, depth + 1, seen, folder)
    return bound


# Sway's modifier names, in any case.
_SWAY_MODS = {"shift": "shift", "control": "control", "ctrl": "control", "mod1": "alt",
              "alt": "alt", "mod4": "super", "super": "super"}


def _sway_combination(text):
    names = text.split("+")
    if not names[-1]:
        return None
    return (frozenset(_SWAY_MODS.get(name.lower(), "other") for name in names[:-1]),
            names[-1].lower())


def _joined(lines):
    """[(number, line)], stripped, with a line that ends in a backslash joined to the
    next, as Sway joins them, under the first one's number."""
    held, start = None, None
    for number, line in lines:
        line = line.strip()
        if held is not None:
            line, number = f"{held} {line}", start
        if line.endswith("\\"):
            held, start = line[:-1].rstrip(), number
            continue
        held = None
        yield number, line
    if held is not None:
        yield start, held


def _sway_binds(path, text=None, bound=None, variables=None, depth=0, seen=None):
    """What Sway's config at `path`, and every file it includes, binds in the default
    mode: see above. A bindsym inside a `mode` block works only in that mode, and
    clashes with nothing of the app's."""
    bound = {} if bound is None else bound
    variables = {} if variables is None else variables
    seen = set() if seen is None else seen
    if not _visit(path, depth, seen):
        return bound
    blocks = []                             # what each { open around the line began
    for number, line in _joined(_config_lines(path, text)):
        if not line or line.startswith("#"):
            continue
        if line == "}":
            if blocks:
                blocks.pop()
            continue
        command, _, rest = line.partition(" ")
        if command == "set" and not blocks:
            name, _, value = rest.strip().partition(" ")
            if name.startswith("$"):
                variables[name[1:]] = _substitute(value.strip(), variables)
            continue
        if line.endswith("{"):
            blocks.append(command)
            continue
        if blocks in (["bindsym"], ["unbindsym"]):
            command, rest = blocks[0], line           # bindsym { Mod4+a exec x }
        elif blocks:
            continue
        rest = _substitute(rest, variables)
        if command == "include":
            for other in _brought_in(rest, os.path.dirname(path)):
                _sway_binds(other, None, bound, variables, depth + 1, seen)
            continue
        if command not in ("bindsym", "unbindsym"):
            continue
        keys = [word for word in rest.split() if not word.startswith("--")]
        combination = _sway_combination(keys[0]) if keys else None
        if combination is None:
            continue
        if command == "unbindsym":
            bound.pop(combination, None)
        else:
            bound.setdefault(combination, (path, number, line))
    return bound


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
