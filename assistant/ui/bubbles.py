"""The transcript as bubbles, for the chat window without WebKit.

The WebKit view draws each turn as a rounded panel with a small role label above it,
the user's turns tinted towards the selection colour. This is the same layout built
from widgets: one Gtk.TextView per turn inside a box that carries the rounding and the
background, so the Markdown renderer's text tags still do the work inside each bubble
while GTK's CSS does the shape.

Colours come from the theme by name - @theme_base_color, @theme_fg_color,
@theme_selected_bg_color - mixed in ui/style.css, so a bubble is right in a light
theme, a dark theme and whatever accent the desktop uses.

Turns are rebuilt in place rather than from scratch: a streaming answer redraws several
times a second, and replacing the whole widget tree each time would throw away the
user's selection and scroll position with it.
"""

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, Gtk

from . import markdown_text, screenshot as screenshot_view

LABELS = {"user": "YOU", "assistant": "ASSISTANT", "lookup": "LOOK UP",
          "search": "ENCYCLOPEDIA", "article": "ENCYCLOPEDIA ARTICLE",
          "sources": "ENCYCLOPEDIA SOURCES"}


class BubbleView(Gtk.ScrolledWindow):
    def __init__(self, on_link=None):
        """`on_link(url)`: offered every link clicked; see markdown_text.configure."""
        super().__init__()
        self.on_link = on_link
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.box.set_margin_start(14)
        self.box.set_margin_end(14)
        self.box.set_margin_top(12)
        self.box.set_margin_bottom(12)
        self.add(self.box)
        self._rows = []          # [{role, label, bubble, view, text}]

    # ---- building ---------------------------------------------------------

    def set_screenshot(self, pixbuf, scale, on_view, on_save):
        """The capture above the first turn: a thumbnail, which opens it full size,
        and View and Save - as the WebKit view has them."""
        label = Gtk.Label(label="SCREENSHOT", xalign=0.0)
        label.get_style_context().add_class("turn-role")
        label.set_margin_top(10)
        label.set_margin_bottom(3)

        thumbnail = Gtk.EventBox()
        thumbnail.add(screenshot_view.picture(pixbuf, scale,
                                              screenshot_view.THUMBNAIL_WIDTH,
                                              screenshot_view.THUMBNAIL_HEIGHT))
        thumbnail.set_halign(Gtk.Align.START)
        thumbnail.set_tooltip_text("View full size")
        thumbnail.connect("button-release-event",
                          lambda _w, event: event.button == Gdk.BUTTON_PRIMARY
                          and (on_view() or True))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_top(6)
        for text, action in (("View", on_view), ("Save…", on_save)):
            button = Gtk.Button.new_with_label(text)
            button.connect("clicked", lambda _w, act=action: act())
            actions.pack_start(button, False, False, 0)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.set_margin_bottom(8)
        for part in (label, thumbnail, actions):
            panel.pack_start(part, False, False, 0)
        self.box.pack_start(panel, False, False, 0)
        self.box.reorder_child(panel, 0)
        panel.show_all()

    def _new_row(self, role, premium, privacy=None):
        # `premium` is the tag after the role: the answering model's short name, or
        # PREMIUM for a turn from before answers recorded their model.
        label = Gtk.Label(xalign=0.0)
        tag = premium if isinstance(premium, str) else ("PREMIUM" if premium else "")
        label.set_text(LABELS.get(role, role.upper()) + (f"  ·  {tag}" if tag else ""))
        label.get_style_context().add_class("turn-role")

        # The role and, in a private conversation, what was proved about this turn -
        # the same badge the WebKit view shows, coloured by the same tone.
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heading.set_margin_top(10)
        heading.set_margin_bottom(3)
        heading.pack_start(label, False, False, 0)
        if privacy:
            badge = Gtk.Label(label=privacy.get("label", ""), xalign=0.0)
            badge.set_tooltip_text(privacy.get("tip") or None)
            badge.get_style_context().add_class("privacy-badge")
            badge.get_style_context().add_class(
                f"privacy-{privacy.get('tone') or 'pending'}")
            heading.pack_start(badge, False, False, 0)

        view = Gtk.TextView()
        markdown_text.configure(view, self.on_link)
        # The bubble supplies the padding; the view keeps two pixels at the bottom so
        # a final line with a paragraph background is not clipped by a rounded corner.
        view.set_left_margin(0)
        view.set_right_margin(0)
        view.set_top_margin(0)
        view.set_bottom_margin(4)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bubble.get_style_context().add_class("chat-bubble")
        bubble.get_style_context().add_class(f"chat-{role}")
        if privacy:
            bubble.get_style_context().add_class("chat-private")
        bubble.add(view)

        self.box.pack_start(heading, False, False, 0)
        self.box.pack_start(bubble, False, False, 0)
        return {"role": role, "premium": premium, "privacy": privacy, "label": label,
                "heading": heading, "bubble": bubble, "view": view, "text": None}

    def _drop_from(self, index):
        for row in self._rows[index:]:
            row["heading"].destroy()
            row["bubble"].destroy()
        del self._rows[index:]

    def set_turns(self, turns):
        """Show exactly these turns, reusing the rows that already match."""
        for index, turn in enumerate(turns):
            role = turn.get("role", "assistant")
            premium = bool(turn.get("is_premium"))
            if turn.get("model"):
                premium = turn["model"].rsplit("/", 1)[-1] + (
                    " · earlier answer" if turn.get("superseded") else "")
            privacy = turn.get("privacy") or None
            text = turn.get("display", turn.get("content", "")) or ""
            if turn.get("_streaming"):
                text += " ▍"

            if index < len(self._rows):
                row = self._rows[index]
                if row["role"] != role or row["premium"] != premium \
                        or row["privacy"] != privacy:
                    self._drop_from(index)          # the shape changed; rebuild the tail
                elif row["text"] == text:
                    continue                        # nothing to do for this one
                else:
                    markdown_text.render_body(row["view"].get_buffer(), text,
                                              widget=row["view"])
                    row["text"] = text
                    continue
            row = self._new_row(role, premium, privacy)
            markdown_text.render_body(row["view"].get_buffer(), text, widget=row["view"])
            row["text"] = text
            self._rows.append(row)

        self._drop_from(len(turns))
        self.box.show_all()

    # ---- helpers ----------------------------------------------------------

    def scroll_to_turn(self, index):
        """Put turn `index`'s heading at the top of the view."""
        adjustment = self.get_vadjustment()
        if adjustment is None or not 0 <= index < len(self._rows):
            return
        y = self._rows[index]["heading"].get_allocation().y
        adjustment.set_value(min(max(y, 0), adjustment.get_upper()
                                 - adjustment.get_page_size()))

    def scroll_to_end(self):
        adjustment = self.get_vadjustment()
        if adjustment is not None:
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

    def transcript_text(self):
        """Everything on screen, in order - the labels included."""
        parts = []
        for row in self._rows:
            buffer = row["view"].get_buffer()
            heading = row["label"].get_text()
            if row["privacy"]:
                heading += f"  {row['privacy'].get('label', '')}"
            parts.append(heading)
            parts.append(buffer.get_text(buffer.get_start_iter(),
                                         buffer.get_end_iter(), False))
        return "\n".join(parts)
