"""Review and settings dialogs.

One review dialog for a capture and one for clipboard text, each with an optional prompt:
confirming what is sent and asking a question about it differ only in that.

The capture dialog shows exactly what will be sent - the post-processing dimensions
and the PNG payload size - so a bad capture is obvious before it costs a request.
"""

import os

from .. import gtkcompat
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from .. import imaging, llm as llm_module, netguard, privacy as privacy_module, sealed
from ..vendored import location as vendored_location
from .model_browser import ModelBrowser

# A dictionary dragged within Settings > Reference's list, by its handle.
_DICTIONARY_TARGET = Gtk.TargetEntry.new("application/x-da-assistant-dictionary",
                                         Gtk.TargetFlags.SAME_APP, 0)
_DRAG_ICON = "list-drag-handle-symbolic"


def web_search_toggle(config, default=None, privacy=None):
    """A 'Search the web' check button seeded from the saved default.

    `default` lets an operation override that seed. Translation passes False: the model
    translates from the text it was given, so a search adds cost and latency for nothing.
    The user can still tick it, and follow-ups in the chat window are unaffected.

    Lives on the compose dialogs because that is the only point at which the user can
    still influence the FIRST request - the chat window's own toggle only takes effect
    from the follow-up onwards, since the first message is already in flight by the time
    that window appears.
    """
    button = Gtk.CheckButton.new_with_label("Search the web")
    button.set_active(bool(getattr(config, "web_search_enabled", False))
                      if default is None else bool(default))
    button.set_tooltip_text("Look things up online before answering this request")
    if privacy is not None:
        # The query is made from what is being sent, and would go to a search provider
        # in the clear. LLMClient never searches for a private request either way.
        button.set_active(False)
        button.set_sensitive(False)
        button.set_tooltip_text("Off in Privacy Mode: the search query would carry your "
                                "text outside the attested enclave")
    return button


def privacy_note(privacy):
    """The compose dialogs' statement that this request is private, or None.

    Shown before anything is sent, because that is the moment it matters: the capture or
    the clipboard text is on screen and about to go somewhere.
    """
    if privacy is None:
        return None
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    box.get_style_context().add_class("privacy-compose")
    icon = Gtk.Image.new_from_icon_name("channel-secure-symbolic",
                                        Gtk.IconSize.LARGE_TOOLBAR)
    box.pack_start(icon, False, False, 0)
    models = []
    for model in privacy.models():
        role = privacy.role(model)
        how = ("end-to-end encrypted" if privacy_module.is_private_mode(model)
               else "TEE, replies signed")
        models.append(f"<b>{GLib.markup_escape_text(model)}</b> ({role}; {how})")
    label = Gtk.Label(xalign=0, wrap=True, max_width_chars=90)
    label.set_markup(
        f"<b>{'🔐' if privacy.all_sealed else '🔒'} Privacy Mode</b> — this goes only to "
        + " and ".join(models)
        + ", each attested before anything is sent to it. Web search is off, and the "
          "conversation stays private until it is closed.")
    box.pack_start(label, True, True, 0)
    return box


def _scaled_pixbuf(image, max_w, max_h):
    from ..capture.selector import pil_to_pixbuf
    pixbuf = pil_to_pixbuf(image)
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w <= max_w and h <= max_h:
        return pixbuf
    scale = min(max_w / w, max_h / h)
    return pixbuf.scale_simple(max(1, int(w * scale)), max(1, int(h * scale)),
                               GdkPixbuf.InterpType.BILINEAR)


