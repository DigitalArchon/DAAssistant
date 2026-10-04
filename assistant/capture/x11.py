"""X11 backend: grab every pixel first, then let the user crop the frozen copy.

The ordering is the whole point. Showing an overlay, destroying it, and grabbing the
screen in the same call catches the overlay's own dimming layer more often than not.
Capturing before any UI exists makes that impossible.

It is also faster in use: the crop is a memory operation, with no second round-trip to
the X server.
"""

from .. import gtkcompat  # noqa: F401
from gi.repository import Gdk

from .base import CaptureError, ScreenshotBackend
from .selector import FrozenOverlay


def total_geometry():
    """Bounding box across all monitors, in logical units."""
    display = Gdk.Display.get_default()
    if display is None:
        raise CaptureError("No display available")
    n = display.get_n_monitors()
    if n == 0:
        raise CaptureError("No monitors reported")
    xs, ys, xe, ye = [], [], [], []
    for i in range(n):
        geo = display.get_monitor(i).get_geometry()
        xs.append(geo.x)
        ys.append(geo.y)
        xe.append(geo.x + geo.width)
        ye.append(geo.y + geo.height)
    return {"x": min(xs), "y": min(ys),
            "width": max(xe) - min(xs), "height": max(ye) - min(ys)}


class X11Backend(ScreenshotBackend):
    name = "x11"

    def is_available(self):
        try:
            from PIL import Image, ImageGrab  # noqa: F401
        except ImportError:
            return False
        try:
            from PIL import Image as _Image
            # Without XCB, Pillow silently falls back to shelling out to
            # gnome-screenshot, which is slow and cannot do a clean full-screen grab.
            return bool(_Image.core.HAVE_XCB)
        except (AttributeError, ImportError):
            return False

    def capture_fullscreen(self):
        try:
            from PIL import ImageGrab
        except ImportError as e:
            raise CaptureError(f"PIL ImageGrab unavailable: {e}")
        try:
            image = ImageGrab.grab(xdisplay=None)
        except Exception as e:
            raise CaptureError(f"X11 grab failed: {e}")
        if image is None:
            raise CaptureError("X11 grab returned nothing")
        return image

    def select_region(self, on_done, on_error):
        try:
            image = self.capture_fullscreen()
            geometry = total_geometry()
        except CaptureError as e:
            on_error(str(e))
            return
        FrozenOverlay(image, geometry, on_done, on_error)
