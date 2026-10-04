"""Screenshot backend interface.

Every backend is asynchronous: select_region(on_done, on_error) eventually calls
on_done(PIL.Image) on the GTK main thread, or on_error(str).

Uniformly async, so that no caller has to know which backends return at once and which
need a selector first.
"""

from abc import ABC, abstractmethod


class CaptureError(Exception):
    pass


class ScreenshotBackend(ABC):
    name = "base"

    @abstractmethod
    def is_available(self):
        """Can this backend run right now on this machine?"""

    @abstractmethod
    def select_region(self, on_done, on_error):
        """Let the user pick a region, then deliver a PIL image to on_done."""

    def capture_fullscreen(self):
        """Grab every monitor as one PIL image. May raise CaptureError."""
        raise CaptureError(f"{self.name} cannot capture fullscreen")

    def supports_fullscreen(self):
        try:
            return type(self).capture_fullscreen is not ScreenshotBackend.capture_fullscreen
        except AttributeError:
            return False
