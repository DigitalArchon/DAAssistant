"""Crop-from-a-frozen-image selection UI.

This is what keeps OCR reliable, on X11 too.

The obvious design draws a translucent overlay, and on button-release calls
self.destroy() and then immediately grabs the screen. GTK destroy is asynchronous, so the
compositor has usually not repainted yet and the grab captures the overlay itself: a 30%
black wash plus the red selection border, baked into the image sent to the model. Being a
race, it fails intermittently, which makes it hard to pin down.

Here the pixels are captured ONCE, before any UI exists. The overlay only ever displays
that frozen buffer, and the crop is taken from the same in-memory image. There is nothing
left to race, and no overlay pixel can possibly reach the model.

Two presentations share the crop maths:
  FrozenOverlay - fullscreen, used on X11 where a client can position a window over
                  every monitor.
  CropDialog    - an ordinary resizable window, used as the universal Wayland fallback
                  where clients cannot place themselves at absolute coordinates.
"""

from .. import gtkcompat  # noqa: F401  (pins GI versions - must come first)
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from .. import imaging

MIN_SELECTION = 8   # px, in image space; below this a drag is treated as a stray click


def pil_to_pixbuf(img):
    """`img` to draw: as it is sent (imaging.flatten), a 16-bit grey's values scaled to
    eight bits and transparency on white. convert("RGB") clipped a 16-bit picture at
    255, so a scan pasted from the clipboard showed blank white in the review and in the
    black-out editor - nothing there to black out, in a picture sent with its greys."""
    img = imaging.flatten(img)
    w, h = img.size
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(img.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)


def _normalise(x0, y0, x1, y1):
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)


