"""A link that is not secure is marked where it is shown, and asked about before it opens.

Plain http to anywhere but this machine, its own network or a Tor onion service opens a
page anyone on the way can read and change. Such a link - in an answer, a search's
sources, a dictionary's entry or an article, in the WebKit view or the plain-text one -
carries the app's "⚠ not secure" after it, put there from the address it really has, and
opens only after "Open anyway". The dialog's answer is faked here except where the
dialog itself is the test.
"""

import html as html_module
import json
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from assistant import collector, linkwarning, netguard  # noqa: E402

collector.install()
from assistant.reference import encyclopedia_html  # noqa: E402
from assistant.ui import markdown_html, markdown_text, webpolicy  # noqa: E402

if gtkcompat.has_webkit():
    from gi.repository import WebKit2  # noqa: E402
else:  # pragma: no cover - only on systems without the typelib
    WebKit2 = None

MARKER = linkwarning.marker()


class WhatIsInsecure(unittest.TestCase):
    def test_plain_http_to_the_internet_is(self):
        for url in ("http://example.com/", "http://93.184.216.34/x",
                    "HTTP://EXAMPLE.COM", "  http://example.com/  ", "http:example.com",
                    "http://[2001:db8::1]/", "http://localhost.example.com/",
                    "http://127.0.0.1.example.com/"):
            self.assertTrue(netguard.insecure_link(url), url)

    def test_an_address_that_cannot_be_judged_is(self):
        # Python and a browser read these differently: to one the host is local, to
        # the other it is not.
        for url in ("http://evil.example\\@localhost/",
                    "http://127.0.0.1%2eevil.example/", "http://local\thost/",
                    "ht\ttp://evil.example/", "http://"):
            self.assertTrue(netguard.insecure_link(url), url)

    def test_this_machine_its_network_and_onions_are_not(self):
        for url in ("http://localhost:8080/", "http://127.0.0.1/", "http://127.9.8.7:1/",
                    "http://[::1]:11434/", "http://192.168.1.201:11434/api",
                    "http://10.0.0.2/", "http://172.20.1.1/", "http://[fd00::5]/",
                    "http://[fe80::1%25eth0]/", "http://printer.local/",
                    "http://box.localhost/", "http://LOCALHOST./",
                    "http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad"
                    ".onion/"):
            self.assertFalse(netguard.insecure_link(url), url)

    def test_https_mailto_and_other_schemes_are_not(self):
        for url in ("https://example.com/", "HTTPS://example.com", "mailto:a@example.com",
                    "lookup:word", "enc://cite/1/2", "hide-dictionary:%2Fd", "ftp://x/",
                    "", None):
            self.assertFalse(netguard.insecure_link(url), url)


class MarkedInTheAnswer(unittest.TestCase):
    def test_an_insecure_link_has_the_mark_after_it(self):
        html = markdown_html.render_markdown("Read [the page](http://example.com/p).")
        self.assertIn(f">the page</a>{MARKER}.", html)

    def test_a_bare_url_is_marked_too(self):
        html = markdown_html.render_markdown("Sources: http://example.com/p and more")
        self.assertIn(f">http://example.com/p</a>{MARKER} and more", html)

    def test_secure_local_and_onion_links_are_not(self):
        html = markdown_html.render_markdown(
            "[a](https://example.com/) http://localhost:11434/ http://192.168.1.2/ "
            "[o](http://abcdefghijklmnop.onion/) [m](mailto:a@example.com)")
        self.assertEqual(html.count("<a "), 5)
        self.assertNotIn(linkwarning.CLASS, html)

    def test_text_that_looks_secure_does_not_hide_it(self):
        html = markdown_html.render_markdown(
            "[https://bank.example/login](http://evil.example/login)")
        self.assertIn(f">https://bank.example/login</a>{MARKER}", html)

    def test_the_links_title_is_the_warning_not_the_pages(self):
        html = markdown_html.sanitise('<a href="http://evil.example/" title="Verified '
                                      'secure ✔">https://bank.example</a>')
        self.assertNotIn("Verified", html)
        self.assertEqual(html.count(f'title="{html_module.escape(linkwarning.TIP)}"'), 2)

    def test_the_href_a_browser_follows_is_the_one_judged(self):
        # Of two, a browser reads the first; the second is not kept.
        first_bad = markdown_html.sanitise('<a href="http://evil.example/" '
                                           'href="https://ok.example/">x</a>')
        self.assertIn(MARKER, first_bad)
        self.assertNotIn("ok.example", first_bad)
        first_good = markdown_html.sanitise('<a href="https://ok.example/" '
                                            'href="http://evil.example/">x</a>')
        self.assertNotIn(MARKER, first_good)
        self.assertNotIn("evil.example", first_good)

    def test_the_mark_stays_beside_its_link_when_links_are_nested(self):
        # A browser ends the first link where the second begins.
        html = markdown_html.sanitise('<a href="http://evil.example/">x<b>y<a href='
                                      '"https://ok.example/">z</a>w</b></a>')
        self.assertIn(f"x<b>y</b></a>{MARKER}<a href", html)
        self.assertEqual(html.count(MARKER), 1)

    def test_an_answer_cannot_make_a_mark_of_its_own(self):
        html = markdown_html.sanitise('<a href="https://bank.example/">ok</a>'
                                      '<span class="insecure-link" title="x"></span>')
        self.assertNotIn(linkwarning.CLASS, html)

    def test_a_link_in_a_redrawn_turn_is_marked(self):
        html = markdown_html.turn_html("assistant", "see http://example.com/", index=1)
        self.assertIn(MARKER, html)


