"""URLs in answers are links, a conversation can be saved as Markdown, and the
settings drop-downs open with every option showing."""

import json
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

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gtk

from assistant import collector, webkitguard

# Windows are made and closed here while worker threads run - Settings' look for the
# Private Mode client among them; a cycle collected on one of those would finalize a
# GTK or WebKit object there, and WebKit aborts. On the main loop instead, as the app.
collector.install()
from assistant.config import Config
from assistant.ui import chat, markdown_text
from assistant.ui.markdown_html import render_markdown


class BareUrlsAreLinks(unittest.TestCase):
    def test_a_bare_url_becomes_a_link(self):
        html = render_markdown("Docs are at https://a.test/guide?x=1&y=2 for now")
        self.assertIn('<a href="https://a.test/guide?x=1&amp;y=2">', html)

    def test_the_full_stop_after_a_url_is_not_part_of_it(self):
        html = render_markdown("See https://a.test/page.")
        self.assertIn('href="https://a.test/page"', html)
        self.assertIn("</a>.", html)

    def test_brackets_around_a_url_stay_outside_it(self):
        self.assertIn('href="https://a.test/p"', render_markdown("(https://a.test/p)"))

    def test_brackets_that_belong_to_the_url_stay_in_it(self):
        html = render_markdown("https://en.wikipedia.org/wiki/Foo_(bar)")
        self.assertIn('href="https://en.wikipedia.org/wiki/Foo_(bar)"', html)

    def test_a_link_already_written_as_one_is_not_linked_again(self):
        html = render_markdown("[https://a.test](https://a.test)")
        self.assertEqual(html.count("<a "), 1)

    def test_urls_in_code_stay_code(self):
        self.assertNotIn("<a ", render_markdown("`curl https://a.test`"))
        self.assertNotIn("<a ", render_markdown("```\ncurl https://a.test\n```"))

    def test_a_url_cannot_break_out_of_its_attribute(self):
        html = render_markdown('https://a.test/"onmouseover="x')
        self.assertIn('<a href="https://a.test/">https://a.test/</a>"onmouseover', html)


class TheFallbackViewLinksToo(unittest.TestCase):
    def _links(self, text):
        buffer = Gtk.TextBuffer()
        markdown_text.render_body(buffer, text)
        names = []
        buffer.get_tag_table().foreach(lambda tag: names.append(tag.get_property("name")))
        shown = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        return shown, sorted(n[len(markdown_text.LINK_PREFIX):] for n in names
                             if n and n.startswith(markdown_text.LINK_PREFIX))

    def test_bare_and_written_links_both_carry_their_url(self):
        shown, urls = self._links("See https://a.test/x_y. or [the docs](https://d.test)")
        self.assertEqual(urls, ["https://a.test/x_y", "https://d.test"])
        self.assertIn("the docs", shown)
        self.assertNotIn("](", shown)

    def test_a_url_in_code_is_not_a_link(self):
        _shown, urls = self._links("`https://a.test`")
        self.assertEqual(urls, [])

    def test_the_apps_own_links_and_escaped_brackets(self):
        # What Ask Encyclopedia's citations and a dictionary's cross-references look like.
        shown, urls = self._links("As [\\[2\\]](enc://cite/1/2) says, see [word](lookup:w). "
                                  "A \\[bracket\\] and `\\[code\\]`.")
        self.assertEqual(urls, ["enc://cite/1/2", "lookup:w"])
        self.assertIn("As [2] says, see word.", shown)
        self.assertIn("A [bracket] and \\[code\\].", shown)

    def test_a_click_on_the_apps_own_link_goes_to_the_window_not_the_browser(self):
        view, followed = Gtk.TextView(), []
        markdown_text.configure(view, lambda url: followed.append(url)
                                or url.startswith("enc://"))
        click = types.SimpleNamespace(button=1, x=0, y=0, time=0)
        with mock.patch.object(markdown_text, "link_at") as link_at, \
                mock.patch.object(markdown_text.webpolicy.hostenv, "open_uri") as browser:
            link_at.return_value = "enc://cite/1/2"
            markdown_text._on_link_click(view, click)
            browser.assert_not_called()
            link_at.return_value = "https://a.test"
            markdown_text._on_link_click(view, click)
            browser.assert_called_once()
            link_at.return_value = "lookup:w"           # not followed: not the browser's
            markdown_text._on_link_click(view, click)
            browser.assert_called_once()
        self.assertEqual(followed, ["enc://cite/1/2", "https://a.test", "lookup:w"])


