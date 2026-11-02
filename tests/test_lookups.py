"""Select a word anywhere the app shows text, right-click, and look it up - in the
dictionaries or the encyclopedia - in a window of its own (ui/lookups.py)."""

import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import GLib, Gtk  # noqa: E402

from assistant import webkitguard  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.ui import chat, lookups  # noqa: E402


class Configured(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.dictionaries_dir = self.dir
        self.config.encyclopedia_enabled = True


class WhatIsOffered(Configured):
    def test_both_where_both_are_there(self):
        kinds = [kind for _label, kind, _what in lookups.offers(self.config, " apple, ")]
        self.assertEqual(kinds, ["dictionary", "encyclopedia"])

    def test_only_what_there_is_to_look_in(self):
        self.config.encyclopedia_enabled = False
        self.assertEqual([k for _l, k, _w in lookups.offers(self.config, "apple")],
                         ["dictionary"])
        self.config.dictionaries_dir = ""
        self.assertEqual(lookups.offers(self.config, "apple"), [])

    def test_the_selection_is_tidied_to_a_word_or_phrase(self):
        [(label, _kind, what), _] = lookups.offers(self.config, "  “ice\n cream”. ")
        self.assertEqual(what, "ice cream")
        self.assertIn("“ice cream”", label)

    def test_a_paragraph_is_a_search_but_not_a_word(self):
        sentence = "the whole of the opening paragraph of a long article, and more besides"
        self.assertEqual([k for _l, k, _w in lookups.offers(self.config, sentence)],
                         ["encyclopedia"])
        self.assertEqual(lookups.offers(self.config, "word " * 40), [])
        self.assertEqual(lookups.offers(self.config, " ,. "), [])


class InAGtkTextView(Configured):
    def test_the_popup_offers_the_lookups_for_the_selection(self):
        seen = []
        view = Gtk.TextView()
        view.get_buffer().set_text("an apple a day")
        start = view.get_buffer().get_iter_at_offset(3)
        end = view.get_buffer().get_iter_at_offset(8)
        view.get_buffer().select_range(start, end)
        menu = Gtk.Menu()
        lookups.add_to_gtk_menu(menu, self.config, lookups.selected_text(view),
                                lambda kind, what: seen.append((kind, what)))
        labels = [item.get_label() for item in menu.get_children()
                  if isinstance(item, Gtk.MenuItem) and item.get_label()]
        self.assertEqual(len(labels), 2)
        menu.get_children()[1].activate()
        self.assertEqual(seen, [("dictionary", "apple")])

    def test_nothing_selected_adds_nothing(self):
        view = Gtk.TextView()
        view.get_buffer().set_text("an apple a day")
        menu = Gtk.Menu()
        lookups.add_to_gtk_menu(menu, self.config, lookups.selected_text(view),
                                lambda *_a: None)
        self.assertEqual(menu.get_children(), [])


class StubApp:
    def __init__(self, config):
        self.config = config
        self.looked_up = []

    def notify(self, *_args, **_kwargs):
        pass

    def look_up_selection(self, kind, what):
        self.looked_up.append((kind, what))


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class InTheTranscript(Configured):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(webkitguard, "STATE_FILE",
                                        os.path.join(self.dir, "webkit-crash")),
                      mock.patch.object(webkitguard, "LOG_DIR", self.dir)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_until(self, predicate, timeout=15):
        state = {"done": False}

        def poll():
            if predicate():
                state["done"] = True
                Gtk.main_quit()
                return False
            return True

        def expire():
            Gtk.main_quit()
            return False

        GLib.timeout_add(50, poll)
        source = GLib.timeout_add_seconds(timeout, expire)
        Gtk.main()
        if state["done"]:
            GLib.source_remove(source)
        return state["done"]

    def test_a_selection_in_an_answer_is_offered_and_opens_a_window(self):
        app = StubApp(self.config)
        window = chat.ChatWindow(app, "Ask anything", [
            {"role": "user", "content": "q", "display": "What is a quince?"},
            {"role": "assistant", "content": "A quince is a fruit.", "model": "m"}])
        self.addCleanup(window.destroy)
        window.show_all()
        self.assertTrue(self.run_until(lambda: window._loaded))
        window._js("var range = document.createRange();"
                   "var node = [...document.querySelectorAll('.bubble')].pop();"
                   "range.selectNodeContents(node.querySelector('p') || node);"
                   "window.getSelection().removeAllRanges();"
                   "window.getSelection().addRange(range);")
        self.assertTrue(self.run_until(lambda: "quince" in window._selection))

        webkit = chat.WebKit2
        menu = webkit.ContextMenu.new()
        menu.append(webkit.ContextMenuItem.new_from_stock_action(webkit.ContextMenuAction.COPY))
        hit = types.SimpleNamespace(context_is_link=lambda: False,
                                    context_is_selection=lambda: True)
        window.guard._on_context_menu(window.webview, menu, None, hit)
        ours = [item for item in menu.get_items()
                if item.get_stock_action() == webkit.ContextMenuAction.CUSTOM]
        self.assertEqual(len(ours), 2)
        ours[0].get_gaction().activate(None)
        self.assertEqual(app.looked_up, [("dictionary", "A quince is a fruit")])

    def test_right_clicking_where_nothing_is_selected_offers_no_lookup(self):
        window = chat.ChatWindow(StubApp(self.config), "Ask anything", [])
        self.addCleanup(window.destroy)
        window._selection = "stale"
        webkit = chat.WebKit2
        menu = webkit.ContextMenu.new()
        menu.append(webkit.ContextMenuItem.new_from_stock_action(webkit.ContextMenuAction.COPY))
        hit = types.SimpleNamespace(context_is_link=lambda: False,
                                    context_is_selection=lambda: False)
        window.guard._on_context_menu(window.webview, menu, None, hit)
        self.assertEqual(len(menu.get_items()), 1)


class TheAppOpensAWindowOfItsOwn(Configured):
    def app(self):
        from assistant.app import Assistant

        class Stub:
            look_up_selection = Assistant.look_up_selection
            look_up_word = Assistant.look_up_word
            search_encyclopedia = Assistant.search_encyclopedia
            _start_privacy = Assistant._start_privacy

            def __init__(stub):
                stub.config = self.config
                stub._reader = None
                stub.dictionaries = types.SimpleNamespace(look_up=None, listing=list)
                stub.encyclopedia = object()

            def _answer_role(stub):
                return "text"

            def _encyclopedia_ready(stub):
                return True

            def _word_from_clipboard(stub, **_kwargs):
                return "clipboard"

            def notify(stub, *_args, **_kwargs):
                pass
        return Stub()

    def test_a_word_opens_a_dictionary_window_on_it(self):
        from assistant import app as app_module
        opened = []
        with mock.patch.object(app_module, "ChatWindow",
                               lambda *a, **k: opened.append(k)):
            self.app().look_up_selection("dictionary", "quince")
        self.assertEqual(opened[0]["lookup_word"], "quince")

    def test_a_phrase_opens_a_new_reader_searching_for_it(self):
        from assistant.ui import encyclopedia_windows
        made = []

        class Reader:
            def __init__(self, *_args, **kwargs):
                self._destroyed = False
                made.append(kwargs)

        app = self.app()
        app._reader = Reader()
        with mock.patch.object(encyclopedia_windows, "SearchEncyclopediaWindow", Reader):
            app.look_up_selection("encyclopedia", "Thirty Years' War")
        self.assertEqual(made[-1]["query"], "Thirty Years' War")
        self.assertTrue(made[-1]["search_now"])
        self.assertIs(type(app._reader), Reader)
        self.assertEqual(len(made), 2, "a new window, not the kept one")


if __name__ == "__main__":
    unittest.main()
