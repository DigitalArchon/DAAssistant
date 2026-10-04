"""Compositor-native screenshot tools, used when the portal is absent or refuses.

Each helper reports its own availability so `--probe` can explain what is missing rather
than failing at the moment the user presses a hotkey.

Details that matter:
  - slurp geometry is parsed with a signed regex. `(\\d+),(\\d+)` failed on any monitor
    positioned left of or above the origin, which is a normal multi-head layout.
  - spectacle flags are probed from --help instead of hardcoding Plasma 5's `-bno`;
    Plasma 6 (the CachyOS target) changed them.
  - temp files are cleaned up on every path, including the error paths.
  - they live in a directory of our own rather than loose in /tmp. See _tempfile.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading

from gi.repository import GLib
from PIL import Image

from .. import hostenv, session
from .base import CaptureError, ScreenshotBackend

SLURP_GEOMETRY = re.compile(r"(-?\d+),(-?\d+)\s+(\d+)x(\d+)")


def _load(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise CaptureError("Screenshot tool produced no image")
    with Image.open(path) as img:
        img.load()
        return img.copy()


def _tempfile():
    """A path for the tool to write to, in a directory only this user can enter.

    The file is not created. Spectacle and Cinnamon insist on creating it themselves,
    which used to mean reserving a name in /tmp, deleting it, and trusting nobody else
    to take it in between - and the file they then wrote got their umask, so a capture
    of the screen sat world-readable in a shared directory. Inside a 0700 directory
    neither matters. XDG_RUNTIME_DIR when there is one: per-user, and never on disk.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not (base and os.path.isdir(base) and os.access(base, os.W_OK)):
        base = None
    directory = tempfile.mkdtemp(prefix="da-assistant-", dir=base)
    return os.path.join(directory, "capture.png")


def _discard(path):
    """Remove what _tempfile handed out, whether or not anything was written to it."""
    for remove, target in ((os.unlink, path), (os.rmdir, os.path.dirname(path))):
        try:
            remove(target)
        except OSError:
            pass


def _run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=hostenv.host_env())
    except FileNotFoundError:
        raise CaptureError(f"{cmd[0]} is not installed")
    except subprocess.TimeoutExpired:
        raise CaptureError(f"{cmd[0]} timed out")


def _has_flag(help_text, flag):
    """Is `flag` offered as a whole option, rather than as part of a longer one?

    A plain substring test says every Spectacle understands `-n`, because `--nonotify`
    and `--new-instance` both contain it.
    """
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def _spectacle_args(path, region):
    """Build Spectacle arguments that work on both Plasma 5 and Plasma 6.

    Plasma 6 renamed/removed several short flags, so ask the binary what it supports
    rather than assuming. Long options are stable across both.
    """
    help_text = ""
    try:
        result = subprocess.run(["spectacle", "--help"], capture_output=True,
                                text=True, timeout=10, env=hostenv.host_env())
        help_text = (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError):
        pass

    args = ["spectacle"]
    # --background is what keeps this to the picker alone: no Spectacle window opens,
    # the rectangle is dragged, and the file is written.
    args.append("--background" if _has_flag(help_text, "--background") else "-b")
    if _has_flag(help_text, "--nonotify"):
        args.append("--nonotify")
    elif _has_flag(help_text, "-n"):
        args.append("-n")
    if region:
        args.append("--region" if _has_flag(help_text, "--region") else "-r")
    else:
        args.append("--fullscreen" if _has_flag(help_text, "--fullscreen") else "-f")
    args += (["--output", path] if _has_flag(help_text, "--output") else ["-o", path])
    return args