class SavingAsMarkdown(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for patch in (mock.patch.object(webkitguard, "STATE_FILE",
                                        os.path.join(self.dir, "webkit-crash")),
                      mock.patch.object(webkitguard, "LOG_DIR", self.dir),
                      mock.patch.object(chat, "WebKit2", None)):
            patch.start()
            self.addCleanup(patch.stop)

    def _window(self):
        config = Config(os.path.join(self.dir, "c.json"))
        app = type("App", (), {"config": config, "client": None,
                               "notify": lambda self, *a, **k: None})()
        window = chat.ChatWindow(app, "Ask: what/is this?", [
            {"role": "user", "content": "prompt", "display": "What is this?"},
            {"role": "assistant", "content": "A **test**.", "model": "org/model-x"}])
        self.addCleanup(window.destroy)
        return window

    def _save_to(self, window, path):
        chooser = mock.Mock()
        chooser.run.return_value = Gtk.ResponseType.ACCEPT if path else \
            Gtk.ResponseType.CANCEL
        chooser.get_filename.return_value = path
        with mock.patch.object(chat.Gtk.FileChooserNative, "new", return_value=chooser):
            window._on_save_markdown(None)
        return chooser

    def test_the_file_holds_what_copy_would(self):
        window = self._window()
        path = os.path.join(self.dir, "out.md")
        chooser = self._save_to(window, path)
        with open(path, encoding="utf-8") as saved:
            self.assertEqual(saved.read(), window._markdown() + "\n")
        self.assertIn("## Assistant (org/model-x)", window._markdown())
        self.assertIn("What is this?", window._markdown())
        chooser.set_do_overwrite_confirmation.assert_called_with(True)

    def test_cancelling_writes_nothing(self):
        window = self._window()
        self._save_to(window, None)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_failed_write_is_said_rather_than_raised(self):
        window = self._window()
        self._save_to(window, os.path.join(self.dir, "missing", "out.md"))
        self.assertIn("Not saved", window.status.get_text())

    HALF = json.loads('"broken \\ud83d emoji"')        # as a reply's JSON can spell it

    def _replacing(self, window, before="an earlier save\n"):
        path = os.path.join(self.dir, "out.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(before)
        return path

    def test_a_reply_with_half_a_character_is_saved(self):
        window = self._window()
        window.conversation[-1]["content"] = self.HALF
        path = self._replacing(window)
        self._save_to(window, path)
        with open(path, encoding="utf-8") as saved:
            self.assertIn("broken � emoji", saved.read())
        self.assertIn("Saved to", window.status.get_text())

    def test_a_write_that_fails_leaves_the_file_it_was_to_replace(self):
        window = self._window()
        path = self._replacing(window)
        with mock.patch.object(chat.os, "replace",
                               side_effect=OSError(28, "No space left on device")):
            self._save_to(window, path)
        with open(path, encoding="utf-8") as saved:
            self.assertEqual(saved.read(), "an earlier save\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["out.md"], "a part left behind")
        self.assertIn("No space left", window.status.get_text())

    def test_a_link_is_written_through_and_a_files_mode_kept(self):
        window = self._window()
        path = self._replacing(window)
        os.chmod(path, 0o600)
        link = os.path.join(self.dir, "link.md")
        os.symlink(path, link)
        self._save_to(window, link)
        self.assertTrue(os.path.islink(link))
        with open(path, encoding="utf-8") as saved:
            self.assertEqual(saved.read(), window._markdown() + "\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_a_name_as_long_as_a_name_can_be_is_saved(self):
        window = self._window()
        path = os.path.join(self.dir, "n" * 252 + ".md")
        self._save_to(window, path)
        self.assertTrue(os.path.exists(path), window.status.get_text())

    def test_a_new_file_is_made_as_the_umask_says(self):
        window = self._window()
        path = os.path.join(self.dir, "new.md")
        mask = os.umask(0o022)
        try:
            self._save_to(window, path)
        finally:
            os.umask(mask)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_a_long_name_in_another_script_is_saved(self):
        window = self._window()
        path = os.path.join(self.dir, "語" * 83 + ".md")       # 252 bytes
        self._save_to(window, path)
        self.assertTrue(os.path.exists(path), window.status.get_text())
        self.assertEqual(os.listdir(self.dir), [os.path.basename(path)])

    def test_a_private_file_is_replaced_by_one_as_private_while_it_is_written(self):
        window = self._window()
        path = self._replacing(window)
        os.chmod(path, 0o600)
        modes, fdopen = [], os.fdopen

        def opened(fd, *args, **kwargs):
            modes.append(os.fstat(fd).st_mode & 0o777)
            return fdopen(fd, *args, **kwargs)

        mask = os.umask(0)
        try:
            with mock.patch.object(chat.os, "fdopen", opened):
                self._save_to(window, path)
        finally:
            os.umask(mask)
        self.assertEqual(modes, [0o600])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    @unittest.skipIf(os.geteuid() == 0, "root may write to anything")
    def test_a_file_that_may_not_be_written_to_is_refused_not_replaced(self):
        window = self._window()
        path = self._replacing(window)
        os.chmod(path, 0o444)
        self._save_to(window, path)
        self.assertIn("Not saved: Permission denied", window.status.get_text())
        with open(path, encoding="utf-8") as saved:
            self.assertEqual(saved.read(), "an earlier save\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o444)

    def test_a_file_with_other_names_stays_one_file(self):
        window = self._window()
        path = self._replacing(window)
        other = os.path.join(self.dir, "other name.md")
        os.link(path, other)
        self._save_to(window, path)
        self.assertTrue(os.path.samefile(path, other))
        with open(other, encoding="utf-8") as saved:
            self.assertEqual(saved.read(), window._markdown() + "\n")

    @unittest.skipIf(os.geteuid() == 0, "root may write to anything")
    def test_a_file_in_a_folder_that_takes_no_new_files_is_written(self):
        window = self._window()
        folder = os.path.join(self.dir, "fixed")
        os.mkdir(folder)
        path = os.path.join(folder, "out.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("an earlier save\n")
        os.chmod(folder, 0o555)
        self.addCleanup(os.chmod, folder, 0o755)
        self._save_to(window, path)
        with open(path, encoding="utf-8") as saved:
            self.assertEqual(saved.read(), window._markdown() + "\n")
        self.assertEqual(os.listdir(folder), ["out.md"])

    def test_half_a_character_streamed_is_drawn_until_the_other_half_comes(self):
        window = self._window()
        window._cancel = types.SimpleNamespace(is_set=lambda: False)
        window._streaming_text = ""
        half = json.loads('"broken \\ud83d"')           # an emoji split across chunks
        window._on_stream_progress(half)
        self.assertEqual(window._streaming_text, "broken �")
        window._on_stream_done(half + json.loads('"\\ude00 emoji"'))
        self.assertEqual(window.conversation[-1]["content"], "broken \U0001f600 emoji")
        window._show_error(self.HALF)
        self.assertEqual(window.conversation[-1]["content"],
                         "**Error:** broken � emoji")

    def test_the_suggested_name_is_a_valid_file_name(self):
        name = chat.markdown_filename("Ask: what/is this?")
        self.assertTrue(name.startswith("Ask what is this "), name)
        self.assertTrue(name.endswith(".md"))
        self.assertNotIn("/", name)
        self.assertTrue(chat.markdown_filename("").startswith("Conversation "))


class SettingsDropDownsOpenBelowThemselves(unittest.TestCase):
    def test_every_drop_down_has_a_wrap_width(self):
        """Without one, GTK lines the menu up on the chosen option and shrinks it to
        fit, which near an edge of the dialog left one option and scroll arrows."""
        from assistant.ui.dialogs import SettingsDialog

        config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        dialog = SettingsDialog(None, config, type("C", (), {"config": config}))
        try:
            combos = [combo for combo, _label in dialog.search_combos.values()]
            combos += [dialog.hotkey_mode, dialog.reasoning_effort]
            for combo in combos:
                self.assertGreater(combo.get_wrap_width(), 0)
        finally:
            dialog.destroy()


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class TheViewStaysOnItsPage(unittest.TestCase):
    """Only the app's own loads may change what a WebKit view shows: a link goes to the
    browser, and Open Link, Reload, Back, a dropped URL or a form go nowhere."""

    def setUp(self):
        from assistant.ui import webpolicy
        self.webkit = chat.WebKit2
        self.view = self.webkit.WebView()
        self.opened, self.dropped, self.said, self.links = [], [], [], []
        self.guard = webpolicy.Guard(
            self.webkit, self.view, on_link=lambda uri, kind: self.links.append(uri)
            or uri.startswith("lookup:"), on_drop=self.dropped.append,
            say=self.said.append)
        self.guard.expect("about:blank")
        patch = mock.patch.object(webpolicy.hostenv, "open_uri", self.opened.append)
        patch.start()
        self.addCleanup(patch.stop)

    def decide(self, uri, kind, decision_type=None):
        ignored = []
        action = types.SimpleNamespace(
            get_navigation_type=lambda: kind,
            get_request=lambda: types.SimpleNamespace(get_uri=lambda: uri))
        decision = types.SimpleNamespace(get_navigation_action=lambda: action,
                                         ignore=lambda: ignored.append(True))
        handled = self.guard._on_decide_policy(
            None, decision, decision_type or self.webkit.PolicyDecisionType.NAVIGATION_ACTION)
        return handled, bool(ignored)

    def test_the_apps_own_page_loads(self):
        self.assertEqual(self.decide("about:blank", self.webkit.NavigationType.OTHER),
                         (False, False))

    def test_a_clicked_link_goes_to_the_browser_and_the_view_stays(self):
        self.assertEqual(self.decide("https://a.test/", self.webkit.NavigationType.LINK_CLICKED),
                         (True, True))
        self.assertEqual(self.opened, ["https://a.test/"])

    def test_the_windows_own_links_are_its_own(self):
        self.decide("lookup:zib", self.webkit.NavigationType.LINK_CLICKED)
        self.assertEqual(self.links, ["lookup:zib"])
        self.assertEqual(self.opened, [])

    def test_open_link_from_a_menu_or_a_dropped_url_goes_nowhere(self):
        for uri, kind in (("https://evil.test/", self.webkit.NavigationType.OTHER),
                          ("about:blank", self.webkit.NavigationType.BACK_FORWARD),
                          ("https://a.test/", self.webkit.NavigationType.FORM_SUBMITTED)):
            self.assertEqual(self.decide(uri, kind), (True, True), (uri, kind))
        self.assertEqual(self.opened, [])

    def test_a_link_for_a_new_window_opens_in_the_browser(self):
        self.decide("https://a.test/", self.webkit.NavigationType.LINK_CLICKED,
                    self.webkit.PolicyDecisionType.NEW_WINDOW_ACTION)
        self.assertEqual(self.opened, ["https://a.test/"])

    def test_a_dropped_file_is_offered_not_opened(self):
        self.decide("file:///tmp/notes.txt", self.webkit.NavigationType.OTHER)
        self.assertEqual(self.dropped, ["file:///tmp/notes.txt"])

    def test_a_link_nothing_can_open_is_said(self):
        from gi.repository import GLib
        from assistant.ui import webpolicy

        def refuse(uri):
            raise GLib.Error("no handler for mailto")

        with mock.patch.object(webpolicy.hostenv, "open_uri", refuse):
            self.decide("mailto:a@b.test", self.webkit.NavigationType.LINK_CLICKED)
        self.assertTrue(self.said and "mailto" in self.said[0])

    def test_the_context_menu_keeps_copying_and_drops_navigating(self):
        actions = self.webkit.ContextMenuAction
        menu = self.webkit.ContextMenu.new()
        for action in (actions.OPEN_LINK, actions.OPEN_LINK_IN_NEW_WINDOW,
                       actions.DOWNLOAD_LINK_TO_DISK, actions.COPY_LINK_TO_CLIPBOARD,
                       actions.GO_BACK, actions.RELOAD, actions.COPY, actions.SELECT_ALL):
            menu.append(self.webkit.ContextMenuItem.new_from_stock_action(action))
        hit = types.SimpleNamespace(context_is_link=lambda: True,
                                    get_link_uri=lambda: "https://a.test/")
        self.assertFalse(self.guard._on_context_menu(self.view, menu, None, hit))
        kept = [item.get_stock_action() for item in menu.get_items()]
        self.assertEqual(kept[0], actions.CUSTOM)          # Open Link in Browser
        self.assertEqual(set(kept[1:]), {actions.COPY_LINK_TO_CLIPBOARD, actions.COPY,
                                         actions.SELECT_ALL})

    def test_a_menu_with_nothing_left_is_not_shown(self):
        menu = self.webkit.ContextMenu.new()
        menu.append(self.webkit.ContextMenuItem.new_from_stock_action(
            self.webkit.ContextMenuAction.RELOAD))
        hit = types.SimpleNamespace(context_is_link=lambda: False)
        self.assertTrue(self.guard._on_context_menu(self.view, menu, None, hit))


class MenuLabelsSayWhatIsSelected(unittest.TestCase):
    """WebKit reads a context menu item's label as a mnemonic, as GTK's own menus built
    from a model do; a text view's popup menu does not."""

    CONFIG = types.SimpleNamespace(dictionaries_dir="/dictionaries",
                                   encyclopedia_enabled=True)

    def test_an_underscore_in_a_webkit_menu_is_shown_not_taken_for_a_key(self):
        from assistant.ui import lookups
        made = []
        webkit = types.SimpleNamespace(ContextMenuItem=types.SimpleNamespace(
            new_from_gaction=lambda _action, label, _target: made.append(label) or label,
            new_separator=lambda: "separator"))
        menu = mock.Mock()
        menu.get_items.return_value = []
        lookups.add_to_webkit_menu(webkit, menu, self.CONFIG, "snake_case_word",
                                   lambda kind, what: None)
        self.assertEqual(made, ["Look up “snake__case__word” in the dictionary",
                                "Search the encyclopedia for “snake__case__word”"])

    def test_a_text_views_menu_takes_the_label_as_written(self):
        from assistant.ui import lookups
        menu = Gtk.Menu()
        lookups.add_to_gtk_menu(menu, self.CONFIG, "snake_case_word",
                                lambda kind, what: None)
        labels = [item.get_label() for item in menu.get_children()
                  if not isinstance(item, Gtk.SeparatorMenuItem)]
        self.assertEqual(labels, ["Look up “snake_case_word” in the dictionary",
                                  "Search the encyclopedia for “snake_case_word”"])

    @unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
    def test_the_item_is_webkits_own_and_runs_what_it_was_given(self):
        from assistant.ui import webpolicy
        ran = []
        item = webpolicy.menu_item(chat.WebKit2, "a_b", lambda: ran.append(True))
        item.get_gaction().activate(None)
        self.assertEqual(ran, [True])


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class AnArticleFromAFolderWithASpace(unittest.TestCase):
    def test_its_own_load_is_let_through_however_the_uri_is_spelled(self):
        from assistant.ui import webpolicy
        webkit = chat.WebKit2
        guard = webpolicy.Guard(webkit, webkit.WebView())
        guard.expect("file:///home/u/My%20DVD%20%5B2015%5D/data/")
        for reported in ("file:///home/u/My%20DVD%20%5B2015%5D/data/",
                         "file:///home/u/My DVD [2015]/data/"):
            action = types.SimpleNamespace(
                get_navigation_type=lambda: webkit.NavigationType.OTHER,
                get_request=lambda r=reported: types.SimpleNamespace(get_uri=lambda: r))
            decision = types.SimpleNamespace(get_navigation_action=lambda a=action: a,
                                             ignore=lambda: self.fail("refused its own"))
            self.assertFalse(guard._on_decide_policy(
                None, decision, webkit.PolicyDecisionType.NAVIGATION_ACTION))

    def test_the_pane_names_its_base_as_a_uri(self):
        import pathlib
        base = pathlib.Path("/home/u/My DVD [2015]/data").as_uri() + "/"
        self.assertEqual(base, "file:///home/u/My%20DVD%20%5B2015%5D/data/")
