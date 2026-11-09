"""What a WebKit view of ours may do with a click, a drop or a right-click.

A transcript or an article is a page the app builds and loads itself. Nothing in it
may take the view somewhere else: a link opens in the browser, and the view stays on
the page it was given. Intercepting a link click was not enough for that. WebKit's own
context menu offers Open Link, Reload, Back and Forward, and a link or URL dropped on
the view loads it in place - each of those a navigation of another kind, and each one
went ahead: right-click a source and Open Link, and the website replaced the answers,
with JavaScript on and the transcript's own script handlers there for it to call.

Guard does both halves. Its navigation policy lets through only the loads the view's
owner announces (expect() before each load_html), refuses everything else, and opens
a link in a new window - target=_blank in a dictionary's HTML - in the browser. Its
context menu keeps what copies and what plays a film, drops what navigates or
downloads, and adds Open Link in Browser. Owners can add items of their own with
`extra(menu, hit)`; the right-click lookups are one (lookup_items).

Links are opened with hostenv.open_uri, so the browser gets the host's environment,
and a link nothing can open - mailto: with no mail program - is said, not raised. A link
that is not secure - plain http to anywhere but this machine, its own network or an
onion service - is opened only once the user has said to, in a dialog that names it
(open_link), however it was opened: clicked, Enter on it, Open Link in Browser, or in
the plain-text view.
"""

from urllib.parse import unquote

from .. import gtkcompat  # noqa: F401
from gi.repository import Gio, GLib, Gtk, Pango

from .. import hostenv, netguard

EXTERNAL = ("http://", "https://", "mailto:")

INSECURE_TITLE = "Open a link that isn't secure?"
INSECURE_TEXT = ("This address uses plain http, so the page isn't encrypted: anyone "
                 "between you and the site can read it or change it before it reaches "
                 "you.")


def open_link(uri, parent=None, say=None):
    """Open `uri` in the browser: at once if it is secure, on this machine or its own
    network, or an onion service's; one that is not, only after asking
    (confirm_insecure). `say(message)` reports a link nothing here can open."""
    if netguard.insecure_link(uri):
        confirm_insecure(parent, uri, lambda: _launch(uri, say))
    else:
        _launch(uri, say)


def _launch(uri, say=None):
    try:
        hostenv.open_uri(uri)
    except GLib.Error as e:
        scheme = uri.split(":", 1)[0]
        (say or (lambda message: print(f"[link] {message}")))(
            f"Nothing here opens {scheme}: links ({e.message})")


def confirm_insecure(parent, uri, then):
    """Ask, in a dialog of its own over `parent`, whether to open `uri`, a link that is
    not secure; `then()` once the answer is Open anyway. Cancel is the default, and what
    Escape and closing the dialog answer. Not run in a loop of its own: it is asked from
    WebKit's handlers, which are not to be held up. Returns the dialog, which a test
    can answer."""
    window = parent if isinstance(parent, Gtk.Window) else None
    # Closed with the window it was asked from, unanswered: nothing opens, and nothing
    # is said to a window that has gone.
    dialog = Gtk.MessageDialog(transient_for=window, modal=True, destroy_with_parent=True,
                               message_type=Gtk.MessageType.WARNING, text=INSECURE_TITLE)
    dialog.format_secondary_text(INSECURE_TEXT)
    # The address in full, between the question and why: what the link really opens,
    # whatever its text said. Selectable, to be copied; broken anywhere, as a long
    # address has nowhere else to break.
    address = Gtk.Label(label=uri, xalign=0.0, selectable=True, wrap=True,
                        wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=60)
    address.get_style_context().add_class("monospace")
    area = dialog.get_message_area()
    area.pack_start(address, False, False, 0)
    area.reorder_child(address, 1)
    address.show()
    cancel = dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Open anyway", Gtk.ResponseType.ACCEPT)
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    dialog.set_focus(cancel)

    def answered(shown, response):
        shown.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            then()

    dialog.connect("response", answered)
    dialog.show()
    return dialog


def _kept_actions(webkit):
    """The stock items that copy, select or play, and nothing that navigates or saves."""
    names = ("COPY", "COPY_LINK_TO_CLIPBOARD", "COPY_IMAGE_TO_CLIPBOARD",
             "COPY_IMAGE_URL_TO_CLIPBOARD", "COPY_VIDEO_LINK_TO_CLIPBOARD",
             "COPY_AUDIO_LINK_TO_CLIPBOARD", "SELECT_ALL", "CUT", "PASTE",
             "PASTE_AS_PLAIN_TEXT", "DELETE", "INSERT_EMOJI", "UNICODE", "INPUT_METHODS",
             "SPELLING_GUESS", "NO_GUESSES_FOUND", "IGNORE_SPELLING", "LEARN_SPELLING",
             "IGNORE_GRAMMAR", "TOGGLE_MEDIA_CONTROLS", "TOGGLE_MEDIA_LOOP",
             "ENTER_VIDEO_FULLSCREEN", "MEDIA_PLAY", "MEDIA_PAUSE", "MEDIA_MUTE", "CUSTOM")
    actions = webkit.ContextMenuAction
    return {getattr(actions, name) for name in names if hasattr(actions, name)}


