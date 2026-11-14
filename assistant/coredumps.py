"""A crash puts nothing of the app's memory on disk.

A conversation, its captures and attachments, and a key typed in with no keyring live
only in the app's memory (docs/security-and-privacy.md), and a core dump is that memory
on disk. no_core_dumps(), the first thing every way in does (__main__.main), as every DA
app does (the DA apps' security standard, Nov 2026):

  - leaves the process's memory out of any dump (/proc/self/coredump_filter 0), for it
    and every process it starts - WebKit's, which draw the conversation, among them: the
    filter is inherited and kept across exec. First, since once the process is
    undumpable its /proc entries are root's, and writing the filter is refused (EACCES,
    measured on MintDev, Nov 14 2026);
  - makes the process undumpable (prctl PR_SET_DUMPABLE 0). The crash collector most
    distributions run, systemd-coredump, is handed a size by the kernel's core_pattern
    whatever the limit says, and kept a crashed process's memory with the limit at 0
    (measured on Mint 22, systemd 255); it keeps nothing of one that is not dumpable.
    Nor can another program of the user's read its memory, as a debugger would;
  - and sets the core size limit's soft part to 1, the hard part left as it is: at
    exactly 1 the kernel skips the dump altogether ("RLIMIT_CORE is set to 1, aborting
    core"), even of a dumpable process - this one while it asks the portal (below), a
    program it starts - where 0 is ignored by systemd-coredump's pipe (measured on
    MintDev, Nov 14 2026). Where the hard limit is 0 already, nothing can be raised to
    1, and 0 stands. As DA Privacy Kit's no_core_dumps.

xdg-desktop-portal refuses every request of a process that is not dumpable: "Portal
operation not allowed: Unable to open /proc/PID/root" (measured with 1.20.0). It opens
/proc/PID/root to tell a sandboxed app from one of the host's, and the kernel lets no
other process open an undumpable one's. The app asks the portal for screenshots and for
its global shortcuts on Wayland, so it is dumpable while it asks: from the moment a
request goes until the portal has answered it (asking_the_portal, for_the_portal). The
portal knows the connection from its first answer on, and asks /proc no more for it.
GTK's file dialogs are its own, never the portal's (GTK_USE_PORTAL=0, for this process
alone: hostenv.set_own); every program it starts gets the value back (hostenv.host_env).
While it is dumpable for the portal, a crash still leaves nothing: the core limit of 1,
and the filter.

Only a request that will be answered opens that window. On the way out (quitting, the
first thing Assistant.quit does, and at exit whatever way the app leaves) the process is
made undumpable again at once, a request still unanswered or not, and nothing asked from
then on opens it: the app closes its connections as it quits, so no answer comes. The
shortcuts' Session.Close, which has no answer to read, never opens it either
(hotkeys.PortalHotkeys._tell). Sent through the window as the app quit, it left the
process dumpable until it exited, 1 to 66 ms on the lab's desktops (Nov 14 2026).
"""

import atexit
import os
import sys
import threading
from contextlib import contextmanager

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
# The core size limit set: at exactly 1 the kernel skips the dump (see the module).
CORE_LIMIT = 1

_lock = threading.Lock()
_undumpable = False         # no_core_dumps made the process so
_asking = 0                 # portal requests not answered yet
_quitting = False           # quitting(): nothing makes the process dumpable any more


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
    Linux only; failures are ignored: nothing here is worth not starting for."""
    global _undumpable
    if not sys.platform.startswith("linux"):
        return
    try:
        # First: an undumpable process may not write its own /proc entries.
        with open("/proc/self/coredump_filter", "w", encoding="ascii") as f:
            f.write("0")
    except OSError:
        pass
    with _lock:
        if _prctl(PR_SET_DUMPABLE, 0) == 0:
            if not _undumpable:
                # Whatever way the app leaves, not dumpable as it does (quitting).
                atexit.register(quitting)
            _undumpable = True
    try:
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        if hard == resource.RLIM_INFINITY or hard >= CORE_LIMIT:
            resource.setrlimit(resource.RLIMIT_CORE, (CORE_LIMIT, hard))
    except (ImportError, ValueError, OSError):
        pass
    if _undumpable and os.environ.get("GTK_USE_PORTAL", "").startswith("1"):
        # Where the desktop asks GTK for the portal's file dialogs (some KDE desktops):
        # the portal would answer none of them. The programs the app starts get the
        # value back (hostenv.host_env).
        from . import hostenv
        hostenv.set_own("GTK_USE_PORTAL", "0")


def _nothing():
    pass


def asking_the_portal():
    """A portal request is about to go: the process is dumpable until the function this
    returns is called, once the portal has answered (more than once does nothing). With
    several under way, until the last is answered. Where no_core_dumps made nothing
    undumpable, nothing changes; nor once the app is quitting (quitting), when no answer
    will come."""
    global _asking
    with _lock:
        if _quitting:
            return _nothing
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


def quitting():
    """The app is on its way out: undumpable again at once, if a request still unanswered
    - a screenshot's picker left open - had made it dumpable, and no request from now on
    makes it so (asking_the_portal): its connections are about to close, and no answer
    will come. The first thing Assistant.quit does, and at exit (atexit, registered by
    no_core_dumps), whatever way the app leaves. Where no_core_dumps made nothing
    undumpable, nothing changes."""
    global _quitting
    with _lock:
        _quitting = True
        if _undumpable:
            _prctl(PR_SET_DUMPABLE, 0)


@contextmanager
def for_the_portal():
    """asking_the_portal, for a request answered before the block ends (call_sync)."""
    done = asking_the_portal()
    try:
        yield
    finally:
        done()
