"""Reading the encyclopedia: the article pane, and the Flash window.

The pane is where an article is read as the DVD presented it - every section, every
picture with its caption, the films playable in place, links to other articles
followed within it, with history for Back and Forward. A picture or film opens on a
page of its own, as it did on the DVD; an animation opens in a window of its own,
played by Ruffle. Search Encyclopedia is built around it.
"""

import html

from .. import gtkcompat
from gi.repository import Gdk, Gtk

from ..reference import encyclopedia_html
from ..reference.encyclopedia import citation_of, text_of
from . import encscheme

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
        if WebKit2 is not None:
            encscheme.install(encyclopedia)
            self.webview = WebKit2.WebView(web_context=encscheme.context())
            self.webview.connect("decide-policy", self._on_decide_policy)
            self.webview.connect("load-changed", self._on_load_changed)
            self.webview.connect("context-menu", lambda *a: False)
            settings = self.webview.get_settings()
            settings.set_enable_developer_extras(False)
            # Left on: WebKit's own controls for films are scripts. The page's policy
            # (encyclopedia_html.CSP) allows it none of its own.
            settings.set_enable_write_console_messages_to_stdout(False)
            colour = Gdk.RGBA()
            colour.parse("#1e1f22" if self.dark else "#ffffff")
            self.webview.set_background_color(colour)
            view = Gtk.ScrolledWindow()
            view.add(self.webview)
        else:
            # No WebKit: the article's text, which is still worth reading and asking
            # about. Pictures and films need the WebKit view.
            self.webview = None
            self.textview = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD)
            for margin in ("left", "right", "top", "bottom"):
                getattr(self.textview, f"set_{margin}_margin")(18)
            view = Gtk.ScrolledWindow()
            view.add(self.textview)
        self.pack_start(view, True, True, 0)
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, _widget):
        # Its web process goes with it, rather than at Python's next collection.
        self.webview = None

    def text_views(self):
        """The article's text, without WebKit - what Ctrl+F searches."""
        return [self.textview]

    # ---- navigation -------------------------------------------------------

    def open(self, target, highlight=None):
        """Show `target` - ("article" | "assembly" | "film", id) - dropping anything
        Forward could have gone back to; with `highlight`, find that text on it."""
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
        self._highlight = highlight
        page = encyclopedia_html.document(title, label, body, self.dark)
        # file:// as the base: WebKit plays the DVD's films only from file addresses.
        self.webview.load_html(page, "file://" + (self.encyclopedia.dvd() or "/").rstrip("/")
                               + "/")

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
            text, self._highlight = find_text(self._highlight), None
            if text:
                view.get_find_controller().search(
                    text, WebKit2.FindOptions.CASE_INSENSITIVE
                    | WebKit2.FindOptions.WRAP_AROUND, 1)

    def _on_decide_policy(self, _view, decision, decision_type):
        if decision_type != WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        action = decision.get_navigation_action()
        if action.get_navigation_type() != WebKit2.NavigationType.LINK_CLICKED:
            return False
        decision.ignore()
        uri = action.get_request().get_uri()
        target = target_of(uri)
        if target is not None:
            self.open(target)
        elif uri.startswith(("http://", "https://")):
            Gtk.show_uri_on_window(self.get_toplevel(), uri, Gdk.CURRENT_TIME)
        return True


def find_text(quote, limit=120):
    """What to search a page for to find `quote`: its longest part where an ellipsis
    marks words left out, and no longer than WebKit finds comfortably."""
    import re
    parts = [p.strip(" .,;:") for p in re.split(r"\s*(?:\.\.\.|…|\[…\])\s*", quote or "")]
    part = max(parts, key=len) if parts else ""
    if len(part) > limit:
        part = part[:limit].rsplit(" ", 1)[0]
    return part


class FlashWindow(Gtk.Window):
    """One of the DVD's Flash animations, played by Ruffle."""

    def __init__(self, encyclopedia, name, parent=None):
        super().__init__(title="Animation")
        if parent is not None:
            self.set_transient_for(parent)
        self.set_default_size(760, 520)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        if WebKit2 is None:
            self.add(Gtk.Label(label="Animations need the WebKit view."))
            self.show_all()
            return
        encscheme.install(encyclopedia)
        self.webview = WebKit2.WebView(web_context=encscheme.context())
        self.webview.get_settings().set_enable_developer_extras(False)
        self.webview.connect("decide-policy", self._on_decide_policy)
        self.add(self.webview)
        self.connect("destroy", lambda _w: setattr(self, "webview", None))
        self.show_all()
        self.webview.load_uri(f"enc://flash/{name}")

    @staticmethod
    def _on_decide_policy(_view, decision, decision_type):
        # An animation may try to open a web page; Ruffle is told not to, and the
        # window would not follow it anyway.
        if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False
