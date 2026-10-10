"""Black out parts of a picture before it is sent.

Drag rectangles over anything that should not reach the model - names, addresses, keys -
then Apply. The same editor as DAToolkit's (web/app.js, redactImage), in GTK: what is on
screen here is only a preview, and the black is painted into the picture's own pixels by
imaging.black_out, with box edges snapped outwards to whole pixels so no anti-aliased
border is left partly showing what was under it. Nothing of what was blacked out exists
in the image this returns, and it is re-encoded from pixels alone before it is sent.
"""

import math

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, Gtk

from .. import imaging
from ..capture.selector import pil_to_pixbuf

MAX_FIT_ZOOM = 4.0          # a small crop is enlarged to draw on, but not without limit
MIN_BOX = 2                 # image pixels; a click is not a box


class RedactDialog(Gtk.Window):
    """on_done(image, boxes) once: the picture with `boxes` - [(x, y, w, h)] in image
    pixels - painted black, flattened to RGB; or (None, []) if the user cancelled."""

    def __init__(self, parent, image, on_done, title="Black out anything sensitive",
                 apply_label="Apply"):
        super().__init__(title=title)
        self.image = image
        self.pixbuf = pil_to_pixbuf(image)
        self.on_done = on_done
        self.boxes = []
        self._drag = None               # (x0, y0, x1, y1) in image pixels, while dragging
        self._finished = False
        self.fit = True

        if parent is not None:
            self.set_transient_for(parent)
        self.set_modal(True)
        self.set_destroy_with_parent(True)
        self.set_default_size(980, 760)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)

        header = Gtk.HeaderBar(show_close_button=False, title=title)
        header.set_subtitle("Drag over what should not be sent")
        self.set_titlebar(header)
        cancel = Gtk.Button.new_with_label("Cancel")
        cancel.connect("clicked", lambda _w: self.cancel())
        header.pack_start(cancel)
        self.apply_button = Gtk.Button.new_with_label(apply_label)
        self.apply_button.get_style_context().add_class("suggested-action")
        self.apply_button.connect("clicked", lambda _w: self.apply())
        header.pack_end(self.apply_button)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(12)
        self.add(box)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=80, label=(
            "Blacked-out areas are replaced with black pixels before the picture leaves "
            "this window. Ctrl+Z undoes the last one."))
        hint.get_style_context().add_class("dim-label")
        row.pack_start(hint, True, True, 0)
        self.count = Gtk.Label(xalign=1)
        self.count.get_style_context().add_class("capture-meta")
        row.pack_start(self.count, False, False, 0)
        self.undo_button = Gtk.Button.new_with_label("Undo")
        self.undo_button.connect("clicked", lambda _w: self.undo())
        row.pack_start(self.undo_button, False, False, 0)
        self.clear_button = Gtk.Button.new_with_label("Clear")
        self.clear_button.connect("clicked", lambda _w: self.clear())
        row.pack_start(self.clear_button, False, False, 0)
        self.zoom_button = Gtk.Button.new_with_label("Actual size")
        self.zoom_button.connect("clicked", lambda _w: self.toggle_fit())
        row.pack_start(self.zoom_button, False, False, 0)
        box.pack_start(row, False, False, 0)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.get_style_context().add_class("preview-frame")
        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                             | Gdk.EventMask.BUTTON_RELEASE_MASK
                             | Gdk.EventMask.BUTTON1_MOTION_MASK)
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("realize", self._on_realize)
        self.scrolled.add(self.area)
        box.pack_start(self.scrolled, True, True, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda _w: self._finish(None))
        self._sync()
        self.show_all()
        self.present()

    # ---- the boxes ------------------------------------------------------------------

    def add_box(self, x0, y0, x1, y1):
        """Black out from one corner to the other, in image pixels: clamped to the
        picture, and snapped outwards to whole pixels. False if too small to be one."""
        width, height = self.image.size
        left = max(0, math.floor(min(x0, x1)))
        top = max(0, math.floor(min(y0, y1)))
        right = min(width, math.ceil(max(x0, x1)))
        bottom = min(height, math.ceil(max(y0, y1)))
        if right - left <= MIN_BOX or bottom - top <= MIN_BOX:
            return False
        self.boxes.append((left, top, right - left, bottom - top))
        self._sync()
        return True

    def undo(self):
        if self.boxes:
            self.boxes.pop()
            self._sync()

    def clear(self):
        self.boxes.clear()
        self._sync()

    def toggle_fit(self):
        self.fit = not self.fit
        self.zoom_button.set_label("Actual size" if self.fit else "Fit to window")
        self._sync()

    def _sync(self):
        n = len(self.boxes)
        self.count.set_text(f"{n} area{'s' if n != 1 else ''} blacked out" if n
                            else "Nothing blacked out yet")
        self.undo_button.set_sensitive(bool(n))
        self.clear_button.set_sensitive(bool(n))
        width, height = self.image.size
        if self.fit:
            self.area.set_size_request(-1, -1)
        else:
            self.area.set_size_request(width, height)
        self.area.queue_draw()

    # ---- finishing ------------------------------------------------------------------

    def apply(self):
        if self._finished:
            return
        self._drag = None
        self._finish(imaging.black_out(self.image, self.boxes))

    def cancel(self):
        self._finish(None)

    def _finish(self, image):
        if self._finished:
            return
        self._finished = True
        boxes = list(self.boxes) if image is not None else []
        self.destroy()
        self.on_done(image, boxes)

    def _on_key(self, _widget, event):
        control = event.state & Gdk.ModifierType.CONTROL_MASK
        if event.keyval == Gdk.KEY_Escape:
            self.cancel()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.apply()
            return True
        if control and event.keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self.undo()
            return True
        return False

    # ---- drawing and the mouse ------------------------------------------------------

    def _view(self):
        """(zoom, offset_x, offset_y): widget units per image pixel, and where the
        picture's corner is in the widget - centred when fitted."""
        width, height = self.image.size
        if not self.fit:
            return 1.0, 0.0, 0.0
        alloc = self.area.get_allocation()
        if alloc.width <= 1 or alloc.height <= 1:
            return 1.0, 0.0, 0.0
        zoom = min(alloc.width / width, alloc.height / height, MAX_FIT_ZOOM)
        return (zoom, (alloc.width - width * zoom) / 2.0,
                (alloc.height - height * zoom) / 2.0)

    def _to_image(self, x, y):
        zoom, ox, oy = self._view()
        width, height = self.image.size
        return (max(0.0, min(width, (x - ox) / zoom)),
                max(0.0, min(height, (y - oy) / zoom)))

    def _on_realize(self, area):
        area.get_window().set_cursor(
            Gdk.Cursor.new_from_name(area.get_display(), "crosshair"))

    def _on_press(self, _widget, event):
        if event.button != 1:
            return False
        x, y = self._to_image(event.x, event.y)
        self._drag = (x, y, x, y)
        return True

    def _on_motion(self, _widget, event):
        if self._drag is None:
            return False
        x, y = self._to_image(event.x, event.y)
        self._drag = (self._drag[0], self._drag[1], x, y)
        self.area.queue_draw()
        return True

    def _on_release(self, _widget, event):
        if event.button != 1 or self._drag is None:
            return False
        drag, self._drag = self._drag, None
        if not self.add_box(*drag):
            self.area.queue_draw()
        return True

    def _on_draw(self, _area, cr):
        zoom, ox, oy = self._view()
        cr.translate(ox, oy)
        cr.scale(zoom, zoom)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        cr.set_source_rgb(0, 0, 0)
        for x, y, w, h in self.boxes:
            cr.rectangle(x, y, w, h)
        cr.fill()
        if self._drag is not None:
            x0, y0, x1, y1 = self._drag
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            cr.fill_preserve()
            cr.set_source_rgb(0.90, 0.33, 0.29)
            cr.set_line_width(2.0 / zoom)
            cr.stroke()
        return False
