"""Regression test for the defect that made OCR unreliable.

A selector that does this on button release:

    self.destroy()
    screenshot = backend.capture_region(x, y, w, h)   # <-- re-grabs the SCREEN

routinely catches the overlay's own 30% black wash and red border, since destroy() is
asynchronous. The image sent to the model is dimmed and low-contrast, and the vision
model cannot read the text.

These tests assert the behaviour that avoids it: the crop comes from the frozen
in-memory buffer, and the selector has no way to capture the screen at all.
"""

import ast
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from PIL import Image

from assistant import gtkcompat  # noqa: F401
from assistant.capture import selector

SELECTOR_SRC = os.path.join(os.path.dirname(selector.__file__), "selector.py")


def overlay(image, view=(500, 500)):
    """The overlay's selection over `image`, laid out at `view` logical pixels.

    _finish/_to_image_rect live on _SelectionMixin, shared verbatim by FrozenOverlay and
    CropDialog, so this exercises the real code path without constructing a GObject (and
    without grabbing the live session's input)."""
    class Harness(selector._SelectionMixin):
        fit = selector.FrozenOverlay.fit

    obj = Harness()

    class Area:
        @staticmethod
        def get_allocation():
            class Alloc:
                width, height = view
            return Alloc
    obj.area = Area
    obj._init_selection(image)
    obj.destroy = lambda: None
    return obj


class CropComesFromFrozenBuffer(unittest.TestCase):
    """The delivered image must be a crop of the buffer captured BEFORE the UI existed."""

    def _make(self, image, view=(500, 500)):
        return overlay(image, view)

    def test_crop_is_taken_from_the_frozen_image(self):
        # A distinctive synthetic buffer that cannot coincidentally match the screen.
        source = Image.new("RGB", (500, 500), (255, 0, 255))
        source.paste(Image.new("RGB", (50, 50), (0, 255, 0)), (100, 100))

        delivered = {}
        obj = self._make(source)
        obj.on_done = lambda img: delivered.setdefault("image", img)
        obj.on_error = lambda msg: delivered.setdefault("error", msg)

        obj.start, obj.end = (100, 100), (150, 150)
        obj._finish()

        self.assertIn("image", delivered, "no image was delivered")
        crop = delivered["image"]
        self.assertEqual(crop.size, (50, 50))
        # Every pixel is the green patch from the frozen buffer. If any re-grab had
        # happened this would be desktop pixels instead.
        colours = crop.convert("RGB").getcolors()
        self.assertEqual(colours, [(50 * 50, (0, 255, 0))],
                         f"crop did not come from the frozen buffer: {colours}")

    def test_no_dimming_is_baked_into_the_crop(self):
        """The 45% wash is a paint-time effect only; it must not reach the pixels."""
        white = Image.new("RGB", (400, 400), (255, 255, 255))
        delivered = {}
        obj = self._make(white, view=(400, 400))
        obj.on_done = lambda img: delivered.setdefault("image", img)
        obj.on_error = lambda msg: None
        obj.start, obj.end = (10, 10), (210, 210)
        obj._finish()

        crop = delivered["image"]
        extrema = crop.convert("L").getextrema()
        self.assertEqual(extrema, (255, 255),
                         f"crop was darkened - overlay contamination is back: {extrema}")

    def test_tiny_drag_is_treated_as_cancel(self):
        delivered = {}
        obj = self._make(Image.new("RGB", (400, 400)))
        obj.on_done = lambda img: delivered.setdefault("image", img)
        obj.on_error = lambda msg: delivered.setdefault("error", msg)
        obj.start, obj.end = (10, 10), (12, 12)
        obj._finish()
        self.assertEqual(delivered.get("error"), "Cancelled")
        self.assertNotIn("image", delivered)


QUARTERS = {"top left": (255, 0, 0), "top right": (0, 255, 0),
            "bottom left": (0, 0, 255), "bottom right": (255, 255, 255)}