def menu_item(webkit, label, callback, name="custom"):
    """A context menu item of our own, saying `label` as written, that runs
    `callback()`.

    WebKit builds its menu from a GMenu (gtk_menu_shell_bind_model), which reads every
    label as a mnemonic, as its own "_Copy" is: an underscore marks the key and is not
    shown. So a selection looked up, snake_case_word, was offered as "snakecaseword".
    Doubled, each one is shown as itself."""
    action = Gio.SimpleAction.new(name, None)
    action.connect("activate", lambda *_args: callback())
    return webkit.ContextMenuItem.new_from_gaction(action, label.replace("_", "__"), None)


class Guard:
    """Navigation policy and context menu for one WebView.

    `on_link(uri, kind)` is offered every link the user clicks, before it goes to the
    browser; True means it was dealt with (a dictionary cross-reference, an article of
    the encyclopedia). `on_drop(uri)` is offered a dropped file:// URI. `say(message)`
    reports what could not be done. `extra(menu, hit)` adds items to the context menu.
    """

    def __init__(self, webkit, view, on_link=None, on_drop=None, say=None, extra=None):
        self.webkit = webkit
        self.on_link = on_link
        self.on_drop = on_drop
        self.say = say or (lambda message: print(f"[view] {message}"))
        self.extra = extra
        self._expected = set()
        self._kept = _kept_actions(webkit)
        view.connect("decide-policy", self._on_decide_policy)
        view.connect("context-menu", self._on_context_menu)

    def expect(self, uri):
        """The view is about to load `uri` itself (load_html's base, or about:blank)."""
        self._expected.add(unquote(uri or "about:blank"))

    def open_external(self, uri, view=None):
        """`uri` to the browser - asked about first if it is not secure (open_link), in
        a dialog over the window `view` is in. The view is the signal's, not kept here:
        held by its own handlers' guard, it was never freed, nor its web process."""
        parent = view.get_toplevel() if view is not None else None
        open_link(uri, parent if parent is not None and parent.is_toplevel() else None,
                  self.say)

    # ---- navigation -------------------------------------------------------

    def _on_decide_policy(self, view, decision, decision_type):
        types = self.webkit.PolicyDecisionType
        if decision_type not in (types.NAVIGATION_ACTION, types.NEW_WINDOW_ACTION):
            return False                    # responses: what a page of ours fetches
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""
        kind = action.get_navigation_type()
        navigation = self.webkit.NavigationType
        if decision_type == types.NAVIGATION_ACTION and unquote(uri) in self._expected \
                and kind in (navigation.OTHER, navigation.RELOAD):
            return False                    # a page of ours, loading
        decision.ignore()
        if uri.startswith("file://") and kind != navigation.LINK_CLICKED:
            # A file dropped on the view: offered to the owner, never opened in it. Only
            # a drop - a link in a page must never pick a file of the user's.
            if self.on_drop is not None:
                self.on_drop(uri)
            return True
        if kind != navigation.LINK_CLICKED and decision_type == types.NAVIGATION_ACTION:
            return True                     # Reload, Back, Forward, a drop, a form: no
        if self.on_link is not None and self.on_link(uri, kind):
            return True
        if uri.startswith(EXTERNAL):
            self.open_external(uri, view)
        return True

    # ---- context menu -----------------------------------------------------

    def _on_context_menu(self, view, menu, _event, hit):
        for item in list(menu.get_items()):
            action = item.get_stock_action()
            if action not in self._kept and not item.is_separator():
                menu.remove(item)
        if hit is not None and hit.context_is_link():
            uri = hit.get_link_uri() or ""
            if uri.startswith(EXTERNAL):
                menu.prepend(menu_item(self.webkit, "Open Link in Browser",
                                       lambda: self.open_external(uri, view),
                                       "open-link"))
        if self.extra is not None:
            self.extra(menu, hit)
        _tidy_separators(menu)
        if not menu.get_items():
            return True                     # nothing left to offer: no menu at all
        return False


def _tidy_separators(menu):
    """No separator first, last or twice in a row, once items have gone."""
    previous_separator = True
    for item in list(menu.get_items()):
        if item.is_separator():
            if previous_separator:
                menu.remove(item)
            previous_separator = True
        else:
            previous_separator = False
    items = menu.get_items()
    if items and items[-1].is_separator():
        menu.remove(items[-1])
