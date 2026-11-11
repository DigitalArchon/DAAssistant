"""Conversation window: WebKit view with live token streaming.

Tokens land in the view as they arrive, markdown is rendered properly (tables, code
blocks with syntax highlighting, lists), and code blocks get copy buttons.
"""

import errno
import json
import os
import re
import stat
import time
from urllib.parse import quote, unquote

from .. import errors, gtkcompat, webkitguard
from gi.repository import Gdk, GLib, Gtk, Pango

from . import (bubbles, find, keys, lookups, markdown_html,
               screenshot as screenshot_view, webpolicy)
from .. import (attachments, models as catalogue, privacy as privacy_module, prompts,
                providers, websearch)
from ..llm import LLMError
from ..reference import dictionaries, markup as reference_markup

if gtkcompat.has_webkit():
    from gi.repository import WebKit2
else:  # pragma: no cover - only on systems without the typelib
    WebKit2 = None

STREAM_REDRAW_INTERVAL = 0.08   # seconds; throttles JS updates during streaming

# What of a conversation's dictionary lookups goes with each request: the latest few,
# each sharing out this many characters among its dictionaries. A word like "set" runs
# to 200,000 characters across nineteen dictionaries; the model needs the senses, not
# every citation of every one.
LOOKUPS_IN_CONTEXT = 3
LOOKUP_CONTEXT_CHARS = 16000

# Turns that are reference material shown in the transcript - a dictionary lookup, an
# answer's Encyclopedia sources - rather than anything said. Each carries its own HTML,
# and none is ever sent as a message; see ChatWindow._request_system.
REFERENCE_ROLES = ("lookup", "sources")
# How long Clear can be undone for.
UNDO_SECONDS = 8

# The bar between the answers and the message box: what the answers keep however far it
# is dragged, and the most of a new window a height remembered from a bigger one can
# take. How long it rests before the height is saved, as a drag moves it many times.
MIN_ANSWERS_HEIGHT = 60
MOST_OF_WINDOW_FOR_PROMPT = 0.7
PROMPT_HEIGHT_SAVE_MS = 400

COPY_BUTTON_JS = """
(function () {
  function decorate() {
    document.querySelectorAll('pre').forEach(function (pre) {
      if (pre.dataset.copyReady) return;
      pre.dataset.copyReady = '1';
      pre.style.position = 'relative';
      var b = document.createElement('button');
      b.textContent = 'Copy';
      b.style.cssText = 'position:absolute;top:6px;right:6px;font:11px system-ui;' +
        'padding:2px 8px;border-radius:6px;border:1px solid rgba(128,128,128,.4);' +
        'background:rgba(128,128,128,.14);color:inherit;cursor:pointer;opacity:.75';
      b.addEventListener('click', function () {
        var code = pre.querySelector('code'), text;
        if (code) {
          text = code.innerText;
        } else {
          // A <pre> of its own, with the button inside it: taken out while the text
          // is read, or its label goes on the clipboard with the code. (Hidden, it
          // still leaves WebKit's innerText a newline.)
          var next = b.nextSibling;
          b.remove();
          text = pre.innerText;
          pre.insertBefore(b, next);
        }
        window.webkit.messageHandlers.copyText.postMessage(text);
        b.textContent = 'Copied';
        setTimeout(function () { b.textContent = 'Copy'; }, 1200);
      });
      pre.appendChild(b);
    });
  }
  decorate();
  new MutationObserver(decorate).observe(
    document.body, { childList: true, subtree: true });
})();
"""


# A Copy button on each finished answer, copying it as Markdown - what it said, not
# the page's rendering of it - through the copyAnswer handler, by its place in the
# conversation, which each turn carries (markdown_html.turn_html). Its place among the
# page's turns was what it sent once: stray markup in an earlier answer, and the
# button copied another one. The streaming answer, which is still being written, has
# none.
ANSWER_COPY_JS = """
(function () {
  function decorate() {
    document.querySelectorAll('body > .turn.assistant').forEach(function (turn) {
      if (turn.querySelector('.cursor') || !turn.dataset.turn) return;
      var role = turn.querySelector('.role');
      if (!role || role.querySelector('.copy-answer')) return;
      var b = document.createElement('button');
      b.className = 'copy-answer';
      b.textContent = 'Copy';
      b.title = 'Copy this answer as Markdown';
      b.addEventListener('click', function () {
        window.webkit.messageHandlers.copyAnswer.postMessage(turn.dataset.turn);
        b.textContent = 'Copied';
        setTimeout(function () { b.textContent = 'Copy'; }, 1200);
      });
      role.appendChild(b);
    });
  }
  decorate();
  new MutationObserver(decorate).observe(
    document.body, { childList: true, subtree: true });
})();
"""

# The text sizes Ctrl+plus and Ctrl+minus step through.
ZOOM_STEPS = (0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
STOPPED_NOTE = "\n\n_(Stopped: the answer was cut short here.)_"


SCREENSHOT_JS = """
document.addEventListener('click', function (e) {
  var el = e.target.closest && e.target.closest('[data-shot]');
  if (!el) return;
  e.preventDefault();
  window.webkit.messageHandlers.screenshot.postMessage(el.dataset.shot);
});
"""


# Whether the page keeps to its end as an answer comes in. Decided in the page, where
# the scrolling happens: Python would hear of a move too late to stop the next chunk
# undoing it. Going up ends it, however little; coming back down to the end, or a new
# request (resumeFollowing), starts it again. NEAR_END is how close counts as back, as
# the end keeps moving away from a reader scrolling down to it.
FOLLOW_JS = """
(function () {
  var NEAR_END = 48;
  var following = true, last = window.scrollY;
  function nearEnd() {
    return document.documentElement.scrollHeight - window.innerHeight -
      window.scrollY <= NEAR_END;
  }
  function toEnd() {
    window.scrollTo(0, document.documentElement.scrollHeight);
    last = window.scrollY;
  }
  window.addEventListener('scroll', function () {
    var y = window.scrollY;
    if (y < last) following = false;
    else if (y > last && nearEnd()) following = true;
    last = y;
  }, { passive: true });
  // A frame ahead of the scroll events these lead to, in which a chunk could land. End
  // goes to where the end was when it was pressed, which a fast answer has left behind.
  window.addEventListener('wheel', function (e) {
    if (e.deltaY < 0 && window.scrollY > 0) following = false;
    else if (e.deltaY > 0 && nearEnd()) following = true;
  }, { passive: true });
  window.addEventListener('keydown', function (e) {
    if (e.key === 'End') following = true;
    else if (window.scrollY > 0 && ['ArrowUp', 'PageUp', 'Home'].indexOf(e.key) >= 0) {
      following = false;
    }
  });
  window.followEnd = function () { if (following) toEnd(); };
  window.resumeFollowing = function () { following = true; toEnd(); };
})();
"""


def history_for(conversation):
    """The turns worth sending upstream, in the shape the API expects.

    Two things in the transcript are for the reader only. An error is shown as an
    assistant turn, but the model never said it, and sent back as history it becomes
    something the model apologises for. And a request that failed or was stopped leaves
    a user turn with no answer, so the next one makes two in a row - which stricter
    providers reject. The unanswered turn is folded into the one that follows rather
    than dropped: on a first request it is the turn carrying the captured text.
    """
    history = []
    for turn in conversation:
        # An answer asked again with another model stays on screen, but only the newest
        # answer to a question is history: two in a row is a shape stricter providers
        # refuse, and the old one is what the user asked to replace. Reference material
        # is not a turn either: it goes with the instructions (ChatWindow._request_system).
        if turn.get("error") or turn.get("superseded") or \
                turn.get("role") in REFERENCE_ROLES:
            continue
        entry = {key: turn[key] for key in ("role", "content", "search_query")
                 if key in turn}
        # Pictures attached to the turn, copied: the worker notes their readings on
        # these, and the window's own turns hear of them by way of the main loop.
        if turn.get("images"):
            entry["images"] = [dict(image) for image in turn["images"]]
        previous = history[-1] if history else None
        if previous and previous["role"] == "user" and entry["role"] == "user":
            parts = [previous.get("content") or "", entry.get("content") or ""]
            entry["content"] = "\n\n".join(part for part in parts if part)
            if "search_query" not in entry and "search_query" in previous:
                entry["search_query"] = previous["search_query"]
            images = (previous.get("images") or []) + (entry.get("images") or [])
            if images:
                entry["images"] = images
            history[-1] = entry
            continue
        history.append(entry)
    return history


def dictionary_initials(name):
    """Up to two letters standing for a dictionary that has no icon of its own."""
    skip = {"the", "of", "and", "a", "en", "eng", "dictionary", "english", "new", "ed"}
    words = [w for w in re.findall(r"[A-Za-z]+", name or "") if w.lower() not in skip]
    if not words:
        return (name or "?")[:2]
    if len(words) == 1:
        return words[0][:2].title()
    return (words[0][0] + words[1][0]).upper()


def markdown_filename(title, extension=".md"):
    """A file name for a conversation saved as Markdown, or its screenshot saved on
    its own: its window title and when."""
    stem = " ".join(re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title or "").split())
    stem = stem.strip(".") or "Conversation"
    return f"{stem} {time.strftime('%Y-%m-%d %H%M')}{extension}"


_SAVED_SCREENSHOT = re.compile(r"^!\[Screenshot\]\(([^)\s/]+)\)$", re.M)


def sound_text(text):
    """`text` with any lone surrogate made U+FFFD, and a pair of them the character
    they spell. A reply is JSON, and JSON can spell half a character - an emoji split
    across two chunks of a stream, or one broken - which Python keeps as a surrogate
    that cannot be written out: not to a file, the clipboard, a text view or WebKit."""
    if not text:
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def write_whole(path, data):
    """Write the bytes `data` to `path` by way of a file of its own beside it, put in
    its place only once it is whole: a write that fails part way - a full disk - leaves
    what was there. Written through a link, as open() would; mode as the file it
    replaces, or as the umask has it for a new one.

    Where another file in its place would not be the same file - one with other names
    (hard links), someone else's, or not a plain file - or the folder takes no new
    files, it is written in place, as open() did. One that may not be written to is
    refused, as open() refused it, not replaced."""
    target = os.path.realpath(path)
    try:
        info = os.stat(target)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not os.access(target, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        if (info.st_nlink > 1 or info.st_uid != os.geteuid()
                or info.st_gid not in (os.getegid(), *os.getgroups())
                or not stat.S_ISREG(info.st_mode)):
            _write_in_place(target, data)
            return
    # Named after it, within the bytes a file name may have: 120 characters of a
    # Japanese name are 360.
    name = os.path.basename(target)
    while len(os.fsencode(name)) > 120:
        name = name[:-1]
    part = os.path.join(os.path.dirname(target),
                        f".{name}.{os.getpid()}-{time.time_ns()}.part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        # Its owner's alone while it is written, when it replaces a file: that may be
        # private. Then the group and mode of the file it replaces - in a shared folder,
        # the group is who it was shared with.
        fd = os.open(part, flags, 0o666 if info is None else 0o600)
    except PermissionError:
        if info is None:
            raise
        _write_in_place(target, data)       # a folder that takes no new files
        return
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            if info is not None:
                if os.fstat(out.fileno()).st_gid != info.st_gid:
                    os.fchown(out.fileno(), -1, info.st_gid)
                os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode))
        os.replace(part, target)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise


def _write_in_place(path, data):
    with open(path, "wb") as out:
        out.write(data)


def screenshot_beside(markdown_path, replacing):
    """Where a saved conversation's screenshot goes: beside it, under the same name, or
    the next free one when a picture of that name is already there.

    Replacing a saved conversation - the file dialog asked about the .md - replaces the
    screenshot that conversation links to, and nothing else. notes.png beside notes.md
    is not that unless notes.md says so: the first save of notes.md beside someone's own
    notes.png wrote "notes (2).png", and replacing it used to write over notes.png.
    """
    if replacing:
        own = _linked_screenshot(markdown_path)
        if own:
            return os.path.join(os.path.dirname(markdown_path), own)
    stem = os.path.splitext(markdown_path)[0]
    path, n = stem + ".png", 2
    while os.path.exists(path):
        path, n = f"{stem} ({n}).png", n + 1
    return path


def _linked_screenshot(markdown_path):
    """The file name of the screenshot a saved conversation links to, or None."""
    try:
        with open(markdown_path, encoding="utf-8") as f:
            found = _SAVED_SCREENSHOT.search(f.read(1 << 20))
    except (OSError, UnicodeDecodeError):
        return None
    name = unquote(found.group(1)) if found else None
    return name if name and os.path.basename(name) == name else None


def _is_dark():
    settings = Gtk.Settings.get_default()
    if settings and settings.get_property("gtk-application-prefer-dark-theme"):
        return True
    theme = (settings.get_property("gtk-theme-name") or "").lower() if settings else ""
    return "dark" in theme


