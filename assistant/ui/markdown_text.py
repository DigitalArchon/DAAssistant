"""Markdown -> a styled Gtk.TextBuffer, for the chat view without WebKit.

The WebKit view is not always available: a host whose webkit2gtk cannot be loaded into
this process leaves the plain-text view as the only one, permanently. It used to be
exactly that - one call to set_text() with every turn run together - which looked like a
log file rather than a conversation, and showed the raw prompt because it printed the
turn's `content` where the HTML view prints its `display`. See ui/bubbles.py for what
arranges these into a transcript.

This is not a second Markdown implementation. It is the small subset that decides whether
a transcript reads well: headings, emphasis, code, quotes and list bullets, rendered as
text tags rather than characters, so the result still follows the user's GTK theme in
both light and dark.
"""

import re

from gi.repository import Gdk, Gtk, Pango

from .markdown_html import URL, url_end

FENCE = re.compile(r"^\s*```(\w*)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
# Inline, in one pass so that a run of bold cannot be re-scanned for italics. Links
# come first, so the underscores and asterisks in a URL are not taken for emphasis.
# A link's text may hold escaped brackets - Ask Encyclopedia's citations are [\[2\]](...) -
# and besides the web's, it may go to the app's own addresses: enc:// for the encyclopedia,
# lookup: for a dictionary's cross-references. Those the window follows itself.
LINK = (r"\[((?:\\.|[^\]\\\n])+)\]\((" + URL + r"|enc://[^\s()]+|lookup:[^\s()]+)\)")
INLINE = re.compile(r"(" + LINK + r")|(" + URL + r")"
                    r"|(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(__[^_\n]+__)"
                    r"|(\*[^*\n]+\*)|(_[^_\n]+_)")
LINK_PREFIX = "link:"       # a link's own tag is named for its URL; see _link_tag
# Markdown's backslash escapes - \[ for a bracket that is only a bracket - which the
# WebKit view's renderer resolves and this one must too, outside code.
ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>|])")


def _mix(a, b, amount):
    """`a` moved `amount` of the way towards `b`, as an RGBA."""
    from gi.repository import Gdk

    out = Gdk.RGBA()
    out.red = a.red + (b.red - a.red) * amount
    out.green = a.green + (b.green - a.green) * amount
    out.blue = a.blue + (b.blue - a.blue) * amount
    out.alpha = 1.0
    return out


def _palette(widget):
    """Two tints taken from the theme in use, rather than colours of our own.

    The WebKit view gives each turn and each code block a panel a shade away from the
    page. Doing the same here needs a colour, and the only safe one is the theme's own
    text and background mixed a few percent - which lands correctly in a light theme, a
    dark theme and whatever the desktop's accent happens to be.
    """
    from gi.repository import Gdk

    context = widget.get_style_context() if widget is not None else None
    base, fg = Gdk.RGBA(), Gdk.RGBA()
    base.parse("#ffffff")
    fg.parse("#000000")
    if context is not None:
        for name, target in (("theme_base_color", base), ("theme_fg_color", fg)):
            found, colour = context.lookup_color(name)
            if found:
                target.red, target.green = colour.red, colour.green
                target.blue, target.alpha = colour.blue, 1.0
    return {"panel": _mix(base, fg, 0.05),
            "panel_strong": _mix(base, fg, 0.09),
            "dim": _mix(fg, base, 0.45)}


def _link_colour(widget):
    """The theme's link colour, as GtkLabel links use; a readable blue without one."""
    colour = Gdk.RGBA()
    colour.parse("#1a5fd0")
    context = widget.get_style_context() if widget is not None else None
    if context is not None:
        found, themed = context.lookup_color("link_color")
        if found:
            colour = themed
    return colour


def _link_tag(buffer, url):
    """A tag carrying `url` in its name, which is how a click finds where to go."""
    name = LINK_PREFIX + url
    tag = buffer.get_tag_table().lookup(name)
    return tag if tag is not None else buffer.create_tag(name)


def link_at(textview, x, y):
    """The URL under window point (x, y) of `textview`, or None."""
    bx, by = textview.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
    found, where = textview.get_iter_at_location(bx, by)
    if not found:
        return None
    for tag in where.get_tags():
        name = tag.get_property("name") or ""
        if name.startswith(LINK_PREFIX):
            return name[len(LINK_PREFIX):]
    return None


def install_tags(buffer, widget=None):
    """Define every tag once per buffer. Sizes are relative, so they follow the theme."""
    table = buffer.get_tag_table()
    if table.lookup("role") is not None:
        return
    colours = _palette(widget)

    def tag(name, **props):
        buffer.create_tag(name, **props)

    tag("role", weight=Pango.Weight.BOLD, scale=0.92,
        foreground_rgba=colours["dim"],
        pixels_above_lines=16, pixels_below_lines=6)
    tag("premium", weight=Pango.Weight.BOLD, scale=0.92, style=Pango.Style.ITALIC,
        foreground_rgba=colours["dim"],
        pixels_above_lines=16, pixels_below_lines=6)
    tag("h1", weight=Pango.Weight.BOLD, scale=1.35, pixels_above_lines=10)
    tag("h2", weight=Pango.Weight.BOLD, scale=1.2, pixels_above_lines=8)
    tag("h3", weight=Pango.Weight.BOLD, scale=1.1, pixels_above_lines=6)
    tag("bold", weight=Pango.Weight.BOLD)
    tag("italic", style=Pango.Style.ITALIC)
    tag("code", family="monospace", scale=0.95,
        background_rgba=colours["panel_strong"])
    tag("pre", family="monospace", scale=0.95, left_margin=14, right_margin=0,
        paragraph_background_rgba=colours["panel_strong"],
        pixels_above_lines=3, pixels_below_lines=3)
    tag("quote", style=Pango.Style.ITALIC, left_margin=14, right_margin=0,
        foreground_rgba=colours["dim"],
        paragraph_background_rgba=colours["panel"])
    # The user's own turns, panelled the way the WebKit view panels them.
    tag("said", paragraph_background_rgba=colours["panel"],
        left_margin=12, right_margin=12)
    tag("list", left_margin=14)
    tag("link", underline=Pango.Underline.SINGLE,
        foreground_rgba=_link_colour(widget))
    tag("dim", scale=0.92)
    tag("rule", scale=0.8)


