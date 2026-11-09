"""Reading the encyclopedia: the article pane, and the Flash window.

The pane is where an article is read as the DVD presented it - every section, every
picture with its caption, the films playable in place, links to other articles
followed within it, with history for Back and Forward. A picture or film opens on a
page of its own, as it did on the DVD; an animation opens in a window of its own,
played by Ruffle. Search Encyclopedia is built around it.
"""

import collections
import html
import os
import pathlib
import re

from .. import errors, gtkcompat, webkitguard
from gi.repository import Gdk, Gtk

from ..reference import encyclopedia_html
from ..reference.encyclopedia import citation_of, text_of
from . import encscheme, lookups, webpolicy

if gtkcompat.has_webkit():
    from gi.repository import WebKit2
else:  # pragma: no cover
    WebKit2 = None


def _is_dark():
    settings = Gtk.Settings.get_default()
    if settings and settings.get_property("gtk-application-prefer-dark-theme"):
        return True
    theme = (settings.get_property("gtk-theme-name") or "").lower() if settings else ""
    return "dark" in theme


def target_of(uri):
    """("article" | "assembly" | "film", id) for a viewer address, ("flash", name) for
    an animation, or None."""
    for kind in ("article", "assembly", "film"):
        prefix = f"enc://{kind}/"
        if uri.startswith(prefix) and uri[len(prefix):].isdigit():
            return kind, int(uri[len(prefix):])
    if uri.startswith("enc://flash/"):
        return "flash", uri[len("enc://flash/"):]
    return None