class ChatWindow(Gtk.Window):
    def __init__(self, app, title, conversation, image_base64=None, first_model=None,
                 image_first=True, autostart=False, web_search=None, preflight=None,
                 system=None, privacy=None, screenshot=None, lookup=None,
                 lookup_word=None, encyclopedia=None, dictionary_list=None):
        """A conversation: its first request, then the user's.

        The first request goes to `first_model` when the operation names one (OCR: the
        vision model, whose transcription is the first answer), else to the answering
        model. Every request after that goes to the model chosen in the header's model
        menu, which starts on the answering model - text, or premium when that option is
        on; their private versions in Privacy Mode - and can be changed at any message.

        `image_base64` is the capture, kept for the whole conversation. The first
        request carries it when `image_first` (OCR, a question put to a model that can
        see); later ones carry it when the model they go to accepts images, and when it
        does not, the vision model reads the image for it once (text and description),
        and that reading goes with every such request instead.

        `screenshot` is the capture as the user took it, PNG bytes: shown above the
        first turn, to view and save, and saved beside the conversation as Markdown.
        It is only ever in memory until the user saves it.

        `lookup` makes it a dictionary window: lookup(word, status) returns what the
        user's dictionaries say (Library.look_up), run off the main loop. The header
        gets a box to look words up in, and every request carries the entries with its
        instructions. Each word looked up starts a page of its own, as a dictionary
        does - its entries and the questions asked about them - with Back and Forward
        through the pages; Shift+Enter adds a word to the page instead, to compare.
        `lookup_word` is looked up as soon as the window opens. `dictionary_list()`
        gives [(path, name, icon)] for the bar of dictionaries under the header, where
        each can be switched off for this conversation: hidden from every lookup in it,
        and from what the model is given.

        `encyclopedia` (reference.encyclopedia.Encyclopedia) serves enc:// - the
        encyclopedia's pictures - to the view. The encyclopedia windows are subclasses
        (see ui/encyclopedia_windows.py), built on the hooks under "for subclasses"
        below.
        """
        super().__init__(title=title)
        self.app = app
        self.config = app.config
        # Each turn: {role, content, display?, model?, superseded?, privacy?}
        self.conversation = list(conversation or [])
        self.image_base64 = image_base64
        self.screenshot = screenshot
        # Built once: the full redraws re-render every turn, and this is the one part
        # that never changes.
        self._screenshot_html = (markdown_html.screenshot_html(screenshot) if screenshot
                                 else "")
        self._first_model = first_model
        self._image_first = image_first
        self._first_request = True
        # The vision model's reading of the capture, for models that cannot see it, and
        # whether it is already in the history (an image query answered from it) or has
        # to ride along in the system prompt.
        self.reading = None
        self._answer_is_reading = False
        self._reading_in_history = False
        # How to answer: sent as a system message on every turn, including follow-ups,
        # and deliberately not part of any user turn - the search query is built from
        # the latest of those.
        self.system = system
        # Optional first-request step run before the model is called, e.g. transcribing
        # an image with the vision model so the text model can translate the result.
        # Signature: preflight(cancel, status, privacy=, web_search=) -> {"content": ...,
        # "display": ...}, for the turn it is made for: the question the window opened
        # with. `privacy` and `web_search` are this request's, not the window's first:
        # a preflight that failed or was stopped is made again with the next request,
        # and the window may have gone private, or its search been switched off, since.
        # The turn's "plain_display", when it has one, is its display without the note
        # saying the step is under way, for when the step does not finish.
        self._preflight = preflight
        self._preflight_turn = next((turn for turn in reversed(self.conversation)
                                     if turn.get("role") == "user"), None)
        # Privacy Mode (assistant/privacy.py): the PrivacySession this conversation
        # started with, or None. It is never replaced or cleared - nothing in this window
        # reads config.privacy_mode - so a private conversation stays on its attested
        # models to the end, whatever the tray or Settings say in the meantime. Privacy
        # only ever tightens: switched on while this window is open, the app hands it a
        # session (go_private), and from its next message it is a private conversation.
        self.privacy = privacy
        self._privacy_refused = None
        self._pending_privacy = None        # to adopt once the answer coming has come
        self._privacy_hold = None           # why nothing may be sent, Privacy Mode being
                                            # on and its models not usable
        # Private models nano-gpt no longer offers, found here, each with what its list
        # offered instead; and the one whose refusal is still to be answered with that.
        self._retired = {}
        self._retired_pending = None
        self._refused_retired = False       # the refusal shown is a retired model's
        self._sent_kinds = set()
        # The state of each TEE/ reply checked on this request's way - a transcription, a
        # reading, the answer: what the question's own mark is read from (sent_badge).
        self._tee_checks = []
        self._streaming_privacy = None
        if privacy is not None:
            privacy.listener = lambda *event: GLib.idle_add(self._on_privacy_event, *event)
        self.dark = _is_dark()
        self._streaming_text = None
        self._streaming_model = None
        self._last_redraw = 0.0
        self._redraw_queued = False     # a redraw is waiting for the main loop
        self._cancel = None
        self._destroyed = False
        self._loaded = False
        self._page_loading = False          # a load_html not yet finished
        self._after_load = None             # the redraw asked for meanwhile: page, full
        self._pending_js = []
        self._lookup = lookup
        self._encyclopedia = encyclopedia
        self._looking = False
        self._hidden_dictionaries = set()       # paths switched off in this window
        self._redraw_after_answer = False
        # A dictionary window's pages, one per word looked up, each a conversation of
        # its own; self.conversation is always the one shown.
        self._pages, self._page = [self.conversation], 0
        self._undo = None                       # what Clear removed, while undoable
        self._undo_timer = None
        # Files attached to the next message (attachments.py), until it is sent.
        self._attachments = []
        # The answers an Ask again is to replace, until its answer has come.
        self._replacing = []

        self.set_default_size(*self._initial_size())
        self.set_position(Gtk.WindowPosition.CENTER)
        self._size_timer = None
        self.connect("configure-event", self._on_configure)
        # The theme can change with the window open: its transcript follows, rather
        # than keeping the palette it opened with until it is closed.
        settings = Gtk.Settings.get_default()
        self._theme_handlers = [] if settings is None else [
            settings.connect(f"notify::{name}", self._on_theme_changed)
            for name in ("gtk-theme-name", "gtk-application-prefer-dark-theme")]

        self.header = header = Gtk.HeaderBar(show_close_button=True, title=title)
        self.set_titlebar(header)

        if privacy is not None:
            lock = Gtk.Button.new_from_icon_name("channel-secure-symbolic",
                                                 Gtk.IconSize.BUTTON)
            lock.set_tooltip_text("Privacy Mode: this conversation uses attested TEE "
                                  "models only, to its end. Click for what was checked.")
            lock.connect("clicked", lambda _w: self._show_privacy_details())
            header.pack_start(lock)

        self.lookup_entry = None
        if lookup is not None:
            pages = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            pages.get_style_context().add_class("linked")
            self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic",
                                                             Gtk.IconSize.BUTTON)
            self.back_button.set_tooltip_text("The word before (Alt+Left)")
            self.back_button.connect("clicked", lambda _w: self._turn_page(-1))
            self.forward_button = Gtk.Button.new_from_icon_name("go-next-symbolic",
                                                                Gtk.IconSize.BUTTON)
            self.forward_button.set_tooltip_text("The word after (Alt+Right)")
            self.forward_button.connect("clicked", lambda _w: self._turn_page(1))
            pages.pack_start(self.back_button, False, False, 0)
            pages.pack_start(self.forward_button, False, False, 0)
            header.pack_start(pages)
            self.lookup_entry = Gtk.SearchEntry(width_chars=24)
            self.lookup_entry.set_placeholder_text("Look up a word")
            self.lookup_entry.set_tooltip_text(
                "Enter: look it up, on a page of its own. Shift+Enter: add it below the "
                "words on this page, to compare them.")
            self.lookup_entry.connect(
                "activate", lambda e: self._look_up(e.get_text()))
            self.lookup_entry.connect("key-press-event", self._on_lookup_key)
            header.pack_start(self.lookup_entry)

        clear = Gtk.Button.new_from_icon_name("edit-clear-all-symbolic",
                                              Gtk.IconSize.BUTTON)
        clear.set_tooltip_text("Start over: clear this conversation (Ctrl+L). Its "
                               "settings stay, and Undo brings it back for a few seconds.")
        clear.connect("clicked", lambda _w: self._clear())
        header.pack_end(clear)

        copy_all = Gtk.Button.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.BUTTON)
        copy_all.set_tooltip_text("Copy the whole conversation as Markdown")
        copy_all.connect("clicked", self._on_copy_all)
        header.pack_end(copy_all)

        save = Gtk.Button.new_from_icon_name("document-save-symbolic", Gtk.IconSize.BUTTON)
        save.set_tooltip_text("Save the whole conversation as a Markdown file")
        save.connect("clicked", self._on_save_markdown)
        header.pack_end(save)

        self.search_toggle = Gtk.ToggleButton()
        self.search_toggle.set_tooltip_text(
            "Search the web before each reply in this conversation")
        self.search_toggle.add(Gtk.Image.new_from_icon_name(
            "system-search-symbolic", Gtk.IconSize.BUTTON))
        # web_search carries the choice made on the compose dialog for this request;
        # None means nothing was asked, so fall back to the saved default.
        self.search_toggle.set_active(
            bool(self.config.web_search_enabled) if web_search is None
            else bool(web_search))
        # Not offered in a private conversation: the query is made from the
        # conversation, and would leave the enclave for a search provider in the clear.
        if privacy is None:
            header.pack_end(self.search_toggle)
        else:
            self.search_toggle.set_active(False)
        self._sync_search_toggle()

        # Which model the next message goes to: a role ("text", "premium", "vision")
        # or "model:<id>" for one picked with Other model... It starts on the answering
        # model, as the premium option says; nothing re-reads that option afterwards.
        self._choice = "premium" if self.config.use_premium else "text"
        self._chosen = []
        self.model_button = Gtk.Button()
        self.model_button.set_tooltip_text(
            "The model your next message goes to. Change it at any point in the "
            "conversation; each answer is labelled with the model that wrote it.")
        self._model_label = Gtk.Label()
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        inner.pack_start(self._model_label, False, False, 0)
        inner.pack_start(Gtk.Image.new_from_icon_name("pan-down-symbolic",
                                                      Gtk.IconSize.BUTTON), False, False, 0)
        self.model_button.add(inner)
        self.model_button.connect("clicked", lambda w: self._popup_models(w, self._choose))
        header.pack_end(self.model_button)
        self._sync_model_display()

        self._main_box = box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        if privacy is not None:
            box.pack_start(self._build_privacy_banner(), False, False, 0)
        self._dictionary_list = dictionary_list
        self._dictionary_buttons = {}
        if lookup is not None and dictionary_list is not None:
            box.pack_start(self._build_dictionary_bar(dictionary_list()), False, False, 0)

        self.bubbles = None                 # the plain-text view, when WebKit is not used
        self.view = self._build_view()
        box.pack_start(self._build_splitter(self.view, self._build_input()), True, True, 0)
        # Ctrl+F's bar, over the whole window: what it searches is whichever view the
        # user was in (see _find_target).
        self.find_bar = find.FindBar(on_close=lambda: self.entry.grab_focus())
        self._finders = {}
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self.find_bar, False, False, 0)
        outer.pack_start(self._layout(box), True, True, 0)
        self.add(outer)

        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self._on_window_key)
        self.show_all()
        self.present()
        self.entry.grab_focus()
        self._render(full=True)
        self._sync_pages()
        if autostart:
            self._start_stream()
        if lookup is not None:
            if lookup_word:
                self.lookup_entry.set_text(lookup_word)
                self._look_up(lookup_word)
            else:
                self.lookup_entry.grab_focus()

    # ---- for subclasses -----------------------------------------------------

    def _layout(self, conversation):
        """The window's content, given the conversation box - transcript and input."""
        return conversation

    def _can_send(self, text):
        """Whether `text` may be sent now. False says why on the status line."""
        return True

    def _extra_context(self, question):
        """Reference material to send with the instructions for `question`."""
        return ""

    def _prepare_answer(self):
        """Read on the main loop before a request: what _gather needs, or None."""
        return None

    def _gather(self, prepared, cancel, status, private_kwargs):
        """Run in the worker before the answer: (text for the instructions, what
        _finish_answer and _after_answer are given). Raise to fail the request."""
        return "", None

    def _finish_answer(self, turn, payload):
        """Shape the finished answer before it is shown."""

    def _after_answer(self, turn, payload):
        """Add whatever follows the answer, once it is shown."""

    def _follow_encyclopedia(self, uri):
        """An enc:// link in the transcript."""

    def message_box_text(self, text):
        """Put `text` in the message box, selected: Enter sends it, typing replaces it.
        A clipboard that held something else costs one keystroke rather than a turn."""
        buf = self.entry.get_buffer()
        buf.set_text(text)
        buf.select_range(buf.get_start_iter(), buf.get_end_iter())
        self.entry.grab_focus()

    # ---- construction -----------------------------------------------------

    # ---- models -----------------------------------------------------------

    def _choices(self):
        """[(key, label, model)] for the model menu: the configured roles, then any
        model picked with Other model... In Privacy Mode, the session's private models:
        the roles are its own, fixed when the conversation started."""
        if self.privacy is not None:
            roles = [("text", "Private text", self.privacy.text_model),
                     ("premium", "Private premium", self.privacy.premium_model),
                     ("vision", "Private vision", self.privacy.vision_model)]
        else:
            # Each with the provider its setting names, which goes with it to where
            # the request is made (providers.Model).
            roles = [("text", "Text", self.config.model("text")),
                     ("premium", "Premium", self.config.model("premium")),
                     ("vision", "Vision", self.config.model("vision"))]
        return roles + [(self._chosen_key(model), "Chosen", model)
                        for model in self._chosen]

    @staticmethod
    def _chosen_key(model):
        """The menu key of a model picked with Other model...: with its provider, since
        two providers can each have a model of the same name."""
        provider = getattr(model, "provider", None)
        return f"model:{provider}:{model}" if provider else f"model:{model}"

    def _where(self, model):
        """' · Ollama' for a model asked of a provider other than NanoGPT, else ''."""
        return "" if self.privacy is not None else providers.where(self.config, model)

    def _model_of(self, key):
        for choice, _label, model in self._choices():
            if choice == key:
                return model
        return key.split(":", 1)[-1]

    def _label_of(self, model):
        """The model's part in the conversation, for status lines and errors."""
        for _key, label, candidate in self._choices():
            if candidate == model:
                return label
        return "Chosen"

    @property
    def current_model(self):
        return self._model_of(self._choice)

    def _vision_reader(self):
        return self.privacy.vision_model if self.privacy else self.config.model("vision")

    def _choose(self, key):
        """Send the next message to the model under `key`. False if it was refused."""
        model = self._model_of(key)
        if self.privacy is not None:
            try:
                self.privacy.enlist(model)
            except privacy_module.PrivacyError as e:
                self.status.set_text(str(e))
                return False
            self._refresh_privacy_banner()
        self._choice = key
        self._sync_model_display()
        return True

    def _choose_other(self, then):
        """Pick any model from the provider's list - a TEE one in Privacy Mode - and
        pass its key to `then`."""
        from .model_browser import ModelBrowser
        browser = ModelBrowser(self, self.config,
                               title=("Choose a TEE model for this conversation"
                                      if self.privacy else
                                      "Choose a model for this conversation"),
                               private_only=self.privacy is not None,
                               current=self.current_model,
                               provider=providers.provider_id(self.config,
                                                              self.current_model))
        browser.run()
        chosen, source = browser.selected_id, browser.provider
        browser.destroy()
        if not chosen:
            return
        if self.privacy is None:
            chosen = providers.Model(chosen, source)
        for key, _label, model in self._choices():
            if model == chosen and (providers.provider_id(self.config, model)
                                    == providers.provider_id(self.config, chosen)):
                then(key)
                return
        self._chosen.append(chosen)
        then(self._chosen_key(chosen))

    def _popup_models(self, widget, then):
        """The model menu under `widget`; picking an entry calls then(key)."""
        menu = Gtk.Menu()
        for key, label, model in self._choices():
            gone = " (no longer offered)" if model in self._retired else ""
            item = Gtk.CheckMenuItem(
                label=f"{label} — {model}{self._where(model)}{gone}")
            item.set_draw_as_radio(True)
            item.set_active(key == self._choice)
            item.connect("activate", lambda _w, k=key: then(k))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        other = Gtk.MenuItem(label="Other TEE model…" if self.privacy else "Other model…")
        other.connect("activate", lambda _w: self._choose_other(then))
        menu.append(other)
        menu.show_all()
        menu.attach_to_widget(widget, None)
        menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST,
                             Gtk.get_current_event())
        self._menu = menu                   # kept alive while it is open

    def settings_changed(self):
        """Settings were saved: the roles may name other models now, and the search
        be switched off or out of reach. The header names the model the next message
        goes to - a role's, read afresh at Send - so it is named again now, not left
        saying the one before until something else redraws it."""
        if self._destroyed:
            return
        self._sync_model_display()
        self._sync_search_toggle()

    def _sync_search_toggle(self):
        """The magnifier offered only when a search can run (websearch.unavailable);
        greyed, with why, when it cannot - left ticked or not as it was, for when it
        can again."""
        reason = websearch.unavailable(self.config)
        self.search_toggle.set_sensitive(reason is None)
        self.search_toggle.set_tooltip_text(
            reason or "Search the web before each reply in this conversation")

    def _search_now(self):
        """Whether this request searches: the magnifier, if a search can run."""
        return (self.search_toggle.get_active()
                and websearch.unavailable(self.config) is None)

    def _sync_model_display(self):
        """Name the model the next message goes to, in the header and on the menu."""
        model = self.current_model
        label = self._label_of(model)
        self._model_label.set_text((label if label != "Chosen" else model.rsplit("/", 1)[-1])
                                   + (" ⚠ no longer offered" if model in self._retired
                                      else ""))
        if self.privacy is not None:
            self.header.set_subtitle(f"🔒 Privacy Mode · {model}")
        else:
            self.header.set_subtitle(model + self._where(model))

    # ---- privacy ----------------------------------------------------------

    def privacy_uses(self):
        """Which private models this conversation would need, were it made private
        now: its answering model, and the vision model where it has pictures."""
        answering = "premium" if self.config.use_premium else "text"
        pictures = bool(self.image_base64 or self.screenshot) or any(
            turn.get("images") for page in self._pages for turn in page)
        return (answering, "vision") if pictures else (answering,)

    def go_private(self, session):
        """Privacy Mode was switched on with this conversation open: from its next
        message it goes only to `session`'s attested models, and never searches the
        web, as a conversation started now would. What it sent before went where it
        went - those turns carry no privacy badge - and an answer already on its way
        comes from where it was asked; the next message is the private one. False
        when it was private already."""
        if self.privacy is not None or self._destroyed:
            return False
        self._privacy_hold = None
        if self._cancel is not None:
            self._pending_privacy = session
        else:
            self._apply_privacy(session)
        return True

    def _apply_privacy(self, session):
        self.privacy = session
        session.listener = lambda *event: GLib.idle_add(self._on_privacy_event, *event)
        self.search_toggle.set_active(False)
        self.search_toggle.set_no_show_all(True)
        self.search_toggle.hide()
        # Its roles are the session's now; a model picked with Other model... is not
        # one of them, and the answering role takes its place.
        self._chosen = []
        if self._choice not in ("text", "premium", "vision"):
            self._choice = "premium" if self.config.use_premium else "text"
        banner = self._build_privacy_banner()
        self._main_box.pack_start(banner, False, False, 0)
        self._main_box.reorder_child(banner, 0)
        banner.show_all()
        self._sync_model_display()
        self.status.set_text(f"Privacy Mode: from here on, this conversation goes only "
                             f"to {self.current_model}, attested before anything is sent")

    def hold_for_privacy(self, reason):
        """Privacy Mode is on and cannot be used for this conversation: nothing is
        sent until it can be, or it is switched off - never to the ordinary models."""
        if self.privacy is not None or self._destroyed:
            return
        self._privacy_hold = reason
        self.status.set_text(f"Privacy Mode is on and cannot be used: {reason}")

    def release_privacy_hold(self):
        if self._privacy_hold and not self._destroyed:
            self._privacy_hold = None
            self.status.set_text("Privacy Mode is off; sending as before")

    def _held(self):
        """True, with the reason shown, while Privacy Mode holds this window's Send."""
        if self._privacy_hold:
            self.status.set_text(f"Privacy Mode is on and cannot be used: "
                                 f"{self._privacy_hold}")
            return True
        return False

    def _build_privacy_banner(self):
        """The strip under the header saying what this conversation's enclaves proved.

        It starts neutral - nothing has been checked yet - and is redrawn from the
        session's attestations as they arrive: green once every enclave used so far has
        proved itself, amber where one proved less than it could, red when one was
        refused and nothing was sent.
        """
        self.privacy_banner = bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                            spacing=10)
        bar.get_style_context().add_class("privacy-banner")
        self._privacy_icon = Gtk.Image.new_from_icon_name("channel-secure-symbolic",
                                                          Gtk.IconSize.LARGE_TOOLBAR)
        bar.pack_start(self._privacy_icon, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._privacy_title = Gtk.Label(xalign=0)
        self._privacy_title.get_style_context().add_class("privacy-title")
        self._privacy_detail = Gtk.Label(xalign=0, wrap=True, max_width_chars=110)
        text.pack_start(self._privacy_title, False, False, 0)
        text.pack_start(self._privacy_detail, False, False, 0)
        bar.pack_start(text, True, True, 0)

        details = Gtk.Button.new_with_label("Details…")
        details.set_valign(Gtk.Align.CENTER)
        details.connect("clicked", lambda _w: self._show_privacy_details())
        bar.pack_end(details, False, False, 0)
        self._refresh_privacy_banner()
        return bar

    def _refresh_privacy_banner(self):
        session = self.privacy
        attested = [session.attestations[m] for m in session.models()
                    if m in session.attestations]
        if self._privacy_refused:
            tone, icon = "bad", "dialog-warning-symbolic"
            title = ("No longer offered — nothing was sent" if self._refused_retired
                     else "Not attested — nothing was sent")
            detail = self._privacy_refused
        elif not attested:
            tone, icon = "pending", "channel-secure-symbolic"
            title = "Privacy Mode"
            detail = (f"Only {' and '.join(session.models())}, each attested before "
                      f"anything is sent to it. Web search is off.")
        else:
            # Amber for whatever Strict would have refused (Attestation.fell_short): an
            # end-to-end encrypted enclave whose release could not be checked against
            # the latest is "full" and names no newer release, and went green.
            partial = any(a.fell_short for a in attested)
            tone = "warn" if partial else "ok"
            icon = "channel-secure-symbolic"
            title = " · ".join(dict.fromkeys(
                ("🔐 " if a.kind == privacy_module.E2EE else "🔒 ") + a.headline
                for a in attested))
            lines = []
            for a in attested:
                if a.kind == privacy_module.E2EE:
                    short = "".join(f"; {what}" for what in a.fell_short)
                    lines.append(f"{a.model}: sealed on this machine to {a.enclave} "
                                 f"({a.hardware or 'attested hardware'}, {a.release}"
                                 f"{short}). nano-gpt relays ciphertext only.")
                else:
                    # What was attested first, and what that leaves to someone's word:
                    # RedPill's gateway is not the machine that runs the model.
                    what = privacy_module.what_was_attested(a, clear=False)
                    lines.append(f"{a.model} — {what + ' ' if what else ''}"
                                 f"{a.summary or a.detail}.")
            waiting = [m for m in session.models() if m not in session.attestations]
            if waiting:
                lines.append(f"{', '.join(waiting)}: will be attested before its first "
                             f"use.")
            detail = "\n".join(lines)
        context = self.privacy_banner.get_style_context()
        for name in ("ok", "warn", "bad", "pending"):
            context.remove_class(name)
        context.add_class(tone)
        self._privacy_icon.set_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
        self._privacy_title.set_text(title)
        self._privacy_detail.set_text(detail)

    def _on_privacy_event(self, event, model, detail):
        """From the worker, by way of the main loop: see PrivacySession.listener."""
        if self._destroyed:
            return False
        if event == "attesting":
            self._set_status(f"Attesting {model}'s enclave…")
        elif event == "attested":
            self._privacy_refused = None
            self._set_status(f"{detail.headline} ✓ {model}")
            self._refresh_privacy_banner()
        elif event == "refused":
            self._privacy_refused = detail
            self._refused_retired = False
            self._refresh_privacy_banner()
        elif event == "retired":
            self._retired[model] = detail.offered
            self._retired_pending = model
            self._refused_retired = True
            self._refresh_privacy_banner()
            self._sync_model_display()
        elif event == "sent":
            kind = privacy_module.kind_of(model)
            self._sent_kinds.add(kind)
            if self._streaming_text is not None:
                # What is arriving, while it arrives: the answer's own badge replaces
                # this once its signature has been checked, or its seal opened.
                self._streaming_privacy = self._badge(privacy_module.receiving_badge(kind))
            # Sealed, said now; sent to a TEE/ model, nothing yet shows the attested
            # enclave received it: the replies' checks will (_on_stream_done).
            self._badge_question(privacy_module.sent_badge(self._sent_kinds))
        elif event == "receipt":
            if detail is not None and detail.kind == privacy_module.TEE:
                self._tee_checks.append(detail.state)
        return False

    def _offer_another_model(self, retired):
        """nano-gpt has retired `retired`: nothing was sent to it, and nothing can be.
        Offer to ask the question again with an end-to-end encrypted model its list
        still has - the list just read, nothing more is fetched - which the conversation
        then carries on with. Settings are left alone."""
        known = {model for _key, _label, model in self._choices()}
        offered = [m for m in self._retired.get(retired, ())
                   if m not in self._retired]
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.WARNING,
                                   text="Model no longer available")
        dialog.format_secondary_text(
            f"{retired} is no longer offered by nano-gpt's Private Mode, so nothing was "
            f"sent to it.\n\n"
            + ("Ask your question again with another end-to-end encrypted model, which "
               "this conversation then carries on with - or choose any private model "
               "with the model button." if offered else
               "nano-gpt lists no other end-to-end encrypted model to carry it on; "
               "choose a TEE model with the model button.")
            + " New private conversations use the models under Settings → Privacy: "
              "choose another there too.")
        picker = None
        if offered:
            picker = Gtk.ComboBoxText()
            for model in offered:
                picker.append(model, model)
            # One the conversation already knows, if it can: its own text model, say.
            picker.set_active_id(next((m for m in offered if m in known), offered[0]))
            picker.set_margin_start(12)
            picker.set_margin_end(12)
            dialog.get_message_area().pack_start(picker, False, False, 0)
            picker.show()
            dialog.add_button("Close", Gtk.ResponseType.CLOSE)
            dialog.add_button("Ask again with it", Gtk.ResponseType.ACCEPT)
            dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        else:
            dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        response = dialog.run()
        chosen = picker.get_active_id() if picker is not None else None
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or not chosen or self._destroyed:
            return
        keys = {model: key for key, _label, model in self._choices()}
        if chosen not in keys:
            self._chosen.append(chosen)
        self._ask_again(keys.get(chosen, f"model:{chosen}"))

    @staticmethod
    def _badge(triple):
        """A badge from (label, tone, tooltip), and the mark a question's carries
        (privacy.sent_badge), if any."""
        if not triple:
            return None
        label, tone, tip, *mark = triple
        badge = {"label": label, "tone": tone, "tip": tip}
        if mark:
            badge["mark"] = mark[0]
        return badge

    def _badge_question(self, triple):
        """Set the badge on the question being answered, and redraw it."""
        for turn in reversed(self.conversation):
            if turn["role"] != "user":
                continue
            current = turn.get("privacy") or {}
            badge = self._badge(triple)
            # A transcription whose signature failed stays marked, whatever follows.
            if current.get("tone") == "bad" and badge and badge["tone"] != "bad":
                return
            # So does a question an earlier take showed the attested enclave received:
            # the same text was received, whatever Ask again shows (SealedLore,
            # 9af15c1). Only a failed check takes it back.
            if current.get("mark") == privacy_module.RECEIVED and badge \
                    and badge["tone"] != "bad":
                return
            turn["privacy"] = badge
            break
        self._render(full=True)

    def _show_privacy_details(self):
        session = self.privacy
        dialog = Gtk.Dialog(title="Privacy Mode", transient_for=self, modal=True)
        dialog.set_default_size(700, -1)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        text = session.details()
        if self._privacy_refused:
            text = f"Refused: {self._privacy_refused}\n\n{text}"
        text += ("\n\nThis conversation stays in Privacy Mode until it is closed, "
                 "whatever Settings or the tray say. Web search is off in it.\n\n"
                 + "\n\n".join(session.caveats(
                     bool(getattr(self.config, "tee_name_upstreams", False)))))
        view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD)
        view.get_buffer().set_text(text)
        for margin in ("start", "end", "top", "bottom"):
            getattr(view, f"set_margin_{margin}")(12)
        dialog.get_content_area().pack_start(view, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _build_view(self):
        """The WebKit view, or the plain-text one - never neither.

        Every way of failing here ends at the same place, because a chat window with no
        view at all is the worst of the three: the exception propagates out of __init__,
        the window never appears, and the user is left looking at a screenshot they just
        sent into nothing.
        """
        self._webkit_tried = False          # whether this window counts in webkitguard
        blocked, reason = webkitguard.blocked()
        if WebKit2 is None or blocked:
            # No usable WebKit (see gtkcompat), or one already seen to take the process
            # down with it (see webkitguard).
            if blocked and reason:
                self.app.notify(f"Chat view: {reason}")
            return self._build_text_view()

        # From here to the first rendered frame is the stretch that can end the process
        # rather than raise; if it does, the next window opens as plain text.
        self._webkit_tried = True
        webkitguard.note_attempt(self)
        try:
            return self._build_webkit_view()
        except Exception as e:
            # Anything the typelib lets through: a library that loaded but does not
            # work leaves classes that raise on first use, not on import.
            errors.record(f"WebKit view unavailable, using plain text: {e!r}")
            webkitguard.note_success(self)  # it raised rather than killed us
            self.app.notify(f"Chat view: WebKit failed ({e}); using plain text")
            return self._build_text_view()

    def _build_text_view(self):
        self.bubbles = bubbles.BubbleView(on_link=self._on_text_link,
                                          on_popup=self._on_text_popup)
        self.bubbles.set_zoom(self._zoom_level())
        self.bubbles.connect("scroll-event", self._on_zoom_scroll)
        self.webview = None
        if self.screenshot:
            self.bubbles.set_screenshot(
                screenshot_view.load(self.screenshot), self.get_scale_factor(),
                self._view_screenshot, self._on_save_screenshot)
        return self.bubbles

    def _on_text_link(self, url):
        """A link clicked in the plain-text view: the app's own are followed here, as
        the WebKit view follows them; True for those."""
        word = reference_markup.word_of(url)
        if word is not None:
            if self._lookup is not None:
                self.lookup_entry.set_text(word)
                self._look_up(word)
            return True
        path = reference_markup.dictionary_to_hide(url)
        if path is not None:
            self._hide_dictionary(path)
            return True
        if url.startswith("enc://"):
            self._follow_encyclopedia(url)
            return True
        return False

    def _build_webkit_view(self):
        # On the app's own context, which serves enc:// - the encyclopedia's pictures.
        from . import encscheme
        encscheme.install(getattr(self.app, "encyclopedia", None) or self._encyclopedia)
        manager = WebKit2.UserContentManager()
        manager.register_script_message_handler("copyText")
        # What is selected, as the page says it changes: the context menu offers to look
        # it up (ui/lookups.py), and cannot ask the page itself.
        self._selection = ""
        lookups.track_selection(manager, WebKit2, self._on_selection)
        manager.connect("script-message-received::copyText", self._on_copy_code)
        manager.register_script_message_handler("copyAnswer")
        manager.connect("script-message-received::copyAnswer", self._on_copy_answer)
        manager.add_script(WebKit2.UserScript(
            ANSWER_COPY_JS,
            WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.END, None, None))
        manager.add_script(WebKit2.UserScript(
            COPY_BUTTON_JS,
            WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.END, None, None))
        manager.add_script(WebKit2.UserScript(
            FOLLOW_JS,
            WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.END, None, None))
        if self.screenshot:
            manager.register_script_message_handler("screenshot")
            manager.connect("script-message-received::screenshot",
                            self._on_screenshot_message)
            manager.add_script(WebKit2.UserScript(
                SCREENSHOT_JS,
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.END, None, None))

        self.webview = WebKit2.WebView(web_context=encscheme.context(),
                                       user_content_manager=manager)
        self.webview.set_zoom_level(self._zoom_level())
        self.webview.connect("scroll-event", self._on_zoom_scroll)
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("web-process-terminated", self._on_web_process_gone)
        # Links to the browser, the view kept on the transcript, and a context menu
        # with nothing in it that navigates (webpolicy). Every page here is load_html's.
        self.guard = webpolicy.Guard(WebKit2, self.webview, on_link=self._on_web_link,
                                     on_drop=self._on_web_drop, say=self._set_status,
                                     extra=self._context_menu_extra)
        self.guard.expect("about:blank")
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_write_console_messages_to_stdout(False)
        # Scripts are the app's alone - the user scripts above and run_javascript - and
        # none of the page's: a <script> or an onclick= in its markup is dropped as it is
        # parsed, whatever got past the sanitiser and the page's policy.
        settings.set_enable_javascript_markup(False)
        bg = Gdk.RGBA()
        bg.parse("#1e1f22" if self.dark else "#ffffff")
        self.webview.set_background_color(bg)

        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.webview)
        return scrolled

    def _build_splitter(self, view, prompt):
        """The answers above the message box, with a bar between them to drag. Resizing
        the window goes to the answers, and the message box keeps its height - in the
        next window too."""
        view.set_size_request(-1, MIN_ANSWERS_HEIGHT)
        self.prompt_box = prompt
        self.splitter = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, wide_handle=True)
        self.splitter.get_style_context().add_class("chat-splitter")
        self.splitter.pack1(view, True, False)
        self.splitter.pack2(prompt, False, False)
        self._prompt_height_timer = None
        self.splitter.connect("size-allocate", self._on_splitter_sized)
        return self.splitter

    def _on_splitter_sized(self, splitter, allocation):
        """Once there is a size to work from: the height remembered, within reason."""
        if allocation.height <= 1:
            return
        splitter.disconnect_by_func(self._on_splitter_sized)
        wanted = self.config.chat_prompt_height
        if wanted:
            prompt = self.prompt_box.get_allocated_height()
            room = splitter.get_position() + prompt
            splitter.set_position(room - min(wanted, int(room * MOST_OF_WINDOW_FOR_PROMPT)))
        GLib.idle_add(self._watch_splitter)     # once that has been laid out

    def _watch_splitter(self):
        if not self._destroyed:
            self._splitter_height = self.splitter.get_allocated_height()
            self._prompt_height = self.prompt_box.get_allocated_height()
            self.splitter.connect("notify::position", self._on_splitter_moved)
        return False

    def _on_splitter_moved(self, splitter, _param):
        height = splitter.get_allocated_height()
        if height != self._splitter_height:
            self._splitter_height = height      # the window resized, not the bar dragged
            return
        if self._prompt_height_timer is not None:
            GLib.source_remove(self._prompt_height_timer)
        self._prompt_height_timer = GLib.timeout_add(PROMPT_HEIGHT_SAVE_MS,
                                                     self._save_prompt_height)

    # Window size: a conversation window opens at the size the last one was left, as
    # long as it fits the screen. A subclass that sizes itself sets this False.
    KEEPS_SIZE = True
    _last_save_folder = None        # where a file was last saved from any window

    def _initial_size(self):
        width = self.config.chat_width if self.KEEPS_SIZE else 0
        height = self.config.chat_height if self.KEEPS_SIZE else 0
        width, height = width or 880, height or 720
        display = Gdk.Display.get_default()
        monitor = display and (display.get_primary_monitor() or display.get_monitor(0))
        if monitor is not None:
            area = monitor.get_workarea()
            width, height = min(width, area.width), min(height, area.height)
        return max(width, 420), max(height, 320)

    def _on_configure(self, _widget, _event):
        if not self.KEEPS_SIZE:
            return False
        if self._size_timer is not None:
            GLib.source_remove(self._size_timer)
        self._size_timer = GLib.timeout_add(PROMPT_HEIGHT_SAVE_MS, self._save_size)
        return False

    def _save_size(self):
        self._size_timer = None
        if self._destroyed or self.is_maximized():
            return False            # a maximised window says nothing of the size wanted
        width, height = self.get_size()
        if (width, height) != (self.config.chat_width, self.config.chat_height):
            self.config.chat_width, self.config.chat_height = width, height
            try:
                self.config.save()
            except OSError as e:
                print(f"[chat] window size not kept: {e}")
        return False

    def _on_theme_changed(self, *_args):
        dark = _is_dark()
        if self._destroyed or dark == self.dark:
            return
        self.dark = dark
        if self.webview is not None:
            bg = Gdk.RGBA()
            bg.parse("#1e1f22" if dark else "#ffffff")
            self.webview.set_background_color(bg)
        self._render(full=True, new_page=True)

    def _save_prompt_height(self):
        self._prompt_height_timer = None
        height = self.prompt_box.get_allocated_height()
        if height != self._prompt_height:
            self._prompt_height = self.config.chat_prompt_height = height
            try:
                self.config.save()
            except OSError as e:
                self.app.notify(f"Could not save settings: {e}", urgency="normal")
        return False

    def _build_input(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        # What is attached to the next message, a chip each; hidden while there is none.
        self.attachment_bar = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                          column_spacing=6, row_spacing=4,
                                          max_children_per_line=6, homogeneous=False)
        self.attachment_bar.set_no_show_all(True)
        box.pack_start(self.attachment_bar, False, False, 0)

        frame = Gtk.ScrolledWindow()
        frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame.set_min_content_height(54)
        frame.set_max_content_height(150)
        frame.get_style_context().add_class("preview-frame")

        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
        self.entry.set_margin_start(8)
        self.entry.set_margin_end(8)
        self.entry.set_margin_top(6)
        self.entry.set_margin_bottom(6)
        self.entry.connect("key-press-event", self._on_entry_key)
        self.entry.connect("paste-clipboard", self._on_entry_paste)
        lookups.attach_to_textview(self.entry, self.config, self._look_up_elsewhere)
        # Files dropped on the message box are attached, not pasted in as their paths.
        self.entry.drag_dest_add_uri_targets()
        self.entry.connect("drag-data-received", self._on_entry_drop)
        frame.add(self.entry)
        box.pack_start(frame, True, True, 0)        # all the room the bar above gives it

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        attach = Gtk.Button.new_from_icon_name("mail-attachment-symbolic",
                                               Gtk.IconSize.BUTTON)
        attach.set_tooltip_text(
            "Attach pictures or text files (Markdown, JSON, logs, code…) to your next "
            "message. Pictures open in an editor to black out anything sensitive, and "
            "are re-encoded with no metadata before they are sent.")
        attach.connect("clicked", lambda _w: self._choose_attachments())
        row.pack_start(attach, False, False, 0)
        hint = Gtk.Label(label="Enter to send  ·  Shift+Enter for a new line", xalign=0)
        hint.get_style_context().add_class("toolbar-hint")
        row.pack_start(hint, False, False, 0)
        row.pack_start(Gtk.Box(), True, True, 0)

        self.status = Gtk.Label(label="", xalign=1)
        self.status.get_style_context().add_class("dim-label")
        # A long status - a privacy hold's reason, a dictionary hidden - is cut short
        # with an ellipsis and given whole in its tooltip: unbounded, its width was the
        # window's least, and one message pushed the window past the screen's edge.
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.connect("notify::label", lambda label, _spec: label.set_tooltip_text(
            label.get_text() or None))
        row.pack_start(self.status, False, False, 0)

        self.undo_button = Gtk.Button.new_with_label("Undo")
        self.undo_button.set_tooltip_text("Bring back the conversation just cleared")
        self.undo_button.connect("clicked", lambda _w: self._undo_clear())
        self.undo_button.set_no_show_all(True)
        row.pack_start(self.undo_button, False, False, 0)

        self.again_button = Gtk.Button.new_with_label("Ask again with…")
        self.again_button.set_tooltip_text(
            "Ask your last question again with another model. The earlier answer stays "
            "on screen, but only the new one is carried into the conversation.")
        self.again_button.connect("clicked",
                                  lambda w: self._popup_models(w, self._ask_again))
        row.pack_start(self.again_button, False, False, 0)

        self.stop_button = Gtk.Button.new_with_label("Stop")
        self.stop_button.connect("clicked", lambda _w: self._stop())
        self.stop_button.set_no_show_all(True)
        row.pack_start(self.stop_button, False, False, 0)

        self.send_button = Gtk.Button.new_with_label("Send")
        self.send_button.get_style_context().add_class("suggested-action")
        self.send_button.connect("clicked", lambda _w: self._send())
        row.pack_start(self.send_button, False, False, 0)

        box.pack_start(row, False, False, 0)
        return box

    # ---- webview plumbing -------------------------------------------------

    def _on_load_changed(self, _view, event):
        if event == WebKit2.LoadEvent.FINISHED:
            webkitguard.note_success(self)  # it rendered; it is not what kills us
            self._page_loading = False
            after, self._after_load = self._after_load, None
            if after == "page":
                self._render(new_page=True)     # asked for while this one loaded
                return
            self._loaded = True
            if after == "full":
                self._render(full=True)         # what changed while it loaded
            for script in self._pending_js:
                self.webview.run_javascript(script, None, None, None)
            self._pending_js.clear()

    def _on_web_process_gone(self, _view, _reason):
        # The page went with it, and a redraw in place needs one: the next loads it anew.
        self._loaded = self._page_loading = False
        self._after_load = None

    def _on_web_link(self, uri, _kind):
        """A link clicked in the WebKit view (webpolicy.Guard): a dictionary's
        cross-reference looks its word up here, an encyclopedia link opens the article;
        True for those. Anything else is the browser's."""
        return self._on_text_link(uri)

    def _on_web_drop(self, uri):
        """A file dropped on the conversation: attached, never opened in it."""
        self.attach_files([GLib.filename_from_uri(uri)[0]])

    def _context_menu_extra(self, menu, hit):
        """Items of this window's own for the transcript's context menu: the lookups,
        for a selection right-clicked."""
        if hit is not None and hit.context_is_selection():
            lookups.add_to_webkit_menu(WebKit2, menu, self.config, self._selection,
                                       self._look_up_elsewhere)

    def _on_text_popup(self, view, menu):
        """A turn's right-click menu in the plain-text view: the lookups too."""
        lookups.add_to_gtk_menu(menu, self.config, lookups.selected_text(view),
                                self._look_up_elsewhere)

    def _on_selection(self, text):
        self._selection = text

    def _look_up_elsewhere(self, kind, what):
        """A selection right-clicked and looked up: in a window of its own."""
        look_up = getattr(self.app, "look_up_selection", None)
        if look_up is not None:
            look_up(kind, what)

    def _on_copy_answer(self, _manager, result):
        """An answer's own Copy button: that answer, as the Markdown it was - found by
        its place in the conversation, and only if what is there is a finished answer."""
        try:
            index = int(result.get_js_value().to_string())
        except (AttributeError, ValueError):
            return
        turns = self._turns()
        if not 0 <= index < len(turns):
            return
        turn = turns[index]
        if turn.get("role") != "assistant" or turn.get("_streaming"):
            return
        text = turn.get("export", turn.get("display", turn.get("content", "")))
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.status.set_text("Copied the answer")

    # ---- text size ----------------------------------------------------------

    def _zoom_level(self):
        try:
            level = float(getattr(self.config, "chat_zoom", 1.0) or 1.0)
        except (TypeError, ValueError):
            level = 1.0
        return min(max(level, ZOOM_STEPS[0]), ZOOM_STEPS[-1])

    def _zoom_views(self):
        """The WebKit views Ctrl+plus and Ctrl+minus size: the transcript's."""
        return [self.webview] if self.webview is not None else []

    def _zoom(self, step):
        """Ctrl+plus (1), Ctrl+minus (-1), Ctrl+0 (0): the conversation's text larger,
        smaller or back to the theme's own - and kept for the next window."""
        current = self._zoom_level()
        if step == 0:
            level = 1.0
        elif step > 0:
            level = next((z for z in ZOOM_STEPS if z > current + 0.001), ZOOM_STEPS[-1])
        else:
            level = next((z for z in reversed(ZOOM_STEPS) if z < current - 0.001),
                         ZOOM_STEPS[0])
        for view in self._zoom_views():
            view.set_zoom_level(level)
        if self.bubbles is not None:
            self.bubbles.set_zoom(level)
        self.config.chat_zoom = level
        try:
            self.config.save()
        except OSError as e:
            print(f"[chat] text size not kept: {e}")
        self.status.set_text(f"Text size {round(level * 100)}%")

    def _on_zoom_scroll(self, _widget, event):
        """Ctrl and the wheel: the text size, as in a browser."""
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        if event.direction == Gdk.ScrollDirection.UP:
            self._zoom(1)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self._zoom(-1)
        else:
            found, _dx, dy = event.get_scroll_deltas()
            if found and dy:
                self._zoom(-1 if dy > 0 else 1)
        return True

    def _on_copy_code(self, _manager, result):
        try:
            text = result.get_js_value().to_string()
        except AttributeError:
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def _js(self, script):
        if self.webview is None:
            return
        if self._loaded:
            self.webview.run_javascript(script, None, None, None)
        else:
            self._pending_js.append(script)

    # ---- rendering --------------------------------------------------------

    def _turns(self):
        turns = list(self.conversation)
        if self._streaming_text is not None:
            turns.append({"role": "assistant", "content": self._streaming_text,
                          "model": self._streaming_model,
                          "privacy": self._streaming_privacy,
                          "_streaming": True})
        return turns

    @staticmethod
    def _turn_html(turn, index=None):
        """`turn`, which is turn `index` of the conversation shown."""
        if turn.get("role") in REFERENCE_ROLES:
            return turn["html"]
        return markdown_html.turn_html(turn["role"],
                                       turn.get("display", turn.get("content", "")),
                                       turn.get("is_premium", False),
                                       turn.get("_streaming", False), turn.get("privacy"),
                                       turn.get("model"), turn.get("superseded", False),
                                       links=turn.get("links"),
                                       attachments=turn.get("attachments"), index=index)

    def _render(self, full=False, new_page=False):
        """Draw the transcript. By default only the final turn, as streaming wants;
        `full`, every turn, on the page already there so the reader's place holds;
        `new_page`, a different page altogether, from its top."""
        if self._destroyed:
            return                      # the view is gone; see _on_destroy
        turns = self._turns()
        if full or new_page:
            self._sync_again()

        if self.webview is None:
            # Same text the WebKit view shows, including `display` rather than the
            # prompt in `content`: the instructions sent with a turn are not something
            # the reader asked to see, and for OCR they are most of the message.
            if new_page:
                self.bubbles.resume_following()
            self.bubbles.set_turns(turns)
            GLib.idle_add(self._scroll_text_to_end)     # after the new rows are sized
            return

        if self._page_loading:
            # One load at a time, and this redraw once it is done (_on_load_changed):
            # loaded again on every redraw, as an answer streaming in asked for, a load
            # slower than the redraw interval never finished, and a load replaced still
            # says it has, so the redraws after it went to a page about to go.
            if new_page or self._after_load == "page":
                self._after_load = "page"
            else:
                self._after_load = "full"
            return

        if new_page or not self._loaded:
            body = self._screenshot_html + "".join(self._turn_html(t, i)
                                                   for i, t in enumerate(turns))
            self._loaded = False
            self.webview.load_html(markdown_html.document(body, self.dark), None)
            # Once it is loading: had it raised, every redraw after waited for it.
            self._page_loading = True
            return

        if full:
            html = "".join(self._turn_html(t, i) for i, t in enumerate(turns))
            # #end is put back at the foot of the page if it is not there: a turn whose
            # markup had wrapped it went with the turns, and the redraw that then found
            # no #end threw, leaving the page blank.
            self._js(
                "(function(){"
                "document.querySelectorAll('body > .turn').forEach(function(t){t.remove();});"
                "var e=document.getElementById('end');"
                "if(!e||e.parentNode!==document.body){if(e){e.remove();}"
                "e=document.createElement('div');e.id='end';document.body.appendChild(e);}"
                f"e.insertAdjacentHTML('beforebegin',{json.dumps(html)});"
                + ("window.followEnd();" if self._streaming_text is not None else "")
                + "})();")
            return

        # Incremental: replace only the final turn, so streaming does not reflow the
        # whole document on every token.
        if not turns:
            return
        html = self._turn_html(turns[-1], len(turns) - 1)
        # With no turn yet, the first goes before #end: after the screenshot, if any.
        self._js(
            "(function(){var t=document.querySelectorAll('body > .turn');"
            f"var h={json.dumps(html)};"
            "if(t.length){t[t.length-1].outerHTML=h;}"
            "else{var e=document.getElementById('end');"
            "if(e){e.insertAdjacentHTML('beforebegin',h);}"
            "else{document.body.insertAdjacentHTML('beforeend',h);}}"
            "window.followEnd();})();")

    def _scroll_text_to_end(self):
        # Queued while the window was open, and it may have closed since: a destroyed
        # scrolled window's adjustment is a GTK critical, not None.
        if not self._destroyed:
            self.bubbles.follow_end()
        return False                                    # one-shot idle callback

    def _scroll_text_to_turn(self, index):
        if not self._destroyed:
            self.bubbles.scroll_to_turn(index)
        return False

    def _append_turn(self, turn, replace_placeholder=False, scroll="end"):
        """Add a turn to the transcript.

        While streaming, _render() puts a placeholder .turn in the DOM holding the
        partial text. That element is a DOM-only artefact - it is not in
        self.conversation - so when the stream finishes the finished turn must REPLACE
        it. Appending instead leaves the placeholder in place and the response appears
        twice, which is what happened before replace_placeholder existed.

        `scroll`: "end" follows the conversation down, as an answer wants, unless the
        reader has scrolled up from it (see FOLLOW_JS); "start" puts
        the new turn's top at the top of the view, as reference material wants - a
        lookup's first dictionary is the one the user ranked first, and was scrolled
        past to the last; None leaves the view where it is.
        """
        self.conversation.append(turn)
        # The first turn from the very top - a screenshot above it is part of it.
        to_start = ("(function(){var t=document.querySelectorAll('body > .turn');"
                    "if(t.length==1){window.scrollTo(0,0);}"
                    "else if(t.length){t[t.length-1].scrollIntoView({block:'start'});}"
                    "})();")
        if self.webview is None or not self._loaded:
            self._render(full=True)
            if scroll == "start":
                if self.webview is None:
                    GLib.idle_add(self._scroll_text_to_turn, len(self.conversation) - 1)
                else:
                    self._js(to_start)          # once the page has loaded
            return
        html = self._turn_html(turn, len(self.conversation) - 1)
        if scroll != "end":
            self._js(
                "(function(){var h=" + json.dumps(html) + ";"
                "var e=document.getElementById('end');"
                "if(e){e.insertAdjacentHTML('beforebegin',h);}"
                "else{document.body.insertAdjacentHTML('beforeend',h);}})();"
                + (to_start if scroll == "start" else ""))
            return
        if replace_placeholder:
            self._js(
                "(function(){var t=document.querySelectorAll('body > .turn');"
                f"var h={json.dumps(html)};"
                "if(t.length){t[t.length-1].outerHTML=h;}"
                "else{document.body.insertAdjacentHTML('beforeend',h);}"
                "window.followEnd();})();")
            return
        self._js(
            "(function(){var h=" + json.dumps(html) + ";"
            "var e=document.getElementById('end');"
            "if(e){e.insertAdjacentHTML('beforebegin',h);}"
            "else{document.body.insertAdjacentHTML('beforeend',h);}"
            "window.followEnd();})();")

    # ---- sending ----------------------------------------------------------

    def _on_entry_key(self, widget, event):
        if event.keyval in keys.ENTER:
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                return False        # let GTK insert the newline itself
            if keys.input_method_takes(widget, event):
                return True         # a candidate committed, not the message sent
            self._send()
            return True
        return False

    def _send(self):
        buf = self.entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        attached = list(self._attachments)
        if (not text and not attached) or self._cancel is not None or \
                not self._can_send(text):
            return
        if self._looking:
            # The lookup, when it lands, makes a page of its own: an answer streaming
            # into this one would land on that word's page, and this question stay
            # unanswered on the last. The question waits in the box.
            self.status.set_text("Wait for the lookup to finish, then send")
            return
        if self._held():
            return
        buf.set_text("")
        self._forget_undo()
        self._attachments.clear()
        self._sync_attachment_bar()

        self._append_turn(attachments.compose(text, attached) if attached
                          else {"role": "user", "content": text})
        self._start_stream()

    # ---- attachments ------------------------------------------------------

    def _choose_attachments(self):
        dialog = Gtk.FileChooserNative.new("Attach pictures or text files", self,
                                           Gtk.FileChooserAction.OPEN, "_Attach",
                                           "_Cancel")
        dialog.set_select_multiple(True)
        both = Gtk.FileFilter()
        both.set_name("Pictures and text files")
        pictures = Gtk.FileFilter()
        pictures.set_name("Pictures")
        texts = Gtk.FileFilter()
        texts.set_name("Text files")
        for pattern in attachments.IMAGE_PATTERNS:
            for chosen in (both, pictures):
                chosen.add_pattern(pattern)
                chosen.add_pattern(pattern.upper())
        for pattern in attachments.TEXT_PATTERNS:
            for chosen in (both, texts):
                chosen.add_pattern(pattern)
        for chosen in (both, texts):
            chosen.add_mime_type("text/*")
        everything = Gtk.FileFilter()
        everything.set_name("All files")
        everything.add_pattern("*")
        for chosen in (both, pictures, texts, everything):
            dialog.add_filter(chosen)
        accepted = dialog.run() == Gtk.ResponseType.ACCEPT
        paths = dialog.get_filenames() if accepted else []
        dialog.destroy()
        self.attach_files(paths)

    def _on_entry_drop(self, widget, context, _x, _y, data, _info, time_):
        uris = data.get_uris()
        if not uris:
            return False                    # text: the message box takes it as usual
        widget.stop_emission_by_name("drag-data-received")
        Gtk.drag_finish(context, True, False, time_)
        paths = [GLib.filename_from_uri(uri)[0] for uri in uris
                 if uri.startswith("file://")]
        self.attach_files(paths)
        return True

    def attach_files(self, paths):
        """Attach these files to the next message, one at a time: each picture opens in
        the black-out editor and is attached only when the user says so. A file that
        cannot be attached is named on the status line, and the rest go ahead."""
        queue = [path for path in paths if path]

        def next_file():
            while queue and not self._destroyed:
                path = queue.pop(0)
                name = os.path.basename(path)
                try:
                    raw = attachments.read_file(path)
                    picture = attachments.sniff_image(raw)
                    if picture is None:
                        self._add_attachment(attachments.text_attachment(
                            name, attachments.decode_text(raw, name)))
                        continue
                except attachments.AttachmentError as e:
                    message = str(e)
                    self.status.set_text(f"Not attached: {message}" if name in message
                                         else f"Not attached: {name}: {message}")
                    continue
                except Exception as e:
                    # Not a refusal of attachments' own, and it must not cost the files
                    # after it, as a ValueError from Pillow once did. Logged by its kind
                    # alone: what it says can quote the file.
                    errors.record(f"Attaching a file failed: {type(e).__name__}")
                    self.status.set_text(f"Not attached: {name} could not be read, as a "
                                         f"picture or as text")
                    continue
                image, photo = picture

                def done(result, _boxes, name=name, photo=photo):
                    if self._destroyed:
                        return
                    if result is None:
                        self.status.set_text(f"{name} not attached")
                    else:
                        self._add_attachment(attachments.image_attachment(
                            name, result, photo, self.config))
                    next_file()

                self._redact(image, done, name)
                return

        next_file()

    def attach_image(self, image, name):
        """Attach a picture from elsewhere - the clipboard - through the black-out
        editor, as a picture file is."""
        def done(result, _boxes):
            if self._destroyed:
                return
            if result is None:
                self.status.set_text(f"{name} not attached")
            else:
                self._add_attachment(attachments.image_attachment(
                    name, result, False, self.config))

        self._redact(image, done, name)

    def _on_entry_paste(self, entry):
        """Ctrl+V, or Paste from the box's menu: a picture on the clipboard, and no
        text, is attached - through the black-out editor - rather than pasting nothing."""
        board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        if board.wait_is_text_available() or not board.wait_is_image_available():
            return
        from .. import clipboard
        image = clipboard.get_image()
        if image is None:
            if clipboard.read_too_much():
                # Not nothing, as the paste that follows would make it look.
                entry.stop_emission_by_name("paste-clipboard")
                self.status.set_text(f"Not attached: {clipboard.unreadable_hint()}")
            return
        entry.stop_emission_by_name("paste-clipboard")
        self.attach_image(image, "Pasted picture")

    def _redact(self, image, on_done, name):
        """The black-out editor, for a picture about to be attached."""
        from .redact import RedactDialog
        RedactDialog(self, image, on_done, title=f"Attach {name}",
                     apply_label="Attach")

    def _add_attachment(self, attachment):
        self._attachments.append(attachment)
        self._sync_attachment_bar()
        self.status.set_text(f"Attached {attachment['name']}")
        self.entry.grab_focus()

    def _remove_attachment(self, attachment):
        self._attachments = [a for a in self._attachments if a is not attachment]
        self._sync_attachment_bar()

    def _sync_attachment_bar(self):
        if self._destroyed:
            return
        from gi.repository import GdkPixbuf
        bar = self.attachment_bar
        for child in bar.get_children():
            child.destroy()
        for attachment in self._attachments:
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            chip.get_style_context().add_class("attachment-chip")
            picture = None
            if attachment.get("thumb"):
                try:
                    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                    loader.write(attachment["thumb"])
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    side = 28
                    scale = side / max(pixbuf.get_width(), pixbuf.get_height())
                    picture = Gtk.Image.new_from_pixbuf(pixbuf.scale_simple(
                        max(1, round(pixbuf.get_width() * scale)),
                        max(1, round(pixbuf.get_height() * scale)),
                        GdkPixbuf.InterpType.BILINEAR))
                except Exception:
                    picture = None
            if picture is None:
                picture = Gtk.Image.new_from_icon_name(
                    "image-x-generic-symbolic" if attachment["kind"] == "image"
                    else "text-x-generic-symbolic", Gtk.IconSize.BUTTON)
            chip.pack_start(picture, False, False, 0)
            chip.pack_start(Gtk.Label(label=attachments.describe(attachment)),
                            False, False, 0)
            remove = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                                   Gtk.IconSize.MENU)
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.set_tooltip_text("Don't attach this")
            remove.connect("clicked", lambda _w, a=attachment: self._remove_attachment(a))
            chip.pack_start(remove, False, False, 0)
            child = Gtk.FlowBoxChild()
            child.add(chip)
            child.show_all()            # the bar is no_show_all, so show_all stops at it
            bar.add(child)
        bar.set_visible(bool(self._attachments))

    def _set_status(self, message, cancel=None):
        """The status line, from the main loop; with `cancel`, for that request only.
        A worker stopped carries on until it notices, and what it had to say - "The web
        search failed", "Reading the image…" - landed on the next request's line."""
        if not self._stale(cancel):
            self.status.set_text(message)
        return False

    def _apply_preflight(self, update, turn):
        """Fold the preflight result into the visible transcript, on `turn`, the one it
        was made for - not the latest, which after a failed first attempt is what was
        typed since. The preflight has done its work, and is not made again.

        `content` becomes what follow-up turns carry as context; `display` is what the
        user sees, so the window shows the transcribed text rather than the machinery.
        """
        if self._destroyed:
            return False
        self._preflight = None
        if not update:
            return False
        if any(t is turn for t in self.conversation):
            turn["content"] = update.get("content", turn["content"])
            if update.get("display"):
                turn["display"] = update["display"]
            if update.get("search_query"):
                turn["search_query"] = update["search_query"]
        if update.get("reading"):
            self.reading = update["reading"]
            self._reading_in_history = True
        self._render(full=True)
        return False

    def _ask_again(self, key):
        """Ask the last question again, with the model under `key` - which is also where
        the conversation goes on from. The answers since stay on screen, marked as
        earlier ones, and drop out of the history the new answer is given."""
        if self._cancel is not None:
            return
        if self._looking:
            self.status.set_text("Wait for the lookup to finish, then ask again")
            return
        if self._held():
            return
        last = max((i for i, t in enumerate(self.conversation) if t["role"] == "user"),
                   default=None)
        if last is None or not self._choose(key):
            return
        replacing = [turn for turn in self.conversation[last + 1:]
                     if turn["role"] == "assistant" and not turn.get("error")
                     and not turn.get("superseded")]
        for turn in replacing:
            turn["superseded"] = True
        self._render(full=True)
        self._start_stream(replacing)

    def _restore_replaced(self):
        """The answer asked again for never came - an error, nothing at all, or a Stop
        before a word of it - so the answers it was to replace are the conversation's
        again, as before. Marked earlier ones for good, the next question went out as
        though the model had never answered: one turn of both questions, without it."""
        replacing, self._replacing = self._replacing, []
        for turn in replacing:
            turn.pop("superseded", None)
        return bool(replacing)

    def _resume_following(self):
        """A new request: back to the end, wherever the reader had gone, and down with
        the answer as it comes."""
        if self.webview is None:
            self.bubbles.resume_following()     # the redraw that follows goes there
        else:
            self._js("window.resumeFollowing();")

    def _start_stream(self, replacing=()):
        """Send the conversation for its next answer. `replacing`: the answers this one
        is asked again for, marked earlier ones until it has come (_restore_replaced)."""
        import threading

        self._replacing = list(replacing)
        self._resume_following()
        # The first request's preflight, kept until it has done its work
        # (_apply_preflight); follow-ups are plain text turns. One that failed, or was
        # stopped, is made again for the next request - Ask again or a message typed -
        # which otherwise went out with the turn still waiting for it: empty, or holding
        # only what was typed, so that a translation translated "try again".
        preflight, target = self._preflight, self._preflight_turn
        if preflight is not None and not any(turn is target for turn in self.conversation):
            preflight = self._preflight = None      # cleared away with its turn
        first = self._first_request
        self._first_request = False
        # Made again, the first request goes as the first did: with the capture only if
        # the operation said so, and no second reading of it beside the preflight's.
        redo = preflight is not None and not first

        privacy = self.privacy
        model = self._first_model if first and self._first_model else self.current_model
        # OCR: the first answer is the vision model's transcription of the capture,
        # and the reading a model that cannot see would otherwise have made for itself
        # (_keep_transcription).
        self._answer_is_reading = bool(first and self._first_model and self.image_base64
                                       and self._image_first)
        if privacy is not None:
            try:
                privacy.enlist(model)
            except privacy_module.PrivacyError as e:
                self._show_error(str(e))
                return

        # The capture: the first request carries it if the operation says so; later
        # ones if their model can see, and otherwise the vision model's reading of it.
        # Made again, a first request that went without it goes without it again, its
        # preflight being the reading; one that carried it is a later one's choice, its
        # model perhaps changed since to one that cannot see.
        image, need_reading, reading_in_system = None, False, False
        if self.image_base64:
            if first:
                image = self.image_base64 if self._image_first else None
            elif redo and not self._image_first:
                pass
            elif catalogue.accepts_images(self.config, model):
                image = self.image_base64
            else:
                need_reading = self.reading is None
                reading_in_system = not self._reading_in_history
        reader = self._vision_reader()
        # Pictures attached to messages: as images to a model that can see, and to one
        # that cannot, as the vision model's reading of each - made once per picture.
        sees = any(turn.get("images") for turn in self.conversation) and \
            bool(catalogue.accepts_images(self.config, model))

        if privacy is not None:
            # No search: the query would carry the conversation out in the clear.
            # LLMClient refuses anything but the session's models, before sending.
            web_search = False
            self._sent_kinds = set()
            self._tee_checks = []
            self._badge_question(privacy_module.pending_badge(privacy))
        else:
            web_search = self._search_now()

        self._cancel = threading.Event()
        self._redraw_queued = False
        self._streaming_text = ""
        self._streaming_model = model
        self._streaming_privacy = self._badge(
            privacy_module.pending_badge(privacy)) if privacy else None
        self.send_button.set_sensitive(False)
        self.again_button.set_sensitive(False)
        self.stop_button.show()
        self.status.set_text("Preparing a private request…" if privacy
                             else "Searching the web…" if web_search else "Thinking…")
        self._render(full=True)

        # Snapshot the history here; the worker works on its own copy so a preflight
        # rewrite cannot race the main thread. With a preflight, the turns themselves
        # too, for the worker to put what it made into the turn it was made for - the
        # first, whatever has been typed since - and the history built again from them.
        # search_query rides along when a preflight built one; strip_internal_keys drops
        # it before the request, but the search reads it first.
        history = history_for(self.conversation)
        turns = at = None
        if preflight is not None:
            turns = [dict(turn) for turn in self.conversation]
            at = next(i for i, turn in enumerate(self.conversation) if turn is target)
        system = self._request_system(model)
        known_reading = self.reading
        prepared = self._prepare_answer()

        cancel = self._cancel

        def say(message):
            # The status line, for this request alone (_set_status).
            GLib.idle_add(self._set_status, message, cancel)

        # The status line, for what the request has to say on its way - a web search
        # that failed, earlier messages left out to fit the model - and, in a private
        # conversation, its session and each step of attesting.
        private_kwargs = {"status": say}
        if privacy is not None:
            private_kwargs["privacy"] = privacy

        def on_delta(_piece, full_text):
            # One redraw waiting at a time. Each renders the whole answer so far, and
            # past some 20,000 characters that takes longer than the interval: queued
            # regardless, they piled up behind the main loop, every one for text the
            # next had already replaced, and the finished answer waited at the back.
            now = time.monotonic()
            if self._redraw_queued or now - self._last_redraw < STREAM_REDRAW_INTERVAL:
                return
            self._last_redraw = now
            self._redraw_queued = True
            GLib.idle_add(self._on_stream_progress, full_text, cancel)

        def worker():
            nonlocal history
            stage = "answer"
            reading = known_reading
            def answer_can_go():
                # The answering model's provider before a step made only for its answer
                # - a reading of the capture or of a picture - goes out: not sent for an
                # answer that cannot be (LLMClient checks again for the answer itself).
                if privacy is None:
                    refused = providers.of(self.config, model).problem()
                    if refused:
                        raise LLMError(f"Nothing was sent. {refused}", model=model)

            try:
                if preflight is not None:
                    answer_can_go()
                    stage = "read"
                    update = preflight(cancel, say, privacy=privacy,
                                       web_search=web_search)
                    # A model's reply, and JSON can spell half a character: sound_text.
                    update = {key: sound_text(value) if isinstance(value, str) else value
                              for key, value in (update or {}).items()}
                    if cancel.is_set():
                        GLib.idle_add(self._on_stream_done, None, cancel)
                        return
                    if privacy is not None:
                        # The transcription was a reply too: if its check failed,
                        # the question built from it says so.
                        read = privacy.pop_receipt()
                        if read is not None and read.state == privacy_module.FAILED:
                            GLib.idle_add(self._badge_question, (
                                "⚠ Transcription · check failed", "bad",
                                read.detail))
                    if update and update.get("content"):
                        turns[at]["content"] = update["content"]
                        if update.get("search_query"):
                            turns[at]["search_query"] = update["search_query"]
                        history = history_for(turns)
                    # Done, whatever it found: not made again.
                    GLib.idle_add(self._apply_preflight, update, target)
                    say("Searching the web…" if web_search else "Thinking…")
                if need_reading:
                    # A model that cannot see, in a conversation about an image: the
                    # vision model reads it once - its text and a description - and
                    # that reading goes with this request and every later one like it.
                    answer_can_go()
                    stage = "read"
                    say(f"Reading the image for {model.rsplit('/', 1)[-1]}…")
                    if privacy is not None:
                        privacy.enlist(reader)
                    reading = sound_text((self.app.client.complete(
                        reader, [{"role": "user", "content": prompts.IMAGE_BRIEF}],
                        image_base64=self.image_base64, cancel=cancel,
                        **private_kwargs) or "").strip())
                    if cancel.is_set():
                        GLib.idle_add(self._on_stream_done, None, cancel)
                        return
                    self._note_reading_receipt(privacy, "Image reading")
                    if not reading:
                        raise LLMError("The vision model returned nothing about the image",
                                       model=reader)
                    GLib.idle_add(self._keep_reading, reading)
                unread = [] if sees else attachments.unread(history)
                if unread:
                    answer_can_go()
                    stage = "read"
                    if privacy is not None:
                        privacy.enlist(reader)
                    readings = {}
                    for picture in unread:
                        say(f"Reading {picture['name']} for {model.rsplit('/', 1)[-1]}…")
                        text = sound_text((self.app.client.complete(
                            reader, attachments.reading_request(picture),
                            cancel=cancel, **private_kwargs) or "").strip())
                        if cancel.is_set():
                            GLib.idle_add(self._on_stream_done, None, cancel)
                            return
                        self._note_reading_receipt(privacy, f"Reading of {picture['name']}")
                        if not text:
                            raise LLMError(f"The vision model returned nothing about "
                                           f"{picture['name']}", model=reader)
                        picture["reading"] = readings[picture["id"]] = text
                    GLib.idle_add(self._keep_image_readings, readings)
                messages = attachments.to_messages(history, sees)
                request_system = system
                if reading_in_system and reading:
                    request_system = ((system + "\n\n") if system else "") + (
                        prompts.READING_CONTEXT + reading)
                payload = None
                if prepared is not None:
                    stage = "gather"
                    addition, payload = self._gather(prepared, cancel, say,
                                                     private_kwargs)
                    if cancel.is_set():
                        GLib.idle_add(self._on_stream_done, None, cancel)
                        return
                    if addition:
                        request_system = ((request_system + "\n\n") if request_system
                                          else "") + addition
                stage = "answer"
                result = self.app.client.stream(model, messages, image, cancel, on_delta,
                                                web_search=web_search,
                                                system=request_system, **private_kwargs)
            except LLMError as e:
                GLib.idle_add(self._on_stream_error,
                              self._failure(stage, e.model, model, str(e)), cancel)
            except privacy_module.PrivacyError as e:
                # A model the conversation cannot send to privately (enlist): its own
                # words say why, not "PrivacyError: " before them.
                GLib.idle_add(self._on_stream_error,
                              self._failure(stage, None, model, str(e)), cancel)
            except Exception as e:
                errors.record(f"A request to {model} failed in its {stage} step: {e!r}")
                GLib.idle_add(self._on_stream_error,
                              self._failure(stage, None, model, errors.describe(e)),
                              cancel)
            else:
                receipt = privacy.pop_receipt() if privacy is not None else None
                GLib.idle_add(self._on_stream_done, result, cancel, receipt, model,
                              payload)

        threading.Thread(target=worker, daemon=True).start()

    def _note_reading_receipt(self, privacy, what):
        """A private reading of a picture was a reply too, and what the answer is given
        to work from: if its check failed, the question says so, as it does for the
        first request's transcription. Called from the worker."""
        if privacy is None:
            return
        read = privacy.pop_receipt()
        if read is not None and read.state == privacy_module.FAILED:
            GLib.idle_add(self._badge_question, (
                f"⚠ {what} · check failed", "bad", read.detail))

    def _request_system(self, model=None):
        """The instructions for this request, then the reference material the
        conversation holds: the dictionary entries looked up, and whatever a subclass
        adds for the question being asked (_extra_context).

        `system` may be a function of whether `model` can see: a question about a
        capture is answered by a model that sees it or one given a reading of it, and a
        conversation can move between the two at any message - told it cannot see the
        image while it is attached, or to read it off a picture it does not have, a
        model is given instructions that contradict what it is sent."""
        system = self.system
        if callable(system):
            system = system(bool(catalogue.accepts_images(self.config, model)))
        parts = [system] if system else []
        looked_up = [turn["context"] for turn in self.conversation
                     if turn.get("role") == "lookup" and turn.get("context")]
        if looked_up:
            parts.append(prompts.LOOKUP_CONTEXT
                         + "\n\n---\n\n".join(looked_up[-LOOKUPS_IN_CONTEXT:]))
        question = next((turn.get("content") or "" for turn in reversed(
            self.conversation) if turn["role"] == "user"), "")
        extra = self._extra_context(question)
        if extra:
            parts.append(extra)
        return "\n\n".join(parts) or None

    def _busy(self):
        """Whether a lookup or search may not start now: one is already running, or
        an answer is - a turn arriving mid-answer would take its placeholder's place."""
        if self._cancel is not None:
            self.status.set_text("Wait for the answer to finish, then try again")
            return True
        if self._looking:
            # What the lookup says it is doing stays, and this after it: "Wait for the
            # answer to finish", there being none, took the place of "Indexing …
            # (once only)", which can go on for minutes.
            running = self.status.get_text()
            note = "the lookup is still running: try again once it is done"
            if note not in running:
                self.status.set_text(f"{running} - {note}" if running
                                     else note.capitalize())
            return True
        return False

    # ---- dictionary lookups -----------------------------------------------

    def _look_up(self, word, add=False):
        """Look `word` up in the user's dictionaries, off the main loop: on a page of
        its own, or with `add`, below what this page has already."""
        import threading

        word = " ".join((word or "").split())
        if not word or self._lookup is None or self._busy():
            return
        self._looking = True
        self.status.set_text(f"Looking up “{word}”…")

        def worker():
            try:
                found = self._lookup(word, lambda m: GLib.idle_add(self._set_status, m))
            except Exception as e:
                errors.record(f"Dictionary lookup of {word!r} failed: {e!r}")
                GLib.idle_add(self._on_looked_up, None, errors.describe(e), add)
            else:
                GLib.idle_add(self._on_looked_up, found, None, add)

        threading.Thread(target=worker, daemon=True).start()

    def _on_lookup_key(self, entry, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and \
                event.state & Gdk.ModifierType.SHIFT_MASK:
            self._look_up(entry.get_text(), add=True)
            return True
        return False

    def _lookup_turn(self, turn):
        """Fill in what a lookup turn shows and gives the model from what was found,
        leaving out the dictionaries switched off in this window."""
        found = turn["found"]
        visible = dictionaries.shown(found, self._hidden_dictionaries)
        turn.update(display=dictionaries.as_markdown(visible),
                    html=markdown_html.lookup_html(
                        visible, hidden=len(found["results"]) - len(visible["results"])),
                    context=dictionaries.as_context(visible, LOOKUP_CONTEXT_CHARS))
        return turn

    def _on_looked_up(self, found, failure, add=False):
        if self._destroyed:
            return False
        self._looking = False
        self._refresh_dictionary_notes()
        if failure:
            self.status.set_text(f"Lookup failed: {failure}")
            return False
        self._forget_undo()
        turn = self._lookup_turn({
            "role": "lookup", "word": found["word"], "content": "", "found": found,
            "heading": f"Look up: {found['word']}"})
        if add or not self.conversation:
            self._append_turn(turn, scroll="start")
        else:
            # A page of its own: the questions asked about the word before were about
            # that word, and stay with it, a Back away.
            del self._pages[self._page + 1:]
            self.conversation = [turn]
            self._pages.append(self.conversation)
            self._page = len(self._pages) - 1
            self._render(new_page=True)
        self._sync_pages()
        if found["results"]:
            self.status.set_text(f"Found in {len(found['results'])} of "
                                 f"{found['dictionaries']} dictionaries")
            self.entry.grab_focus()
        elif found["dictionaries"]:
            self.status.set_text("No entry")
        elif found.get("unusable"):
            names = found["unusable"]
            self.status.set_text(
                (f"{names[0]} could not be read: its button says why" if len(names) == 1
                 else f"None of the {len(names)} dictionaries could be read: their "
                      f"buttons say why"))
        else:
            self.status.set_text("No dictionaries found - see Settings > Reference")
        return False

    def _turn_page(self, step):
        """Back or Forward through the words looked up in this window."""
        page = self._page + step
        if self._lookup is None or not 0 <= page < len(self._pages) or self._busy():
            return
        self._forget_undo()
        self._page = page
        self.conversation = self._pages[page]
        for turn in self.conversation:
            if turn.get("role") == "lookup" and "found" in turn:
                self._lookup_turn(turn)     # a dictionary may have been switched since
        words = [t["word"] for t in self.conversation if t.get("role") == "lookup"]
        if words:
            self.lookup_entry.set_text(words[-1])
        self.status.set_text("")
        self._render(new_page=True)
        self._sync_pages()

    def _sync_pages(self):
        if self._lookup is not None:
            self.back_button.set_sensitive(self._page > 0)
            self.forward_button.set_sensitive(self._page < len(self._pages) - 1)

    def _on_window_key(self, _widget, event):
        control = event.state & Gdk.ModifierType.CONTROL_MASK
        alt = event.state & Gdk.ModifierType.MOD1_MASK
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        if control and event.keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self._clear()
            return True
        if control and event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self._find()
            return True
        if control and event.keyval in (Gdk.KEY_w, Gdk.KEY_W):
            self.destroy()
            return True
        if control and event.keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self._zoom(1)
            return True
        if control and event.keyval in (Gdk.KEY_minus, Gdk.KEY_underscore,
                                        Gdk.KEY_KP_Subtract):
            self._zoom(-1)
            return True
        if control and event.keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self._zoom(0)
            return True
        if self.find_bar.is_open():
            if event.keyval == Gdk.KEY_F3:
                self.find_bar.step(-1 if shift else 1)
                return True
            if event.keyval == Gdk.KEY_Escape:
                self.find_bar.close()
                return True
        if event.keyval == Gdk.KEY_Escape and self._cancel is not None:
            if keys.input_method_takes(self.get_focus(), event):
                return True             # what was being composed abandoned
            self._stop()                # Escape stops an answer, as Stop does
            return True
        if alt and event.keyval in (Gdk.KEY_Left, Gdk.KEY_Right) and self._lookup:
            self._turn_page(-1 if event.keyval == Gdk.KEY_Left else 1)
            return True
        return False

    # ---- finding ----------------------------------------------------------

    def _find(self):
        """Ctrl+F: the find bar, on the view the user was in - or, from the bar
        itself, on the one it is already searching."""
        focus = self.get_focus()
        if self.find_bar.finder is not None and focus is not None and \
                focus.is_ancestor(self.find_bar):
            self.find_bar.open(self.find_bar.finder)
            return
        key, view, placeholder = self._find_target(focus)
        finder = self._finders.get(key)
        if finder is None or finder.view is not view:
            finder = (find.WebFinder(view, placeholder) if WebKit2 is not None
                      and isinstance(view, WebKit2.WebView)
                      else find.TextFinder(view.text_views, placeholder))
            finder.view = view
            self._finders[key] = finder
        self.find_bar.open(finder)

    def _find_target(self, focus):
        """What Ctrl+F searches, given the widget that had focus: (key, view,
        placeholder) - the view a WebKit one, or one with text_views(). A subclass with
        more than the conversation to read chooses between them."""
        return ("conversation", self.webview or self.bubbles, "Find in conversation")

    # ---- clearing ---------------------------------------------------------

    def _clear(self):
        """Start over in this window: the conversation, and everything it gives the
        model - lookups, articles, research passages - gone; the window's own settings
        (model, Privacy Mode, dictionaries hidden, Ask Encyclopedia, the screenshot)
        kept. Undo, for a few seconds, brings it all back."""
        if self._cancel is not None:
            self._stop()
        if not self.conversation and len(self._pages) <= 1:
            return
        self._forget_undo()
        self._undo = {"pages": self._pages, "page": self._page,
                      "reading_in_history": self._reading_in_history}
        self.conversation = []
        self._pages, self._page = [self.conversation], 0
        # A reading of the capture that was in the history now has to ride with the
        # instructions instead, for the next question about the image.
        self._reading_in_history = False
        self._render(new_page=True)
        self._sync_pages()
        self.status.set_text("Cleared")
        self.undo_button.show()
        self._undo_timer = GLib.timeout_add_seconds(UNDO_SECONDS, self._expire_undo)

    def _undo_clear(self):
        if self._undo is None or self._cancel is not None:
            return
        undo, self._undo = self._undo, None
        self._pages, self._page = undo["pages"], undo["page"]
        self.conversation = self._pages[self._page]
        self._reading_in_history = undo["reading_in_history"]
        self._cancel_undo_timer()
        self.undo_button.hide()
        self.status.set_text("")
        self._render(new_page=True)
        self._sync_pages()

    def _expire_undo(self):
        self._undo_timer = None
        self._forget_undo()
        return False

    def _cancel_undo_timer(self):
        if self._undo_timer is not None:
            GLib.source_remove(self._undo_timer)
            self._undo_timer = None

    def _forget_undo(self):
        """Something new has happened since Clear: what it removed is gone for good."""
        if self._undo is None:
            return
        self._undo = None
        self._cancel_undo_timer()
        if not self._destroyed:
            self.undo_button.hide()
            if self.status.get_text() == "Cleared":
                self.status.set_text("")

    def _build_dictionary_bar(self, listed):
        """A button per dictionary, its own icon where it has one (Lingvo's .bmp), its
        initials where not, its name and description on hover. Pressed is shown; released is hidden
        from this conversation's lookups and from what its questions are sent with."""
        from gi.repository import GdkPixbuf
        bar = self.dictionary_bar = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, column_spacing=2,
                          row_spacing=2, max_children_per_line=60, homogeneous=False)
        bar.get_style_context().add_class("dictionary-bar")
        for path, name, icon, about in listed:
            button = Gtk.ToggleButton(active=True)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_tooltip_text(f"{name}\n\n{about}" if about else name)
            self._dictionary_buttons[path] = button
            button.get_style_context().add_class("dictionary-toggle")
            picture = None
            if icon:
                try:
                    picture = Gtk.Image.new_from_pixbuf(
                        GdkPixbuf.Pixbuf.new_from_file_at_scale(icon, -1, 20, True))
                except Exception:
                    picture = None
            if picture is None:
                picture = Gtk.Label(label=dictionary_initials(name))
                picture.get_style_context().add_class("dictionary-initials")
            button.add(picture)
            button.connect("toggled", self._on_dictionary_toggled, path, name)
            bar.add(button)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.set_margin_start(10)
        frame.set_margin_end(10)
        frame.set_margin_top(4)
        frame.set_margin_bottom(4)
        frame.pack_start(bar, False, False, 0)
        return frame

    def _refresh_dictionary_notes(self):
        """Each button's hover text as the dictionaries now are. The bar is made before
        the window's first lookup, which is what finds a dictionary that cannot be read,
        so a reason the status line pointed to was not on its button yet."""
        if self._dictionary_list is None or not self._dictionary_buttons:
            return
        for path, name, _icon, about in self._dictionary_list():
            button = self._dictionary_buttons.get(path)
            if button is not None:
                button.set_tooltip_text(f"{name}\n\n{about}" if about else name)

    def _on_dictionary_toggled(self, button, path, name):
        self._show_dictionary(path, name, button.get_active())

    def _hide_dictionary(self, path):
        """The ✕ beside a dictionary's name in a lookup: as its button in the bar,
        which is released to say so and pressed to bring it back. Folding the entry
        away with its arrow hides it only from sight; the questions are still sent
        with it."""
        button = self._dictionary_buttons.get(path)
        if button is not None:
            if not button.get_active():
                return          # hidden already: the page is redrawn after the answer
            button.set_active(False)                # _on_dictionary_toggled
            self.status.set_text(self.status.get_text()
                                 + "; its button in the bar brings it back")
        elif path not in self._hidden_dictionaries:
            names = [result["dictionary"] for page in self._pages for turn in page
                     if turn.get("role") == "lookup" and "found" in turn
                     for result in turn["found"]["results"] if result.get("path") == path]
            self._show_dictionary(path, names[0] if names else os.path.basename(path),
                                  False)

    def _show_dictionary(self, path, name, shown):
        if shown:
            self._hidden_dictionaries.discard(path)
            self.status.set_text(f"Showing {name}")
        else:
            self._hidden_dictionaries.add(path)
            self.status.set_text(f"{name} hidden in this conversation, and left out "
                                 f"of what questions are sent with")
        for page in self._pages:
            for turn in page:
                if turn.get("role") == "lookup" and "found" in turn:
                    self._lookup_turn(turn)
        if self._cancel is None:
            self._render(full=True)
        else:
            # A full redraw now would drop the answer streaming in; it comes after.
            self._redraw_after_answer = True

    def _redraw(self):
        if not self._destroyed:
            self._render(full=True)
        return False                    # one-shot idle callback

    def _keep_image_readings(self, readings):
        """Note the vision model's readings of attached pictures on the turns they came
        with - every page's - so no picture is read twice."""
        if self._destroyed:
            return False
        pages = self._pages + (self._undo["pages"] if self._undo else [])
        for conversation in pages:
            for turn in conversation:
                for image in turn.get("images") or ():
                    if image["id"] in readings:
                        image["reading"] = readings[image["id"]]
        return False

    def _keep_reading(self, reading):
        if not self._destroyed and self.reading is None:
            self.reading = reading
        return False

    def _failure(self, stage, failed, answering, message):
        """Name the model that failed, and its part in the conversation.

        A conversation can use two models before one answer - the OCR model reads a
        capture, then the text or image-query model answers - and a bare "model
        overloaded" left the reader to guess which. `failed` is the model the error
        came from when it says (LLMError.model); `answering` is the one this request
        was going to answer with.
        """
        if stage == "read":
            return (f"The vision model reading the image"
                    f"{f' ({failed})' if failed else ''} failed: {message}")
        if stage == "gather":
            return message
        model = failed or answering
        role = self._label_of(model).lower()
        return f"The {role} model ({model}) failed: {message}"

    def _stale(self, cancel):
        """Whether a worker's callback has outlived the request it belongs to.

        The window may be gone, or Stop may already have ended the request on screen
        while the worker was still blocked on the network. Each request has its own
        cancel event, so that is what identifies it.
        """
        return self._destroyed or (cancel is not None and cancel is not self._cancel)

    def _on_stream_progress(self, text, cancel=None):
        self._redraw_queued = False
        if self._cancel is None or self._stale(cancel) or self._cancel.is_set():
            return False
        self._streaming_text = sound_text(text)
        self.status.set_text(f"Receiving… {len(text)} chars")
        self._render()
        return False

    def _on_stream_done(self, result, cancel=None, receipt=None, model=None,
                        payload=None):
        if self._stale(cancel):
            return False
        result = sound_text(result)         # no half characters kept: see sound_text
        cancelled = self._cancel is not None and self._cancel.is_set()
        # Note whether a streaming placeholder is on screen BEFORE _finish_stream
        # clears the flag - it decides replace-vs-append below.
        placeholder = self._streaming_text is not None
        partial = (self._streaming_text or "").strip() if cancelled else ""
        # Whether this answer came privately is decided before _finish_stream, which
        # takes up a session that Privacy Mode brought while it was on its way: it came
        # from where it was asked, and was badged as having come from attested models.
        private = self.privacy is not None
        if partial or result:
            self._replacing = []        # an answer came, if only part of one: it replaces
        self._finish_stream()
        if cancelled:
            self.status.set_text("Stopped")
            self._settle_question()
            if partial:
                # What had come is kept, said to be cut short, and carried into the
                # conversation like any answer - not thrown away with the Stop.
                turn = {"role": "assistant", "content": partial + STOPPED_NOTE,
                        "model": self._streaming_model, "stopped": True}
                if private:
                    turn["privacy"] = self._badge((
                        "🔒 Stopped · unchecked", "warn",
                        "Stopped before the reply was complete, so it was not checked "
                        "against a signed record: nothing shows which machine sent it. "
                        "It was asked of this conversation's attested models only."))
                self._append_turn(turn, replace_placeholder=placeholder)
                return
            self._restore_replaced()
            self._render(full=True)      # full redraw drops the placeholder
            return
        if not result:
            self._show_error("The model returned an empty response", placeholder)
            return
        turn = {"role": "assistant", "content": result,
                "model": model or self._streaming_model}
        if private:
            turn["privacy"] = self._badge(privacy_module.receipt_badge(receipt))
            # And the question it answers, only as far as the replies made from it show
            # it reached the attested enclave.
            self._badge_question(privacy_module.sent_badge(self._sent_kinds,
                                                           self._tee_checks))
        if getattr(self, "_answer_is_reading", False):
            self._keep_transcription(result)
        self._finish_answer(turn, payload)
        self._append_turn(turn, replace_placeholder=placeholder)
        self.status.set_text(turn["privacy"]["label"] if turn.get("privacy") else "")
        self._after_answer(turn, payload)

    def _keep_transcription(self, text):
        """OCR's first answer is the capture's text, in the conversation: a follow-up to
        a model that cannot see works from it, rather than the capture being sent to
        the vision model again for a reading of the same pixels."""
        if self.reading is None and (text or "").strip():
            self.reading = text.strip()
            self._reading_in_history = True

    def _on_stream_error(self, message, cancel=None):
        if self._stale(cancel):
            return False
        placeholder = self._streaming_text is not None
        self._finish_stream()
        self._show_error(message, placeholder)
        retired, self._retired_pending = self._retired_pending, None
        if retired is not None:
            self._offer_another_model(retired)

    def _settle_question(self):
        """A question whose request never went out keeps no "Private" promise badge, and
        one that went to a TEE/ model and was stopped or failed on its way says how far
        the replies checked by then showed it received - not at all, as a rule."""
        if self.privacy is None:
            return
        for turn in reversed(self.conversation):
            if turn["role"] == "user":
                if (turn.get("privacy") or {}).get("tone") == "pending":
                    if privacy_module.TEE in self._sent_kinds:
                        turn["privacy"] = self._badge(privacy_module.sent_badge(
                            self._sent_kinds, self._tee_checks))
                    else:
                        turn["privacy"] = self._badge((
                            "🔒 Not answered", "warn",
                            "The request did not complete. It was only ever addressed "
                            "to this conversation's attested models."))
                break

    def _show_error(self, message, placeholder=False):
        self.status.set_text("")
        self._settle_question()
        restored = self._restore_replaced()
        # Marked so history_for() keeps it out of what is sent upstream. A provider's
        # error body is JSON too: see sound_text.
        self._append_turn({"role": "assistant",
                           "content": f"**Error:** {sound_text(str(message))}",
                           "error": True},
                          replace_placeholder=placeholder)
        if restored:
            self._render(full=True)     # the answers back, no longer marked earlier ones

    def _finish_stream(self):
        self._cancel = None
        self._streaming_text = None
        # A first step that did not finish - it failed, or was stopped - leaves its
        # question as it was asked, not saying the step is still under way; it is made
        # again with the next request, and the status line says so then.
        turn = self._preflight_turn
        if (self._preflight is not None and turn is not None
                and "plain_display" in turn and not self._destroyed):
            turn["display"] = turn.pop("plain_display")
            self._render(full=True)
        if self._pending_privacy is not None and not self._destroyed:
            session, self._pending_privacy = self._pending_privacy, None
            self._apply_privacy(session)
        if self._redraw_after_answer:
            self._redraw_after_answer = False
            # After the finished answer has taken the placeholder's place.
            GLib.idle_add(self._redraw)
        self.send_button.set_sensitive(True)
        self._sync_again()
        self.stop_button.hide()
        return False

    def _sync_again(self):
        """Ask again with… only where there is a question to ask again: not in an empty
        window, after Clear, or on a page of lookups alone, where choosing a model did
        nothing at all - and not while an answer is coming."""
        if self._cancel is not None:
            return
        asked = any(turn.get("role") == "user" for turn in self.conversation)
        self.again_button.set_sensitive(asked)
        self.again_button.set_tooltip_text(
            "Ask your last question again with another model. The earlier answer stays "
            "on screen, but only the new one is carried into the conversation." if asked
            else "Nothing has been asked here yet")

    def _stop(self):
        """End the request on screen now, whatever the worker is doing.

        Cancel is only noticed between lines of the stream, and not at all during the
        one-shot transcription, so a stalled connection used to leave "Stopping…" and a
        dead Send button up until the request timed out. The worker still winds down in
        its own time; _stale() discards whatever it reports afterwards.
        """
        if self._cancel is None:
            return
        self._cancel.set()
        self._on_stream_done(None)

    # ---- misc -------------------------------------------------------------

    def _markdown(self, screenshot_name=None):
        """The whole conversation as Markdown: what Copy puts on the clipboard, and
        what Save writes - with, for Save, a link to the screenshot saved beside it,
        `screenshot_name`. Copy leaves the picture out: there is no file to link."""
        parts = []
        if screenshot_name:
            parts.append(f"## Screenshot\n\n![Screenshot]({quote(screenshot_name)})")
        for turn in self.conversation:
            if turn["role"] in REFERENCE_ROLES:
                text = turn.get("export", turn["display"])
                parts.append(f"## {turn['heading']}\n\n{text}")
                continue
            who = "You" if turn["role"] == "user" else "Assistant"
            if turn.get("model"):
                who += f" ({turn['model']}" + (", earlier answer)" if turn.get("superseded")
                                               else ")")
            elif turn.get("is_premium"):
                who += " (premium)"
            if turn.get("privacy"):
                who += f" [{turn['privacy']['label']}]"
            # "export": what an answer says without the links into this window.
            text = turn.get("export", turn.get("display", turn["content"]))
            attached = [f"- 📎 {a['label']}" for a in turn.get("attachments") or ()]
            if attached:
                text = "\n\n".join(part for part in (text, "\n".join(attached)) if part)
            parts.append(f"## {who}\n\n{text}")
        return "\n\n".join(parts)

    def _on_copy_all(self, _widget):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self._markdown(), -1)
        # Said here, not as a desktop notification: that one replaced whatever the
        # app's notification was saying, a settings or keyring failure among them.
        self.status.set_text("Conversation copied to the clipboard")

    def _ask_save_path(self, title, name, kind, pattern, mime, parent=None):
        """Where the user wants a file saved, or None if they cancelled."""
        # Native, so a sandboxed or portal-only desktop gets its own file dialog.
        dialog = Gtk.FileChooserNative.new(title, parent or self,
                                           Gtk.FileChooserAction.SAVE, "_Save", "_Cancel")
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(name)
        # Where the last file was saved, while the app runs; Documents before that.
        folder = ChatWindow._last_save_folder or GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_DOCUMENTS)
        if folder and os.path.isdir(folder):
            dialog.set_current_folder(folder)
        files = Gtk.FileFilter()
        files.set_name(kind)
        files.add_pattern(pattern)
        files.add_mime_type(mime)
        dialog.add_filter(files)
        accepted = dialog.run() == Gtk.ResponseType.ACCEPT
        path = dialog.get_filename() if accepted else None
        dialog.destroy()
        if path:
            ChatWindow._last_save_folder = os.path.dirname(path)
        return path

    def _on_save_markdown(self, _widget):
        path = self._ask_save_path("Save conversation as Markdown",
                                   markdown_filename(self.get_title()),
                                   "Markdown", "*.md", "text/markdown")
        if not path:
            return
        # The screenshot goes beside the Markdown as a PNG it links to: a picture
        # written into the text would be megabytes of base64 that many viewers drop.
        picture = None
        if self.screenshot:
            picture = screenshot_beside(path, replacing=os.path.exists(path))
        # Encoded before anything is opened, so that it cannot fail part way: open(path,
        # "w") emptied a saved conversation being replaced, and then a reply with half
        # a character in it raised, which left the file empty.
        text = sound_text(self._markdown(picture and os.path.basename(picture)) + "\n")
        try:
            if picture:
                write_whole(picture, self.screenshot)
            write_whole(path, text.encode("utf-8"))
        except OSError as e:
            self.status.set_text(f"Not saved: {e.strerror or e}")
            return
        self.status.set_text(f"Saved to {os.path.basename(path)}"
                             + (f" and {os.path.basename(picture)}" if picture else ""))

    # ---- the screenshot ---------------------------------------------------

    def _on_screenshot_message(self, _manager, result):
        try:
            action = result.get_js_value().to_string()
        except AttributeError:
            return
        if action == "view":
            self._view_screenshot()
        elif action == "save":
            self._on_save_screenshot()

    def _view_screenshot(self):
        screenshot_view.ScreenshotViewer(self, self.screenshot, self.get_title(),
                                         self._on_save_screenshot)

    def _on_save_screenshot(self, parent=None):
        path = self._ask_save_path("Save screenshot",
                                   markdown_filename(self.get_title(), ".png"),
                                   "PNG image", "*.png", "image/png", parent)
        if not path:
            return
        try:
            write_whole(path, self.screenshot)
        except OSError as e:
            self.status.set_text(f"Screenshot not saved: {e.strerror or e}")
            return
        self.status.set_text(f"Saved to {os.path.basename(path)}")

    def _on_destroy(self, _widget):
        self._cancel_undo_timer()
        settings = Gtk.Settings.get_default()
        for handler in self._theme_handlers:
            settings.disconnect(handler)
        self._theme_handlers = []
        if self._size_timer is not None:
            GLib.source_remove(self._size_timer)
            self._size_timer = None
        if self._prompt_height_timer is not None:
            GLib.source_remove(self._prompt_height_timer)
            self._save_prompt_height()          # dragged, and closed straight after
        # Reaching here at all means the view did not end the process, whether or not
        # the page ever finished loading. A window closed early must not count against
        # WebKit - see webkitguard. Only a window that built a view says so: one that
        # did not, WebKit having ended the process twice, cleared those strikes when it
        # closed, and the next window built the WebView that ends it.
        if self._webkit_tried:
            webkitguard.note_success(self)
        self._destroyed = True
        if self.privacy is not None:
            self.privacy.listener = None
        if self._cancel is not None:
            self._cancel.set()
        # Let go of the view now. Each WebView has a web process of its own - 150 MB
        # and more - which lives until the view is freed, and this window is a cycle
        # Python would otherwise free whenever it next collected: the test suite, which
        # opens a window a test, ran a machine out of memory that way.
        if self.webview is not None:
            self.webview = None
