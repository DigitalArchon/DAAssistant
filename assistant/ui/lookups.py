"""Right-click a selection to look it up: in the dictionaries, or the encyclopedia.

Wherever the app shows text - an answer, a dictionary's entries, an article, the box a
question is typed in, the text about to be sent - selecting a word or a phrase and
right-clicking it offers "Look up in the dictionary" and "Search the encyclopedia", each
opening a window of its own on it. Each is offered only when there is something to look
in: a dictionaries folder chosen, the encyclopedia switched on.

What is selected in a WebKit view is not something its context menu can say, so the
page tells us as it changes (SELECTION_JS, through the "selection" message handler),
and the menu reads the last it was told. A GTK text view's selection is its buffer's.
"""

from .. import gtkcompat  # noqa: F401
from gi.repository import Gtk

# A dictionary takes a word or a short phrase; the encyclopedia's search a few words.
DICTIONARY_LIMIT = 60
ENCYCLOPEDIA_LIMIT = 120
LABEL_LIMIT = 28
STRIP = " \t\r\n.,;:!?\"'()[]{}«»“”‘’„‚<>*_`#|"

# The most of a selection the page tells. The selection is told at every change, and a
# drag through a long article changes it at every movement: each time, all of it was
# posted to Python - megabytes - to be thrown away there as too long to look up. Told
# no more than this, with its runs of space made one as clean() makes them, what is too
# long is too long all the same.
SELECTION_LIMIT = 4 * max(DICTIONARY_LIMIT, ENCYCLOPEDIA_LIMIT)

SELECTION_JS = """
(function () {
  var last = null;
  function report() {
    var text = String(window.getSelection() || '');
    if (text.length > %(limit)d) text = text.replace(/\\s+/g, ' ').slice(0, %(limit)d);
    if (text === last) return;
    last = text;
    window.webkit.messageHandlers.selection.postMessage(text);
  }
  document.addEventListener('selectionchange', report);
  document.addEventListener('mouseup', report);
})();
""" % {"limit": SELECTION_LIMIT}


def clean(text, limit):
    """The selection as something to look up, or None: one line, its edges' punctuation
    trimmed, and not too long to be a word, a phrase or a search."""
    text = " ".join((text or "").split()).strip(STRIP)
    if not text or len(text) > limit:
        return None
    return text


def _short(text):
    return text if len(text) <= LABEL_LIMIT else text[:LABEL_LIMIT - 1].rstrip() + "…"


def offers(config, text):
    """[(label, kind, what)] the menu offers for selected `text`: "dictionary" when a
    dictionaries folder is chosen, "encyclopedia" when it is switched on."""
    found = []
    word = clean(text, DICTIONARY_LIMIT)
    if word and getattr(config, "dictionaries_dir", ""):
        found.append((f"Look up “{_short(word)}” in the dictionary", "dictionary", word))
    query = clean(text, ENCYCLOPEDIA_LIMIT)
    if query and getattr(config, "encyclopedia_enabled", False):
        found.append((f"Search the encyclopedia for “{_short(query)}”", "encyclopedia",
                      query))
    return found


def add_to_webkit_menu(webkit, menu, config, text, look_up):
    """Append the lookups for `text` to a WebKit context menu; `look_up(kind, what)`
    opens the window."""
    from .webpolicy import menu_item
    found = offers(config, text)
    if not found:
        return
    if menu.get_items():
        menu.append(webkit.ContextMenuItem.new_separator())
    for label, kind, what in found:
        menu.append(menu_item(webkit, label,
                              lambda k=kind, w=what: look_up(k, w), f"look-up-{kind}"))


def add_to_gtk_menu(menu, config, text, look_up):
    """Append the lookups for `text` to a GTK popup menu (a text view's populate-popup)."""
    found = offers(config, text)
    if not found or not isinstance(menu, Gtk.Menu):
        return
    menu.append(Gtk.SeparatorMenuItem())
    for label, kind, what in found:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda _item, k=kind, w=what: look_up(k, w))
        menu.append(item)
    menu.show_all()


def selected_text(textview):
    """What is selected in a GTK text view, or ""."""
    buffer = textview.get_buffer()
    bounds = buffer.get_selection_bounds()
    if not bounds:
        return ""
    return buffer.get_text(bounds[0], bounds[1], False)


def attach_to_textview(textview, config, look_up):
    """Offer the lookups in `textview`'s own right-click menu, for its selection."""
    textview.connect("populate-popup", lambda view, menu: add_to_gtk_menu(
        menu, config, selected_text(view), look_up))


def track_selection(manager, webkit, on_change):
    """Have a WebKit view's page report its selection: `on_change(text)` on every
    change. `manager` is the view's UserContentManager."""
    manager.register_script_message_handler("selection")

    def received(_manager, result):
        try:
            on_change(result.get_js_value().to_string())
        except AttributeError:
            pass

    manager.connect("script-message-received::selection", received)
    manager.add_script(webkit.UserScript(
        SELECTION_JS, webkit.UserContentInjectedFrames.TOP_FRAME,
        webkit.UserScriptInjectionTime.END, None, None))
