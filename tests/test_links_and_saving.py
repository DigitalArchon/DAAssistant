"""URLs in answers are links, a conversation can be saved as Markdown, and the
settings drop-downs open with every option showing."""

import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gtk

from assistant import webkitguard
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
                mock.patch.object(Gtk, "show_uri_on_window") as browser:
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