def quartered(size):
    """A screen grab of `size` device pixels, each quarter its own colour."""
    w, h = size
    image = Image.new("RGB", size)
    for (name, colour), (x, y) in zip(QUARTERS.items(),
                                      ((0, 0), (w // 2, 0), (0, h // 2), (w // 2, h // 2))):
        image.paste(Image.new("RGB", (w // 2, h // 2), colour), (x, y))
    return image


class TheOverlayOnHiDPI(unittest.TestCase):
    """An X11 grab is in device pixels and the overlay is laid out in logical ones, so
    at scale factor 2 the image is twice the overlay's size. It used to be drawn at 1.0:
    the overlay showed the screen's top-left quarter, magnified, and nothing else of it
    could be selected."""

    def _crop(self, image, view, start, end):
        delivered = {}
        obj = overlay(image, view)
        obj.on_done = lambda img: delivered.setdefault("image", img)
        obj.on_error = lambda msg: delivered.setdefault("error", msg)
        obj.start, obj.end = start, end
        obj._finish()
        self.assertIn("image", delivered, delivered.get("error"))
        return delivered["image"]

    def test_a_drag_from_corner_to_corner_takes_the_whole_screen(self):
        source = quartered((3840, 2160))
        crop = self._crop(source, (1920, 1080), (0, 0), (1920, 1080))
        self.assertEqual(crop.size, (3840, 2160))
        self.assertEqual(crop.tobytes(), source.tobytes())

    def test_the_bottom_right_quarter_drawn_round_is_that_quarter_exactly(self):
        source = quartered((3840, 2160))
        crop = self._crop(source, (1920, 1080), (960, 540), (1920, 1080))
        self.assertEqual(crop.size, (1920, 1080))
        self.assertEqual(crop.getcolors(), [(1920 * 1080, QUARTERS["bottom right"])])

    def test_at_scale_1_a_drag_is_taken_pixel_for_pixel(self):
        source = quartered((1920, 1080))
        crop = self._crop(source, (1920, 1080), (950, 530), (1010, 600))
        self.assertEqual(crop.size, (60, 70))
        self.assertEqual(crop.tobytes(), source.crop((950, 530, 1010, 600)).tobytes())

    def test_the_whole_screen_is_what_is_shown(self):
        try:
            import cairo
            import gi
            gi.require_foreign("cairo")
        except ImportError:
            self.skipTest("needs pycairo and PyGObject's cairo support")
        obj = overlay(quartered((3840, 2160)), (1920, 1080))
        # What GTK hands the overlay at scale factor 2: 1920x1080 units, each 2x2
        # device pixels.
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 3840, 2160)
        surface.set_device_scale(2, 2)
        obj._draw_selection(cairo.Context(surface), dim=0.45)
        surface.flush()
        data, stride = surface.get_data(), surface.get_stride()
        for name, (x, y) in (("top left", (900, 600)), ("top right", (3000, 600)),
                             ("bottom left", (600, 1900)),
                             ("bottom right", (3800, 2100))):
            blue, green, red = data[y * stride + x * 4:y * stride + x * 4 + 3]
            shown = tuple(255 if channel > 100 else 0 for channel in (red, green, blue))
            self.assertEqual(shown, QUARTERS[name], f"device pixel {x},{y}")


class SelectorCannotCaptureTheScreen(unittest.TestCase):
    """Structural guarantee: re-grabbing is not merely avoided, it is unavailable."""

    def test_selector_imports_no_capture_backend(self):
        tree = ast.parse(pathlib.Path(SELECTOR_SRC).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(a.name for a in node.names)

        for forbidden in ("ImageGrab", "portal", "cli", "x11", "subprocess",
                          "PortalBackend", "X11Backend", "CliBackend"):
            self.assertNotIn(forbidden, imported,
                             f"selector.py must not be able to capture the screen "
                             f"(imports {forbidden})")

    def test_selector_never_calls_a_capture_function(self):
        source = pathlib.Path(SELECTOR_SRC).read_text()
        for forbidden in ("capture_region", "capture_fullscreen", "ImageGrab.grab"):
            self.assertNotIn(forbidden, source,
                             f"selector.py calls {forbidden} - the overlay race is back")


if __name__ == "__main__":
    unittest.main(verbosity=2)
