"""The encyclopedia windows: Search Encyclopedia, to read; Ask Encyclopedia, to be told.

Search Encyclopedia is a reader. The article fills the top of the window - the DVD's
page, pictures and films - with Back and Forward, the search box and its results in the
header. Questions below it are optional, and only about what the user has tagged with
"Ask about this": tagging sends nothing, it only chooses what goes with the next
question asked.

Ask Encyclopedia answers a question from the encyclopedia, for a user who wants the
answer to rest on it rather than on whatever else a model learned. Each question is
planned, searched, checked and answered (see AskEncyclopediaWindow._gather); every
quotation in the answer is then checked word for word against the passage it cites, and
its citations open the article in Search Encyclopedia at what was quoted.

Both are ChatWindows - the model menu, Privacy Mode, Clear, Copy and Save work as in
any other conversation - built on its hooks for subclasses.
"""

import re
import threading

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import GLib, Gtk, Pango

from .. import prompts
from ..llm import LLMError
from ..reference.encyclopedia import (ARTICLE_CONTEXT_CHARS, AUDIENCE_NAMES,
                                      RESEARCH_CHARS, byline, check_quotes, cited,
                                      read_json, research_context)
from . import markdown_html
from .article_view import ArticlePane, target_of
from .chat import ChatWindow

# What all the tagged articles share of the context with a question: each whole up to
# ARTICLE_CONTEXT_CHARS, fewer and shorter as more are tagged.
TAGGED_CHARS = 80000
TAGGED_MINIMUM = 12000

# Ask Encyclopedia: passages carried from the answer before into a follow-up, and how
# many a question may have in all once the second search has added its own.
CARRIED_PASSAGES = 4
MOST_PASSAGES = 14
# The plan and the check are a few lines of JSON each.
PLAN_TOKENS = 600

_CITATION = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")


def open_reader(app, encyclopedia, target, highlight=None):
    """Show `target` in Search Encyclopedia: the app's window when there is one, else a
    new one. `highlight`: text to find on the page - what an answer quoted."""
    opener = getattr(app, "search_encyclopedia", None)
    if opener is not None:
        opener(target=target, highlight=highlight)
    else:
        SearchEncyclopediaWindow(app, encyclopedia, target=target, highlight=highlight)


