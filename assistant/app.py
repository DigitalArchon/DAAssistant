"""Main application: tray, action dispatch, and the operations."""

import os

from . import gtkcompat
from gi.repository import Gdk, GLib, Gtk

from . import (APP_ID, APP_NAME, clipboard, errors, icon as app_icon, imaging, ipc,
                models as catalogue, privacy as privacy_module, prompts, webkitguard)
from .capture import CANCELLED, CaptureManager
from .config import Config
from .keychain import KeychainError
from .hotkeys import ACTIONS, ENCYCLOPEDIA_ACTIONS, HotkeyManager
from .llm import LLMClient, LLMError
from .reference.encyclopedia import Encyclopedia
from .reference.dictionaries import Library
from .ui.chat import ChatWindow
from .ui.dialogs import CaptureReviewDialog, SettingsDialog, TextReviewDialog
from .ui.tray import Tray

if gtkcompat.has_notify():
    from gi.repository import Notify
else:  # pragma: no cover
    Notify = None


def privacy_lines(config):
    """Privacy Mode's state, for Diagnostics and --probe."""
    from . import sealed, vendored
    ok, message = sealed.available()
    found = privacy_module.problems(config)
    return [
        f"Privacy Mode  : {'on' if config.privacy_mode else 'off'} "
        f"({config.private_text_model}, premium {config.private_premium_model}, "
        f"vision {config.private_vision_model})"
        + (f" - {' '.join(found)}" if found else ""),
        f"E2EE client   : {message if ok else 'not installed'}"
        + (f" from {vendored.location()}" if ok and vendored.location() else ""),
    ]


