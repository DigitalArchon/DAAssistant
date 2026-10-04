"""Screenshot backend selection and fallback.

Ordering rationale:

X11  - X11Backend first. Freeze-then-crop is instant and artifact-free, and our own
       overlay gives a consistent picker across desktops.

Wayland - the portal's interactive mode first: the compositor runs the picker, so the
       image can contain nothing of ours and no client-side coordinate maths is involved.
       Except on KDE, whose portal dialog only offers whole screens and windows - there
       Spectacle's own --region picker leads, and the portal keeps the crop rung below.
       Then the desktop's native tool. Then, as a universal last resort, a fullscreen grab
       fed to CropDialog - an ordinary window, so it works on any compositor with nothing
       extra installed. That last rung is what stops "no screenshot tool available" from
       being a dead end on Wayland.

A user cancelling is NOT an error and never falls through to the next backend; only a
genuine failure does.
"""

from .. import session
from .base import CaptureError, ScreenshotBackend  # noqa: F401
from .cli import CliBackend
from .portal import PortalBackend
from .selector import CropDialog, FrozenOverlay  # noqa: F401
from .x11 import X11Backend

CANCELLED = "Cancelled"


def _is_cancel(message):
    return str(message).strip().lower() == "cancelled"


class CaptureManager:
    """Runs the fallback chain and reports which rung succeeded."""

    def __init__(self):
        self.session_type = session.session_type()
        self.portal = PortalBackend()
        self.cli = CliBackend()
        self.x11 = X11Backend()
        self.last_backend = None

    def describe_chain(self):
        return [name for name, _ in self._chain()]

    def _chain(self):
        """[(label, callable(on_done, on_error))] in the order they are attempted."""
        steps = []

        if self.session_type == "x11" and self.x11.is_available():
            steps.append(("x11-frozen-overlay", self.x11.select_region))

        # Not merely v2: on KDE the portal's picker has no rectangular region in it, so
        # supports_region_selection sends that desktop down to Spectacle instead.
        if self.portal.is_available() and self.portal.supports_region_selection():
            steps.append(("portal-interactive", self.portal.select_region))

        if self.cli.is_available() and self.cli.supports_region_selection():
            steps.append((f"cli-{self.cli.tool}", self.cli.select_region))

        # Universal fallback: whole screen, cropped in a normal window.
        if self.portal.is_available():
            steps.append(("portal-fullscreen+crop", self._portal_fullscreen_crop))
        if self.cli.is_available():
            steps.append((f"cli-{self.cli.tool}-fullscreen+crop", self._cli_fullscreen_crop))
        if self.session_type == "x11" and self.x11.is_available():
            steps.append(("x11-fullscreen+crop", self._x11_fullscreen_crop))

        return steps

    # ---- fullscreen + crop rungs -----------------------------------------

    def _portal_fullscreen_crop(self, on_done, on_error):
        self.portal.capture_fullscreen_async(
            lambda image: CropDialog(image, on_done, on_error), on_error)

    def _cli_fullscreen_crop(self, on_done, on_error):
        import threading

        from gi.repository import GLib

        def show(image):
            CropDialog(image, on_done, on_error)
            return False        # one-shot idle callback

        def worker():
            try:
                image = self.cli.capture_fullscreen()
            except Exception as e:
                GLib.idle_add(on_error, str(e))
            else:
                GLib.idle_add(show, image)

        threading.Thread(target=worker, daemon=True).start()

    def _x11_fullscreen_crop(self, on_done, on_error):
        try:
            image = self.x11.capture_fullscreen()
        except Exception as e:
            on_error(str(e))
            return
        CropDialog(image, on_done, on_error)

    # ---- public API -------------------------------------------------------

    def select_region(self, on_done, on_error):
        steps = self._chain()
        if not steps:
            on_error(self._no_backend_message())
            return

        errors = []

        def attempt(index):
            if index >= len(steps):
                on_error("Screenshot failed.\n" + "\n".join(errors))
                return
            label, func = steps[index]

            def done(image):
                self.last_backend = label
                on_done(image)

            def failed(message):
                if _is_cancel(message):
                    on_error(CANCELLED)     # deliberate: do not try the next backend
                    return
                errors.append(f"  {label}: {message}")
                attempt(index + 1)

            try:
                func(done, failed)
            except Exception as e:
                failed(f"{type(e).__name__}: {e}")

        attempt(0)

    def _no_backend_message(self):
        desktop = session.desktop()
        hints = {
            "hyprland": "install grim and slurp",
            "sway": "install grim and slurp",
            "kde": "install spectacle",
            "gnome": "install gnome-screenshot",
        }
        hint = hints.get(desktop, "install grim+slurp, spectacle or gnome-screenshot")
        return (f"No screenshot backend available on {desktop}/{self.session_type}.\n"
                f"Try: {hint}, or install xdg-desktop-portal for your desktop.")