def _insert(buffer, text, tags):
    if not text:
        return
    if tags:
        buffer.insert_with_tags_by_name(buffer.get_end_iter(), text, *tags)
    else:
        buffer.insert(buffer.get_end_iter(), text)


def _insert_link(buffer, label, url, tags):
    _insert(buffer, label, tags)
    end = buffer.get_end_iter()
    start = end.copy()
    start.backward_chars(len(label))
    buffer.apply_tag_by_name("link", start, end)
    buffer.apply_tag(_link_tag(buffer, url), start, end)


def _insert_inline(buffer, text, *tags):
    """Insert one line, turning links, `code`, **bold** and *italic* into tags.

    One pass over the line, so a run that has already been taken as bold cannot be
    scanned again for italics, as it would be by one regex substitution after another.
    """
    position = 0
    for match in INLINE.finditer(text):
        _insert(buffer, ESCAPED.sub(r"\1", text[position:match.start()]), tags)
        token = match.group(0)
        if match.group(1):
            label = re.sub(r"\\(.)", r"\1", match.group(2))
            _insert_link(buffer, label, match.group(3), tags)
            position = match.end()
            continue
        if match.group(4):
            url = url_end(token)
            _insert_link(buffer, url, url, tags)
            position = match.start() + len(url)
            continue
        if token.startswith("`"):
            inner, kind = token[1:-1], "code"
        elif token.startswith("**") or token.startswith("__"):
            inner, kind = token[2:-2], "bold"
        else:
            inner, kind = token[1:-1], "italic"
        _insert(buffer, inner if kind == "code" else ESCAPED.sub(r"\1", inner),
                (kind,) + tags)
        position = match.end()
    _insert(buffer, ESCAPED.sub(r"\1", text[position:]), tags)


def _insert_markdown(buffer, text, panel=None):
    """`panel` tags every line, which is how a whole turn gets one background."""
    extra = (panel,) if panel else ()
    in_fence = False
    for line in (text or "").splitlines():
        fence = FENCE.match(line)
        if fence:
            in_fence = not in_fence
            continue
        if in_fence:
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), line + "\n",
                                            "pre", *extra)
            continue

        heading = HEADING.match(line)
        if heading:
            level = min(len(heading.group(1)), 3)
            _insert_inline(buffer, heading.group(2) + "\n", f"h{level}", *extra)
            continue
        if RULE.match(line):
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), "─" * 24 + "\n",
                                            "rule", *extra)
            continue
        quote = QUOTE.match(line)
        if quote:
            _insert_inline(buffer, quote.group(1) + "\n", "quote", *extra)
            continue
        bullet = BULLET.match(line)
        if bullet:
            _insert_inline(buffer, f"{bullet.group(1)}• {bullet.group(2)}\n",
                           "list", *extra)
            continue
        numbered = NUMBERED.match(line)
        if numbered:
            _insert_inline(
                buffer, f"{numbered.group(1)}{numbered.group(2)}. {numbered.group(3)}\n",
                "list", *extra)
            continue
        _insert_inline(buffer, line + "\n", *extra)


def render_body(buffer, text, widget=None):
    """One turn's Markdown, with no role label and no panel of its own.

    What a bubble contains: the bubble itself carries the shape and the background.
    """
    install_tags(buffer, widget)
    buffer.set_text("")
    _insert_markdown(buffer, text)
    # Markdown ends every line, including the last; the trailing blank line would
    # otherwise sit inside the bubble as an empty row.
    end = buffer.get_end_iter()
    if end.ends_line() and end.get_line() > 0:
        start = end.copy()
        start.backward_char()
        if start.get_char() == "\n":
            buffer.delete(start, end)


def configure(textview, on_link=None):
    """Margins and wrapping that make a transcript readable in a bare TextView.

    `on_link(url)` is offered every link clicked first, and returns True for one it has
    followed: the app's own addresses. The web's go to the browser."""
    textview.on_link = on_link
    textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    textview.set_editable(False)
    textview.set_cursor_visible(False)
    textview.set_left_margin(16)
    textview.set_right_margin(16)
    textview.set_top_margin(10)
    textview.set_bottom_margin(10)
    textview.set_pixels_below_lines(2)
    textview.connect("button-release-event", _on_link_click)
    textview.connect("motion-notify-event", _on_link_hover)


def _on_link_click(textview, event):
    if event.button != Gdk.BUTTON_PRIMARY:
        return False
    if textview.get_buffer().get_has_selection():
        return False            # the end of a drag that selected text, not a click
    url = link_at(textview, event.x, event.y)
    if not url:
        return False
    handler = getattr(textview, "on_link", None)
    if handler is not None and handler(url):
        return False
    if url.startswith(("http://", "https://", "mailto:")):
        Gtk.show_uri_on_window(None, url, event.time)
    return False


def _on_link_hover(textview, event):
    window = textview.get_window(Gtk.TextWindowType.TEXT)
    if window is not None:
        name = "pointer" if link_at(textview, event.x, event.y) else "text"
        window.set_cursor(Gdk.Cursor.new_from_name(window.get_display(), name))
    return False
