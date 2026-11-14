"""A crash puts nothing of the app's memory on disk.

A conversation, its captures and attachments, and a key typed in with no keyring live
only in the app's memory (docs/security-and-privacy.md), and a core dump is that memory
on disk. no_core_dumps(), the first thing every way in does (__main__.main):

  - sets the core size limit to 0, soft and hard: no core file, for this process or any
    it starts - WebKit's, which draw the conversation, among them;
  - and makes the process undumpable (prctl PR_SET_DUMPABLE 0). The crash collector most
    distributions run, systemd-coredump, is handed an unlimited size by the kernel's
    core_pattern whatever the limit says, and kept a crashed process's memory with the
    limit at 0 (measured on Mint 22, systemd 255); it keeps nothing of one that is not
    dumpable. Nor can another program of the user's read its memory, as a debugger
    would. As SealedLore's gui/app.py _no_core_dumps.

xdg-desktop-portal refuses every request of a process that is not dumpable: "Portal
operation not allowed: Unable to open /proc/PID/root" (measured with 1.20.0). It opens
/proc/PID/root to tell a sandboxed app from one of the host's, and the kernel lets no
other process open an undumpable one's. The app asks the portal for screenshots and for
its global shortcuts on Wayland, so it is dumpable while it asks: from the moment a
request goes until the portal has answered it (asking_the_portal, for_the_portal). The
portal knows the connection from its first answer on, and asks /proc no more for it.
GTK's file dialogs are its own, never the portal's (GTK_USE_PORTAL=0, for this process
alone: hostenv.set_own).
"""

import os
import sys
import threading
from contextlib import contextmanager

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4

_lock = threading.Lock()
_undumpable = False         # no_core_dumps made the process so
_asking = 0                 # portal requests not answered yet


def _prctl(option, value=0):
    """prctl(option, value), or None where it cannot be asked."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(option, value, 0, 0, 0)
    except (OSError, AttributeError):
        return None


def dumpable():
    """Whether the process is dumpable now: 1 or 0, or None where that is not known."""
    return _prctl(PR_GET_DUMPABLE)


def no_core_dumps():
    """No core file and no crash collector's copy for this process (see the module).
    Failures are ignored: nothing here is worth not starting for."""
    global _undumpable
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    with _lock:
        if _prctl(PR_SET_DUMPABLE, 0) == 0:
            _undumpable = True
    if _undumpable and os.environ.get("GTK_USE_PORTAL", "").startswith("1"):
        # Where the desktop asks GTK for the portal's file dialogs (some KDE desktops):
        # the portal would answer none of them. The programs the app starts get the
        # value back (hostenv.host_env).
        from . import hostenv
        hostenv.set_own("GTK_USE_PORTAL", "0")


def asking_the_portal():
    """A portal request is about to go: the process is dumpable until the function this
    returns is called, once the portal has answered (more than once does nothing). With
    several under way, until the last is answered. Where no_core_dumps made nothing
    undumpable, nothing changes."""
    global _asking
    with _lock:
        _asking += 1
        if _undumpable and _asking == 1:
            _prctl(PR_SET_DUMPABLE, 1)
    answered = []

    def done():
        global _asking
        with _lock:
            if answered:
                return
            answered.append(True)
            _asking -= 1
            if _undumpable and _asking == 0:
                _prctl(PR_SET_DUMPABLE, 0)

    return done


@contextmanager
def for_the_portal():
    """asking_the_portal, for a request answered before the block ends (call_sync)."""
    done = asking_the_portal()
    try:
        yield
    finally:
        done()
