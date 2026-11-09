"""xdg-desktop-portal Screenshot backend.

This is the correct primary path on Wayland: the
compositor itself runs the region picker, so there is no overlay of ours in the image and
no client-side coordinate guessing. Confirmed present at version 2 on Mint/Cinnamon, and
it is also what KDE and GNOME provide.

Version matters: `interactive` was added in Screenshot v2. On v1 we can only get a
fullscreen grab, which we then hand to our own crop dialog. The same goes for KDE and for
xdg-desktop-portal-wlr at any version - see supports_region_selection.
"""

import os
import urllib.parse

from gi.repository import Gio, GLib
from PIL import Image

from .. import session
from .base import CaptureError, ScreenshotBackend

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
REQUEST_IFACE = "org.freedesktop.portal.Request"


class PortalBackend(ScreenshotBackend):
    name = "portal"

    def __init__(self):
        self._bus = None
        self._version = None
        self._token = 0

    def bus(self):
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    def version(self):
        """The portal's Screenshot version; 1 while it has not answered, which is not
        kept: a portal still starting when the app did - at login - would otherwise
        have been taken for one without the region picker for the whole run."""
        if self._version is None:
            found = session.portal_version("Screenshot")
            if not found:
                return 1
            self._version = found
        return self._version

    def is_available(self):
        return session.portal_has("Screenshot")

    def supports_interactive(self):
        return self.version() >= 2

    def supports_region_selection(self):
        """Can the portal's own picker return a region, rather than a whole screen?

        Not on KDE. xdg-desktop-portal-kde's "Request screenshot" dialog offers three
        choices - full screen, current screen, active window - and no rectangular
        region at all, yet it answers with an image we would hand back as though the
        user had selected it. Spectacle's own --region picker is the one a KDE user
        expects, so here this rung stands aside for the CLI backend; the portal is
        still used below it for the fullscreen-then-crop fallback.

        Nor where xdg-desktop-portal-wlr takes the screenshots - Sway's portal, and that
        of the other wlroots compositors that use it. It reads `interactive` and does
        nothing with it: no picker opens, and it answers with the whole of one output,
        which, again, would be handed back as the region the user chose. grim+slurp
        is what picks a region there, and is the rung below; without them the
        fullscreen-then-crop rung still lets the user choose one, which this rung
        never does - so it stands aside whether they are installed or not.

        Hyprland keeps this rung, as before: its own portal, xdg-desktop-portal-hyprland,
        does honour `interactive`, by running slurp itself - the same picker as the
        grim+slurp rung. Sway is told apart by name, and any other compositor by
        whether the wlr portal is running (session.portal_backend_running), which also
        catches a Hyprland left with the wlr portal instead of its own. GNOME's, KDE's
        and Cinnamon's portals are known not to be it, and are not asked.
        """
        if not self.supports_interactive():
            return False
        desktop = session.desktop()
        if desktop in ("kde", "sway"):
            return False
        if desktop in ("gnome", "cinnamon"):
            return True
        return not session.portal_backend_running("wlr")

    def _next_handle(self):
        """Predict the Request object path so we can subscribe before calling.

        Racing the signal is a real failure mode otherwise: the portal can answer before
        a subscription made after the call lands.
        """
        self._token += 1
        token = f"daassistant{os.getpid()}_{self._token}"
        unique = self.bus().get_unique_name()            # ":1.234"
        sender = unique[1:].replace(".", "_")            # "1_234"
        return token, f"{PORTAL_PATH}/request/{sender}/{token}"

    def _screenshot(self, interactive, on_done, on_error):
        try:
            bus = self.bus()
        except GLib.Error as e:
            on_error(f"Cannot reach the session bus: {e.message}")
            return

        token, handle = self._next_handle()
        state = {"sub": None}

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            if state["sub"] is not None:
                bus.signal_unsubscribe(state["sub"])
                state["sub"] = None
            code, results = params.unpack()
            if code == 1:
                on_error("Cancelled")          # user dismissed the picker
                return
            if code != 0:
                on_error(f"Screenshot portal returned code {code}")
                return
            uri = results.get("uri")
            if not uri:
                on_error("Screenshot portal returned no image")
                return
            # Only the loading is the capture's to fail. What on_done does with the
            # picture - the review dialog - is not: inside this try, a fault of the
            # review's went down the chain as a failed capture, and the next rung's
            # picker came up.
            try:
                image = self._load_uri(uri)
            except Exception as e:
                on_error(str(e))
                return
            on_done(image)

        state["sub"] = bus.signal_subscribe(
            PORTAL_BUS, REQUEST_IFACE, "Response", handle, None,
            Gio.DBusSignalFlags.NONE, on_response)

        options = {
            "handle_token": GLib.Variant("s", token),
            "modal": GLib.Variant("b", True),
        }
        if interactive and self.supports_interactive():
            options["interactive"] = GLib.Variant("b", True)

        def on_called(src, res):
            try:
                src.call_finish(res)
            except GLib.Error as e:
                if state["sub"] is not None:
                    bus.signal_unsubscribe(state["sub"])
                    state["sub"] = None
                on_error(f"Screenshot portal call failed: {e.message}")

        bus.call(
            PORTAL_BUS, PORTAL_PATH, SCREENSHOT_IFACE, "Screenshot",
            GLib.Variant("(sa{sv})", ("", options)),
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None,
            on_called)

    @staticmethod
    def _load_uri(uri):
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme and parsed.scheme != "file":
            raise CaptureError(f"Unsupported screenshot URI scheme: {parsed.scheme}")
        path = urllib.parse.unquote(parsed.path)
        if not os.path.exists(path):
            raise CaptureError(f"Screenshot file vanished: {path}")
        try:
            with Image.open(path) as img:
                img.load()
                return img.copy()
        finally:
            # The portal hands us a file it expects the caller to own. Leaving these
            # behind slowly fills the user's Pictures directory.
            try:
                os.unlink(path)
            except OSError:
                pass

    def select_region(self, on_done, on_error):
        """Interactive: the compositor's own picker returns an already-cropped image."""
        if not self.supports_interactive():
            on_error("Portal Screenshot v1 has no interactive mode")
            return
        if not self.supports_region_selection():
            on_error("This desktop's portal picker cannot select a region")
            return
        self._screenshot(True, on_done, on_error)

    def capture_fullscreen_async(self, on_done, on_error):
        self._screenshot(False, on_done, on_error)