class MarkedInADictionaryAndAnArticle(unittest.TestCase):
    LOOKUP = {"word": "w", "found": "w", "dictionaries": 1, "results": [
        {"dictionary": "D", "path": "/d", "entries": [{"html":
            '<b>w</b> see <a href="http://example.com/w" title="safe" '
            'style="font-size:0px">https://example.com/w</a>, '
            '<a href="https://example.com/v">v</a> and <a href="lookup:x">x</a>'}]}]}

    def test_a_dictionarys_insecure_link_is_marked_and_only_it(self):
        html = markdown_html.lookup_html(self.LOOKUP)
        self.assertEqual(html.count(MARKER), 1)
        self.assertIn(f"https://example.com/w</a>{MARKER},", html)
        self.assertNotIn('title="safe"', html)
        self.assertIn('style="font-size:0px"', html)     # the rest of it as it was

    def test_an_entrys_link_left_open_is_closed_before_its_mark(self):
        marked = linkwarning.mark('<a href="http://a.example/">a<a href='
                                  '"http://b.example/">b</a> rest <a href='
                                  '"http://c.example/">c')
        self.assertEqual(marked.count(MARKER), 3)
        self.assertIn(f">a</a>{MARKER}<a ", marked)
        self.assertTrue(marked.endswith(f">c</a>{MARKER}"))

    def test_html_without_links_is_left_alone(self):
        text = "<p>No <b>links</b> here &amp; none</p>"
        self.assertIs(linkwarning.mark(text), text)

    def test_an_articles_web_link_is_marked(self):
        html, _films = encyclopedia_html.render(
            '<p>See <a href="http://www.example.com/">the site</a> and '
            '<a href="https://www.example.org/">this</a>.</p>', lambda name: None)
        self.assertIn(f">the site</a>{MARKER} and", html)
        self.assertEqual(html.count(MARKER), 1)

    def test_both_pages_style_the_mark_for_either_theme(self):
        for css in (markdown_html.page_css(False), markdown_html.page_css(True),
                    encyclopedia_html.page_css(False), encyclopedia_html.page_css(True)):
            self.assertIn(f".{linkwarning.CLASS}::after", css)
            self.assertIn("user-select: none", css)
        self.assertNotEqual(linkwarning.css(False), linkwarning.css(True))


class MarkedInThePlainTextView(unittest.TestCase):
    def render(self, text, view=None):
        buffer = view.get_buffer() if view is not None else Gtk.TextBuffer()
        markdown_text.render_body(buffer, text, widget=view)
        anchors, where = [], buffer.get_start_iter()
        while True:
            if where.get_child_anchor() is not None:
                anchors.append(where.get_offset())
            if not where.forward_char():
                break
        return buffer, anchors

    def test_an_insecure_link_has_a_mark_after_it_that_is_not_text(self):
        buffer, anchors = self.render("See [https://bank.example](http://evil.example/) "
                                      "and http://example.com/x now")
        self.assertEqual(len(anchors), 2)
        shown = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.assertEqual(shown, "See https://bank.example and http://example.com/x now")
        self.assertEqual(anchors[0], len("See https://bank.example"))

    def test_secure_and_local_links_have_none(self):
        _buffer, anchors = self.render("[a](https://example.com/) http://localhost:8080/ "
                                       "[c](enc://cite/1/2)")
        self.assertEqual(anchors, [])

    def test_in_a_view_the_mark_is_a_label_with_the_warning(self):
        view = Gtk.TextView()
        self.render("see http://example.com/", view)
        labels = [child for child in view.get_children() if isinstance(child, Gtk.Label)]
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].get_text(), linkwarning.WORDS)
        self.assertEqual(labels[0].get_tooltip_text(), linkwarning.TIP)
        self.assertTrue(labels[0].get_style_context().has_class(linkwarning.CLASS))
        # Redrawn, as a streaming answer is: the old marks go with the old text.
        self.render("now http://example.com/ and http://example.org/", view)
        self.assertEqual(len(view.get_children()), 2)

    def test_copying_the_text_leaves_the_mark_out(self):
        view = Gtk.TextView()
        window = Gtk.Window()
        window.add(view)
        self.addCleanup(window.destroy)
        buffer, _anchors = self.render("go [here](http://example.com/) now", view)
        buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        buffer.copy_clipboard(clipboard)
        self.assertEqual(clipboard.wait_for_text(), "go here now")


