"""A link that is not secure: marked where it is shown, and asked about before it opens.

A link in an answer, in a web search's sources, in a dictionary's entry or in an article
goes to the browser. Over plain http to anywhere but this machine, its own network or a
Tor onion service (netguard.insecure_link), the page it opens is not encrypted: anyone
between the user and the site can read it, or change it before it arrives. So such a
link is shown with a small "⚠ not secure" after it, and opening it asks first
(ui/webpolicy.open_link). https, mailto: and the app's own links are not marked.

The mark is the app's own, put after the link by what builds the page, from the address
the link really has. Nothing shown can make one, as nothing shown keeps a class of its
own - the sanitiser, a dictionary's balancer and the article filter each let through
only theirs - and nothing can take one away: the link's text says nothing to it, so an
http address written as "https://bank.example" is marked all the same, and the link's
title, which a page could have written to reassure, is the warning's.

Its words are drawn with CSS (::after) rather than written into the page, so that they
are not part of its text: a selection copied, a code block's Copy and Find all go
without them, and it cannot be selected (user-select: none). And it sets every property
of its own that a page's style could otherwise hand down to it - a dictionary's entry
may colour and size its text, down to nothing - so that none can make it unreadable.
"""

import html
import re
from html.parser import HTMLParser

from . import netguard

WORDS = "⚠ not secure"
TIP = ("Not secure: this address uses plain http, so the page isn't encrypted. Anyone "
       "between you and the site can read it, or change it on its way.")
CLASS = "insecure-link"


def insecure(href):
    """Whether a link to `href` is to be marked, and asked about before it opens."""
    return href is not None and netguard.insecure_link(href)


def marker():
    """The mark that follows an insecure link: empty, its words the stylesheet's."""
    return (f'<span class="{CLASS}" title="{html.escape(TIP)}" role="note" '
            f'aria-label="{html.escape(WORDS)}"></span>')


def after(href):
    """What goes after a link to `href`: the mark, if it is insecure, else nothing."""
    return marker() if insecure(href) else ""


def css(dark):
    """The mark's style, for a page of either theme. Fixed colours rather than the
    page's: it is a warning, and reads the same on any bubble, panel or highlight."""
    if dark:
        fg, bg, border = "#f2c15b", "#3a3016", "#8a6d24"
    else:
        fg, bg, border = "#7a4f00", "#fff3d1", "#d9a636"
    return f"""
.{CLASS} {{
  display: inline-block !important; position: static !important;
  visibility: visible !important; opacity: 1 !important; transform: none !important;
  margin: 0 0 0 .35em !important; padding: 0 .5em !important;
  border: 1px solid {border} !important; border-radius: 7px !important;
  background: {bg} !important; color: {fg} !important;
  font: normal normal 600 11px/1.5 system-ui, "Cantarell", sans-serif !important;
  font-variant: normal !important; letter-spacing: normal !important;
  word-spacing: normal !important; text-transform: none !important;
  text-decoration: none !important; text-shadow: none !important;
  text-indent: 0 !important; vertical-align: baseline !important;
  white-space: nowrap !important; cursor: help;
  -webkit-user-select: none !important; user-select: none !important;
}}
.{CLASS}::after {{ content: "{WORDS}"; }}
"""


def tip_attrs(attrs):
    """A link's attributes, (name, value) as written, with its title the warning's."""
    return [(name, value) for name, value in attrs if name != "title"] + [("title", TIP)]


class _Marker(HTMLParser):
    """Finds the links in HTML already made, and where each ends - as a browser ends
    it: at its </a>, or where another link begins, a link never being inside one."""

    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.edits = []             # (start, end, replacement), in order
        self._open = []             # each link open: whether it is insecure
        self._lines = [0] + [found.end() for found in re.finditer("\n", text)]

    def _here(self):
        line, column = self.getpos()
        return self._lines[line - 1] + column

    def _close(self, at, ended=True):
        """The innermost link open ends at `at`: at its own </a>, or - not `ended` -
        where another begins, or where the HTML does, as a browser would end it. Then
        it is ended here, so that the mark follows the link rather than being in it."""
        if self._open and self._open.pop():
            self.edits.append((at, at, marker() if ended else "</a>" + marker()))

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        start = self._here()
        if self._open:
            self._close(start, ended=False)
        href = next((value for name, value in attrs if name == "href"), None)
        bad = insecure(href)
        self._open.append(bad)
        if bad:
            written = "".join(f" {name}" if value is None
                              else f' {name}="{html.escape(value, quote=True)}"'
                              for name, value in tip_attrs(attrs))
            self.edits.append((start, start + len(self.get_starttag_text()),
                               f"<a{written}>"))

    def handle_startendtag(self, tag, attrs):
        # <a/> is a link left open, to a browser: the slash means nothing in HTML.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            start = self._here()
            end = self.text.find(">", start)
            self._close(len(self.text) if end == -1 else end + 1)

    def result(self):
        while self._open:
            self._close(len(self.text), ended=False)
        out, position = [], 0
        for start, end, replacement in self.edits:
            out.append(self.text[position:start])
            out.append(replacement)
            position = end
        out.append(self.text[position:])
        return "".join(out)


def mark(text):
    """HTML made elsewhere - a dictionary's entry - with each insecure link marked, and
    its title the warning's. Nothing else in it is touched."""
    if "<a" not in (text or "").lower():
        return text or ""
    parser = _Marker(text)
    parser.feed(text)
    parser.close()
    return parser.result()