class CliBackend(ScreenshotBackend):
    """Dispatches to whichever native tool this desktop provides."""

    name = "cli"

    def __init__(self):
        self.desktop = session.desktop()
        self.tool = self._pick_tool()

    def _pick_tool(self):
        have = shutil.which
        # Prefer the tool native to the running desktop; it is the one whose region
        # picker matches what the user expects and that the compositor trusts.
        if self.desktop in ("hyprland", "sway") and have("grim"):
            return "grim"
        if self.desktop == "kde" and have("spectacle"):
            return "spectacle"
        if self.desktop == "gnome" and have("gnome-screenshot"):
            return "gnome-screenshot"
        if self.desktop == "cinnamon" and session.has_cinnamon_screenshot():
            return "cinnamon"
        for name in ("grim", "spectacle", "gnome-screenshot"):
            if have(name):
                return name
        if session.has_cinnamon_screenshot():
            return "cinnamon"
        return None

    def is_available(self):
        return self.tool is not None

    def supports_region_selection(self):
        if self.tool == "grim":
            return shutil.which("slurp") is not None
        return self.tool in ("spectacle", "gnome-screenshot", "cinnamon")

    # ---- region selection -------------------------------------------------

    def select_region(self, on_done, on_error):
        if not self.tool:
            on_error("No screenshot tool installed")
            return
        if not self.supports_region_selection():
            on_error(f"{self.tool} cannot select a region here")
            return

        def worker():
            try:
                image = self._select_blocking()
            except CaptureError as e:
                GLib.idle_add(on_error, str(e))
            except Exception as e:
                GLib.idle_add(on_error, f"{type(e).__name__}: {e}")
            else:
                if image is None:
                    GLib.idle_add(on_error, "Cancelled")
                else:
                    GLib.idle_add(on_done, image)

        threading.Thread(target=worker, daemon=True).start()

    def _select_blocking(self):
        if self.tool == "grim":
            slurp = _run(["slurp"], timeout=300)
            if slurp.returncode != 0:
                return None                      # user pressed Esc
            match = SLURP_GEOMETRY.search(slurp.stdout.strip())
            if not match:
                raise CaptureError(f"Could not parse slurp output: {slurp.stdout!r}")
            x, y, w, h = (int(g) for g in match.groups())
            path = _tempfile()
            try:
                result = _run(["grim", "-g", f"{x},{y} {w}x{h}", path], timeout=60)
                if result.returncode != 0:
                    raise CaptureError(f"grim failed: {result.stderr.strip()}")
                return _load(path)
            finally:
                _discard(path)

        if self.tool == "spectacle":
            path = _tempfile()
            try:
                result = _run(_spectacle_args(path, region=True), timeout=300)
                if not os.path.exists(path) or os.path.getsize(path) == 0:
                    # Nothing written. Esc during the rectangle looks exactly like this,
                    # and some Spectacle versions also exit non-zero for it, so only call
                    # it a failure when the tool actually complained about something.
                    if result.returncode != 0 and result.stderr.strip():
                        raise CaptureError(f"spectacle failed: {result.stderr.strip()}")
                    return None
                return _load(path)
            finally:
                _discard(path)

        if self.tool == "gnome-screenshot":
            path = _tempfile()
            try:
                result = _run(["gnome-screenshot", "-a", "-f", path], timeout=300)
                # Esc leaves no file, or an empty one, depending on the version.
                if result.returncode != 0 or not os.path.exists(path) \
                        or os.path.getsize(path) == 0:
                    return None
                return _load(path)
            finally:
                _discard(path)

        if self.tool == "cinnamon":
            path = _tempfile()
            try:
                result = _run(["gdbus", "call", "--session", "--dest", "org.Cinnamon",
                               "--object-path", "/org/Cinnamon", "--method",
                               "org.Cinnamon.ScreenshotArea", "false", path],
                              timeout=300)
                if result.returncode != 0 or not os.path.exists(path):
                    return None
                return _load(path)
            finally:
                _discard(path)

        raise CaptureError(f"Unsupported tool: {self.tool}")

    # ---- fullscreen -------------------------------------------------------

    def capture_fullscreen(self):
        if not self.tool:
            raise CaptureError("No screenshot tool installed")
        path = _tempfile()
        try:
            if self.tool == "grim":
                result = _run(["grim", path])
            elif self.tool == "spectacle":
                result = _run(_spectacle_args(path, region=False))
            elif self.tool == "gnome-screenshot":
                result = _run(["gnome-screenshot", "-f", path])
            elif self.tool == "cinnamon":
                result = _run(["gdbus", "call", "--session", "--dest", "org.Cinnamon",
                               "--object-path", "/org/Cinnamon", "--method",
                               "org.Cinnamon.Screenshot", "false", path])
            else:
                raise CaptureError(f"Unsupported tool: {self.tool}")
            if result.returncode != 0 and not os.path.exists(path):
                raise CaptureError(f"{self.tool} failed: {result.stderr.strip()}")
            return _load(path)
        finally:
            _discard(path)
