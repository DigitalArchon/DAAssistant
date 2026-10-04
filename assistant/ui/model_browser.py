"""Searchable model picker.

The provider lists ~600 models, so this is a filtered list rather than a combo box:
type to narrow, tick to restrict to vision-capable or free models, and the pricing and
context length are shown inline so a choice can be made without leaving the app.
"""

import threading

from .. import gtkcompat
from gi.repository import GLib, Gtk, Pango

from .. import models as catalogue, privacy, sealed

COL_ID, COL_PRICE, COL_CONTEXT, COL_CAPS, COL_HAYSTACK, COL_DESC = range(6)


class ModelBrowser(Gtk.Dialog):
    """Pick a model id. Returns the chosen id via .selected_id after run()."""

    def __init__(self, parent, config, title="Choose a model",
                 vision_only=False, current=None, private_only=False):
        """private_only offers TEE/ and private/ models alone, and cannot be unticked:
        it is how the Privacy Mode fields are browsed, and anything else there would be
        refused when used."""
        super().__init__(title=title, transient_for=parent, modal=True)
        self.config = config
        self.selected_id = None
        self.models = []
        self._current = current

        self.set_default_size(920, 620)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.choose_button = self.add_button("Use this model", Gtk.ResponseType.OK)
        self.choose_button.get_style_context().add_class("suggested-action")
        self.choose_button.set_sensitive(False)

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search by name, provider or description…")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda _w: self._refilter())
        controls.pack_start(self.search, True, True, 0)

        self.vision_check = Gtk.CheckButton.new_with_label("Vision only")
        self.vision_check.set_active(vision_only)
        self.vision_check.connect("toggled", lambda _w: self._refilter())
        controls.pack_start(self.vision_check, False, False, 0)

        self.free_check = Gtk.CheckButton.new_with_label("Free only")
        self.free_check.connect("toggled", lambda _w: self._refilter())
        controls.pack_start(self.free_check, False, False, 0)

        self.private_check = Gtk.CheckButton.new_with_label("TEE only")
        self.private_check.set_active(private_only)
        self.private_check.set_sensitive(not private_only)
        self.private_check.set_tooltip_text(
            "Models that run in an attested enclave: TEE/ (attested, replies signed) "
            "and private/ (end-to-end encrypted as well)."
            + ("\nPrivacy Mode can only use these." if private_only else ""))
        self.private_check.connect("toggled", lambda _w: self._refilter())
        controls.pack_start(self.private_check, False, False, 0)

        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Re-fetch the model list from the provider")
        refresh.connect("clicked", lambda _w: self._load(force=True))
        controls.pack_start(refresh, False, False, 0)
        box.pack_start(controls, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, str, str)
        self.filtered = self.store.filter_new()
        self.filtered.set_visible_func(self._visible)

        self.tree = Gtk.TreeView(model=self.filtered, headers_visible=True)
        self.tree.set_enable_search(False)       # our own search entry drives filtering
        self.tree.connect("row-activated", self._on_row_activated)
        self.tree.get_selection().connect("changed", self._on_selection_changed)

        for title_text, column, width, expand in (
            ("Model", COL_ID, 380, True),
            ("Price / Mtok", COL_PRICE, 180, False),
            ("Context", COL_CONTEXT, 100, False),
            ("Capabilities", COL_CAPS, 240, False),
        ):
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
            col = Gtk.TreeViewColumn(title_text, renderer, text=column)
            col.set_resizable(True)
            col.set_min_width(width)
            col.set_expand(expand)
            self.tree.append_column(col)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.get_style_context().add_class("preview-frame")
        scrolled.add(self.tree)
        box.pack_start(scrolled, True, True, 0)

        self.detail = Gtk.Label(label="", xalign=0, wrap=True, max_width_chars=110)
        self.detail.set_lines(3)
        self.detail.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail.get_style_context().add_class("dim-label")
        box.pack_start(self.detail, False, False, 0)

        self.status = Gtk.Label(label="Loading models…", xalign=0)
        self.status.get_style_context().add_class("capture-meta")
        box.pack_start(self.status, False, False, 0)

        self.show_all()
        self._load()

    # ---- loading ----------------------------------------------------------

    def _load(self, force=False):
        self.status.set_text("Fetching models…" if force else "Loading models…")
        self.search.set_sensitive(False)

        def worker():
            try:
                models, cached = catalogue.fetch(self.config, force=force)
            except Exception as e:
                GLib.idle_add(self._on_load_failed, str(e))
            else:
                GLib.idle_add(self._on_loaded, models, cached)

        threading.Thread(target=worker, daemon=True).start()

    def _on_load_failed(self, message):
        self.status.set_text(f"Could not load models — {message}")
        self.search.set_sensitive(True)
        return False

    def _on_loaded(self, models, cached):
        self.models = models
        self.store.clear()
        for model in models:
            caps = []
            kind = privacy.kind_of(model["id"])
            if kind == privacy.E2EE:
                caps.append("🔐 E2EE")
            elif kind == privacy.TEE:
                caps.append("🔒 TEE")
            if model["vision"]:
                caps.append("vision")
            if model["reasoning"]:
                caps.append("reasoning")
            if model["tool_calling"]:
                caps.append("tools")
            haystack = " ".join([model["id"], model["name"], model["owned_by"],
                                 model["category"], model["description"],
                                 " ".join(model["aliases"])]).lower()
            self.store.append([
                model["id"],
                catalogue.format_price(model),
                catalogue.format_context(model),
                ", ".join(caps),
                haystack,
                model["description"],
            ])

        self.search.set_sensitive(True)
        self.search.grab_focus()
        self._refilter()
        source = "cached" if cached else "fetched"
        self.status.set_text(f"{len(models)} models ({source}). "
                             f"Double-click a row to choose it.")
        self._select_current()
        return False

    def _select_current(self):
        """Highlight the model already configured, if it is still offered."""
        if not self._current:
            return
        resolved = catalogue.resolve(self.models, self._current)
        target = resolved["id"] if resolved else self._current
        for row in self.filtered:
            if row[COL_ID] == target:
                self.tree.get_selection().select_iter(row.iter)
                self.tree.scroll_to_cell(row.path, None, True, 0.4, 0.0)
                return

    # ---- filtering --------------------------------------------------------

    def _visible(self, model, iterator, _data):
        if self.private_check.get_active() and not privacy.kind_of(model[iterator][COL_ID]):
            return False
        if self.vision_check.get_active() or self.free_check.get_active():
            model_id = model[iterator][COL_ID]
            entry = next((m for m in self.models if m["id"] == model_id), None)
            if entry is None:
                return False
            if self.vision_check.get_active() and not entry["vision"]:
                return False
            if self.free_check.get_active() and (entry.get("prompt_price") or 0) != 0:
                return False

        terms = self.search.get_text().lower().split()
        if not terms:
            return True
        haystack = model[iterator][COL_HAYSTACK] or ""
        return all(term in haystack for term in terms)

    def _refilter(self):
        self.filtered.refilter()
        if self.models:
            shown = len(self.filtered)
            self.status.set_text(f"{shown} of {len(self.models)} models shown")
        self._on_selection_changed(self.tree.get_selection())

    # ---- selection --------------------------------------------------------

    def _current_id(self):
        model, iterator = self.tree.get_selection().get_selected()
        return model[iterator][COL_ID] if iterator else None

    def _on_selection_changed(self, _selection):
        model_id = self._current_id()
        self.choose_button.set_sensitive(model_id is not None)
        if model_id is None:
            self.detail.set_text("")
            return
        entry = next((m for m in self.models if m["id"] == model_id), None)
        if entry is None:
            self.detail.set_text("")
            return
        bits = [entry["description"] or "No description provided."]
        label, note = privacy.describe(model_id)
        if label and note not in bits[0]:
            bits.insert(0, note)
        if privacy.is_private_mode(model_id) and not sealed.available()[0]:
            bits.insert(0, sealed.INSTALL_HINT)
        if entry["aliases"]:
            bits.append(f"Also known as: {', '.join(entry['aliases'][:4])}")
        self.detail.set_text("  ".join(bits))

    def _on_row_activated(self, _tree, _path, _column):
        self.selected_id = self._current_id()
        if self.selected_id:
            self.response(Gtk.ResponseType.OK)

    def run(self):
        response = super().run()
        if response == Gtk.ResponseType.OK:
            self.selected_id = self.selected_id or self._current_id()
        else:
            self.selected_id = None
        return response