class _SelectionMixin:
    """Drag state plus the widget-space -> image-space mapping."""

    def _init_selection(self, image):
        self.image = image
        self.pixbuf = pil_to_pixbuf(image)
        self.start = None
        self.end = None
        self._dragging = False
        self._finished = False

    def _scale(self):
        """(widget units per image pixel, x offset, y offset): how the image is drawn,
        and so how a selection maps back onto it.

        The image is in device pixels and GTK lays out in logical ones, so on HiDPI the
        two differ - an X11 grab at scale factor 2 is twice the overlay's size. Both
        presentations draw the whole image at this ratio: the crop window centred in
        itself, the overlay from its own corner, as the screen it stands in for. Drawn
        at 1.0, as the overlay once was, a scale-2 screen showed its top-left quarter
        at twice the size, and the rest could not be selected at all.
        """
        alloc = self.area.get_allocation()
        if alloc.width <= 0 or alloc.height <= 0:
            return 1.0, 0.0, 0.0
        iw, ih = self.image.size
        scale = min(alloc.width / iw, alloc.height / ih)
        if not self.fit:
            return scale, 0.0, 0.0
        return scale, (alloc.width - iw * scale) / 2.0, (alloc.height - ih * scale) / 2.0

    def _to_image_rect(self):
        if self.start is None or self.end is None:
            return None
        scale, ox, oy = self._scale()
        if scale <= 0:
            return None
        x, y, w, h = _normalise(*self.start, *self.end)
        ix, iy = int(round((x - ox) / scale)), int(round((y - oy) / scale))
        iw, ih = int(round(w / scale)), int(round(h / scale))
        # Clamp to the image; a drag that runs off the edge should still work.
        img_w, img_h = self.image.size
        ix, iy = max(0, min(ix, img_w)), max(0, min(iy, img_h))
        iw, ih = max(0, min(iw, img_w - ix)), max(0, min(ih, img_h - iy))
        return ix, iy, iw, ih

    def _on_press(self, _widget, event):
        if event.button != 1:
            return False
        self.start = (event.x, event.y)
        self.end = (event.x, event.y)
        self._dragging = True
        return True

    def _on_motion(self, _widget, event):
        """The corner being dragged follows the pointer - only while the button is held.

        The area asks for every motion, held or not, and a selection made in the crop
        window went on following the pointer after the button was let go: on its way to
        "Use selection" the rectangle stretched to the area's edge, and that was what
        was sent."""
        if self._dragging:
            self.end = (event.x, event.y)
            self.area.queue_draw()
        return True

    def _end_drag(self, event):
        """The button was let go: the selection ends where it was released. False if no
        drag was under way."""
        if event.button != 1 or not self._dragging:
            return False
        self._dragging = False
        self.end = (event.x, event.y)
        return True

    def _draw_selection(self, cr, dim=0.45):
        scale, ox, oy = self._scale()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        cr.restore()

        alloc = self.area.get_allocation()
        rect = None
        if self.start is not None and self.end is not None:
            rect = _normalise(*self.start, *self.end)

        # Dim everything, then punch the selection back to full brightness. The dimming
        # is a display effect only; the crop comes from self.image, never from the screen.
        cr.set_source_rgba(0, 0, 0, dim)
        if rect and rect[2] > 0 and rect[3] > 0:
            x, y, w, h = rect
            cr.rectangle(0, 0, alloc.width, alloc.height)
            cr.rectangle(x + w, y, -w, h)   # reverse winding -> hole
            cr.set_fill_rule(1)             # EVEN_ODD
            cr.fill()

            cr.set_source_rgba(0.30, 0.65, 1.0, 0.95)
            cr.set_line_width(1.5)
            cr.rectangle(x, y, w, h)
            cr.stroke()

            crop = self._to_image_rect()
            if crop and crop[2] and crop[3]:
                self._draw_badge(cr, x, y, f"{crop[2]} x {crop[3]}")
        else:
            cr.rectangle(0, 0, alloc.width, alloc.height)
            cr.fill()
            self._draw_badge(cr, 12, 12, "Drag to select a region    Esc to cancel")

    @staticmethod
    def _draw_badge(cr, x, y, text):
        cr.select_font_face("Sans")
        cr.set_font_size(13)
        extents = cr.text_extents(text)
        pad, bh = 8, 24
        by = y - bh - 6 if y - bh - 6 > 0 else y + 6
        cr.set_source_rgba(0, 0, 0, 0.78)
        cr.rectangle(x, by, extents.width + pad * 2, bh)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(x + pad, by + bh - 7)
        cr.show_text(text)

    def _finish(self, cancelled=False):
        if self._finished:
            return
        self._finished = True
        rect = None if cancelled else self._to_image_rect()
        self.destroy()
        if cancelled or not rect or rect[2] < MIN_SELECTION or rect[3] < MIN_SELECTION:
            self.on_error("Cancelled")
            return
        x, y, w, h = rect
        self.on_done(self.image.crop((x, y, x + w, y + h)))


