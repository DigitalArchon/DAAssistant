"""The pages the app shows run no script of their own, and copy no chrome.

The chat transcript and an article run the app's user scripts and its run_javascript,
and nothing in their markup: a <script> or an onclick= that got past the sanitiser and
the page's policy is dropped as the page is parsed. Everything the views do is the
app's scripts', and goes on working. And a selection dragged across a turn copies what
was said, not its label, its badges or its Copy buttons.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from test_chat_transcript import (WindowCase, clipboard_text, run_until,  # noqa: E402
                                  settle)

from gi.repository import Gdk, Gtk  # noqa: E402

from assistant.ui import article_view, chat  # noqa: E402

TURNS = [{"role": "user", "content": "q"},
         {"role": "assistant", "model": "vendor/some-model",
          "content": "First line of the answer.\n\n```\ncode here\n```\n\nLast line."}]


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class NoScriptInThePage(WindowCase):
    def test_the_views_take_no_script_from_markup(self):
        window = self.window(TURNS)
        self.assertFalse(window.webview.get_settings().get_enable_javascript_markup())
        pane = article_view.ArticlePane(encyclopedia=None)
        self.addCleanup(pane.destroy)
        self.assertFalse(pane.webview.get_settings().get_enable_javascript_markup())

    def test_markup_put_into_the_page_runs_nothing(self):
        # As a redraw puts a turn's HTML in (insertAdjacentHTML), past the sanitiser.
        window = self.window(TURNS)
        self.page(window, "(function () { document.body.insertAdjacentHTML('beforeend',"
                          " '<img id=\"probe\" src=\"data:,x\" onerror=\"document.title="
                          "\\'ran\\'\"><script>document.title = \"ran too\";</script>'); "
                          "return 1; })()")
        settle(300)
        self.assertEqual(self.page(window, "[document.title, document.getElementById("
                                           "'probe').hasAttribute('onerror'), "
                                           "document.querySelectorAll('script').length]"),
                         ["", False, 0])

    def test_the_apps_own_scripts_still_run(self):
        window = self.window(TURNS)
        settle()
        self.assertEqual(self.page(window, "[document.querySelectorAll('pre > button')"
                                           ".length, document.querySelectorAll("
                                           "'.role .copy-answer').length, typeof "
                                           "window.followEnd, typeof "
                                           "window.resumeFollowing]"),
                         [1, 1, "function", "function"])
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("untouched", -1)
        self.page(window, "(function () { document.querySelector('pre > button')"
                          ".click(); return 1; })()")
        self.assertTrue(run_until(lambda: clipboard_text() != "untouched"))
        self.assertEqual(clipboard_text().rstrip("\n"), "code here")
        # And a redraw, as streaming does, through run_javascript.
        window.conversation[-1]["content"] = "Redrawn."
        window._render(full=True)
        self.assertTrue(run_until(lambda: "Redrawn." in (self.page(
            window, "document.body.innerText") or "")))


@unittest.skipUnless(chat.WebKit2 is not None, "needs WebKitGTK")
class CopyingTakesNoChrome(WindowCase):
    def test_a_selection_across_the_turns_copies_only_what_was_said(self):
        window = self.window(TURNS)
        settle()
        selected = self.page(window, "(function () { var r = document.createRange();"
                                     " r.selectNodeContents(document.body);"
                                     " var s = getSelection(); s.removeAllRanges();"
                                     " s.addRange(r); return s.toString(); })()")
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("untouched", -1)
        window.webview.execute_editing_command(chat.WebKit2.EDITING_COMMAND_COPY)
        self.assertTrue(run_until(lambda: clipboard_text() != "untouched"))
        for copied in (selected, clipboard_text()):
            self.assertIn("First line of the answer.", copied)
            self.assertIn("code here", copied)
            self.assertIn("Last line.", copied)
            for chrome in ("You", "YOU", "Assistant", "ASSISTANT", "some-model", "Copy"):
                self.assertNotIn(chrome, copied)


if __name__ == "__main__":
    unittest.main()