class Assistant:
    def __init__(self):
        # Before any worker thread exists: see collector.py.
        from . import collector
        collector.install()
        self.config = Config()
        self.client = LLMClient(self.config)
        self.capture = CaptureManager()
        self.dictionaries = Library(self.config)
        self.encyclopedia = Encyclopedia(self.config)
        self._reader = None                 # the Search Encyclopedia window, while open
        self.hotkeys = HotkeyManager(self.config, self.dispatch)
        self._notification = None
        self._settings_dialog = None

        self._load_css()

        if Notify is not None:
            Notify.init(APP_NAME)

        # From here on a failure inside a GTK callback is something the user can see and
        # send back, rather than a traceback written to a stderr nobody is reading.
        errors.install(self.notify)

        self.service = ipc.Service(self.dispatch)
        self.is_primary = self.service.acquire()

        # Start hotkeys before building the menu: the menu shows key hints only when
        # a hotkey mechanism is actually active.
        self.hotkeys.start()
        self.menu = self._build_menu()
        # Applies to every window the app opens: chat, settings, the selector.
        Gtk.Window.set_default_icon_name(app_icon.name())
        self.tray = Tray(APP_ID, APP_NAME, self.menu)

        if self.config.load_error:
            self.notify(f"Config problem, using defaults: {self.config.load_error}")
        elif self.config.keychain_error:
            self.notify(f"{self.config.keychain_error} The API key cannot be kept "
                        "between runs.", urgency="normal")
        if not self.config.api_key:
            GLib.idle_add(self._first_run_hint)
        else:
            self._warm_catalogue()

    # ---- setup ------------------------------------------------------------

    def _load_css(self):
        path = os.path.join(os.path.dirname(__file__), "ui", "style.css")
        if not os.path.exists(path):
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(path)
        except GLib.Error as e:
            print(f"[ui] stylesheet failed to load: {e}")
            return
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _warm_catalogue(self):
        """Refresh the model catalogue in the background, if it is stale.

        An image query asks it whether the answering text model accepts images, and
        must not wait on the network to find out (models.accepts_images).
        """
        import threading

        def worker():
            try:
                catalogue.fetch(self.config)
            except Exception as e:
                print(f"[models] catalogue not refreshed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _first_run_hint(self):
        self.notify("No API key set yet — open Settings from the tray menu")
        return False

    def _build_menu(self):
        menu = Gtk.Menu()
        show_keys = self.hotkeys.mode in ("x11", "portal")

        # First: a conversation with nothing captured, to type into straight away.
        ask = Gtk.MenuItem(label="Ask anything")
        ask.connect("activate", lambda _w: self.dispatch("ask_anything"))
        menu.append(ask)
        menu.append(Gtk.SeparatorMenuItem())

        self._encyclopedia_items = []
        for name, label, trigger in ACTIONS:
            text = label
            if show_keys:
                text = f"{label}\t{trigger.replace('<Control>', 'Ctrl+').replace('<Shift>', 'Shift+')}"
            item = Gtk.MenuItem(label=text)
            item.connect("activate", lambda _w, n=name: self.dispatch(n))
            menu.append(item)
            if name in ENCYCLOPEDIA_ACTIONS:
                self._encyclopedia_items.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        self.premium_item = Gtk.CheckMenuItem(label="Use premium text model")
        self.premium_item.set_active(bool(self.config.use_premium))
        self.premium_item.connect("toggled", self._on_premium_toggled)
        menu.append(self.premium_item)

        self.privacy_item = Gtk.CheckMenuItem(label="Privacy Mode (TEE models only)")
        self.privacy_item.set_active(bool(self.config.privacy_mode))
        self._privacy_handler = self.privacy_item.connect("toggled",
                                                          self._on_privacy_toggled)
        menu.append(self.privacy_item)

        settings_item = Gtk.MenuItem(label="Settings…")
        settings_item.connect("activate", lambda _w: self.show_settings())
        menu.append(settings_item)

        diagnostics_item = Gtk.MenuItem(label="Diagnostics…")
        diagnostics_item.connect("activate", lambda _w: self.show_diagnostics())
        menu.append(diagnostics_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _w: self.quit())
        menu.append(quit_item)

        menu.show_all()
        self._sync_encyclopedia_items()
        return menu

    def _sync_encyclopedia_items(self):
        for item in self._encyclopedia_items:
            item.set_visible(bool(self.config.encyclopedia_enabled))

    # ---- helpers ----------------------------------------------------------

    def notify(self, message, urgency="low"):
        """Replace the previous notification rather than stacking a new one."""
        if Notify is not None:
            try:
                if self._notification is None:
                    self._notification = Notify.Notification.new(APP_NAME, message, None)
                else:
                    self._notification.update(APP_NAME, message, None)
                self._notification.set_urgency(
                    Notify.Urgency.NORMAL if urgency == "normal" else Notify.Urgency.LOW)
                self._notification.show()
                return
            except Exception:
                pass
        print(f"[{APP_NAME}] {message}")

    def _on_premium_toggled(self, widget):
        self.config.use_premium = widget.get_active()
        try:
            self.config.save()
        except OSError as e:
            self.notify(f"Could not save settings: {e}", urgency="normal")
            return
        self.notify(f"Text model: {self.config.active_text_model()}")

    def _on_privacy_toggled(self, widget):
        self.config.privacy_mode = widget.get_active()
        try:
            self.config.save()
        except OSError as e:
            self.notify(f"Could not save settings: {e}", urgency="normal")
            return
        if not self.config.privacy_mode:
            self.notify("Privacy Mode off for new conversations. Open private "
                        "conversations stay private until they are closed.")
            return
        found = privacy_module.problems(self.config)
        if found:
            self.notify("Privacy Mode is on, but cannot be used yet: " + " ".join(found)
                        + " Choose models in Settings > Privacy.", urgency="normal")
            return
        answers = (self.config.private_premium_model if self.config.use_premium
                   else self.config.private_text_model)
        self.notify(f"Privacy Mode on: {answers} answers and "
                    f"{self.config.private_vision_model} reads images, each attested "
                    f"before anything is sent. Web search is off.")

    def _sync_privacy_item(self):
        """Settings can change privacy_mode too; keep the tray's tick honest without
        firing its handler, which would save and notify a second time."""
        self.privacy_item.handler_block(self._privacy_handler)
        try:
            self.privacy_item.set_active(bool(self.config.privacy_mode))
        finally:
            self.privacy_item.handler_unblock(self._privacy_handler)

    def _start_privacy(self, uses=("text", "vision")):
        """This operation's PrivacySession, None when Privacy Mode is off, or False when
        it is on and cannot be honoured - in which case the user has been told, and the
        operation must not go ahead on the ordinary models instead. `uses` is which of
        the private models the operation needs; see PrivacySession."""
        try:
            return privacy_module.PrivacySession.start(self.config, uses=uses)
        except privacy_module.PrivacyError as e:
            self.notify(str(e), urgency="normal")
            return False

    def _confirm_privacy(self, privacy, uses=("text", "vision")):
        """The session to send with, decided at Send. Privacy only ever tightens here:
        a dialog opened in Privacy Mode sends privately even if it was switched off
        since, and one opened outside it is upgraded if it was switched on - nothing
        has been sent yet, so it is not too late."""
        if privacy is not None:
            return privacy
        return self._start_privacy(uses)

    def dispatch(self, action):
        handler = {
            "ask_anything": self.ask_anything,
            "translate_text": self.translate_text,
            "ocr_translate": self.ocr_translate,
            "query_text": self.query_text,
            "query_image": self.query_image,
            "ocr_only": self.ocr_only,
            "look_up_word": self.look_up_word,
            "search_encyclopedia": self.search_encyclopedia,
            "ask_encyclopedia": self.ask_encyclopedia,
            "settings": self.show_settings,
            "diagnostics": self.show_diagnostics,
        }.get(action)
        if handler is None:
            self.notify(f"Unknown action: {action}")
            return False
        if action in ENCYCLOPEDIA_ACTIONS and not self.config.encyclopedia_enabled:
            # A desktop binding installed while it was on, or --action by hand.
            self.notify("The encyclopedia is off - turn it on in Settings > Reference",
                        urgency="normal")
            return False
        handler()
        return False

    def _open_chat(self, title, prompt, display, image_base64=None, first_model=None,
                   image_first=True, web_search=None, preflight=None, system=None,
                   privacy=None, screenshot=None):
        ChatWindow(
            self, title,
            [{"role": "user", "content": prompt, "display": display}],
            image_base64=image_base64, first_model=first_model,
            image_first=image_first, autostart=True,
            web_search=web_search, preflight=preflight, system=system,
            privacy=privacy, screenshot=screenshot)

    def _answer_role(self):
        """Which model answers a conversation: the premium one when the premium option
        is on, else the text model - private versions of each in Privacy Mode."""
        return "premium" if self.config.use_premium else "text"

    def _require_api_key(self):
        if self.config.api_key:
            return True
        self.notify("Set an API key in Settings first", urgency="normal")
        self.show_settings()
        return False

    # ---- capture plumbing -------------------------------------------------

    def _start_capture(self, title, ask_prompt, build, web_search_default=None,
                       transcribe_first=False, system=None, answer_by_text=False):
        """Capture -> review -> send. `build(prompt)` returns (prompt_text, display).

        The first step goes to the model chosen for its job, and the conversation then
        belongs to the answering model - text or premium, private versions of each in
        Privacy Mode - which the chat window's model menu can change at any message. The
        capture stays with the conversation throughout: see ChatWindow.

          OCR (neither flag)  the vision model transcribes; that is the first answer,
                              pure transcription, and follow-ups go to the answering
                              model.
          transcribe_first    the vision model transcribes, and the answering model
                              works on the text. See _transcribe_preflight.
          answer_by_text      the answering model answers a question about the image.
                              See _image_answer.
        """
        if not self._require_api_key():
            return
        # Every capture operation uses the vision model for its first step, and the
        # answering model for what follows.
        uses = ("vision", self._answer_role())
        privacy = self._start_privacy(uses)
        if privacy is False:
            return

        def on_error(message):
            if message == CANCELLED:
                return
            self.notify(f"Screenshot failed: {message}", urgency="normal")
            print(f"[capture] {message}")

        def on_image(image):
            self._review_capture(image, title, ask_prompt, build, on_error,
                                 web_search_default, transcribe_first, system, privacy,
                                 answer_by_text)

        self.capture.select_region(on_image, on_error)

    def _review_capture(self, image, title, ask_prompt, build, on_error,
                        web_search_default=None, transcribe_first=False, system=None,
                        privacy=None, answer_by_text=False):
        def on_recrop(original):
            from .capture import CropDialog
            CropDialog(
                original,
                lambda cropped: self._review_capture(
                    cropped, title, ask_prompt, build, on_error,
                    web_search_default, transcribe_first, system, privacy,
                    answer_by_text),
                on_error, title=f"{title} — re-crop")

        def on_confirm(image, image_base64, info, prompt, web_search):
            if info.get("debug_path"):
                print(f"[capture] sent image saved to {info['debug_path']}")
            session = self._confirm_privacy(privacy, ("vision", self._answer_role()))
            if session is False:
                return
            # What the chat window shows and saves: the crop as taken, not the copy
            # scaled for the model. Held in memory only; see ChatWindow.
            screenshot = imaging.screenshot_png(image)
            if answer_by_text:
                self._image_answer(title, image_base64, prompt, web_search, session,
                                   screenshot)
                return
            prompt_text, display = build(prompt)
            if transcribe_first:
                # The preflight transcribes, and the answering model translates the
                # text. The image stays with the conversation, for a follow-up that
                # needs it, but the translation itself is given only the text.
                self._open_chat(
                    title, prompt_text, display, image_base64=image_base64,
                    image_first=False, web_search=web_search, system=system,
                    preflight=self._transcribe_preflight(image_base64, build, session),
                    privacy=session, screenshot=screenshot)
            else:
                # OCR: the vision model's transcription is the first answer, and the
                # answering model takes the follow-ups.
                reader = session.vision_model if session else self.config.vision_model
                self._open_chat(title, prompt_text, display,
                                image_base64=image_base64, first_model=reader,
                                web_search=web_search, system=system, privacy=session,
                                screenshot=screenshot)

        CaptureReviewDialog(image, title, self.config, on_confirm,
                            ask_prompt=ask_prompt, on_recrop=on_recrop,
                            web_search_default=web_search_default, privacy=privacy)

    def _image_answerer(self, privacy):
        """The model that answers: see _answer_role."""
        if privacy is not None:
            return privacy.model_for(self._answer_role())
        return (self.config.premium_text_model if self.config.use_premium
                else self.config.text_model)

    def _image_answer(self, title, image_base64, question, web_search, privacy,
                      screenshot=None):
        """Answer a question about a capture with the answering model.

        If that model accepts images, it is given the image itself, with the OCR model's
        literal reading beside the question (see _read_preflight). If it does not - or
        the catalogue does not list it - the vision model reads the image first,
        transcribing its text and describing what it shows, and the answering model
        answers from that reading. Either way the capture stays with the conversation,
        so a follow-up sent to a model that can see is given it.
        """
        language = self.config.default_language
        answerer = self._image_answerer(privacy)
        if catalogue.accepts_images(self.config, answerer):
            self._open_chat(
                title, prompts.query_image(question), question,
                image_base64=image_base64,
                web_search=web_search, system=prompts.query_image_system(language),
                preflight=self._read_preflight(
                    image_base64,
                    lambda q, extracted=None: (prompts.query_image(q, extracted), q),
                    question, privacy),
                privacy=privacy, screenshot=screenshot)
            return
        reader = privacy.vision_model if privacy else self.config.vision_model
        self._open_chat(
            title, question, f"{question}\n\n_Reading the image with {reader}…_",
            image_base64=image_base64, image_first=False,
            web_search=web_search, system=prompts.query_described_system(language),
            preflight=self._describe_preflight(image_base64, question, privacy),
            privacy=privacy, screenshot=screenshot)

    def _describe_preflight(self, image_base64, question, privacy=None):
        """The vision model reads the image for a text model that cannot see it: the
        text, verbatim, and a description of the rest. Shown under the question, so the
        reader can see what the answer was given to work from."""
        def preflight(cancel, status):
            status("Reading and describing the image…")
            if privacy is None:
                reader = self.config.vision_model
                reading = self.client.complete(
                    reader, [{"role": "user", "content": prompts.IMAGE_BRIEF}],
                    image_base64=image_base64, cancel=cancel)
            else:
                reader = privacy.vision_model
                reading = self.client.complete(
                    reader, [{"role": "user", "content": prompts.IMAGE_BRIEF}],
                    image_base64=image_base64, cancel=cancel, privacy=privacy,
                    status=status)
            if cancel.is_set():
                return None
            reading = (reading or "").strip()
            if not reading:
                raise LLMError("The vision model returned nothing about the image",
                               model=reader)
            shown = "\n".join(f"> {line}" for line in reading.splitlines()[:40])
            return {"content": prompts.query_described(question, reading),
                    "display": f"{question}\n\n_Read from the image by {reader}:_\n\n"
                               f"{shown}",
                    "search_query": f"{question} {prompts.excerpt(reading)}".strip(),
                    # The chat window keeps it, so a later follow-up to a model that
                    # cannot see is not sent to the vision model a second time.
                    "reading": reading}

        return preflight

    def _ocr(self, image_base64, cancel, status, privacy):
        """The OCR model's reading of a capture: the session's vision model in Privacy
        Mode, attested and checked like any other private request."""
        if privacy is None:
            return self.client.complete(
                self.config.vision_model,
                [{"role": "user", "content": prompts.OCR}],
                image_base64=image_base64, cancel=cancel)
        return self.client.complete(
            privacy.vision_model, [{"role": "user", "content": prompts.OCR}],
            image_base64=image_base64, cancel=cancel, privacy=privacy, status=status)

    def _transcribe_preflight(self, image_base64, build, privacy=None):
        """Vision model transcribes, then the text model does the real work.

        Two stages, because doing it in one vision pass is worse in practice: the cheap,
        accurate OCR models are usually poor translators, so the transcription and the
        translation want different models.

        Note that a question about an image does not go through here. Splitting that one
        costs more than it saves: whatever the transcription failed to mention is simply
        gone by the time anything reasons about it, so it gets its own vision model and
        the image itself. See query_image.
        """
        def preflight(cancel, status):
            status("Reading text from the image…")
            extracted = self._ocr(image_base64, cancel, status, privacy)
            if cancel.is_set():
                return None
            extracted = (extracted or "").strip()
            if not extracted or extracted.upper().startswith("NO TEXT FOUND"):
                raise LLMError("No readable text was found in the selection")
            prompt_text, display = build(extracted)
            return {"content": prompt_text, "display": display}

        return preflight

    def _read_preflight(self, image_base64, build, question, privacy=None):
        """Read the image with the OCR model, and put the text beside the question.

        Two jobs for one cheap call. The model answering has the picture, but a literal
        reading from a model doing nothing else is what keeps an error code, a version
        or a stack frame exact - reading pixels while also reasoning about them is where
        a digit goes astray, and this app is pointed at error dialogs and consoles. And
        a web search cannot see the image at all, so without this it could only search
        for the question.

        Unlike _transcribe_preflight this adds to the request rather than replacing it:
        the image still goes to the model that answers. If nothing readable comes back,
        the question goes out unchanged and the picture speaks for itself.
        """
        def preflight(cancel, status):
            status("Reading the text in the image…")
            extracted = self._ocr(image_base64, cancel, status, privacy)
            if cancel.is_set():
                return None
            extracted = (extracted or "").strip()
            if not extracted or extracted.upper().startswith("NO TEXT FOUND"):
                return None
            prompt_text, display = build(question, extracted)
            # The query is built from the two things worth searching for, rather than
            # read back out of the prompt: the prompt carries labels the model needs and
            # a search engine trips over. See websearch.query_from.
            return {"content": prompt_text, "display": display,
                    "search_query": f"{question} {prompts.excerpt(extracted)}".strip()}

        return preflight

    def _start_clipboard(self, title, ask_prompt, build, web_search_default=None,
                         system=None):
        if not self._require_api_key():
            return
        privacy = self._start_privacy((self._answer_role(),))
        if privacy is False:
            return
        text, kind = clipboard.get_text()
        if kind != "text":
            message = {
                "image": "The clipboard holds an image, not text",
                "file": "The clipboard holds a file, not text",
            }.get(kind, "The clipboard is empty")
            if kind == "empty":
                # It may well not be: see clipboard.unreadable_hint.
                hint = clipboard.unreadable_hint()
                if hint:
                    message = f"Could not read the clipboard. {hint}"
            self.notify(message, urgency="normal")
            return

        def on_confirm(edited_text, prompt, web_search):
            session = self._confirm_privacy(privacy, (self._answer_role(),))
            if session is False:
                return
            prompt_text, display = build(edited_text, prompt)
            self._open_chat(title, prompt_text, display, web_search=web_search,
                            system=system, privacy=session)

        TextReviewDialog(text, title, on_confirm, ask_prompt=ask_prompt,
                         config=self.config, web_search_default=web_search_default,
                         privacy=privacy)

    # ---- the five operations ---------------------------------------------

    def ask_anything(self):
        """A conversation with nothing captured: the user types, and can attach pictures
        and text files as they go. Answered like any other - by the answering model,
        privately in Privacy Mode - and searched only if web search is on by default."""
        if not self._require_api_key():
            return
        privacy = self._start_privacy((self._answer_role(),))
        if privacy is False:
            return
        ChatWindow(self, "Ask anything", [],
                   system=prompts.ask_anything_system(self.config.default_language),
                   privacy=privacy)

    def ocr_only(self):
        # Same reasoning as translation: transcription reads the image in front of it,
        # so a search cannot contribute. Still available per request and for follow-ups.
        self._start_capture(
            "OCR", ask_prompt=False,
            build=lambda _p: (prompts.OCR,
                              "_Transcribe all text in the selected region._"),
            web_search_default=False)

    def ocr_translate(self):
        language = self.config.default_language

        def build(extracted=None):
            # Called twice: once before the capture is sent, to label the turn, and
            # again by the preflight with the transcribed text.
            if not extracted:
                return ("", f"_Reading the selection and translating it into "
                            f"{language}…_")
            return (prompts.translate_text(extracted),
                    f"_Translate the text from the selection into {language}._\n\n"
                    + "\n".join(f"> {line}" for line in
                                 extracted.strip().splitlines()[:20]))

        self._start_capture("OCR + Translate", ask_prompt=False, build=build,
                            web_search_default=False, transcribe_first=True,
                            system=prompts.translate_system(language))

    def query_image(self):
        # Answered by the selected text model: with the image itself when it accepts
        # images, otherwise from the vision model's reading of it. See _image_answer.
        self._start_capture("Query image", ask_prompt=True,
                            build=lambda prompt, extracted=None: (prompt, prompt),
                            answer_by_text=True)

    def translate_text(self):
        language = self.config.default_language
        self._start_clipboard(
            "Translate text", ask_prompt=False,
            build=lambda text, _p: (
                prompts.translate_text(text),
                f"_Translate the clipboard text into {language}._\n\n"
                f"> {text[:400]}{'…' if len(text) > 400 else ''}"),
            # Translation works from the text it was given; a search only adds cost and
            # latency. Still available per request, and for follow-ups.
            web_search_default=False,
            system=prompts.translate_system(language))

    def query_text(self):
        language = self.config.default_language
        self._start_clipboard(
            "Query text", ask_prompt=True,
            build=lambda text, prompt: (prompts.query_text(text, prompt),
                                        prompt),
            system=prompts.query_text_system(language))

    def look_up_word(self):
        """A dictionary window, GoldenDict-style, that can answer questions.

        The lookup itself never leaves this machine, so it needs no API key; questions
        asked about it go to the answering model with the entries as context - privately
        in Privacy Mode, like any other conversation. The word comes from the clipboard
        when it holds one, as GoldenDict's clipboard scan does.
        """
        if not self.config.dictionaries_dir:
            self.notify("Choose your dictionaries folder in Settings > Reference first",
                        urgency="normal")
            self.show_settings()
            return
        privacy = self._start_privacy((self._answer_role(),))
        if privacy is False:
            return
        ChatWindow(self, "Look up", [], web_search=False,
                   system=prompts.lookup_system(self.config.default_language),
                   privacy=privacy, lookup=self.dictionaries.look_up,
                   dictionary_list=self.dictionaries.listing,
                   lookup_word=self._word_from_clipboard())

    def _encyclopedia_ready(self):
        if self.encyclopedia.available():
            return True
        self.notify("Import your encyclopedia DVD in Settings > Reference first",
                    urgency="normal")
        self.show_settings()
        return False

    def search_encyclopedia(self, target=None, highlight=None):
        """Read the encyclopedia, and ask about what you tag. One window, kept: a
        citation in an Ask Encyclopedia answer opens its article here, at what was
        quoted, rather than in a window of its own each time.

        The clipboard waits in the search box, selected, rather than being searched for
        at once: Enter takes it, typing replaces it.
        """
        from .ui.encyclopedia_windows import SearchEncyclopediaWindow
        if not self._encyclopedia_ready():
            return
        reader = self._reader
        if reader is not None and not reader._destroyed:
            if target is not None:
                reader.open(target, highlight)
            else:
                reader.present()
            return
        privacy = self._start_privacy((self._answer_role(),))
        if privacy is False:
            return
        copied = None if target else self._word_from_clipboard(
            limit=120, strip=" \"'()[]{}«»“”‘’")
        self._reader = SearchEncyclopediaWindow(self, self.encyclopedia, query=copied,
                                                target=target, highlight=highlight,
                                                privacy=privacy)

    def ask_encyclopedia(self):
        """Ask a question and have it answered from the encyclopedia - planned, searched,
        checked and quoted - rather than from whatever else the model learned. The
        search never leaves this machine; what is sent is the question and the
        passages found, privately in Privacy Mode. A question on the clipboard waits in
        the message box, selected."""
        from .ui.encyclopedia_windows import AskEncyclopediaWindow
        if not self._encyclopedia_ready():
            return
        privacy = self._start_privacy((self._answer_role(),))
        if privacy is False:
            return
        AskEncyclopediaWindow(self, self.encyclopedia, privacy=privacy,
                              question=self._word_from_clipboard(
                                  limit=300, strip=" \"'()[]{}«»“”‘’"))

    @staticmethod
    def _word_from_clipboard(limit=60, strip=" .,;:!?\"'()[]{}«»“”‘’"):
        """The clipboard's text if it looks like a word or phrase to look up, else
        None - a paragraph copied an hour ago is not what the user wants defined.
        `strip` is what is trimmed from its ends: a question keeps its question mark."""
        text, kind = clipboard.get_text()
        if kind != "text" or "\n" in text.strip():
            return None
        word = " ".join(text.split()).strip(strip)
        return word if 0 < len(word) <= limit else None

    # ---- menu items -------------------------------------------------------

    def show_settings(self):
        # run() spins its own main loop, so a hotkey or --action=settings can arrive
        # while the dialog is up. Bring that one forward instead of stacking another.
        if self._settings_dialog is not None:
            self._settings_dialog.present()
            return
        dialog = self._settings_dialog = SettingsDialog(
            None, self.config, self.client, encyclopedia=self.encyclopedia,
            notify=self.notify)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                hotkey_mode = self.config.hotkey_mode
                encyclopedia = self.config.encyclopedia_enabled
                for key, value in dialog.values().items():
                    setattr(self.config, key, value)
                try:
                    self.config.save()
                except OSError as e:
                    self.notify(f"Could not save settings: {e}", urgency="normal")
                    return
                except KeychainError as e:
                    # Everything else is saved; the key is in memory until the app quits.
                    self.notify(f"Settings saved, but the API key could not be kept: {e} "
                                "It is used until the app quits.", urgency="normal")
                else:
                    self.notify("Settings saved")
                self._sync_privacy_item()
                self._sync_encyclopedia_items()
                # Only when it changed: tearing the grabs down and asking the
                # portal for a new session is not free, and most saves are about
                # something else entirely. The encyclopedia's two keys come and go
                # with it.
                if (self.config.hotkey_mode != hotkey_mode
                        or self.config.encyclopedia_enabled != encyclopedia):
                    self.hotkeys.stop()
                    self.hotkeys.start()
                if (self.config.encyclopedia_enabled != encyclopedia
                        and self.hotkeys.mode == "static"):
                    # The desktop holds these bindings, not us.
                    change = "add" if self.config.encyclopedia_enabled else "remove"
                    self.notify(f"Run install-hotkeys again to {change} the "
                                "encyclopedia's keys", urgency="normal")
        finally:
            self._settings_dialog = None
            dialog.destroy()

    def show_diagnostics(self):
        from . import session, theming
        lines = [session.summary()]
        lines += theming.summary()
        lines += [
            f"Capture chain : {' -> '.join(self.capture.describe_chain()) or 'none'}",
            f"Last used     : {self.capture.last_backend or '(none yet)'}",
            f"Tray backend  : {self.tray.kind}",
            f"Hotkeys       : {self.hotkeys.mode} ({self.hotkeys.detail})",
            f"WebKit view   : {gtkcompat.webkit_status()}",
            f"Dictionaries  : {self.config.dictionaries_dir or '(no folder chosen)'}",
            f"Encyclopedia  : {self.encyclopedia.describe()}",
        ]
        from . import bundle
        if bundle.status():
            lines.append(f"Fallbacks     : {bundle.status()}")
        advice = gtkcompat.webkit_advice()
        if advice:
            lines.append(advice)
        lines += privacy_lines(self.config)
        lines += [
            f"Config file   : {self.config.config_file}",
            f"Keyring       : {self.config.keychain_error or self.config.keychain.describe()}",
            f"Error log     : {errors.log_path()}",
        ]
        text = "\n".join(lines)
        dialog = Gtk.Dialog(title="Diagnostics", modal=True)
        dialog.set_default_size(660, -1)
        dialog.add_button("Copy", 1)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        view = Gtk.TextView(editable=False, monospace=True)
        view.get_buffer().set_text(text)
        view.set_margin_start(14)
        view.set_margin_end(14)
        view.set_margin_top(12)
        view.set_margin_bottom(12)
        dialog.get_content_area().pack_start(view, True, True, 0)
        dialog.show_all()
        if dialog.run() == 1:
            clipboard.set_text(text)
            self.notify("Diagnostics copied")
        dialog.destroy()

    def quit(self):
        # Quitting with a chat window still loading is not a WebKit crash; say so, or
        # the next start would wrongly fall back to plain text. See webkitguard.
        webkitguard.note_clean_exit()
        self.hotkeys.stop()
        self.service.release()
        if Notify is not None:
            try:
                Notify.uninit()
            except Exception:
                pass
        Gtk.main_quit()

    def run(self):
        print(f"{APP_NAME} started")
        print(f"  capture chain : {' -> '.join(self.capture.describe_chain()) or 'none'}")
        print(f"  tray backend  : {self.tray.kind}")
        from . import theming
        print(f"  gtk theme     : {theming.theme_line()}")
        if not gtkcompat.has_webkit():
            # Said out loud at startup: it changes what every chat window looks like,
            # and the reason is otherwise a warning nobody was there to read.
            print(f"  chat view     : {gtkcompat.webkit_status()}")
            advice = gtkcompat.webkit_advice()
            if advice:
                print(advice)
        print(f"  hotkeys       : {self.hotkeys.mode} ({self.hotkeys.detail})")
        if self.hotkeys.mode not in ("x11", "portal"):
            print("  for global hotkeys run: tools/install-hotkeys.sh --print")
        Gtk.main()