class SearchEncyclopediaWindow(ChatWindow):
    def __init__(self, app, encyclopedia, query=None, target=None, highlight=None,
                 privacy=None):
        super().__init__(app, "Search Encyclopedia", [], web_search=False,
                         system=prompts.encyclopedia_system(app.config.default_language),
                         privacy=privacy, encyclopedia=encyclopedia)
        header = self.header

        pages = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pages.get_style_context().add_class("linked")
        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic",
                                                         Gtk.IconSize.BUTTON)
        self.back_button.set_tooltip_text("Back (Alt+Left)")
        self.back_button.connect("clicked", lambda _w: self._go(-1))
        self.forward_button = Gtk.Button.new_from_icon_name("go-next-symbolic",
                                                            Gtk.IconSize.BUTTON)
        self.forward_button.set_tooltip_text("Forward (Alt+Right)")
        self.forward_button.connect("clicked", lambda _w: self._go(1))
        pages.pack_start(self.back_button, False, False, 0)
        pages.pack_start(self.forward_button, False, False, 0)
        header.pack_start(pages)

        self.search_entry = Gtk.SearchEntry(width_chars=22)
        self.search_entry.set_placeholder_text("Search Encyclopedia")
        self.search_entry.set_tooltip_text("Find articles to read")
        self.search_entry.connect("activate", lambda e: self._search(e.get_text()))
        header.pack_start(self.search_entry)

        self.results_button = Gtk.MenuButton(label="Results")
        self.results_button.set_tooltip_text("The other articles found - pick one to read")
        self.results_button.set_sensitive(False)
        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.NONE)
        self.results.connect("row-activated", self._on_result)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_width(520)
        scrolled.set_max_content_height(560)
        scrolled.set_propagate_natural_height(True)
        scrolled.add(self.results)
        popover = Gtk.Popover()
        popover.add(scrolled)
        scrolled.show_all()
        self.results_button.set_popover(popover)
        header.pack_start(self.results_button)

        self.tag_button = Gtk.ToggleButton()
        self._tag_icon = Gtk.Image.new_from_icon_name("non-starred-symbolic",
                                                      Gtk.IconSize.BUTTON)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inner.pack_start(self._tag_icon, False, False, 0)
        inner.pack_start(Gtk.Label(label="Ask about this"), False, False, 0)
        self.tag_button.add(inner)
        self.tag_button.set_tooltip_text(
            "Tag this article to send it with your questions below. Tagging sends "
            "nothing by itself; untag it to stop sending it.")
        self._tag_handler = self.tag_button.connect("toggled", self._on_tag_toggled)
        header.pack_end(self.tag_button)

        self.resize(1040, 920)
        header.show_all()
        self._sync_reader()
        if target is not None:
            self.open(target, highlight)
        else:
            self.pane.message("Search Encyclopedia",
                              "Search for an article above to read it here. Tag it with "
                              "☆ Ask about this to ask about it below.")
            self.search_entry.set_text(query or "")
            self.search_entry.grab_focus()
            self.search_entry.select_region(0, -1)

    # ---- layout -------------------------------------------------------------

    def _layout(self, conversation):
        self._tags = []                     # article ids, in the order tagged
        self._titles = {}
        self._result_rows = []
        self.pane = ArticlePane(self._encyclopedia, on_change=self._on_pane_change)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(6)
        bar.set_margin_bottom(4)
        heading = Gtk.Label(label="Asking about:", xalign=0)
        heading.get_style_context().add_class("dim-label")
        bar.pack_start(heading, False, False, 0)
        self.chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                 max_children_per_line=20, column_spacing=4,
                                 row_spacing=4, homogeneous=False)
        bar.pack_start(self.chips, True, True, 0)
        self.untagged_hint = Gtk.Label(
            label="nothing yet - tag an article with ☆ Ask about this, and only it goes "
                  "with your questions", xalign=0)
        self.untagged_hint.get_style_context().add_class("dim-label")
        bar.pack_start(self.untagged_hint, True, True, 0)
        self.answers_toggle = Gtk.ToggleButton(label="Hide answers")
        self.answers_toggle.set_relief(Gtk.ReliefStyle.NONE)
        self.answers_toggle.set_tooltip_text("Hide the questions and answers, for "
                                             "reading; the question box stays")
        self.answers_toggle.connect("toggled", self._on_answers_toggled)
        bar.pack_end(self.answers_toggle, False, False, 0)

        lower = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lower.pack_start(bar, False, False, 0)
        lower.pack_start(Gtk.Separator(), False, False, 0)
        lower.pack_start(conversation, True, True, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.pack1(self.pane, True, False)
        paned.pack2(lower, True, False)
        paned.set_position(560)
        self.paned = paned
        return paned

    def _on_answers_toggled(self, button):
        hidden = button.get_active()
        self.view.set_visible(not hidden)
        button.set_label("Show answers" if hidden else "Hide answers")
        # The reader takes the room the answers had.
        self.paned.set_position(self.paned.get_allocated_height() - 150 if hidden
                                else int(self.paned.get_allocated_height() * 0.62))

    # ---- reading ------------------------------------------------------------

    def open(self, target, highlight=None):
        """Read `target` here - an article, or a picture's or film's page."""
        self.pane.open(tuple(target), highlight)
        self.present()

    def _go(self, step):
        self.pane.go(step)

    def _on_pane_change(self, kind, ident, title, label):
        if kind == "article":
            self._titles[ident] = title
        self.header.set_title(title or "Search Encyclopedia")
        self._sync_reader()

    def _sync_reader(self):
        if not hasattr(self, "tag_button"):
            return                          # still being built
        self.back_button.set_sensitive(self.pane.can_go(-1))
        self.forward_button.set_sensitive(self.pane.can_go(1))
        current = self.pane.current
        is_article = bool(current) and current[0] == "article"
        self.tag_button.set_sensitive(is_article)
        tagged = is_article and current[1] in self._tags
        self.tag_button.handler_block(self._tag_handler)
        self.tag_button.set_active(tagged)
        self.tag_button.handler_unblock(self._tag_handler)
        self._tag_icon.set_from_icon_name(
            "starred-symbolic" if tagged else "non-starred-symbolic", Gtk.IconSize.BUTTON)

    def _on_window_key(self, widget, event):
        from gi.repository import Gdk
        if event.state & Gdk.ModifierType.MOD1_MASK and \
                event.keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            self._go(-1 if event.keyval == Gdk.KEY_Left else 1)
            return True
        return super()._on_window_key(widget, event)

    # ---- searching ----------------------------------------------------------

    def _search(self, query):
        """Search for `query` off the main loop; the best match is read at once, and
        the rest wait under Results."""
        query = " ".join((query or "").split())
        if not query or self._looking:
            return
        self._looking = True
        self.status.set_text(f"Searching the encyclopedia for “{query}”…")

        def worker():
            try:
                found = self._encyclopedia.search(query, limit=25)
            except Exception as e:
                GLib.idle_add(self._on_searched, query, None, f"{e}")
            else:
                GLib.idle_add(self._on_searched, query, found, None)

        threading.Thread(target=worker, daemon=True).start()

    def _on_searched(self, query, results, failure):
        if self._destroyed:
            return False
        self._looking = False
        if failure:
            self.status.set_text(f"Search failed: {failure}")
            return False
        for row in self.results.get_children():
            row.destroy()
        self._result_rows = []
        for result in results:
            self.results.add(self._result_row(result))
        self.results.show_all()
        self.results_button.set_label(f"Results ({len(results)})" if results
                                      else "Results")
        self.results_button.set_sensitive(bool(results))
        if not results:
            self.status.set_text(f"Nothing in the encyclopedia matches “{query}”")
            return False
        # Best first: an article titled exactly what was searched for, the longest of
        # several - Rome, Italy rather than Rome, Georgia - or else the best match.
        self.open(("article", results[0]["id"]))
        self.status.set_text(f"{len(results)} articles found - the others are under "
                             f"Results")
        return False

    def _result_row(self, result):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for margin in ("start", "end"):
            getattr(box, f"set_margin_{margin}")(10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        title = Gtk.Label(xalign=0)
        title.set_markup(
            f"<b>{GLib.markup_escape_text(result['title'] or '(untitled)')}</b>  "
            f"<small>{GLib.markup_escape_text(markdown_html.EDITION_NAMES.get(result['library'], ''))}</small>")
        summary = Gtk.Label(label=result["summary"], xalign=0, wrap=True,
                            max_width_chars=70, lines=2)
        summary.set_ellipsize(Pango.EllipsizeMode.END)
        summary.get_style_context().add_class("dim-label")
        box.pack_start(title, False, False, 0)
        box.pack_start(summary, False, False, 0)
        row = Gtk.ListBoxRow()
        row.add(box)
        row.article_id = result["id"]
        return row

    def _on_result(self, _listbox, row):
        self.results_button.get_popover().popdown()
        self.open(("article", row.article_id))

    # ---- tagging ------------------------------------------------------------

    def _on_tag_toggled(self, button):
        current = self.pane.current
        if not current or current[0] != "article":
            return
        ident = current[1]
        title = self._titles.get(ident, "this article")
        if button.get_active() and ident not in self._tags:
            self._tags.append(ident)
            self.status.set_text(f"“{title}” goes with your questions from now on")
        elif not button.get_active() and ident in self._tags:
            self._tags.remove(ident)
            self.status.set_text(f"“{title}” no longer goes with your questions")
        self._sync_reader()
        self._sync_chips()

    def untag(self, ident):
        if ident in self._tags:
            self._tags.remove(ident)
            self.status.set_text(f"“{self._titles.get(ident, 'That article')}” no "
                                 f"longer goes with your questions")
        self._sync_reader()
        self._sync_chips()

    def _sync_chips(self):
        for child in self.chips.get_children():
            child.destroy()
        for ident in self._tags:
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            chip.get_style_context().add_class("tag-chip")
            name = Gtk.Button(label=f"★ {self._titles.get(ident, '(untitled)')}")
            name.set_relief(Gtk.ReliefStyle.NONE)
            name.set_tooltip_text("Read it")
            name.connect("clicked", lambda _w, i=ident: self.open(("article", i)))
            remove = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                                   Gtk.IconSize.MENU)
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.set_tooltip_text("Untag: stop sending it with your questions")
            remove.connect("clicked", lambda _w, i=ident: self.untag(i))
            chip.pack_start(name, False, False, 0)
            chip.pack_start(remove, False, False, 0)
            self.chips.add(chip)
        self.chips.show_all()
        self.chips.set_visible(bool(self._tags))
        self.untagged_hint.set_visible(not self._tags)

    # ---- asking -------------------------------------------------------------

    def _can_send(self, text):
        if self._tags:
            return True
        self.status.set_text("Tag an article first - ☆ Ask about this - so the question "
                             "has something from the encyclopedia to go with it")
        return False

    def _extra_context(self, question):
        if not self._tags:
            return ""
        budget = min(ARTICLE_CONTEXT_CHARS,
                     max(TAGGED_MINIMUM, TAGGED_CHARS // len(self._tags)))
        texts = []
        for ident in self._tags:
            try:
                texts.append(self._encyclopedia.context(ident, question, budget=budget))
            except Exception as e:          # an import replaced under us, say
                print(f"[encyclopedia] article {ident} unavailable: {e!r}")
        return prompts.ARTICLE_CONTEXT + "\n\n---\n\n".join(texts) if texts else ""

    def _follow_encyclopedia(self, uri):
        target = target_of(uri)
        if target is not None:
            self.open(target)


class AskEncyclopediaWindow(ChatWindow):
    def __init__(self, app, encyclopedia, question=None, privacy=None):
        super().__init__(app, "Ask Encyclopedia", [], web_search=False,
                         system=prompts.ask_encyclopedia_system(app.config.default_language),
                         privacy=privacy, encyclopedia=encyclopedia)
        header = self.header
        # Answers come from the encyclopedia; a web search would bring in everything else.
        self.search_toggle.set_active(False)
        self.search_toggle.set_no_show_all(True)
        self.search_toggle.hide()

        self.strict_switch = Gtk.Switch(
            active=bool(getattr(app.config, "encyclopedia_strict", True)),
            valign=Gtk.Align.CENTER)
        strict = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        strict.pack_start(Gtk.Label(label="Strict"), False, False, 0)
        strict.pack_start(self.strict_switch, False, False, 0)
        strict.set_tooltip_text(
            "On: the answer is the encyclopedia's alone - where it has nothing, the "
            "answer says so rather than filling in. Off: what the encyclopedia does not "
            "cover may be answered too, under a heading of its own, Not from the "
            "encyclopedia.")
        header.pack_end(strict)

        self.audience = Gtk.ComboBoxText()
        self.audience.set_wrap_width(1)
        for key, name in AUDIENCE_NAMES.items():
            self.audience.append(key, name)
        self.audience.set_active_id(getattr(app.config, "encyclopedia_audience", "adult")
                                    or "adult")
        if self.audience.get_active_id() is None:
            self.audience.set_active_id("adult")
        self.audience.set_tooltip_text(
            "Who the answer is for. General reader prefers the main Encyclopedia; "
            "Student, the Student Encyclopedia; Young reader, the Elementary one - each "
            "falls back on the others, and the answer is written for that reader.")
        header.pack_end(self.audience)
        header.show_all()
        if question:
            self.message_box_text(question)

    # ---- asking -------------------------------------------------------------

    def _prepare_answer(self):
        asked = [turn.get("content") or "" for turn in self.conversation
                 if turn["role"] == "user" and not turn.get("error")]
        if not asked:
            return None
        carried = next((turn["passages"] for turn in reversed(self.conversation)
                        if turn["role"] == "assistant" and turn.get("passages")), [])
        return {"question": asked[-1], "before": asked[-2] if len(asked) > 1 else None,
                "carried": carried[:CARRIED_PASSAGES],
                "strict": self.strict_switch.get_active(),
                "audience": self.audience.get_active_id() or "adult",
                "model": self.current_model}

    def _ask_json(self, prepared, system, content, cancel, private_kwargs):
        """A short call for JSON - the plan, or the check - as {} if it fails: a search
        without a plan is worse, not impossible."""
        try:
            reply = self.app.client.complete(
                prepared["model"], [{"role": "user", "content": content}],
                cancel=cancel, system=system, max_tokens=PLAN_TOKENS, **private_kwargs)
        except LLMError as e:
            print(f"[ask encyclopedia] {system[:30]}…: {e}")
            return {}
        finally:
            if self.privacy is not None:
                self.privacy.pop_receipt()
        return read_json(reply)

    def _gather(self, prepared, cancel, status, private_kwargs):
        """Plan, search, check, and if need be search again - then the passages, and how
        the answer is to use them.

        The plan: a model turns the question into what an answer would contain, for a
        search engine that matches words - names, terms, likely article titles. The
        check: shown what was found, the model says whether it answers the question,
        and if not, what to search for instead; one more search follows. If that adds
        little, the encyclopedia has little on the question, and a Strict answer will say
        less than it might - which the sources say, with what to do about it.
        """
        question, before = prepared["question"], prepared["before"]
        audience, strict = prepared["audience"], prepared["strict"]
        encyclopedia = self._encyclopedia

        status("Planning the search…")
        plan = self._ask_json(prepared, prompts.ENCYCLOPEDIA_PLAN,
                              prompts.encyclopedia_plan(question, before), cancel,
                              private_kwargs)
        if cancel.is_set():
            return "", None
        status("Searching the encyclopedia…")
        passages = encyclopedia.gather(question, also=before,
                                       queries=_strings(plan.get("queries"), 5),
                                       titles=_strings(plan.get("titles"), 5),
                                       audience=audience)

        status("Checking what was found…")
        check = self._ask_json(prepared, prompts.ENCYCLOPEDIA_CHECK,
                               prompts.encyclopedia_check(question, passages), cancel,
                               private_kwargs)
        if cancel.is_set():
            return "", None
        limited = False
        if check.get("enough") is False or not passages:
            queries = _strings(check.get("queries"), 4)
            titles = _strings(check.get("titles"), 4)
            more = []
            if queries or titles:
                status("Searching the encyclopedia again…")
                more = encyclopedia.gather(
                    question, also=before, queries=queries, titles=titles,
                    audience=audience, limit=6, budget=RESEARCH_CHARS // 2,
                    exclude={(p["article"], p["section"]) for p in passages})
                passages += more
            limited = len(more) < 2

        found = {(p["article"], p["section"]) for p in passages}
        for passage in prepared["carried"]:
            if (passage["article"], passage["section"]) not in found:
                passages.append(passage)
        passages = passages[:MOST_PASSAGES]

        if passages:
            addition = (prompts.ask_encyclopedia_rules(strict, audience)
                        + research_context(passages))
            status(f"Answering from {len(passages)} encyclopedia passages…")
        else:
            addition = (prompts.ASK_ENCYCLOPEDIA_NOTHING if strict
                        else prompts.ASK_ENCYCLOPEDIA_NOTHING_LABELLED)
            status("Nothing found in the encyclopedia…")
        return addition, {"passages": passages, "limited": limited and strict}

    # ---- the answer ---------------------------------------------------------

    def _finish_answer(self, turn, payload):
        """Check every quotation against its passage, mark the ones not found, and make
        the citations links to what they cite."""
        passages = (payload or {}).get("passages")
        if not passages:
            return
        index = len(self.conversation)      # where the answer is about to go
        text = turn["content"]
        checks = check_quotes(text, passages)
        for check in sorted((c for c in checks if not c["ok"]), key=lambda c: -c["end"]):
            text = text[:check["end"]] + " ⚠" + text[check["end"]:]

        def link(match):
            numbers = [n for n in cited(match.group(0)) if 1 <= n <= len(passages)]
            if not numbers:
                return match.group(0)
            return "".join(f"[\\[{n}\\]](enc://cite/{index}/{n})" for n in numbers)

        turn.update(passages=passages, checks=checks, export=text,
                    display=_CITATION.sub(link, text), links=("enc://cite/",))

    def _after_answer(self, turn, payload):
        passages = turn.get("passages")
        if not passages:
            return
        index = self.conversation.index(turn)
        numbers = [n for n in cited(turn["content"]) if 1 <= n <= len(passages)]
        checks = turn.get("checks") or []
        failed = sum(1 for c in checks if not c["ok"])
        limited = (payload or {}).get("limited")
        self._append_turn({
            "role": "sources", "content": "", "heading": "Encyclopedia sources",
            # The plain-text view's, with links it follows; and Copy's and Save's.
            "display": sources_markdown(passages, numbers, index, checks, limited),
            "export": sources_markdown(passages, numbers, None, checks, limited),
            "html": markdown_html.sources_html(passages, numbers, index, checks,
                                               limited)},
            scroll=None)
        if failed:
            self.status.set_text(f"⚠ {failed} quotation{'s' if failed > 1 else ''} not "
                                 f"found word for word - see the sources")

    def _follow_encyclopedia(self, uri):
        """A citation opens its article in Search Encyclopedia at what was quoted from it;
        a picture or film from the sources, on its own page there."""
        found = re.match(r"enc://cite/(\d+)/(\d+)$", uri)
        if found:
            index, number = int(found.group(1)), int(found.group(2))
            if index >= len(self.conversation):
                return
            turn = self.conversation[index]
            passages = turn.get("passages") or []
            if not 1 <= number <= len(passages):
                return
            passage = passages[number - 1]
            quoted = next((c["quote"] for c in turn.get("checks") or []
                           if c["ok"] and c["found"] == number), None)
            open_reader(self.app, self._encyclopedia, ("article", passage["article"]),
                        highlight=quoted or passage["heading"])
            return
        target = target_of(uri)
        if target is not None:
            open_reader(self.app, self._encyclopedia, target)


def _md(text):
    """`text` safe in Markdown: its brackets, emphasis and backticks taken literally."""
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", " ".join((text or "").split()))


def sources_markdown(passages, cited_numbers, turn, checks=(), limited=False):
    """An answer's sources as Markdown - what sources_html shows, for the plain-text
    view and for Copy and Save. With `turn`, the answer's place in the conversation, the
    sources and their pictures and films are links the window follows; without, plain.
    """
    def link(label, address):
        return f"[{_md(label)}]({address})" if turn is not None else _md(label)

    numbered = list(enumerate(passages, 1))
    used = [(n, p) for n, p in numbered if n in cited_numbers] or numbered
    rest = [(n, p) for n, p in numbered if (n, p) not in used]

    def item(n, p):
        edition = markdown_html.EDITION_NAMES.get(p["library"], "")
        by = byline(p.get("contributors") or [], most=markdown_html.BYLINE_NAMES)
        return (f"{n}. {link(p['title'], f'enc://cite/{turn}/{n}')} › {_md(p['heading'])}"
                f" - {edition}{f' · {_md(by)}' if by else ''}")

    def credited(label, credit):
        return f"{label} ({credit})" if credit else label

    lines = []
    if limited:
        lines += ["_The encyclopedia has little on this question, even after a second "
                  "search. Strict keeps the answer to what it has; switch Strict off for a "
                  "fuller one, with what is not from the encyclopedia set apart._", ""]
    lines += [item(n, p) for n, p in used]
    if rest:
        lines += ["", "Also found:"] + [item(n, p) for n, p in rest]
    if checks:
        good = sum(1 for c in checks if c["ok"])
        lines += ["", f"Quotations checked against the passages: {good} of {len(checks)} "
                      f"found word for word."]
        for check in checks:
            if check["ok"]:
                continue
            where = (f"found in [{check['found']}] instead" if check["found"]
                     else "not found in any passage")
            cited_as = (" cited as " + ", ".join(f"[{n}]" for n in check["cited"])
                        if check["cited"] else "")
            lines.append(f"⚠ “{_md(check['quote'])}”{cited_as}: {where}.")
    media, seen = [], set()
    for _n, passage in used:
        credits = passage.get("credits") or {}
        for assembly, _picture, caption in passage["pictures"]:
            if ("picture", assembly) not in seen:
                seen.add(("picture", assembly))
                credit = credits.get(assembly) or credits.get(str(assembly)) or ""
                media.append(credited(link(caption or "Picture",
                                           f"enc://assembly/{int(assembly)}"), _md(credit)))
        for film in passage["films"]:
            if ("film", film["assembly"]) not in seen:
                seen.add(("film", film["assembly"]))
                media.append(credited(link("▶ " + (film["caption"] or film["title"] or "Film"),
                                           f"enc://film/{int(film['assembly'])}"),
                                      _md(film.get("credit") or "")))
    if media:
        lines += ["", "Pictures and films: " + " · ".join(media[:12])]
    lines += ["", "_From the 2015 encyclopedia DVD._"]
    return "\n".join(lines)


def _strings(value, limit):
    """A model's list of strings, as one - whatever it sent instead."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(" ".join(item.split())[:120])
    return out[:limit]