class ArticlePane(Gtk.Box):
    """Reads the encyclopedia: an article, a picture's or film's page, with history.

    `on_change(kind, ident, title, label)` is called whenever what is shown changes,
    for the window around it - Search Encyclopedia's header and its tag button. An
    animation's link opens a FlashWindow rather than a page here.
    """

    def __init__(self, encyclopedia, on_change=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.encyclopedia = encyclopedia
        self.on_change = on_change
        self.history, self.position = [], -1
        self.current = None                 # (kind, ident) shown, or None
        self.dark = _is_dark()
        self._highlight = None              # text to find once the page has loaded
        self.webview = None
        self._webkit_tried = False          # whether this pane counts in webkitguard
        view = self._build_webkit_view(encyclopedia)
        if view is None:
            # No WebKit, or none that may be used: the article's text, which is still
            # worth reading and asking about. Pictures and films need the WebKit view.
            self.textview = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD)
            for margin in ("left", "right", "top", "bottom"):
                getattr(self.textview, f"set_{margin}_margin")(18)
            view = Gtk.ScrolledWindow()
            view.add(self.textview)
        self.pack_start(view, True, True, 0)
        self.connect("destroy", self._on_destroy)
        # The theme can change with an article open: it is shown again in the new one,
        # as the conversation beside it is (ChatWindow._on_theme_changed).
        settings = Gtk.Settings.get_default()
        self._theme_handlers = [] if settings is None else [
            settings.connect(f"notify::{name}", self._on_theme_changed)
            for name in ("gtk-theme-name", "gtk-application-prefer-dark-theme")]

    def _on_theme_changed(self, *_args):
        dark = _is_dark()
        if dark == self.dark or self.webview is None:
            return
        self.dark = dark
        colour = Gdk.RGBA()
        colour.parse("#1e1f22" if dark else "#ffffff")
        self.webview.set_background_color(colour)
        if self.history:
            self._show()

    def _build_webkit_view(self, encyclopedia):
        """The article's WebKit view, scrolled; None to read the article as text.

        Guarded as the chat window's view is (ChatWindow._build_view): not built where
        the typelib is missing or webkitguard has seen WebKit end the process, or where
        DA_ASSISTANT_NO_WEBKIT says not to, and counted while it is first loading. A
        chat fell back to plain text there, but Search Encyclopedia still built its
        WebView, and on a host whose WebKit kills the process the app died with it -
        and with no strike recorded, every time.
        """
        if WebKit2 is None or webkitguard.blocked()[0]:
            return None
        self._webkit_tried = True
        webkitguard.note_attempt(self)
        try:
            encscheme.install(encyclopedia)
            self.webview = WebKit2.WebView(web_context=encscheme.context())
            self.webview.connect("load-changed", self._on_load_changed)
            self.webview.connect("load-changed", self._on_first_load)
            # Links between articles followed here, the rest in the browser, and the
            # view never taken off the article (webpolicy). The window around it adds
            # its own context menu items through self.guard.extra.
            self.guard = webpolicy.Guard(WebKit2, self.webview, on_link=self._on_web_link,
                                         extra=self._menu_extra)
            self.guard.expect("about:blank")
            self._selection = ""
            lookups.track_selection(self.webview.get_user_content_manager(), WebKit2,
                                    lambda text: setattr(self, "_selection", text))
            settings = self.webview.get_settings()
            settings.set_enable_developer_extras(False)
            # Left on: WebKit's own controls for films are scripts. The page's policy
            # (encyclopedia_html.CSP) allows it none of its own.
            settings.set_enable_write_console_messages_to_stdout(False)
            colour = Gdk.RGBA()
            colour.parse("#1e1f22" if self.dark else "#ffffff")
            self.webview.set_background_color(colour)
        except Exception as e:
            # A library that loaded but does not work raises on first use; see gtkcompat.
            errors.record(f"WebKit article view unavailable, using text: {e!r}")
            webkitguard.note_success(self)  # it raised rather than killed us
            self.webview = None
            return None
        view = Gtk.ScrolledWindow()
        view.add(self.webview)
        return view

    def _on_first_load(self, view, event):
        if event == WebKit2.LoadEvent.FINISHED:
            view.disconnect_by_func(self._on_first_load)
            webkitguard.note_success(self)  # it rendered; it is not what kills us

    def _on_destroy(self, _widget):
        # Closed, loaded or not: a view built did not end the process (see webkitguard;
        # one never built says nothing, and must not clear the strikes). And its web
        # process goes with it, rather than at Python's next collection.
        if self._webkit_tried:
            webkitguard.note_success(self)
        settings = Gtk.Settings.get_default()
        for handler in getattr(self, "_theme_handlers", ()):
            settings.disconnect(handler)
        self._theme_handlers = []
        self.webview = None

    def text_views(self):
        """The article's text, without WebKit - what Ctrl+F searches."""
        return [self.textview]

    # ---- navigation -------------------------------------------------------

    def open(self, target, highlight=None):
        """Show `target` - ("article" | "assembly" | "film", id) - dropping anything
        Forward could have gone back to; with `highlight`, text or a Highlight, find it
        on the page (see place)."""
        if target[0] == "flash":
            FlashWindow(self.encyclopedia, target[1], parent=self.get_toplevel())
            return
        if self.current == tuple(target) and not highlight:
            return
        del self.history[self.position + 1:]
        self.history.append(tuple(target))
        self.position = len(self.history) - 1
        self._show(highlight)

    def can_go(self, step):
        return 0 <= self.position + step < len(self.history)

    def go(self, step):
        if self.can_go(step):
            self.position += step
            self._show()

    def _show(self, highlight=None):
        kind, ident = self.history[self.position]
        self.current = (kind, ident)
        if kind == "article":
            article = self.encyclopedia.article(ident)
            if article is None:
                return self.message("Not found", "This article is not in the import.")
            title, label = article["title"] or "(untitled)", article["library_name"]
            body = encyclopedia_html.article_body(article, self.encyclopedia.media_path)
            text = "\n\n".join(f"{h}\n\n{t}" for h, t in self.encyclopedia.sections(article))
            cite = text_of(citation_of(article["page"]))
            if cite:
                text += "\n\n" + cite
        else:
            assembly = self.encyclopedia.assembly(ident,
                                                  "media" if kind == "film" else "image")
            if assembly is None:
                return self.message("Not found",
                                    "This picture or film is not in the import.")
            title = assembly["title"] or "Encyclopedia"
            label = "Film or animation" if kind == "film" else "Picture"
            body = ('<div class="assembly-page">'
                    + encyclopedia_html.assembly_body(assembly, self.encyclopedia.media_path)
                    + "</div>")
            text = assembly["caption"]
        if self.on_change:
            self.on_change(kind, ident, title, label)
        if self.webview is None:
            self.textview.get_buffer().set_text(f"{title}\n\n{text}")
            return
        # What to find once the page has loaded, and how many of the places it is to
        # pass over to reach the one meant.
        self._highlight = (place(page_text(title, label, body), highlight)
                           if highlight else None)
        page = encyclopedia_html.document(title, label, body, self.dark)
        # file:// as the base: WebKit plays the DVD's films only from file addresses.
        # Spelled as a URI - a folder named "My DVD" is My%20DVD - for WebKit, and for
        # the guard, which lets only this page's own load through.
        base = pathlib.Path(os.path.abspath(self.encyclopedia.dvd() or "/")).as_uri()
        base = base if base.endswith("/") else base + "/"
        self.guard.expect(base)
        self.webview.load_html(page, base)

    def message(self, title, text):
        """A page of words of our own: before the first search, or for what is missing."""
        self.current = None
        if self.on_change:
            self.on_change(None, None, title, "")
        if self.webview is None:
            self.textview.get_buffer().set_text(f"{title}\n\n{text}")
        else:
            self.webview.load_html(encyclopedia_html.document(
                title, "", f"<p>{html.escape(text)}</p>", self.dark), None)

    def _on_load_changed(self, view, event):
        if event == WebKit2.LoadEvent.FINISHED and self._highlight:
            (text, passed), self._highlight = self._highlight, None
            if text:
                finder = view.get_find_controller()
                finder.search(text, WebKit2.FindOptions.CASE_INSENSITIVE
                              | WebKit2.FindOptions.WRAP_AROUND, 1)
                # Each from where the last one was: the web process takes them in turn.
                for _ in range(passed):
                    finder.search_next()

    def offer_lookups(self, config, look_up):
        """Right-click a selection in the article to look it up (ui/lookups.py):
        `look_up(kind, what)` opens the window."""
        self._lookup_config, self._look_up = config, look_up
        if self.webview is None:
            lookups.attach_to_textview(self.textview, config, look_up)

    def _menu_extra(self, menu, hit):
        look_up = getattr(self, "_look_up", None)
        if look_up is not None and hit is not None and hit.context_is_selection():
            lookups.add_to_webkit_menu(WebKit2, menu, self._lookup_config,
                                       self._selection, look_up)

    def _on_web_link(self, uri, _kind):
        """Another of the encyclopedia's pages, opened here; True for those."""
        target = target_of(uri)
        if target is not None:
            self.open(target)
            return True
        return False


