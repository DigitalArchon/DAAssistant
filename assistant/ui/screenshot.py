"""The capture a conversation is about: its thumbnail and its full-size view.

Everything here works from the PNG bytes in memory. Privacy Mode's rule is that nothing
reaches the disk unless the user saves it, so viewing never hands a temporary file to
an image viewer: the picture is decoded into a pixbuf and GTK draws it.

A capture is in device pixels, so on a HiDPI display it is drawn through a cairo surface
at the window's scale - one pixel of the capture to one pixel of the screen - rather
than as a pixbuf, which GTK would double.
"""

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, GdkPixbuf, Gtk

THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT = 560, 220     # logical pixels


def load(png):
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(png)
    loader.close()
    return loader.get_pixbuf()


def picture(pixbuf, scale, max_width=None, max_height=None):
    """A Gtk.Image of `pixbuf` at `scale` device pixels per logical one, shrunk to fit
    max_width x max_height logical pixels if given."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if max_width and max_height:
        fit = min(max_width * scale / w, max_height * scale / h)
        if fit < 1:
            pixbuf = pixbuf.scale_simple(max(1, round(w * fit)), max(1, round(h * fit)),
                                         GdkPixbuf.InterpType.BILINEAR)
    surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, scale, None)
    return Gtk.Image.new_from_surface(surface)


class ScreenshotViewer(Gtk.Window):
    """The capture at full size, scrolling if it is larger than the screen allows."""

    def __init__(self, parent, png, title, on_save):
        super().__init__(title=f"Screenshot — {title}")
        self.set_transient_for(parent)
        self.set_destroy_with_parent(True)

        header = Gtk.HeaderBar(show_close_button=True, title="Screenshot", subtitle=title)
        save = Gtk.Button.new_from_icon_name("document-save-symbolic", Gtk.IconSize.BUTTON)
        save.set_tooltip_text("Save the screenshot as a PNG file")
        save.connect("clicked", lambda _w: on_save(self))
        header.pack_end(save)
        self.set_titlebar(header)

        scale = parent.get_scale_factor() if parent is not None else 1
        pixbuf = load(png)
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(picture(pixbuf, scale))
        self.add(scrolled)

        # As large as the picture, within most of the screen it opens on.
        width, height = pixbuf.get_width() / scale, pixbuf.get_height() / scale
        area = self._workarea(parent)
        if area is not None:
            width = min(width, area.width * 0.9)
            height = min(height, area.height * 0.85)
        self.set_default_size(max(240, round(width) + 4), max(160, round(height) + 4))
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)

        self.connect("key-press-event", self._on_key)
        self.show_all()
        self.present()

    @staticmethod
    def _workarea(parent):
        display = Gdk.Display.get_default()
        if display is None:
            return None
        window = parent.get_window() if parent is not None else None
        monitor = (display.get_monitor_at_window(window) if window is not None
                   else display.get_primary_monitor() or display.get_monitor(0))
        return monitor.get_workarea() if monitor is not None else None

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False