class Answering:
    """confirm_insecure, faked: records what it was asked, and answers `yes` - None
    for not at all, as a dialog left open has not."""

    def __init__(self, yes):
        self.yes, self.asked = yes, []

    def __call__(self, parent, uri, then):
        self.asked.append(uri)
        if self.yes:
            then()


class OpeningAsksFirst(unittest.TestCase):
    def setUp(self):
        self.opened = []
        patch = mock.patch.object(webpolicy.hostenv, "open_uri", self.opened.append)
        patch.start()
        self.addCleanup(patch.stop)

    def answer(self, yes):
        answering = Answering(yes)
        patch = mock.patch.object(webpolicy, "confirm_insecure", answering)
        patch.start()
        self.addCleanup(patch.stop)
        return answering

    def test_an_insecure_link_opens_only_after_open_anyway(self):
        for yes in (False, None):
            answering = self.answer(yes)
            webpolicy.open_link("http://example.com/")
            self.assertEqual(answering.asked, ["http://example.com/"])
            self.assertEqual(self.opened, [])
        self.answer(True)
        webpolicy.open_link("http://example.com/")
        self.assertEqual(self.opened, ["http://example.com/"])

    def test_secure_local_and_onion_links_open_at_once(self):
        answering = self.answer(False)
        links = ["https://example.com/", "http://localhost:8080/", "http://[fd00::1]/",
                 "http://abcdefghijklmnop.onion/", "mailto:a@example.com"]
        for uri in links:
            webpolicy.open_link(uri)
        self.assertEqual(answering.asked, [])
        self.assertEqual(self.opened, links)

    def test_a_click_in_the_plain_text_view_asks(self):
        answering = self.answer(False)
        view = Gtk.TextView()
        markdown_text.configure(view, lambda url: False)
        click = types.SimpleNamespace(button=1, x=0, y=0, time=0)
        with mock.patch.object(markdown_text, "link_at",
                               return_value="http://example.com/"):
            markdown_text._on_link_click(view, click)
        self.assertEqual(answering.asked, ["http://example.com/"])
        self.assertEqual(self.opened, [])


@unittest.skipUnless(WebKit2 is not None, "needs WebKitGTK")
class TheWebKitViewAsksFirst(unittest.TestCase):
    def setUp(self):
        self.opened = []
        patch = mock.patch.object(webpolicy.hostenv, "open_uri", self.opened.append)
        patch.start()
        self.addCleanup(patch.stop)
        self.answering = Answering(False)
        patch = mock.patch.object(webpolicy, "confirm_insecure", self.answering)
        patch.start()
        self.addCleanup(patch.stop)
        self.view = WebKit2.WebView()
        self.guard = webpolicy.Guard(WebKit2, self.view)

    def click(self, uri, decision_type=None):
        action = types.SimpleNamespace(
            get_navigation_type=lambda: WebKit2.NavigationType.LINK_CLICKED,
            get_request=lambda: types.SimpleNamespace(get_uri=lambda: uri))
        decision = types.SimpleNamespace(get_navigation_action=lambda: action,
                                         ignore=lambda: None)
        self.guard._on_decide_policy(
            None, decision, decision_type or WebKit2.PolicyDecisionType.NAVIGATION_ACTION)

    def test_a_click_or_enter_on_an_insecure_link_asks(self):
        # Enter on a link is a click, to WebKit; so is a middle click, for a new window.
        self.click("http://example.com/")
        self.click("http://example.com/new", WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION)
        self.assertEqual(self.answering.asked, ["http://example.com/",
                                                "http://example.com/new"])
        self.assertEqual(self.opened, [])

    def test_a_secure_link_is_not_asked_about(self):
        self.click("https://example.com/")
        self.assertEqual(self.answering.asked, [])
        self.assertEqual(self.opened, ["https://example.com/"])

    def test_open_link_in_browser_asks(self):
        menu = WebKit2.ContextMenu.new()
        hit = types.SimpleNamespace(context_is_link=lambda: True,
                                    get_link_uri=lambda: "http://example.com/")
        self.guard._on_context_menu(self.view, menu, None, hit)
        menu.get_items()[0].get_gaction().activate(None)
        self.assertEqual(self.answering.asked, ["http://example.com/"])
        self.assertEqual(self.opened, [])