def find_text(quote, limit=120):
    """What to search a page for to find `quote`: its longest part where an ellipsis
    marks words left out, or a paragraph ends - WebKit finds nothing across one - and
    no longer than WebKit finds comfortably."""
    parts = [p.strip(" .,;:")
             for p in re.split(r"\s*(?:\.\.\.|…|\[…\])\s*|\n", quote or "")]
    part = max(parts, key=len) if parts else ""
    if len(part) > limit:
        part = part[:limit].rsplit(" ", 1)[0]
    return part


class Highlight(collections.namedtuple("Highlight", ("text", "before", "after"),
                                       defaults=("", ""))):
    """Text to find on an article's page, with what lies either side of it where it is
    meant. WebKit's find goes to the first of the places the text is, and the words of
    a quotation can be on the page more than once - in another section, in a caption -
    so the words around it say which place is the one."""
    __slots__ = ()

    @classmethod
    def at(cls, text, span, around=80):
        """text[start:end] for a `span` of (start, end), and what is around it there."""
        start, end = span
        return cls(text[start:end], text[max(0, start - around):start],
                   text[end:end + around])


def page_text(title, label, body):
    """The text of an article's page as find goes through it: its heading, then a line
    for each paragraph, heading and caption of the article - pictures' captions, which
    the passages do without, among them."""
    body = re.sub(r"</?(?:figure|figcaption|section)\b[^>]*>", "<br>", body)
    return "\n".join((title, label, text_of(body)))


