"""Conversation window: WebKit view with live token streaming.

Tokens land in the view as they arrive, markdown is rendered properly (tables, code
blocks with syntax highlighting, lists), and code blocks get copy buttons.
"""

import json
import os
import re
import time
from urllib.parse import quote

from .. import errors, gtkcompat, webkitguard
from gi.repository import Gdk, GLib, Gtk

from . import bubbles, markdown_html, markdown_text, screenshot as screenshot_view
from .. import attachments, models as catalogue, privacy as privacy_module, prompts
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
        var code = pre.querySelector('code');
        window.webkit.messageHandlers.copyText.postMessage(
          code ? code.innerText : pre.innerText);
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


SCREENSHOT_JS = """
document.addEventListener('click', function (e) {
  var el = e.target.closest && e.target.closest('[data-shot]');
  if (!el) return;
  e.preventDefault();
  window.webkit.messageHandlers.screenshot.postMessage(el.dataset.shot);
});
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


def screenshot_beside(markdown_path, replacing):
    """Where a saved conversation's screenshot goes: beside it, under the same name.

    Replacing a saved conversation - the file dialog asked first - replaces its
    screenshot too. Otherwise a picture already under that name is some other file, and
    the next free name is used instead.
    """
    stem = os.path.splitext(markdown_path)[0]
    path, n = stem + ".png", 2
    while not replacing and os.path.exists(path):
        path, n = f"{stem} ({n}).png", n + 1
    return path


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
        self._reading_in_history = False
        # How to answer: sent as a system message on every turn, including follow-ups,
        # and deliberately not part of any user turn - the search query is built from
        # the latest of those.
        self.system = system
        # Optional first-request step run before the model is called, e.g. transcribing
        # an image with the vision model so the text model can translate the result.
        # Signature: preflight(cancel, status) -> {"content": ..., "display": ...}
        self._preflight = preflight
        # Privacy Mode (assistant/privacy.py): the PrivacySession this conversation
        # started with, or None. It is never replaced or cleared - nothing in this window
        # reads config.privacy_mode - so a private conversation stays on its attested
        # models to the end, whatever the tray or Settings say in the meantime.
        self.privacy = privacy
        self._privacy_refused = None
        self._sent_kinds = set()
        self._streaming_privacy = None
        if privacy is not None:
            privacy.listener = lambda *event: GLib.idle_add(self._on_privacy_event, *event)
        self.dark = _is_dark()
        self._streaming_text = None
        self._streaming_model = None
        self._last_redraw = 0.0
        self._cancel = None
        self._destroyed = False
        self._loaded = False
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

        self.set_default_size(880, 720)
        self.set_position(Gtk.WindowPosition.CENTER)

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

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        if privacy is not None:
            box.pack_start(self._build_privacy_banner(), False, False, 0)
        if lookup is not None and dictionary_list is not None:
            box.pack_start(self._build_dictionary_bar(dictionary_list()), False, False, 0)

        self.view = self._build_view()
        box.pack_start(self.view, True, True, 0)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        box.pack_start(self._build_input(), False, False, 0)
        self.add(self._layout(box))

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
            roles = [("text", "Text", self.config.text_model),
                     ("premium", "Premium", self.config.premium_text_model),
                     ("vision", "Vision", self.config.vision_model)]
        return roles + [(f"model:{model}", "Chosen", model) for model in self._chosen]

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
        return self.privacy.vision_model if self.privacy else self.config.vision_model

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
                               current=self.current_model)
        browser.run()
        chosen = browser.selected_id
        browser.destroy()
        if not chosen:
            return
        known = {model: key for key, _label, model in self._choices()}
        if chosen not in known:
            self._chosen.append(chosen)
        then(known.get(chosen, f"model:{chosen}"))

    def _popup_models(self, widget, then):
        """The model menu under `widget`; picking an entry calls then(key)."""
        menu = Gtk.Menu()
        for key, label, model in self._choices():
            item = Gtk.CheckMenuItem(label=f"{label} — {model}")
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

    def _sync_model_display(self):
        """Name the model the next message goes to, in the header and on the menu."""
        model = self.current_model
        label = self._label_of(model)
        self._model_label.set_text(label if label != "Chosen" else model.rsplit("/", 1)[-1])
        if self.privacy is not None:
            self.header.set_subtitle(f"🔒 Privacy Mode · {model}")
        else:
            self.header.set_subtitle(model)

    # ---- privacy ----------------------------------------------------------

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
            title = "Not attested — nothing was sent"
            detail = self._privacy_refused
        elif not attested:
            tone, icon = "pending", "channel-secure-symbolic"
            title = "Privacy Mode"
            detail = (f"Only {' and '.join(session.models())}, each attested before "
                      f"anything is sent to it. Web search is off.")
        else:
            partial = any(a.level != "full" or a.latest_release for a in attested)
            tone = "warn" if partial else "ok"
            icon = "channel-secure-symbolic"
            title = " · ".join(dict.fromkeys(
                ("🔐 " if a.kind == privacy_module.E2EE else "🔒 ") + a.headline
                for a in attested))
            lines = []
            for a in attested:
                if a.kind == privacy_module.E2EE:
                    older = (f"; an older release than {a.latest_release}"
                             if a.latest_release else "")
                    lines.append(f"{a.model}: sealed on this machine to {a.enclave} "
                                 f"({a.hardware or 'attested hardware'}, {a.release}"
                                 f"{older}). nano-gpt relays ciphertext only.")
                else:
                    lines.append(f"{a.model}: {a.summary or a.detail}.")
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
            self._refresh_privacy_banner()
        elif event == "sent":
            kind = privacy_module.kind_of(model)
            self._sent_kinds.add(kind)
            if self._streaming_text is not None:
                # What is arriving, while it arrives: the answer's own badge replaces
                # this once its signature has been checked, or its seal opened.
                self._streaming_privacy = self._badge(privacy_module.receiving_badge(kind))
            self._badge_question(privacy_module.sent_badge(self._sent_kinds))
        return False

    @staticmethod
    def _badge(triple):
        if not triple:
            return None
        label, tone, tip = triple
        return {"label": label, "tone": tone, "tip": tip}

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
                 + "\n\n".join(session.caveats()))
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
        blocked, reason = webkitguard.blocked()
        if WebKit2 is None or blocked:
            # No usable WebKit (see gtkcompat), or one already seen to take the process
            # down with it (see webkitguard).
            if blocked and reason:
                self.app.notify(f"Chat view: {reason}")
            return self._build_text_view()

        # From here to the first rendered frame is the stretch that can end the process
        # rather than raise; if it does, the next window opens as plain text.
        webkitguard.note_attempt()
        try:
            return self._build_webkit_view()
        except Exception as e:
            # Anything the typelib lets through: a library that loaded but does not
            # work leaves classes that raise on first use, not on import.
            errors.record(f"WebKit view unavailable, using plain text: {e!r}")
            webkitguard.note_success()      # it raised rather than killed us
            self.app.notify(f"Chat view: WebKit failed ({e}); using plain text")
            return self._build_text_view()

    def _build_text_view(self):
        self.bubbles = bubbles.BubbleView(on_link=self._on_text_link)
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
        manager.connect("script-message-received::copyText", self._on_copy_code)
        manager.add_script(WebKit2.UserScript(
            COPY_BUTTON_JS,
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
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("decide-policy", self._on_decide_policy)
        self.webview.connect("context-menu", lambda *a: False)
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_write_console_messages_to_stdout(False)
        bg = Gdk.RGBA()
        bg.parse("#1e1f22" if self.dark else "#ffffff")
        self.webview.set_background_color(bg)

        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.webview)
        return scrolled

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
        # Files dropped on the message box are attached, not pasted in as their paths.
        self.entry.drag_dest_add_uri_targets()
        self.entry.connect("drag-data-received", self._on_entry_drop)
        frame.add(self.entry)
        box.pack_start(frame, False, False, 0)

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
            webkitguard.note_success()      # it rendered; it is not what kills us
            self._loaded = True
            for script in self._pending_js:
                self.webview.run_javascript(script, None, None, None)
            self._pending_js.clear()

    def _on_decide_policy(self, _view, decision, decision_type):
        # Open real links in the user's browser; never navigate the chat view away. A
        # dictionary's cross-reference looks its word up here instead.
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            nav = decision.get_navigation_action()
            uri = nav.get_request().get_uri()
            if uri.startswith("file://") and \
                    nav.get_navigation_type() != WebKit2.NavigationType.LINK_CLICKED:
                # A file dropped on the conversation: attached, never opened in it. Only
                # a drop - a link in an answer must never pick a file of yours to send.
                decision.ignore()
                self.attach_files([GLib.filename_from_uri(uri)[0]])
                return True
            if nav.get_navigation_type() == WebKit2.NavigationType.LINK_CLICKED:
                decision.ignore()
                word = reference_markup.word_of(uri)
                if word is not None:
                    if self._lookup is not None:
                        self.lookup_entry.set_text(word)
                        self._look_up(word)
                    return True
                if uri.startswith("enc://"):
                    self._follow_encyclopedia(uri)
                    return True
                Gtk.show_uri_on_window(None, uri, Gdk.CURRENT_TIME)
                return True
        return False

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
    def _turn_html(turn):
        if turn.get("role") in REFERENCE_ROLES:
            return turn["html"]
        return markdown_html.turn_html(turn["role"],
                                       turn.get("display", turn.get("content", "")),
                                       turn.get("is_premium", False),
                                       turn.get("_streaming", False), turn.get("privacy"),
                                       turn.get("model"), turn.get("superseded", False),
                                       links=turn.get("links"),
                                       attachments=turn.get("attachments"))

    def _render(self, full=False):
        if self._destroyed:
            return                      # the view is gone; see _on_destroy
        turns = self._turns()

        if self.webview is None:
            # Same text the WebKit view shows, including `display` rather than the
            # prompt in `content`: the instructions sent with a turn are not something
            # the reader asked to see, and for OCR they are most of the message.
            self.bubbles.set_turns(turns)
            GLib.idle_add(self._scroll_text_to_end)     # after the new rows are sized
            return

        if full or not self._loaded:
            body = self._screenshot_html + "".join(self._turn_html(t) for t in turns)
            self._loaded = False
            self.webview.load_html(markdown_html.document(body, self.dark), None)
            return

        # Incremental: replace only the final turn, so streaming does not reflow the
        # whole document on every token.
        if not turns:
            return
        html = self._turn_html(turns[-1])
        # With no turn yet, the first goes before #end: after the screenshot, if any.
        self._js(
            "(function(){var t=document.querySelectorAll('.turn');"
            f"var h={json.dumps(html)};"
            "if(t.length){t[t.length-1].outerHTML=h;}"
            "else{var e=document.getElementById('end');"
            "if(e){e.insertAdjacentHTML('beforebegin',h);}"
            "else{document.body.insertAdjacentHTML('beforeend',h);}}"
            "window.scrollTo(0,document.body.scrollHeight);})();")

    def _scroll_text_to_end(self):
        self.bubbles.scroll_to_end()
        return False                                    # one-shot idle callback

    def _scroll_text_to_turn(self, index):
        self.bubbles.scroll_to_turn(index)
        return False

    def _append_turn(self, turn, replace_placeholder=False, scroll="end"):
        """Add a turn to the transcript.

        While streaming, _render() puts a placeholder .turn in the DOM holding the
        partial text. That element is a DOM-only artefact - it is not in
        self.conversation - so when the stream finishes the finished turn must REPLACE
        it. Appending instead leaves the placeholder in place and the response appears
        twice, which is what happened before replace_placeholder existed.

        `scroll`: "end" follows the conversation down, as an answer wants; "start" puts
        the new turn's top at the top of the view, as reference material wants - a
        lookup's first dictionary is the one the user ranked first, and was scrolled
        past to the last; None leaves the view where it is.
        """
        self.conversation.append(turn)
        # The first turn from the very top - a screenshot above it is part of it.
        to_start = ("(function(){var t=document.querySelectorAll('.turn');"
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
        html = self._turn_html(turn)
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
                "(function(){var t=document.querySelectorAll('.turn');"
                f"var h={json.dumps(html)};"
                "if(t.length){t[t.length-1].outerHTML=h;}"
                "else{document.body.insertAdjacentHTML('beforeend',h);}"
                "window.scrollTo(0,document.body.scrollHeight);})();")
            return
        self._js(
            "(function(){var h=" + json.dumps(html) + ";"
            "var e=document.getElementById('end');"
            "if(e){e.insertAdjacentHTML('beforebegin',h);}"
            "else{document.body.insertAdjacentHTML('beforeend',h);}"
            "window.scrollTo(0,document.body.scrollHeight);})();")

    # ---- sending ----------------------------------------------------------

    def _on_entry_key(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                return False        # let GTK insert the newline itself
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

    def _set_status(self, message):
        if not self._destroyed:
            self.status.set_text(message)
        return False

    def _apply_preflight(self, update):
        """Fold the preflight result into the visible transcript.

        `content` becomes what follow-up turns carry as context; `display` is what the
        user sees, so the window shows the transcribed text rather than the machinery.
        """
        if self._destroyed:
            return False
        for turn in reversed(self.conversation):
            if turn["role"] == "user":
                turn["content"] = update.get("content", turn["content"])
                if update.get("display"):
                    turn["display"] = update["display"]
                if update.get("search_query"):
                    turn["search_query"] = update["search_query"]
                break
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
        last = max((i for i, t in enumerate(self.conversation) if t["role"] == "user"),
                   default=None)
        if last is None or not self._choose(key):
            return
        for turn in self.conversation[last + 1:]:
            if turn["role"] == "assistant" and not turn.get("error"):
                turn["superseded"] = True
        self._render(full=True)
        self._start_stream()

    def _start_stream(self):
        import threading

        preflight = self._preflight
        self._preflight = None      # first request only; follow-ups are plain text turns
        first = self._first_request
        self._first_request = False

        privacy = self.privacy
        model = self._first_model if first and self._first_model else self.current_model
        if privacy is not None:
            try:
                privacy.enlist(model)
            except privacy_module.PrivacyError as e:
                self._show_error(str(e))
                return

        # The capture: the first request carries it if the operation says so; later
        # ones if their model can see, and otherwise the vision model's reading of it.
        image, need_reading, reading_in_system = None, False, False
        if self.image_base64:
            if first:
                image = self.image_base64 if self._image_first else None
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
            self._badge_question(privacy_module.pending_badge(privacy))
        else:
            web_search = self.search_toggle.get_active()

        self._cancel = threading.Event()
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

        # Snapshot the history here; the worker mutates its own copy so a preflight
        # rewrite cannot race the main thread.
        # search_query rides along when a preflight built one; strip_internal_keys drops
        # it before the request, but the search reads it first.
        history = history_for(self.conversation)
        system = self._request_system()
        known_reading = self.reading
        prepared = self._prepare_answer()

        cancel = self._cancel
        # Passed only to a private conversation, so the ordinary path - and every stub
        # standing in for LLMClient - sees exactly the call it always has.
        private_kwargs = {}
        if privacy is not None:
            private_kwargs = {"privacy": privacy,
                              "status": lambda m: GLib.idle_add(self._set_status, m)}

        def on_delta(_piece, full_text):
            now = time.monotonic()
            if now - self._last_redraw < STREAM_REDRAW_INTERVAL:
                return
            self._last_redraw = now
            GLib.idle_add(self._on_stream_progress, full_text, cancel)

        def worker():
            stage = "answer"
            reading = known_reading
            try:
                if preflight is not None:
                    stage = "read"
                    update = preflight(cancel, lambda m: GLib.idle_add(self._set_status, m))
                    if cancel.is_set():
                        GLib.idle_add(self._on_stream_done, None, cancel)
                        return
                    if privacy is not None:
                        # The transcription was a reply too: if its signature failed,
                        # the question built from it says so.
                        read = privacy.pop_receipt()
                        if read is not None and read.state == privacy_module.MISMATCH:
                            GLib.idle_add(self._badge_question, (
                                "⚠ Transcription · signature mismatch", "bad",
                                read.detail))
                    if update and update.get("content"):
                        for turn in reversed(history):
                            if turn["role"] == "user":
                                turn["content"] = update["content"]
                                if update.get("search_query"):
                                    turn["search_query"] = update["search_query"]
                                break
                        GLib.idle_add(self._apply_preflight, update)
                    GLib.idle_add(self._set_status,
                                  "Searching the web…" if web_search else "Thinking…")
                if need_reading:
                    # A model that cannot see, in a conversation about an image: the
                    # vision model reads it once - its text and a description - and
                    # that reading goes with this request and every later one like it.
                    stage = "read"
                    GLib.idle_add(self._set_status,
                                  f"Reading the image for {model.rsplit('/', 1)[-1]}…")
                    if privacy is not None:
                        privacy.enlist(reader)
                    reading = (self.app.client.complete(
                        reader, [{"role": "user", "content": prompts.IMAGE_BRIEF}],
                        image_base64=self.image_base64, cancel=cancel,
                        **private_kwargs) or "").strip()
                    if cancel.is_set():
                        GLib.idle_add(self._on_stream_done, None, cancel)
                        return
                    if privacy is not None:
                        privacy.pop_receipt()
                    if not reading:
                        raise LLMError("The vision model returned nothing about the image",
                                       model=reader)
                    GLib.idle_add(self._keep_reading, reading)
                unread = [] if sees else attachments.unread(history)
                if unread:
                    stage = "read"
                    if privacy is not None:
                        privacy.enlist(reader)
                    readings = {}
                    for picture in unread:
                        GLib.idle_add(self._set_status,
                                      f"Reading {picture['name']} for "
                                      f"{model.rsplit('/', 1)[-1]}…")
                        text = (self.app.client.complete(
                            reader, attachments.reading_request(picture),
                            cancel=cancel, **private_kwargs) or "").strip()
                        if cancel.is_set():
                            GLib.idle_add(self._on_stream_done, None, cancel)
                            return
                        if privacy is not None:
                            privacy.pop_receipt()
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
                    addition, payload = self._gather(
                        prepared, cancel, lambda m: GLib.idle_add(self._set_status, m),
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
            except Exception as e:
                GLib.idle_add(self._on_stream_error,
                              self._failure(stage, None, model,
                                            f"{type(e).__name__}: {e}"), cancel)
            else:
                receipt = privacy.pop_receipt() if privacy is not None else None
                GLib.idle_add(self._on_stream_done, result, cancel, receipt, model,
                              payload)

        threading.Thread(target=worker, daemon=True).start()

    def _request_system(self):
        """The instructions for this request, then the reference material the
        conversation holds: the dictionary entries looked up, and whatever a subclass
        adds for the question being asked (_extra_context)."""
        parts = [self.system] if self.system else []
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
        if self._cancel is not None or self._looking:
            self.status.set_text("Wait for the answer to finish, then try again")
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
                GLib.idle_add(self._on_looked_up, None, f"{type(e).__name__}: {e}", add)
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
            self._render(full=True)
        self._sync_pages()
        if found["results"]:
            self.status.set_text(f"Found in {len(found['results'])} of "
                                 f"{found['dictionaries']} dictionaries")
            self.entry.grab_focus()
        else:
            self.status.set_text("No entry" if found["dictionaries"]
                                 else "No dictionaries found - see Settings > Reference")
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
        self._render(full=True)
        self._sync_pages()

    def _sync_pages(self):
        if self._lookup is not None:
            self.back_button.set_sensitive(self._page > 0)
            self.forward_button.set_sensitive(self._page < len(self._pages) - 1)

    def _on_window_key(self, _widget, event):
        control = event.state & Gdk.ModifierType.CONTROL_MASK
        alt = event.state & Gdk.ModifierType.MOD1_MASK
        if control and event.keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self._clear()
            return True
        if alt and event.keyval in (Gdk.KEY_Left, Gdk.KEY_Right) and self._lookup:
            self._turn_page(-1 if event.keyval == Gdk.KEY_Left else 1)
            return True
        return False

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
        self._render(full=True)
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
        self._render(full=True)
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

    def _on_dictionary_toggled(self, button, path, name):
        if button.get_active():
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
        if self._cancel is None or self._stale(cancel) or self._cancel.is_set():
            return False
        self._streaming_text = text
        self.status.set_text(f"Receiving… {len(text)} chars")
        self._render()
        return False

    def _on_stream_done(self, result, cancel=None, receipt=None, model=None,
                        payload=None):
        if self._stale(cancel):
            return False
        cancelled = self._cancel is not None and self._cancel.is_set()
        # Note whether a streaming placeholder is on screen BEFORE _finish_stream
        # clears the flag - it decides replace-vs-append below.
        placeholder = self._streaming_text is not None
        self._finish_stream()
        if cancelled:
            self.status.set_text("Stopped")
            self._settle_question()
            self._render(full=True)      # full redraw drops the placeholder
            return
        if not result:
            self._show_error("The model returned an empty response", placeholder)
            return
        turn = {"role": "assistant", "content": result,
                "model": model or self._streaming_model}
        if self.privacy is not None:
            turn["privacy"] = self._badge(privacy_module.receipt_badge(receipt))
        self._finish_answer(turn, payload)
        self._append_turn(turn, replace_placeholder=placeholder)
        self.status.set_text(turn["privacy"]["label"] if turn.get("privacy") else "")
        self._after_answer(turn, payload)

    def _on_stream_error(self, message, cancel=None):
        if self._stale(cancel):
            return False
        placeholder = self._streaming_text is not None
        self._finish_stream()
        self._show_error(message, placeholder)

    def _settle_question(self):
        """A question whose request never went out keeps no "Private" promise badge."""
        if self.privacy is None:
            return
        for turn in reversed(self.conversation):
            if turn["role"] == "user":
                if (turn.get("privacy") or {}).get("tone") == "pending":
                    turn["privacy"] = self._badge((
                        "🔒 Not answered", "warn",
                        "The request did not complete. It was only ever addressed to "
                        "this conversation's attested models."))
                break

    def _show_error(self, message, placeholder=False):
        self.status.set_text("")
        self._settle_question()
        # Marked so history_for() keeps it out of what is sent upstream.
        self._append_turn({"role": "assistant", "content": f"**Error:** {message}",
                           "error": True},
                          replace_placeholder=placeholder)

    def _finish_stream(self):
        self._cancel = None
        self._streaming_text = None
        if self._redraw_after_answer:
            self._redraw_after_answer = False
            # After the finished answer has taken the placeholder's place.
            GLib.idle_add(self._redraw)
        self.send_button.set_sensitive(True)
        self.again_button.set_sensitive(True)
        self.stop_button.hide()
        return False

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
        self.app.notify("Conversation copied to clipboard")

    def _ask_save_path(self, title, name, kind, pattern, mime, parent=None):
        """Where the user wants a file saved, or None if they cancelled."""
        # Native, so a sandboxed or portal-only desktop gets its own file dialog.
        dialog = Gtk.FileChooserNative.new(title, parent or self,
                                           Gtk.FileChooserAction.SAVE, "_Save", "_Cancel")
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(name)
        folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
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
        try:
            if picture:
                with open(picture, "wb") as out:
                    out.write(self.screenshot)
            with open(path, "w", encoding="utf-8") as out:
                out.write(self._markdown(picture and os.path.basename(picture)) + "\n")
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
            with open(path, "wb") as out:
                out.write(self.screenshot)
        except OSError as e:
            self.status.set_text(f"Screenshot not saved: {e.strerror or e}")
            return
        self.status.set_text(f"Saved to {os.path.basename(path)}")

    def _on_destroy(self, _widget):
        self._cancel_undo_timer()
        # Reaching here at all means the view did not end the process, whether or not
        # the page ever finished loading. A window closed early must not count against
        # WebKit - see webkitguard.
        webkitguard.note_success()
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