class FrozenOverlay(Gtk.Window, _SelectionMixin):
    """Fullscreen overlay showing the already-captured screen. X11 path."""

    fit = False

    # How often the grab is tried again while another client holds the keyboard, and
    # how many times in all: a second. See _grab.
    GRAB_RETRY_MS = 50
    GRAB_ATTEMPTS = 20

    def __init__(self, image, geometry, on_done, on_error):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.on_done, self.on_error = on_done, on_error
        self._init_selection(image)
        self._grab_attempts = 0
        self._grab_retry = None             # the timeout trying the grab again

        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.move(geometry["x"], geometry["y"])
        self.set_default_size(geometry["width"], geometry["height"])
        self.resize(geometry["width"], geometry["height"])

        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self._on_draw)
        self.area.set_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                             Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.add(self.area)

        self.connect("key-press-event", self._on_key)
        self.show_all()
        self.present()

        # A POPUP window gets no focus from the WM, so grab input explicitly or Esc and
        # the drag would go to whatever was focused underneath.
        GLib.idle_add(self._grab)

    @staticmethod
    def _seat():
        return Gdk.Display.get_default().get_default_seat()

    def _grab(self):
        """Take the pointer and the keyboard - again a little later while another client
        holds them, for up to GRAB_ATTEMPTS tries.

        Started by the app's own X11 shortcut, the overlay opens while that shortcut's
        keys are still down, and X keeps the keyboard with the key grab until they are
        let go: a grab asked for in the meantime is refused (AlreadyGrabbed). It was
        asked for once, and its answer never looked at, so Esc went to whatever had
        focus underneath and the overlay could not be cancelled from the keyboard."""
        self._grab_retry = None
        window = self.get_window()
        if self._finished or window is None:
            return False
        if self._grab_attempts == 0:
            cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "crosshair")
            if cursor:
                window.set_cursor(cursor)
        self._grab_attempts += 1
        status = self._seat().grab(window, Gdk.SeatCapabilities.ALL, True, None, None,
                                   None, None)
        if status == Gdk.GrabStatus.SUCCESS:
            return False
        if self._grab_attempts < self.GRAB_ATTEMPTS:
            self._grab_retry = GLib.timeout_add(self.GRAB_RETRY_MS, self._grab)
        else:
            print(f"[capture] the overlay could not take the keyboard "
                  f"({status.value_nick}): Esc may not reach it")
        return False

    def _ungrab(self):
        if self._grab_retry is not None:
            GLib.source_remove(self._grab_retry)
            self._grab_retry = None
        self._seat().ungrab()

    def _on_draw(self, _widget, cr):
        self._draw_selection(cr, dim=0.45)
        return False

    def _on_release(self, _widget, event):
        if not self._end_drag(event):
            return False
        self._ungrab()
        self._finish()
        return True

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self._ungrab()
            self._finish(cancelled=True)
            return True
        return False


class CropDialog(Gtk.Window, _SelectionMixin):
    """Ordinary window for cropping a fullscreen grab.

    Universal Wayland fallback: needs no absolute positioning, no layer-shell and no
    external tooling, so it works on any compositor. Also reachable deliberately when a
    capture needs re-cropping.
    """

    fit = True

    def __init__(self, image, on_done, on_error, title="Select region"):
        super().__init__(title=title)
        self.on_done, self.on_error = on_done, on_error
        self._init_selection(image)

        self.set_default_size(1000, 700)
        self.set_position(Gtk.WindowPosition.CENTER)

        header = Gtk.HeaderBar(show_close_button=True, title=title)
        header.set_subtitle("Drag to choose the area to send")
        self.set_titlebar(header)

        cancel = Gtk.Button.new_with_label("Cancel")
        cancel.connect("clicked", lambda _w: self._finish(cancelled=True))
        header.pack_start(cancel)

        self.use_button = Gtk.Button.new_with_mnemonic("_Use selection")
        self.use_button.get_style_context().add_class("suggested-action")
        self.use_button.set_sensitive(False)
        self.use_button.connect("clicked", lambda _w: self._finish())
        header.pack_end(self.use_button)

        self.whole_button = Gtk.Button.new_with_mnemonic("Use _whole image")
        self.whole_button.connect("clicked", self._on_whole)
        header.pack_end(self.whole_button)

        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self._on_draw)
        self.area.set_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                             Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.add(self.area)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)
        self.show_all()
        self.present()

    def _on_draw(self, _widget, cr):
        style = self.get_style_context()
        bg = style.get_background_color(Gtk.StateFlags.NORMAL)
        cr.set_source_rgb(bg.red, bg.green, bg.blue)
        cr.paint()
        self._draw_selection(cr, dim=0.5)
        return False

    def _on_release(self, _widget, event):
        if not self._end_drag(event):
            return False
        rect = self._to_image_rect()
        self.use_button.set_sensitive(
            bool(rect and rect[2] >= MIN_SELECTION and rect[3] >= MIN_SELECTION))
        self.area.queue_draw()
        return True

    def _on_whole(self, _widget):
        if self._finished:
            return
        self._finished = True
        image = self.image
        self.destroy()
        self.on_done(image)

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self._finish(cancelled=True)
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.use_button.get_sensitive():
            self._finish()
            return True
        return False

    def _on_delete(self, _widget, _event):
        if not self._finished:
            self._finished = True
            self.on_error("Cancelled")
        return False