def place(page, highlight):
    """(what to search `page` for, how many of the places it is to pass over) to reach
    `highlight`: a Highlight - of the places, the one with its words before and after
    it, or most of them - or text, to be found where it first is."""
    if not isinstance(highlight, Highlight):
        highlight = Highlight(highlight or "")
    text = find_text(highlight.text)
    at = highlight.text.find(text) if text else -1
    if at == -1:
        return text, 0
    before = _fold(highlight.before + highlight.text[:at], flat=True)
    after = _fold(highlight.text[at + len(text):] + highlight.after, flat=True)
    folded, wanted = _fold(page), _fold(text)
    places, found = [], folded.find(wanted)
    while found != -1:
        places.append(found)
        found = folded.find(wanted, found + len(wanted))
    if len(places) < 2:
        return text, 0
    flat = folded.replace("\n", " ")

    def fit(start):
        end = start + len(wanted)
        return (_agree(flat[max(0, start - len(before)):start][::-1], before[::-1])
                + _agree(flat[end:end + len(after)], after))
    return text, max(range(len(places)), key=lambda n: fit(places[n]))


def _fold(text, flat=False):
    """`text` as find compares it, case aside and each run of spaces one - and with
    `flat`, its lines run together - its ends as they are, for words around a place
    to be compared up to it."""
    return re.sub(r"\s+" if flat else r"[^\S\n]+", " ", text.casefold())


def _agree(one, other):
    """How many characters `one` and `other` begin with alike."""
    return len(os.path.commonprefix((one, other)))


class FlashWindow(Gtk.Window):
    """One of the DVD's Flash animations, played by Ruffle."""

    def __init__(self, encyclopedia, name, parent=None):
        super().__init__(title="Animation")
        if parent is not None:
            self.set_transient_for(parent)
        self.set_default_size(760, 520)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.webview = None
        # Guarded as every WebKit view of the app's is: see ArticlePane._build_webkit_view.
        blocked, reason = webkitguard.blocked() if WebKit2 is not None else (True, "")
        self._webkit_tried = not blocked
        if not blocked:
            webkitguard.note_attempt(self)
            try:
                encscheme.install(encyclopedia)
                self.webview = WebKit2.WebView(web_context=encscheme.context())
                self.webview.get_settings().set_enable_developer_extras(False)
                # An animation may try to open a web page. Ruffle is told not to, and
                # the window follows nothing but the animation's own page (webpolicy):
                # no link, in the browser or here.
                self.guard = webpolicy.Guard(WebKit2, self.webview,
                                             on_link=lambda *_a: True)
                self.guard.expect(f"enc://flash/{name}")
            except Exception as e:
                errors.record(f"WebKit animation view unavailable: {e!r}")
                webkitguard.note_success(self)
                self.webview, reason = None, f"WebKit failed ({e})"
        self.connect("destroy", self._on_destroy)
        if self.webview is None:
            self.add(Gtk.Label(label="Animations need the WebKit view"
                               + (f", which is off here: {reason}." if reason else "."),
                               wrap=True, max_width_chars=70))
            self.show_all()
            return
        self.webview.connect("load-changed", self._on_load_changed)
        self.add(self.webview)
        self.show_all()
        self.webview.load_uri(f"enc://flash/{name}")

    def _on_load_changed(self, view, event):
        if event == WebKit2.LoadEvent.FINISHED:
            view.disconnect_by_func(self._on_load_changed)
            webkitguard.note_success(self)  # it rendered; it is not what kills us

    def _on_destroy(self, _widget):
        if self._webkit_tried:
            webkitguard.note_success(self)  # closed, loaded or not
        self.webview = None