class TheDialog(unittest.TestCase):
    def setUp(self):
        self.window = Gtk.Window()
        self.addCleanup(self.window.destroy)
        self.ran = []

    def ask(self):
        dialog = webpolicy.confirm_insecure(self.window, "http://example.com/a?b=c",
                                            lambda: self.ran.append(True))
        self.addCleanup(lambda: dialog.get_realized() and dialog.destroy())
        return dialog

    @staticmethod
    def labels(widget):
        found = []
        if isinstance(widget, Gtk.Label):
            found.append(widget.get_text())
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found += TheDialog.labels(child)
        return found

    def test_it_names_the_address_and_says_why(self):
        dialog = self.ask()
        self.assertTrue(dialog.get_modal())
        self.assertIs(dialog.get_transient_for(), self.window)
        self.assertEqual(dialog.get_property("text"), "Open a link that isn't secure?")
        shown = self.labels(dialog.get_message_area())
        self.assertIn("http://example.com/a?b=c", shown)
        self.assertTrue(any("plain http" in text and "read it or change it" in text
                            for text in shown))

    def test_cancel_is_the_default_and_opens_nothing(self):
        dialog = self.ask()
        buttons = {dialog.get_widget_for_response(r).get_label(): r
                   for r in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.ACCEPT)}
        self.assertEqual(buttons, {"Cancel": Gtk.ResponseType.CANCEL,
                                   "Open anyway": Gtk.ResponseType.ACCEPT})
        cancel = dialog.get_widget_for_response(Gtk.ResponseType.CANCEL)
        self.assertTrue(cancel.has_default())
        dialog.response(Gtk.ResponseType.CANCEL)
        self.assertEqual(self.ran, [])

    def test_escape_or_closing_it_opens_nothing(self):
        self.ask().response(Gtk.ResponseType.DELETE_EVENT)
        self.assertEqual(self.ran, [])

    def test_open_anyway_opens_it(self):
        self.ask().response(Gtk.ResponseType.ACCEPT)
        self.assertEqual(self.ran, [True])

    def test_it_goes_unanswered_with_its_window(self):
        dialog = self.ask()
        gone = []
        dialog.connect("destroy", lambda _d: gone.append(True))
        self.window.destroy()
        self.assertEqual(gone, [True])
        self.assertEqual(self.ran, [])


@unittest.skipUnless(WebKit2 is not None, "needs WebKitGTK")
class TheMarkInTheRenderedPage(unittest.TestCase):
    """What WebKit makes of it: a mark that shows, beside the link, and is not part of
    the text a selection copies."""

    def setUp(self):
        self.view = WebKit2.WebView()
        self.window = Gtk.Window()
        self.window.set_default_size(700, 400)
        self.window.add(self.view)
        self.window.show_all()
        self.addCleanup(self.window.destroy)

    def load(self, body, dark=False):
        done = []
        self.view.connect("load-changed", lambda _v, event: done.append(True)
                          if event == WebKit2.LoadEvent.FINISHED else None)
        self.view.load_html(markdown_html.document(body, dark), None)
        deadline = time.monotonic() + 15
        while not done and time.monotonic() < deadline:
            Gtk.main_iteration_do(False)
            time.sleep(0.005)
        self.assertTrue(done, "the page never loaded")

    def page(self, expression):
        got = {}

        def finish(view, result):
            got["value"] = json.loads(view.evaluate_javascript_finish(result).to_string())

        self.view.evaluate_javascript(f"JSON.stringify({expression})", -1, None, None,
                                      None, finish)
        deadline = time.monotonic() + 10
        while "value" not in got and time.monotonic() < deadline:
            Gtk.main_iteration_do(False)
            time.sleep(0.005)
        return got.get("value")

    def test_it_shows_beside_the_link_in_both_themes_and_is_not_copied(self):
        for dark in (False, True):
            self.load(markdown_html.turn_html(
                "assistant", "Go [https://bank.example](http://evil.example/) now"), dark)
            shown = self.page(
                "(function () { var m = document.querySelector('.insecure-link'),"
                " r = m.getBoundingClientRect(), s = getComputedStyle(m);"
                " return [m.previousElementSibling.getAttribute('href'),"
                " getComputedStyle(m, '::after').content, r.width > 20, r.height > 8,"
                " s.color !== s.backgroundColor]; })()")
            self.assertEqual(shown, ["http://evil.example/", f'"{linkwarning.WORDS}"',
                                     True, True, True])
            copied = self.page(
                "(function () { var r = document.createRange();"
                " r.selectNodeContents(document.body); var s = getSelection();"
                " s.removeAllRanges(); s.addRange(r); return s.toString(); })()")
            self.assertIn("Go https://bank.example now", copied)
            self.assertNotIn("not secure", copied)


if __name__ == "__main__":
    unittest.main()
