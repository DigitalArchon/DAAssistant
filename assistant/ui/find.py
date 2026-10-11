"""Find in a window: Ctrl+F, as in a browser or an editor.

The bar sits under the header and searches one view at a time - the conversation, or in
Search Encyclopedia the article being read when that is where the user was. Every match
is marked, Enter and the arrows go from one to the next, Escape closes it and takes the
marks away.

A view is searched by a finder: WebFinder for a WebKit view, which does its own marking
through its find controller, and TextFinder for the plain-text views used without
WebKit - one Gtk.TextView or several, as the bubbles are - which marks with text tags.
"""

from .. import gtkcompat
from gi.repository import Gdk, Gtk

if gtkcompat.has_webkit():
    from gi.repository import WebKit2
else:  # pragma: no cover
    WebKit2 = None

# More than this many matches is shown as "999+": WebKit stops counting there.
MOST_COUNTED = 999


class FindBar(Gtk.SearchBar):
    def __init__(self, on_close=None):
        """`on_close()`: when the bar is closed, for the window to put focus back."""
        super().__init__(show_close_button=True)
        self.on_close = on_close
        self.finder = None

        self.entry = Gtk.SearchEntry(width_chars=32)
        self.entry.connect("search-changed", lambda _e: self._search())
        self.entry.connect("activate", lambda _e: self.step(1))
        self.entry.connect("next-match", lambda _e: self.step(1))
        self.entry.connect("previous-match", lambda _e: self.step(-1))
        self.entry.connect("stop-search", lambda _e: self.close())
        self.entry.connect("key-press-event", self._on_key)

        steps = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        steps.get_style_context().add_class("linked")
        for icon, step, tip in (("go-up-symbolic", -1, "Previous match (Shift+Enter)"),
                                ("go-down-symbolic", 1, "Next match (Enter)")):
            button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            button.set_tooltip_text(tip)
            button.connect("clicked", lambda _w, s=step: self.step(s))
            steps.pack_start(button, False, False, 0)

        self.count = Gtk.Label(width_chars=12, xalign=0)
        self.count.get_style_context().add_class("dim-label")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(self.entry, False, False, 0)
        box.pack_start(steps, False, False, 0)
        box.pack_start(self.count, False, False, 0)
        self.add(box)
        self.connect_entry(self.entry)
        self.connect("notify::search-mode-enabled", self._on_mode)

    def open(self, finder):
        """Show the bar searching with `finder`, the text in it chosen to type over."""
        if finder is not self.finder:
            if self.finder is not None:
                self.finder.clear()
            self.finder = finder
            finder.on_result = self._show_count
        self.entry.set_placeholder_text(finder.placeholder)
        self.set_search_mode(True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)
        if self.entry.get_text():
            self._search()

    def close(self):
        self.set_search_mode(False)

    def is_open(self):
        return self.get_search_mode()

    def step(self, step):
        """To the next match (1) or the one before (-1)."""
        if self.finder is not None and self.entry.get_text():
            self.finder.step(self.entry.get_text(), step)

    def _search(self):
        if self.finder is None:
            return
        text = self.entry.get_text()
        if text:
            self.finder.search(text)
        else:
            self.finder.clear()
            self._show_count(None)

    def _show_count(self, found, current=None):
        """`found`: how many matches, or None for no search; `current`: which one is
        shown, counted from 1, when the finder knows."""
        context = self.entry.get_style_context()
        if found == 0:
            context.add_class("error")
        else:
            context.remove_class("error")
        if found is None:
            self.count.set_text("")
        elif found == 0:
            self.count.set_text("No matches")
        elif found > MOST_COUNTED:
            self.count.set_text(f"{MOST_COUNTED}+ matches")
        elif current:
            self.count.set_text(f"{current} of {found}")
        else:
            self.count.set_text(f"{found} match{'es' if found != 1 else ''}")

    def _on_key(self, _entry, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and \
                event.state & Gdk.ModifierType.SHIFT_MASK:
            self.step(-1)
            return True
        return False

    def _on_mode(self, _bar, _param):
        if self.get_search_mode():
            return
        if self.finder is not None:
            self.finder.clear()
        self._show_count(None)
        if self.on_close:
            self.on_close()


class WebFinder:
    """Finds in a WebKit view, with its find controller: WebKit marks every match and
    scrolls to the one it is on, and counts them."""

    OPTIONS = ((WebKit2.FindOptions.CASE_INSENSITIVE | WebKit2.FindOptions.WRAP_AROUND)
               if WebKit2 is not None else 0)

    def __init__(self, webview, placeholder):
        self.placeholder = placeholder
        self.on_result = None
        self.controller = webview.get_find_controller()
        self._text = None
        self.controller.connect("counted-matches",
                                lambda _c, n: self._report(n))
        self.controller.connect("failed-to-find-text", lambda _c: self._report(0))

    def _report(self, found):
        if self.on_result is not None and self._text is not None:
            self.on_result(int(found))

    def search(self, text):
        self._text = text
        self.controller.search(text, self.OPTIONS, MOST_COUNTED)
        self.controller.count_matches(text, self.OPTIONS, MOST_COUNTED)

    def step(self, text, step):
        if text != self._text:
            self.search(text)
        elif step > 0:
            self.controller.search_next()
        else:
            self.controller.search_previous()

    def clear(self):
        self._text = None
        self.controller.search_finish()


class TextFinder:
    """Finds in text views - `views()` gives them, in order, as they are now - marking
    every match with a tag and the one it is on with another, scrolled into sight.

    Each search starts again from the text as it is, so a turn redrawn since the last -
    an answer still streaming in - is searched as it now reads."""

    def __init__(self, views, placeholder):
        self.views = views
        self.placeholder = placeholder
        self.on_result = None
        self.current = -1

    def search(self, text):
        self.current = -1
        self.step(text, 1)

    def step(self, text, step):
        matches = self._mark(text)
        if not matches:
            self.current = -1
            self.on_result(0)
            return
        self.current = (self.current + step) % len(matches) if self.current >= 0 \
            else (0 if step > 0 else len(matches) - 1)
        view, start, end = matches[self.current]
        buffer = view.get_buffer()
        buffer.apply_tag(_tag(buffer, "find-current"), start, end)
        _reveal(view, start)
        self.on_result(len(matches), self.current + 1)

    def clear(self):
        self.current = -1
        self._unmark()

    def _unmark(self):
        for view in self.views():
            buffer = view.get_buffer()
            for name in ("find-match", "find-current"):
                tag = buffer.get_tag_table().lookup(name)
                if tag is not None:
                    buffer.remove_tag(tag, buffer.get_start_iter(), buffer.get_end_iter())

    def _mark(self, text):
        """Every match of `text`, case aside, marked: [(view, start, end)]."""
        self._unmark()
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE | Gtk.TextSearchFlags.TEXT_ONLY
        matches = []
        for view in self.views():
            buffer = view.get_buffer()
            where = buffer.get_start_iter()
            while True:
                found = where.forward_search(text, flags, None)
                if not found:
                    break
                start, end = found
                buffer.apply_tag(_tag(buffer, "find-match"), start, end)
                matches.append((view, start, end))
                where = end
        return matches


def _tag(buffer, name):
    """The find tags, as a browser marks its matches: whatever the theme, dark text on
    yellow, and on orange for the one it is on."""
    table = buffer.get_tag_table()
    tag = table.lookup(name)
    if tag is None:
        tag = buffer.create_tag(name, foreground="#000000",
                                background="#f5a623" if name == "find-current"
                                else "#fce94f")
    # Above every other tag - those a redrawn turn has made since too - so a match in a
    # link or in code is still marked, and the current one over the rest.
    tag.set_priority(table.get_size() - 1)
    return tag


def _reveal(view, where):
    """Scroll `where`, in `view`, into sight: by the view itself when it is what scrolls,
    else by the scrolled window it sits in, as a bubble's view does among the others."""
    scrolled = view.get_ancestor(Gtk.ScrolledWindow)
    if scrolled is None:
        return
    if scrolled.get_child() is view:
        view.scroll_to_iter(where, 0.0, True, 0.0, 0.4)
        return
    content = scrolled.get_child()
    if isinstance(content, Gtk.Viewport):
        content = content.get_child()
    place = view.get_iter_location(where)
    x, y = view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, place.x, place.y)
    translated = view.translate_coordinates(content, x, y)
    if not translated:
        return
    y = translated[-1]
    adjustment = scrolled.get_vadjustment()
    page = adjustment.get_page_size()
    adjustment.set_value(max(0, min(y - page * 0.4, adjustment.get_upper() - page)))