class CaptureReviewDialog(Gtk.Window):
    """Preview a capture, optionally ask a question about it, then send.

    on_confirm(image, image_base64, info, prompt_or_None, web_search_bool)
    on_recrop(image) - optional; lets the user re-select from the same capture without
    taking a new screenshot.

    Black out... paints over parts of the capture (ui/redact.py). The blacked-out image
    replaces the capture outright: it is what is sent, what the chat window shows and
    saves, and what a re-crop starts from.
    """

    def __init__(self, image, operation, config, on_confirm,
                 ask_prompt=False, on_recrop=None, prompt_label="Your question",
                 web_search_default=None, privacy=None):
        super().__init__(title=operation)
        self.image = image
        self.config = config
        self.on_confirm = on_confirm
        self.on_recrop = on_recrop
        self._done = False
        self._blacked_out = 0

        # Prepare once, here, so the dialog can report the true payload and the caller
        # does not repeat the work.
        self.image_base64, self.info = imaging.prepare_for_vision(image, config)

        self.set_default_size(880, 720)
        self.set_position(Gtk.WindowPosition.CENTER)

        header = Gtk.HeaderBar(show_close_button=True, title=operation)
        header.set_subtitle("Review before sending privately" if privacy
                            else "Review before sending")
        self.set_titlebar(header)

        cancel = Gtk.Button.new_with_label("Cancel")
        cancel.connect("clicked", lambda _w: self.destroy())
        header.pack_start(cancel)

        if on_recrop is not None:
            recrop = Gtk.Button.new_with_label("Re-crop")
            recrop.set_tooltip_text("Choose a different area of this same capture")
            recrop.connect("clicked", self._on_recrop)
            header.pack_start(recrop)

        self.redact_button = Gtk.Button.new_with_label("Black out…")
        self.redact_button.set_tooltip_text(
            "Paint over anything that should not be sent - names, addresses, keys")
        self.redact_button.connect("clicked", lambda _w: self._on_redact())
        header.pack_start(self.redact_button)

        self.send = Gtk.Button.new_with_label("Send privately" if privacy else "Send")
        self.send.get_style_context().add_class("suggested-action")
        self.send.connect("clicked", lambda _w: self._confirm())
        header.pack_end(self.send)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_top(12)
        box.set_margin_bottom(14)
        self.add(box)

        note = privacy_note(privacy)
        if note is not None:
            box.pack_start(note, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.get_style_context().add_class("preview-frame")
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        self.preview = preview = Gtk.Image.new_from_pixbuf(
            _scaled_pixbuf(image, 1400, 900))
        preview.set_margin_top(6)
        preview.set_margin_bottom(6)
        scrolled.add(preview)
        box.pack_start(scrolled, True, True, 0)

        meta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.meta = meta = Gtk.Label(label=imaging.describe(self.info), xalign=0)
        meta.get_style_context().add_class("capture-meta")
        meta_row.pack_start(meta, False, False, 0)
        meta_row.pack_start(Gtk.Box(), True, True, 0)
        self.search_check = web_search_toggle(config, web_search_default, privacy)
        meta_row.pack_start(self.search_check, False, False, 0)
        box.pack_start(meta_row, False, False, 0)

        self.prompt_view = None
        if ask_prompt:
            label = Gtk.Label(label=prompt_label, xalign=0)
            label.get_style_context().add_class("dim-label")
            box.pack_start(label, False, False, 0)

            frame = Gtk.ScrolledWindow()
            frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            frame.set_min_content_height(64)
            frame.set_max_content_height(140)
            frame.get_style_context().add_class("preview-frame")
            self.prompt_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
            self.prompt_view.set_margin_start(8)
            self.prompt_view.set_margin_end(8)
            self.prompt_view.set_margin_top(6)
            self.prompt_view.set_margin_bottom(6)
            self.prompt_view.connect("key-press-event", self._on_key)
            frame.add(self.prompt_view)
            box.pack_start(frame, False, False, 0)

        self.connect("key-press-event", self._on_window_key)
        self.show_all()
        self.present()
        (self.prompt_view or self.send).grab_focus()

    def _prompt_text(self):
        if self.prompt_view is None:
            return None
        buf = self.prompt_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

    def _on_key(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and \
                not (event.state & Gdk.ModifierType.SHIFT_MASK):
            self._confirm()
            return True
        return False

    def _on_window_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _on_redact(self):
        from .redact import RedactDialog
        if self._done:
            return
        self.redact_button.set_sensitive(False)

        def done(image, boxes):
            if self._done or not self.get_realized():
                return
            self.redact_button.set_sensitive(True)
            if image is None or not boxes:
                return
            self.image = image
            self._blacked_out += len(boxes)
            self.image_base64, self.info = imaging.prepare_for_vision(image, self.config)
            self.preview.set_from_pixbuf(_scaled_pixbuf(image, 1400, 900))
            self.meta.set_text(f"{imaging.describe(self.info)} · {self._blacked_out} "
                               f"area{'s' if self._blacked_out != 1 else ''} blacked out")

        RedactDialog(self, self.image, done, apply_label="Apply")

    def _on_recrop(self, _widget):
        if self._done:
            return
        self._done = True
        image = self.image
        self.destroy()
        self.on_recrop(image)

    def _confirm(self):
        if self._done:
            return
        prompt = self._prompt_text()
        if self.prompt_view is not None and not prompt:
            self.prompt_view.grab_focus()
            return
        self._done = True
        web_search = self.search_check.get_active()
        self.destroy()
        self.on_confirm(self.image, self.image_base64, self.info, prompt, web_search)


class TextReviewDialog(Gtk.Window):
    """Review/edit text, optionally ask a question about it, then send.

    on_confirm(text, prompt_or_None, web_search_bool)
    """

    def __init__(self, text, operation, on_confirm, ask_prompt=False,
                 prompt_label="Your question", config=None, web_search_default=None,
                 privacy=None):
        super().__init__(title=operation)
        self.on_confirm = on_confirm
        self._done = False

        self.set_default_size(760, 600)
        self.set_position(Gtk.WindowPosition.CENTER)

        header = Gtk.HeaderBar(show_close_button=True, title=operation)
        header.set_subtitle("Review before sending privately" if privacy
                            else "Review before sending")
        self.set_titlebar(header)

        cancel = Gtk.Button.new_with_label("Cancel")
        cancel.connect("clicked", lambda _w: self.destroy())
        header.pack_start(cancel)

        self.send = Gtk.Button.new_with_label("Send privately" if privacy else "Send")
        self.send.get_style_context().add_class("suggested-action")
        self.send.connect("clicked", lambda _w: self._confirm())
        header.pack_end(self.send)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_top(12)
        box.set_margin_bottom(14)
        self.add(box)

        note = privacy_note(privacy)
        if note is not None:
            box.pack_start(note, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.get_style_context().add_class("preview-frame")
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        self.textview = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
        self.textview.set_margin_start(8)
        self.textview.set_margin_end(8)
        self.textview.set_margin_top(6)
        self.textview.set_margin_bottom(6)
        self.textview.get_buffer().set_text(text or "")
        scrolled.add(self.textview)
        box.pack_start(scrolled, True, True, 0)

        meta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        counter = Gtk.Label(label=f"{len(text or '')} characters", xalign=0)
        counter.get_style_context().add_class("capture-meta")
        meta_row.pack_start(counter, False, False, 0)
        meta_row.pack_start(Gtk.Box(), True, True, 0)
        self.search_check = web_search_toggle(config, web_search_default, privacy)
        meta_row.pack_start(self.search_check, False, False, 0)
        box.pack_start(meta_row, False, False, 0)
        self.textview.get_buffer().connect(
            "changed", lambda b: counter.set_text(
                f"{b.get_char_count()} characters"))

        self.prompt_view = None
        if ask_prompt:
            label = Gtk.Label(label=prompt_label, xalign=0)
            label.get_style_context().add_class("dim-label")
            box.pack_start(label, False, False, 0)

            frame = Gtk.ScrolledWindow()
            frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            frame.set_min_content_height(64)
            frame.set_max_content_height(140)
            frame.get_style_context().add_class("preview-frame")
            self.prompt_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
            self.prompt_view.set_margin_start(8)
            self.prompt_view.set_margin_end(8)
            self.prompt_view.set_margin_top(6)
            self.prompt_view.set_margin_bottom(6)
            self.prompt_view.connect("key-press-event", self._on_key)
            frame.add(self.prompt_view)
            box.pack_start(frame, False, False, 0)

        self.connect("key-press-event", self._on_window_key)
        self.show_all()
        self.present()
        (self.prompt_view or self.textview).grab_focus()

    def _on_key(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and \
                not (event.state & Gdk.ModifierType.SHIFT_MASK):
            self._confirm()
            return True
        return False

    def _on_window_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _confirm(self):
        if self._done:
            return
        buf = self.textview.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            return
        prompt = None
        if self.prompt_view is not None:
            pbuf = self.prompt_view.get_buffer()
            prompt = pbuf.get_text(pbuf.get_start_iter(), pbuf.get_end_iter(), False).strip()
            if not prompt:
                self.prompt_view.grab_focus()
                return
        self._done = True
        web_search = self.search_check.get_active()
        self.destroy()
        self.on_confirm(text, prompt, web_search)


class SettingsDialog(Gtk.Dialog):
    TEXT_FIELDS = [
        ("api_url", "API URL", "OpenAI-compatible /chat/completions endpoint. It must be "
                               "https, unless it is on this machine or its local network."),
        ("api_key", "API key", "Kept in your desktop's keyring (GNOME Keyring, KWallet)"),
        ("default_language", "Target language", "Used for translation and replies"),
    ]
    # (config key, label, must accept images)
    MODEL_FIELDS = [
        ("text_model", "Text model", False),
        ("premium_text_model", "Premium text model", False),
        ("vision_model", "Vision model (OCR)", True),
    ]

    # (config key, label, must accept images) - browsed among TEE models only.
    PRIVATE_FIELDS = [
        ("private_text_model", "Private text model", False),
        ("private_premium_model", "Private premium model", False),
        ("private_vision_model", "Private vision model", True),
    ]

    SEARCH_OPTION_LABELS = {
        ("web_search_context_size", "low"): "low — 3 results",
        ("web_search_context_size", "medium"): "medium — 5 results",
        ("web_search_context_size", "high"): "high — 8 results",
    }

    MODEL_TOOLTIPS = {
        "text_model": ("Answers clipboard operations, translations and follow-ups - and "
                       "questions about an image: with the image itself if it accepts "
                       "images, otherwise from the vision model's reading of it."),
        "vision_model": ("Reads images: OCR, stage one of Translate image selection, and "
                         "the transcription and description a text model that cannot "
                         "see images answers from. Wants a cheap, literal reader."),
    }

    def __init__(self, parent, config, client, encyclopedia=None, notify=None,
                 hotkeys=None):
        super().__init__(title="Settings", transient_for=parent, modal=True)
        self.config = config
        # The running HotkeyManager, for Settings > Shortcuts to say how keys are bound
        # here and which could not be.
        self.hotkeys = hotkeys
        self.client = client
        # The app's own encyclopedia, so that an import started here replaces the one
        # every window is reading, and `notify` says when it is done - it can take a
        # minute, and Settings may well be closed by then.
        self.encyclopedia = encyclopedia
        self.notify = notify or (lambda message, urgency="low": None)
        self._closed = False
        self.connect("destroy", lambda _w: setattr(self, "_closed", True))
        self.set_default_size(680, -1)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.save_button = save = self.add_button("Save", Gtk.ResponseType.OK)
        save.get_style_context().add_class("suggested-action")

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(12)
        content.set_margin_bottom(12)

        notebook = self.notebook = Gtk.Notebook()
        content.pack_start(notebook, True, True, 0)
        notebook.append_page(self._build_general(), Gtk.Label(label="General"))
        notebook.append_page(self._build_privacy(), Gtk.Label(label="Privacy"))
        notebook.append_page(self._build_search(), Gtk.Label(label="Web search"))
        notebook.append_page(self._build_reference(), Gtk.Label(label="Reference"))
        from .shortcuts import ShortcutsPage
        self.shortcuts_page = ShortcutsPage(config, hotkeys)
        notebook.append_page(self.shortcuts_page, Gtk.Label(label="Shortcuts"))
        notebook.append_page(self._build_advanced(), Gtk.Label(label="Advanced"))

        self.show_all()
        self._sync_search_sensitivity()
        self._sync_privacy()

    @staticmethod
    def _page():
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        return box

    @staticmethod
    def _combo():
        """A drop-down that opens below itself with every option showing.

        A GtkComboBox left to its default opens its menu over itself, lined up on the
        chosen option, and is allowed to shrink but not to flip - so one near the top
        or bottom of the dialog opened as a sliver of one option and scroll arrows.
        With a wrap width it is placed the way any other menu is: below, or above
        when there is no room below.
        """
        combo = Gtk.ComboBoxText()
        combo.set_wrap_width(1)
        return combo

    @staticmethod
    def _label(text):
        label = Gtk.Label(label=text, xalign=1)
        label.get_style_context().add_class("dim-label")
        return label

    # ---- general ----------------------------------------------------------

    def _build_general(self):
        page = self._page()
        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        page.pack_start(grid, False, False, 0)

        self.entries = {}
        row = 0
        for key, label_text, tooltip in self.TEXT_FIELDS:
            entry = Gtk.Entry(hexpand=True)
            entry.set_text(str(getattr(self.config, key, "")))
            if tooltip:
                entry.set_tooltip_text(tooltip)
            if key == "api_key":
                entry.set_visibility(False)
                entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
                keychain = getattr(self.config, "keychain", None)
                problem = getattr(self.config, "keychain_error", None) or (
                    keychain.problem() if keychain is not None else None)
                if problem:
                    entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY,
                                                  "dialog-warning-symbolic")
                    entry.set_icon_tooltip_text(
                        Gtk.EntryIconPosition.SECONDARY,
                        f"{problem} A key typed here is used until the app quits.")
            grid.attach(self._label(label_text), 0, row, 1, 1)
            grid.attach(entry, 1, row, 2, 1)
            self.entries[key] = entry
            row += 1

        for key, label_text, vision_only in self.MODEL_FIELDS:
            entry = Gtk.Entry(hexpand=True)
            entry.set_text(str(getattr(self.config, key, "")))
            if key in self.MODEL_TOOLTIPS:
                entry.set_tooltip_text(self.MODEL_TOOLTIPS[key])
            browse = Gtk.Button.new_with_label("Browse…")
            browse.set_tooltip_text("Search the provider's model list")
            browse.connect("clicked", self._on_browse, key, vision_only)
            grid.attach(self._label(label_text), 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            grid.attach(browse, 2, row, 1, 1)
            self.entries[key] = entry
            row += 1

        # Not a warning to click through: an address that would send the key in the
        # clear is refused, here and again wherever a request is made (netguard.py).
        self.url_warning = Gtk.Label(xalign=0, wrap=True, max_width_chars=72)
        self.url_warning.set_no_show_all(True)
        page.pack_start(self.url_warning, False, False, 0)
        self.entries["api_url"].connect("changed", self._sync_url_warning)
        self._sync_url_warning(self.entries["api_url"])

        test_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.test_button = Gtk.Button.new_with_label("Test connection")
        self.test_button.connect("clicked", self._on_test)
        test_row.pack_start(self.test_button, False, False, 0)
        self.test_result = Gtk.Label(label="", xalign=0, wrap=True)
        self.test_result.get_style_context().add_class("dim-label")
        test_row.pack_start(self.test_result, True, True, 0)
        page.pack_start(test_row, False, False, 0)
        return page

    def url_problem(self):
        """Why the API URL as typed may not be used, or None."""
        return netguard.problem(self.entries["api_url"].get_text())

    def _sync_url_warning(self, _entry):
        problem = self.url_problem()
        self.url_warning.set_markup(
            f'<span foreground="#d23a31">{GLib.markup_escape_text(problem)}</span>'
            if problem else "")
        self.url_warning.set_visible(bool(problem))
        if hasattr(self, "privacy_problems"):       # built: Save follows what is typed
            self._sync_privacy()

    def _on_browse(self, _widget, key, vision_only, private_only=False):
        kind = "TEE " if private_only else ""
        browser = ModelBrowser(self, self.config,
                               title=f"Choose a {kind}{'vision ' if vision_only else ''}"
                                     f"model",
                               vision_only=vision_only, private_only=private_only,
                               current=self.entries[key].get_text().strip())
        browser.run()
        chosen = browser.selected_id
        browser.destroy()
        if chosen:
            self.entries[key].set_text(chosen)

    # ---- privacy ----------------------------------------------------------

    def _build_privacy(self):
        page = self._page()

        self.privacy_enabled = Gtk.CheckButton.new_with_label(
            "Privacy Mode — send everything to attested TEE models only")
        self.privacy_enabled.set_active(bool(self.config.privacy_mode))
        self.privacy_enabled.connect("toggled", lambda _w: self._sync_privacy())
        page.pack_start(self.privacy_enabled, False, False, 0)

        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=78)
        note.set_markup(
            "<small>Every operation - translation, OCR, questions and their follow-ups - "
            "goes to the models below instead of the ones on the General page, and only "
            "after its enclave has proved itself. The premium model answers instead of "
            "the text model when the premium option is on, and a conversation's model "
            "menu can switch to any TEE model. Web search is off.\n\n"
            "<b>TEE/</b> models run in an attested trusted execution environment and "
            "sign their replies; your text still passes nano-gpt's gateway in the "
            "clear. <b>private/</b> models are also <b>end-to-end encrypted</b>: sealed "
            "on this machine to a key only the attested enclave holds.\n\n"
            "A conversation keeps the privacy it started with: switching this off, or "
            "choosing other models, changes only the conversations that start "
            "afterwards.</small>")
        note.get_style_context().add_class("dim-label")
        page.pack_start(note, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        page.pack_start(grid, False, False, 0)
        self.privacy_kinds = {}
        for row, (key, label_text, vision_only) in enumerate(self.PRIVATE_FIELDS):
            entry = Gtk.Entry(hexpand=True)
            entry.set_text(str(getattr(self.config, key, "")))
            entry.connect("changed", lambda _w: self._sync_privacy())
            browse = Gtk.Button.new_with_label("Browse…")
            browse.set_tooltip_text("Choose among the provider's TEE models")
            browse.connect("clicked", self._on_browse, key, vision_only, True)
            kind = Gtk.Label(xalign=0)
            kind.get_style_context().add_class("privacy-badge")
            grid.attach(self._label(label_text), 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            grid.attach(kind, 2, row, 1, 1)
            grid.attach(browse, 3, row, 1, 1)
            self.entries[key] = entry
            self.privacy_kinds[key] = kind

        self.privacy_problems = Gtk.Label(xalign=0, wrap=True, max_width_chars=78)
        page.pack_start(self.privacy_problems, False, False, 0)

        self.e2ee_status = e2ee = Gtk.Label(xalign=0, wrap=True, max_width_chars=78)
        e2ee.get_style_context().add_class("dim-label")
        page.pack_start(e2ee, False, False, 0)
        self._show_e2ee()
        if sealed.known() is None:
            # Finding out imports Tinfoil's client, most of a second the first time and
            # every window frozen for it if done here. A thread of its own does it, and
            # the line above and the problems it bears on are filled in when it has.
            sealed.check(lambda _answer: GLib.idle_add(self._on_e2ee_known))

        check_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.attest_button = Gtk.Button.new_with_label("Check attestation")
        self.attest_button.set_tooltip_text(
            "Attest the private models' enclaves now, exactly as a conversation will before "
            "it sends anything. Attestation is free; nothing is sent to the models.")
        self.attest_button.connect("clicked", self._on_check_attestation)
        check_row.pack_start(self.attest_button, False, False, 0)
        page.pack_start(check_row, False, False, 0)
        self.attest_result = Gtk.Label(xalign=0, wrap=True, max_width_chars=78)
        self.attest_result.set_selectable(True)
        page.pack_start(self.attest_result, False, False, 0)
        return page

    def _show_e2ee(self):
        """Whether Tinfoil's client can be used, as far as is known yet."""
        answer = sealed.known()
        if answer is None:
            text = "checking…"
        elif answer[0]:
            where = f" from {vendored_location()}" if vendored_location() else ""
            text = f"available ({answer[1]}{where})"
        else:
            text = answer[1]
        self.e2ee_status.set_markup("<small>End-to-end encryption: "
                                    + GLib.markup_escape_text(text) + "</small>")

    def _on_e2ee_known(self):
        if not self._closed:
            self._show_e2ee()
            self._sync_privacy()
        return False                    # one-shot idle callback

    def _staged(self):
        """The settings as they stand in this dialog, without touching the real ones."""
        return self.config.detached(**self.values())

    def _sync_privacy(self):
        """Say what each private model is, and hold Save while what is typed could not
        be used: an API URL that is not https, or Privacy Mode on and not able to be
        honoured - saving that would only make every operation refuse."""
        for key, label in self.privacy_kinds.items():
            model = self.entries[key].get_text().strip()
            kind = privacy_module.kind_of(model)
            context = label.get_style_context()
            for tone in ("ok", "bad"):
                context.remove_class(f"privacy-{tone}")
            if kind == privacy_module.E2EE:
                label.set_text("🔐 E2EE")
                context.add_class("privacy-ok")
            elif kind == privacy_module.TEE:
                label.set_text("🔒 TEE")
                context.add_class("privacy-ok")
            else:
                label.set_text("not TEE" if model else "")
                context.add_class("privacy-bad")
            label.set_tooltip_text(privacy_module.describe(model)[1] or
                                   "Privacy Mode can use TEE/ and private/ models only")
        # Not waiting to hear whether Tinfoil's client can be used: _on_e2ee_known comes
        # back here when that is known. Until then Save waits too, if it matters.
        found = privacy_module.problems(self._staged(), wait=False)
        unsure = sealed.known() is None and any(
            privacy_module.is_private_mode(self.entries[key].get_text().strip())
            for key in self.privacy_kinds)
        enabled = self.privacy_enabled.get_active()
        if found:
            text = " ".join(found)
            if enabled:
                text = "Privacy Mode cannot be switched on: " + text[0].lower() + text[1:]
            self.privacy_problems.set_markup(
                f'<span foreground="#d23a31">{GLib.markup_escape_text(text)}</span>')
        else:
            self.privacy_problems.set_text("")
        self.privacy_problems.set_visible(bool(found))
        self.save_button.set_sensitive(not (enabled and (found or unsure))
                                       and not self.url_problem())

    def _on_check_attestation(self, _widget):
        import threading
        self.attest_button.set_sensitive(False)
        self.attest_result.set_text("Attesting…")
        staged = self.config.detached(**dict(self.values(), privacy_mode=True))

        def worker():
            lines = []
            try:
                session = privacy_module.PrivacySession.start(
                    staged, uses=("text", "premium", "vision"))
            except privacy_module.PrivacyError as e:
                GLib.idle_add(self._show_attestation, [f"✗ {e}"])
                return
            for model in session.models():
                try:
                    attestation = session.ensure(model)
                except privacy_module.ModelRetired:
                    lines.append(f"✗ {model}: no longer offered by nano-gpt's Private "
                                 f"Mode — choose another private model")
                except privacy_module.PrivacyError as e:
                    lines.append(f"✗ {model}: {e}")
                else:
                    lines.append(f"✓ {model}: {attestation.headline} — "
                                 f"{attestation.detail}")
            GLib.idle_add(self._show_attestation, lines)

        threading.Thread(target=worker, daemon=True).start()

    def _show_attestation(self, lines):
        if self._closed:
            return False
        self.attest_result.set_text("\n".join(lines))
        self.attest_button.set_sensitive(True)
        return False

    # ---- web search -------------------------------------------------------

    def _build_search(self):
        page = self._page()

        self.search_enabled = Gtk.CheckButton.new_with_label(
            "Enable web search by default")
        self.search_enabled.set_active(bool(self.config.web_search_enabled))
        self.search_enabled.connect("toggled", lambda _w: self._sync_search_sensitivity())
        page.pack_start(self.search_enabled, False, False, 0)

        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=72)
        note.set_markup(
            "<small>The app searches the web itself and hands the results to the model, "
            "so the query is what you asked and the sources can be listed. "
            "This is only the default: every request can be switched with the "
            "<b>Search the web</b> box on the send dialog, and follow-ups with the "
            "magnifier in the chat window.</small>")
        note.get_style_context().add_class("dim-label")
        page.pack_start(note, False, False, 0)

        self.search_craft = Gtk.CheckButton.new_with_label(
            "Let the model write the search query")
        self.search_craft.set_active(bool(
            getattr(self.config, "web_search_craft_query", True)))
        self.search_craft.set_tooltip_text(
            "Turns the question and what was captured into a search query first — the "
            "exception name rather than the whole stack trace, the error code rather "
            "than \"what does this mean\". Costs one cheap call per searched request.")
        page.pack_start(self.search_craft, False, False, 0)

        self.search_citations = Gtk.CheckButton.new_with_label(
            "List the sources searched")
        self.search_citations.set_active(bool(
            getattr(self.config, "web_search_citations", True)))
        self.search_citations.set_tooltip_text(
            "Ends answers with a Sources section listing the pages the search returned. "
            "The app searches the web itself, so these are the results it fetched rather "
            "than a list the model was asked to remember.")
        page.pack_start(self.search_citations, False, False, 0)

        self.search_grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        page.pack_start(self.search_grid, False, False, 0)

        self.search_combos = {}
        rows = [
            ("web_search_mode", "Mode", llm_module.WEB_SEARCH_MODES),
            ("web_search_provider", "Provider", llm_module.WEB_SEARCH_PROVIDERS),
            ("web_search_context_size", "Results", llm_module.WEB_SEARCH_CONTEXT_SIZES),
            ("web_search_valyu_search_type", "Valyu search type",
             llm_module.WEB_SEARCH_VALYU_TYPES),
        ]
        for row, (key, label_text, options) in enumerate(rows):
            combo = self._combo()
            for option in options:
                combo.append(option, self.SEARCH_OPTION_LABELS.get((key, option), option))
            combo.set_active_id(str(getattr(self.config, key, "") or options[0]))
            if combo.get_active_id() is None:
                combo.set_active(0)
            label = self._label(label_text)
            self.search_grid.attach(label, 0, row, 1, 1)
            self.search_grid.attach(combo, 1, row, 1, 1)
            self.search_combos[key] = (combo, label)

        # Provider-specific rows appear only for their provider.
        self.search_combos["web_search_provider"][0].connect(
            "changed", lambda _w: self._sync_search_sensitivity())
        return page

    def _sync_search_sensitivity(self):
        on = self.search_enabled.get_active()
        self.search_grid.set_sensitive(on)
        self.search_citations.set_sensitive(on)
        self.search_craft.set_sensitive(on)
        provider = self.search_combos["web_search_provider"][0].get_active_id() or ""
        for key, owner in (("web_search_valyu_search_type", "valyu"),):
            combo, label = self.search_combos[key]
            visible = provider == owner
            combo.set_visible(visible)
            label.set_visible(visible)
            combo.set_no_show_all(not visible)
            label.set_no_show_all(not visible)

    # ---- reference ----------------------------------------------------------

    def _build_reference(self):
        page = self._page()

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.pack_start(self._label("Dictionaries folder"), False, False, 0)
        self.dictionaries_dir = Gtk.Entry(hexpand=True)
        self.dictionaries_dir.set_text(self.config.dictionaries_dir or "")
        self.dictionaries_dir.set_tooltip_text(
            "Searched with everything inside it for DSL (.dsl, .dsl.dz) and StarDict "
            "(.ifo) dictionaries - the formats GoldenDict reads")
        # Not on every keystroke: half a path can be a folder with the whole disk in it.
        self.dictionaries_dir.connect("activate", lambda _w: self._list_dictionaries())
        self.dictionaries_dir.connect("focus-out-event",
                                      lambda *_a: self._list_dictionaries() and False)
        row.pack_start(self.dictionaries_dir, True, True, 0)
        choose = Gtk.Button.new_with_label("Choose…")
        choose.connect("clicked", self._on_choose_dictionaries)
        row.pack_start(choose, False, False, 0)
        page.pack_start(row, False, False, 0)

        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=78)
        note.set_markup(
            "<small><b>Look up word</b> searches the ticked dictionaries and shows every "
            "entry, as GoldenDict does, in the order listed here - drag a dictionary by "
            "its handle, or nudge it with its arrows. Anything you ask about them goes to "
            "the answering model with the entries. The files are read where they are. "
            "The first lookup "
            "indexes them into ~/.cache/da-assistant - about 15 seconds for 2 GB - and "
            "again only when a file changes.</small>")
        note.get_style_context().add_class("dim-label")
        page.pack_start(note, False, False, 0)

        scrolled = self.dictionary_scroller = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(320)
        scrolled.get_style_context().add_class("preview-frame")
        self.dictionary_list = Gtk.ListBox()
        self.dictionary_list.set_selection_mode(Gtk.SelectionMode.NONE)
        # Dropped on: a row dragged by its handle lands where it is let go.
        self.dictionary_list.drag_dest_set(Gtk.DestDefaults.ALL, [_DICTIONARY_TARGET],
                                           Gdk.DragAction.MOVE)
        self.dictionary_list.connect("drag-motion", self._on_dictionary_drag_motion)
        self.dictionary_list.connect("drag-leave", self._on_dictionary_drag_leave)
        self.dictionary_list.connect("drag-data-received", self._on_dictionary_dropped)
        scrolled.add(self.dictionary_list)
        page.pack_start(scrolled, True, True, 0)
        self._dictionary_checks = {}
        self._listed_folder = None
        self._list_dictionaries()

        page.pack_start(Gtk.Separator(), False, False, 6)
        self.encyclopedia_enabled = Gtk.CheckButton.new_with_label(
            "Search Encyclopedia and Ask Encyclopedia")
        self.encyclopedia_enabled.set_active(bool(self.config.encyclopedia_enabled))
        self.encyclopedia_enabled.set_tooltip_text(
            "For those with the 2015 encyclopedia DVD: read it, and have questions "
            "answered from it. Off, neither is in the tray menu and Ctrl+Shift+7 and 8 "
            "are left to other programs.")
        self.encyclopedia_enabled.connect(
            "toggled", lambda _w: self._sync_encyclopedia_sensitivity())
        page.pack_start(self.encyclopedia_enabled, False, False, 0)
        # Everything below follows the box above, as web search's settings follow its.
        section = self.encyclopedia_section = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.pack_start(section, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.pack_start(self._label("Encyclopedia DVD folder"), False, False, 0)
        self.encyclopedia_dir = Gtk.Entry(hexpand=True)
        self.encyclopedia_dir.set_text(self.config.encyclopedia_dir or "")
        self.encyclopedia_dir.set_tooltip_text(
            "The encyclopedia DVD's data folder: the one holding "
            "ARTICLE_DATA.zip, INSTALL_DATA.zip, IMAGE_DATA and MEDIA1_DATA to MEDIA4_DATA")
        row.pack_start(self.encyclopedia_dir, True, True, 0)
        choose = Gtk.Button.new_with_label("Choose…")
        choose.connect("clicked", self._on_choose_encyclopedia)
        row.pack_start(choose, False, False, 0)
        self.import_button = Gtk.Button.new_with_label("Import")
        self.import_button.set_tooltip_text(
            "Read the DVD's articles into a searchable database of the app's own, "
            "about 1 GB in ~/.local/share/da-assistant. About a minute. Pictures and "
            "films stay where they are and are read from this folder.")
        self.import_button.connect("clicked", self._on_import)
        row.pack_start(self.import_button, False, False, 0)
        section.pack_start(row, False, False, 0)

        self.import_progress = Gtk.ProgressBar(show_text=True)
        self.import_progress.set_no_show_all(True)
        section.pack_start(self.import_progress, False, False, 0)
        self.encyclopedia_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=78)
        self.encyclopedia_status.get_style_context().add_class("dim-label")
        section.pack_start(self.encyclopedia_status, False, False, 0)

        from ..reference.encyclopedia import AUDIENCE_NAMES
        asking = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.encyclopedia_strict = Gtk.CheckButton.new_with_label(
            "Ask Encyclopedia answers strictly from the encyclopedia")
        self.encyclopedia_strict.set_active(bool(self.config.encyclopedia_strict))
        self.encyclopedia_strict.set_tooltip_text(
            "The default for new Ask Encyclopedia windows; each can switch it. Strict: "
            "where the encyclopedia has nothing, the answer says so rather than filling in.")
        asking.pack_start(self.encyclopedia_strict, False, False, 0)
        asking.pack_start(Gtk.Box(), True, True, 0)
        asking.pack_start(self._label("Written for"), False, False, 0)
        self.encyclopedia_audience = self._combo()
        for key, name in AUDIENCE_NAMES.items():
            self.encyclopedia_audience.append(key, name)
        self.encyclopedia_audience.set_active_id(self.config.encyclopedia_audience or "adult")
        if self.encyclopedia_audience.get_active_id() is None:
            self.encyclopedia_audience.set_active_id("adult")
        asking.pack_start(self.encyclopedia_audience, False, False, 0)
        section.pack_start(asking, False, False, 0)
        self._sync_encyclopedia_sensitivity()
        self._show_encyclopedia_status()
        if self.encyclopedia is not None and self.encyclopedia.importing:
            self._watch_import()
        return page

    def _sync_encyclopedia_sensitivity(self):
        self.encyclopedia_section.set_sensitive(self.encyclopedia_enabled.get_active())

    def _on_choose_encyclopedia(self, _widget):
        dialog = Gtk.FileChooserNative.new("Choose the encyclopedia DVD's data folder", self,
                                           Gtk.FileChooserAction.SELECT_FOLDER,
                                           "_Choose", "_Cancel")
        current = os.path.expanduser(self.encyclopedia_dir.get_text().strip())
        if os.path.isdir(current):
            dialog.set_current_folder(current)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            self.encyclopedia_dir.set_text(dialog.get_filename() or "")
        dialog.destroy()

    def _show_encyclopedia_status(self):
        if self.encyclopedia is None:
            self.encyclopedia_status.set_text("")
            self.import_button.set_sensitive(False)
            return
        text = ("<small>Ask Encyclopedia: "
                + GLib.markup_escape_text(self.encyclopedia.describe()))
        from ..reference import ruffle
        if not ruffle.version():
            text += (". Flash animations need Ruffle: run tools/install-ruffle.sh from "
                     "the assistant's folder")
        self.encyclopedia_status.set_markup(text + "</small>")

    def _on_import(self, _widget):
        import threading
        folder = os.path.expanduser(self.encyclopedia_dir.get_text().strip())
        if not os.path.isdir(folder):
            self.encyclopedia_status.set_text("Choose the DVD's data folder first")
            return
        encyclopedia, notify = self.encyclopedia, self.notify

        def worker():
            try:
                meta = encyclopedia.import_from(folder)
            except Exception as e:
                GLib.idle_add(notify, f"Encyclopedia import failed: {e}", "normal")
            else:
                total = sum(int(meta[k]) for k in ("EB", "EBI", "EBK"))
                GLib.idle_add(notify, f"Encyclopedia imported: {total:,} articles")

        # Set before the thread starts, so the watcher below sees it running.
        encyclopedia.importing = (0.0, "Starting…")
        threading.Thread(target=worker, daemon=True).start()
        self._watch_import()

    def _watch_import(self):
        self.import_button.set_sensitive(False)
        self.import_progress.show()

        def tick():
            if self._closed:
                return False
            state = self.encyclopedia.importing
            if state is None:
                self.import_progress.hide()
                self.import_button.set_sensitive(True)
                self._show_encyclopedia_status()
                return False
            fraction, message = state
            self.import_progress.set_fraction(fraction)
            self.import_progress.set_text(message)
            return True

        GLib.timeout_add(250, tick)

    def _on_choose_dictionaries(self, _widget):
        dialog = Gtk.FileChooserNative.new("Choose your dictionaries folder", self,
                                           Gtk.FileChooserAction.SELECT_FOLDER,
                                           "_Choose", "_Cancel")
        current = os.path.expanduser(self.dictionaries_dir.get_text().strip())
        if os.path.isdir(current):
            dialog.set_current_folder(current)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            self.dictionaries_dir.set_text(dialog.get_filename() or "")
            self._list_dictionaries()
        dialog.destroy()

    def _list_dictionaries(self):
        """A row per dictionary in the folder, in lookup order: ticked unless disabled,
        with arrows to move it up and down."""
        from ..reference.dictionaries import Library
        folder = self.dictionaries_dir.get_text().strip()
        if folder == self._listed_folder:
            return
        self._listed_folder = folder
        # Keep what was unticked and moved here before the folder changed back and
        # forth.
        disabled = set(self.config.dictionaries_disabled or [])
        disabled |= {path for path, check in self._dictionary_checks.items()
                     if not check.get_active()}
        order = self._dictionary_order() or list(self.config.dictionaries_order or [])
        for child in self.dictionary_list.get_children():
            child.destroy()
        self._dictionary_checks = {}
        found = Library(self.config.detached(dictionaries_dir=folder,
                                             dictionaries_order=order)).available()
        if not found:
            text = ("No DSL or StarDict dictionaries in this folder" if folder
                    else "No folder chosen")
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class("dim-label")
            label.set_margin_start(8)
            label.set_margin_top(6)
            self.dictionary_list.add(label)
        for path, name in found:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            handle = Gtk.EventBox()
            handle.add(Gtk.Image.new_from_icon_name(_DRAG_ICON, Gtk.IconSize.MENU))
            handle.set_tooltip_text("Drag to move it up or down the list")
            handle.set_margin_start(6)
            handle.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [_DICTIONARY_TARGET],
                                   Gdk.DragAction.MOVE)
            handle.connect("drag-begin", self._on_dictionary_drag_begin, row)
            handle.connect("drag-data-get",
                           lambda _w, _c, data, _i, _t, p=path:
                           data.set(data.get_target(), 8, p.encode("utf-8")))
            handle.connect("realize", lambda w: w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(w.get_display(), "grab")))
            row.pack_start(handle, False, False, 0)
            check = Gtk.CheckButton.new_with_label(name)
            check.set_tooltip_text(path)
            check.set_active(path not in disabled)
            row.pack_start(check, True, True, 0)
            for icon, step, tip in (("go-up-symbolic", -1, "Earlier in the results"),
                                    ("go-down-symbolic", 1, "Later in the results")):
                button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
                button.set_relief(Gtk.ReliefStyle.NONE)
                button.set_tooltip_text(tip)
                button.connect("clicked", self._move_dictionary, row, step)
                row.pack_start(button, False, False, 0)
            row.dictionary_path = path
            self.dictionary_list.add(row)
            self._dictionary_checks[path] = check
        self.dictionary_list.show_all()

    def _move_dictionary(self, _button, row, step):
        listed = row.get_parent()
        index = listed.get_index() + step
        if 0 <= index < len(self._dictionary_checks):
            self.dictionary_list.remove(listed)
            self.dictionary_list.insert(listed, index)

    def _place_dictionary(self, path, index):
        """Move the dictionary at `path` to position `index` in the list, counted as the
        list stands before it moves - where a drop between two rows puts it."""
        rows = self.dictionary_list.get_children()
        moving = next((r for r in rows if getattr(r.get_child(), "dictionary_path", None)
                       == path), None)
        if moving is None:
            return
        if moving.get_index() < index:
            index -= 1                      # its own place closes up behind it
        self.dictionary_list.remove(moving)
        self.dictionary_list.insert(moving, max(0, min(index, len(rows) - 1)))

    def _drop_index(self, y):
        """Where a drop at `y` puts a row: before the row under it, or after it when
        in its lower half; after the last row below the list's end."""
        row = self.dictionary_list.get_row_at_y(y)
        if row is None:
            return len(self.dictionary_list.get_children())
        allocation = row.get_allocation()
        after = y > allocation.y + allocation.height / 2
        return row.get_index() + (1 if after else 0)

    def _on_dictionary_drag_begin(self, handle, context, row):
        # The row itself follows the pointer, so it is plain which is moving.
        try:
            import cairo
            allocation = row.get_allocation()
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, allocation.width,
                                         allocation.height)
            row.get_parent().draw(cairo.Context(surface))
            Gtk.drag_set_icon_surface(context, surface)
        except Exception:
            Gtk.drag_set_icon_name(context, _DRAG_ICON, 0, 0)

    def _on_dictionary_drag_motion(self, listbox, context, _x, y, time):
        row = listbox.get_row_at_y(y)
        if row is not None:
            listbox.drag_highlight_row(row)
        else:
            listbox.drag_unhighlight_row()
        # Near the top or bottom of what shows, the list scrolls: a dictionary can be
        # dragged from the bottom of a long list to the top of it in one go.
        adjustment = self.dictionary_scroller.get_vadjustment()
        top = adjustment.get_value()
        bottom = top + adjustment.get_page_size()
        if y < top + 32:
            adjustment.set_value(max(adjustment.get_lower(), top - 18))
        elif y > bottom - 32:
            adjustment.set_value(min(adjustment.get_upper() - adjustment.get_page_size(),
                                     top + 18))
        Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
        return True

    def _on_dictionary_drag_leave(self, listbox, _context, _time):
        listbox.drag_unhighlight_row()

    def _on_dictionary_dropped(self, listbox, _context, _x, y, data, _info, _time):
        listbox.drag_unhighlight_row()
        path = (data.get_data() or b"").decode("utf-8", "replace")
        if path:
            self._place_dictionary(path, self._drop_index(y))

    def _dictionary_order(self):
        """The paths of the listed dictionaries, as they are ordered now."""
        return [getattr(listed.get_child(), "dictionary_path", None)
                for listed in self.dictionary_list.get_children()
                if getattr(listed.get_child(), "dictionary_path", None)]

    # ---- advanced ---------------------------------------------------------

    def _build_advanced(self):
        page = self._page()

        self.upscale = Gtk.CheckButton.new_with_label(
            "Upscale small captures before sending (improves OCR accuracy)")
        self.upscale.set_active(bool(self.config.upscale_small_captures))
        page.pack_start(self.upscale, False, False, 0)

        self.debug_captures = Gtk.CheckButton.new_with_label(
            "Save every image sent to ~/.cache/da-assistant/captures")
        self.debug_captures.set_active(bool(self.config.debug_save_captures))
        page.pack_start(self.debug_captures, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        page.pack_start(grid, False, False, 0)

        self.hotkey_mode = self._combo()
        for value, text in (("auto", "automatic — best for this session"),
                            ("x11", "X11 key grabs"),
                            ("portal", "GlobalShortcuts portal"),
                            ("desktop", "the desktop's own keybindings"),
                            ("off", "off — no global shortcuts")):
            self.hotkey_mode.append(value, text)
        self.hotkey_mode.set_active_id(self.config.hotkey_mode or "auto")
        if self.hotkey_mode.get_active_id() is None:
            self.hotkey_mode.set_active_id("auto")
        self.hotkey_mode.set_tooltip_text(
            "How the keys in Settings > Shortcuts are bound. Automatic grabs them itself "
            "on X11, asks the GlobalShortcuts portal on KDE Plasma and GNOME, and "
            "otherwise writes them into the desktop's own keybindings. Choose another "
            "only if automatic gets it wrong.")
        grid.attach(self._label("Shortcuts by"), 0, 0, 1, 1)
        grid.attach(self.hotkey_mode, 1, 0, 1, 1)

        self.max_tokens = Gtk.SpinButton.new_with_range(256, 32768, 256)
        self.max_tokens.set_value(int(self.config.max_tokens or 4096))
        self.max_tokens.set_tooltip_text(
            "How long an answer may be. A model asked for a reasoning effort gets room to "
            "think on top of this, so its thinking cannot use up the answer's share.")
        grid.attach(self._label("Max response tokens"), 0, 1, 1, 1)
        grid.attach(self.max_tokens, 1, 1, 1, 1)

        self.reasoning_effort = self._combo()
        for value, text in (("none", "none — only where a model can switch it off"),
                            ("low", "low — fastest, cheapest"), ("medium", "medium"),
                            ("high", "high — thinks longest")):
            self.reasoning_effort.append(value, text)
        self.reasoning_effort.set_active_id(
            getattr(self.config, "reasoning_effort", "low") or "low")
        if self.reasoning_effort.get_active_id() is None:
            self.reasoning_effort.set_active_id("low")
        self.reasoning_effort.set_tooltip_text(
            "How hard models that always reason (GLM 5.3, Kimi K3, Claude's :thinking "
            "models...) think before answering. Low is fast and does well at "
            "translation, OCR and most questions; higher levels are slower and cost more "
            "thinking tokens. Each model gets the nearest level it offers. Models that "
            "reason only when asked are never sent it, since asking would switch their "
            "thinking on.\n\n"
            "None only works for models that support switching their thinking off. "
            "Those that cannot - GLM 5.3 and Kimi K3 among them - think at their lowest "
            "level instead.")
        grid.attach(self._label("Reasoning effort"), 0, 2, 1, 1)
        grid.attach(self.reasoning_effort, 1, 2, 1, 1)
        return page

    # ---- connection test --------------------------------------------------

    def _on_test(self, _widget):
        import threading
        self.test_button.set_sensitive(False)
        self.test_result.set_text("Contacting the provider…")
        # Tried out on a copy, with a client of its own. This used to write the staged
        # values into the live config and put the old ones back when the test returned -
        # so Save, clicked while it was still running, was undone a moment later, in
        # memory and then on disk by whatever saved next.
        client = type(self.client)(self.config.detached(**self.values()))

        def worker():
            ok, message = client.test_connection()
            GLib.idle_add(self._show_test_result, ok, message)

        threading.Thread(target=worker, daemon=True).start()

    def _show_test_result(self, ok, message):
        if self._closed:
            return False
        self.test_result.set_text(("OK — " if ok else "Failed — ") + message)
        self.test_button.set_sensitive(True)
        return False

    def values(self):
        out = {key: entry.get_text().strip() for key, entry in self.entries.items()}
        out["upscale_small_captures"] = self.upscale.get_active()
        out["debug_save_captures"] = self.debug_captures.get_active()
        out["hotkey_mode"] = self.hotkey_mode.get_active_id() or "auto"
        out.update(self.shortcuts_page.values())
        out["max_tokens"] = int(self.max_tokens.get_value())
        out["reasoning_effort"] = self.reasoning_effort.get_active_id() or "low"
        out["web_search_enabled"] = self.search_enabled.get_active()
        out["web_search_citations"] = self.search_citations.get_active()
        out["web_search_craft_query"] = self.search_craft.get_active()
        out["privacy_mode"] = self.privacy_enabled.get_active()
        out["dictionaries_dir"] = self.dictionaries_dir.get_text().strip()
        out["encyclopedia_enabled"] = self.encyclopedia_enabled.get_active()
        out["encyclopedia_dir"] = self.encyclopedia_dir.get_text().strip()
        out["encyclopedia_strict"] = self.encyclopedia_strict.get_active()
        out["encyclopedia_audience"] = self.encyclopedia_audience.get_active_id() or "adult"
        out["dictionaries_order"] = self._dictionary_order()
        out["dictionaries_disabled"] = sorted(
            path for path, check in self._dictionary_checks.items()
            if not check.get_active())
        for key, (combo, _label) in self.search_combos.items():
            value = combo.get_active_id()
            if value:
                out[key] = value
        return out
