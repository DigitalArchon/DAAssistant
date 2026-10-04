"""Markdown -> themed HTML for the WebKit conversation view.

Markdown is not something to convert with regexes: `\\*(.+?)\\*` run after the bold
substitution chews through its results, and fenced code blocks, tables, links and
ordered lists are beyond it.

python3-markdown and python3-pygments are already installed on the target machines, so
there is no reason to hand-roll it.
"""

import base64
import html
import re
from html.parser import HTMLParser

import markdown
from pygments.formatters import HtmlFormatter

from ..reference.encyclopedia import byline

EXTENSIONS = ["fenced_code", "tables", "codehilite", "sane_lists", "nl2br"]
EXTENSION_CONFIG = {
    "codehilite": {"guess_lang": False, "noclasses": False, "pygments_style": "default"},
}


# A bare URL in running text. Markdown links only what is written as [text](url) or
# <url>, and models mostly write the URL on its own - in a Sources list, for one.
URL = r"https?://[^\s<>\"'`\[\]]+"
BARE_URL = re.compile(r"\b" + URL, re.I)
# What ends a sentence rather than the URL in it: "see https://a.test/x." links /x.
TRAILING = ".,;:!?*~"


def url_end(url):
    """`url` less the punctuation after it, keeping a ) that closes a ( inside it -
    https://en.wikipedia.org/wiki/Foo_(bar) is one URL, (https://a.test) is not."""
    while url:
        if url[-1] in TRAILING:
            url = url[:-1]
        elif url[-1] == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        else:
            break
    return url


def linkify(text):
    """Plain text -> escaped HTML, with its bare http(s) URLs made into links."""
    out, position = [], 0
    for match in BARE_URL.finditer(text):
        url = url_end(match.group(0))
        if "://" not in url or url.endswith("://"):
            continue
        out.append(html.escape(text[position:match.start()], quote=False))
        out.append(f'<a href="{html.escape(url, quote=True)}">'
                   f'{html.escape(url, quote=False)}</a>')
        position = match.start() + len(url)
    out.append(html.escape(text[position:], quote=False))
    return "".join(out)


def _pygments_css(dark):
    style = "monokai" if dark else "default"
    return HtmlFormatter(style=style).get_style_defs(".codehilite")


class _Sanitiser(HTMLParser):
    """Allowlist filter for the HTML that Markdown produces.

    Model output is not trustworthy input. OCR in particular transcribes whatever
    happens to be on screen, so a crafted page could put `<script>` or a tracking
    `<img src=http://...>` into a response, and this HTML is loaded into a live WebKit
    view.

    Escaping the Markdown source before rendering would also work, but it double-escapes
    inside code spans: `&lt;` gets re-escaped by Markdown to `&amp;lt;` and the reader
    sees "&lt;" instead of "<". Filtering the output instead keeps code blocks correct,
    which matters a lot here - OCR results are frequently code.

    Disallowed tags are dropped, but their text content is kept and escaped, so nothing
    silently vanishes from a response.
    """

    ALLOWED = {
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "strong", "em", "b", "i", "u", "del", "s", "sup", "sub", "small",
        "code", "pre", "kbd", "samp", "var",
        "ul", "ol", "li", "dl", "dt", "dd", "blockquote",
        "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
        "a", "span", "div", "img",
    }
    VOID = {"br", "hr", "img"}
    ALLOWED_ATTRS = {
        "class": None,                       # pygments/codehilite styling
        "title": None,
        "alt": None,
        "colspan": None,
        "rowspan": None,
        "href": ("http:", "https:", "mailto:"),
        "src": ("data:image/",),             # inline images only; never remote fetches
    }

    # Content of these is not text a user wants to read; drop it outright rather
    # than spilling script bodies into the transcript as stray words.
    DROP_CONTENT = {"script", "style", "template", "iframe", "object", "embed"}

    # Text inside these keeps its URLs as written: a link is not nested in a link, and
    # a URL in code is code.
    NO_LINKS = {"a", "code", "pre", "kbd", "samp"}

    def __init__(self, links=None):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._open = []
        self._suppress = 0
        # The link schemes allowed in href: the web, unless the caller adds its own -
        # a dictionary's cross-references are lookup: links the chat window follows.
        self._links = tuple(links) if links else self.ALLOWED_ATTRS["href"]

    def _attrs(self, attrs):
        out = []
        for name, value in attrs:
            name = name.lower()
            if name not in self.ALLOWED_ATTRS or value is None:
                continue
            prefixes = self._links if name == "href" else self.ALLOWED_ATTRS[name]
            if prefixes is not None:
                lowered = value.strip().lower()
                if not any(lowered.startswith(prefix) for prefix in prefixes):
                    continue
            out.append(f' {name}="{html.escape(value, quote=True)}"')
        return "".join(out)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.DROP_CONTENT:
            self._suppress += 1
            return
        if tag not in self.ALLOWED:
            return
        if tag in self.VOID:
            self.parts.append(f"<{tag}{self._attrs(attrs)}>")
            return
        self._open.append(tag)
        self.parts.append(f"<{tag}{self._attrs(attrs)}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.DROP_CONTENT:
            self._suppress = max(0, self._suppress - 1)
            return
        if tag not in self.ALLOWED or tag in self.VOID:
            return
        if tag in self._open:
            # Close anything left dangling so malformed output cannot break the layout.
            while self._open:
                current = self._open.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in self.ALLOWED:
            self.parts.append(f"<{tag.lower()}{self._attrs(attrs)}>")

    def handle_data(self, data):
        if self._suppress:
            return
        if self.NO_LINKS.intersection(self._open):
            self.parts.append(html.escape(data, quote=False))
        else:
            self.parts.append(linkify(data))

    def result(self):
        while self._open:
            self.parts.append(f"</{self._open.pop()}>")
        return "".join(self.parts)


_DROPPED_TAG = re.compile(
    r"<(?=/?(?:%s)\b)" % "|".join(sorted(_Sanitiser.DROP_CONTENT)), re.I)


def sanitise(raw_html, links=None):
    """`raw_html` through the allowlist. `links` replaces the href schemes allowed."""
    parser = _Sanitiser(links)
    parser.feed(raw_html)
    parser.close()
    if parser._suppress:
        # A dropped element that never closed: the model was talking about the tag, not
        # writing one - "add a <script> tag to your page" - and suppressing to a closing
        # tag that never comes threw away the rest of the answer. Show those tags as the
        # text they were meant as; escaped, they are as inert as anything else here.
        parser = _Sanitiser(links)
        parser.feed(_DROPPED_TAG.sub("&lt;", raw_html))
        parser.close()
    return parser.result()


def render_markdown(text, links=None):
    """Markdown -> HTML, filtered through an allowlist. See _Sanitiser. `links` adds
    link schemes to the web's: an Ask Encyclopedia answer's citations, made links by the
    app (enc://cite/). A model could write one too; all it can do is open one of the
    passages the answer was given, which the window checks it was."""
    try:
        rendered = markdown.markdown(text or "", extensions=EXTENSIONS,
                                     extension_configs=EXTENSION_CONFIG)
    except Exception:
        # Never show raw markup on a renderer failure - show readable plain text.
        return f"<pre>{html.escape(text or '')}</pre>"
    if links:
        return sanitise(rendered, ("http:", "https:", "mailto:") + tuple(links))
    return sanitise(rendered)


def page_css(dark):
    if dark:
        bg, fg, muted = "#1e1f22", "#e6e6e6", "#a0a4ab"
        user_bg, bot_bg, border = "#2b3a52", "#26282d", "#3a3d44"
        code_bg, link = "#16171a", "#7cb7ff"
        ok, warn, bad = "#5cc98a", "#e0b04a", "#ff7b72"
    else:
        bg, fg, muted = "#ffffff", "#1b1c1e", "#6a6f76"
        user_bg, bot_bg, border = "#e8f0fe", "#f5f6f8", "#e0e2e6"
        code_bg, link = "#f0f1f4", "#1a5fd0"
        ok, warn, bad = "#1d7f47", "#94650a", "#c4271f"

    # A dictionary's own colours are for a white page; the dark theme lifts each one
    # to a shade that reads (see reference.markup.dark_variant).
    dictionary_dark = (".dict [data-dk] { color: var(--dk) !important; }\n"
                       if dark else "")

    return f"""
:root {{ color-scheme: {'dark' if dark else 'light'}; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 20px 22px 28px;
  background: {bg}; color: {fg};
  font-family: system-ui, "Cantarell", "Segoe UI", sans-serif;
  font-size: 14.5px; line-height: 1.62;
  overflow-wrap: anywhere;
}}
.turn {{ margin: 0 0 18px; }}
.role {{
  font-size: 11px; font-weight: 700; letter-spacing: .07em;
  text-transform: uppercase; color: {muted}; margin-bottom: 6px;
}}
.badge {{
  display: inline-block; margin-left: 8px; padding: 1px 7px; border-radius: 9px;
  background: {user_bg}; color: {fg}; font-size: 10px; letter-spacing: .04em;
  text-transform: none;
}}
.privacy {{
  display: inline-block; margin-left: 8px; padding: 1px 8px; border-radius: 9px;
  font-size: 10px; letter-spacing: .03em; text-transform: none; font-weight: 650;
  border: 1px solid currentColor;
}}
.privacy.ok {{ color: {ok}; }}
.privacy.warn {{ color: {warn}; }}
.privacy.bad {{ color: {bad}; }}
.privacy.pending {{ color: {muted}; }}
.turn.superseded {{ opacity: .55; }}
.turn.private .bubble {{ border-left: 3px solid {ok}; }}
.turn.private.bad .bubble {{ border-left-color: {bad}; }}
.bubble {{
  background: {bot_bg}; border: 1px solid {border};
  border-radius: 12px; padding: 12px 15px;
}}
.turn.user .bubble {{ background: {user_bg}; }}
.bubble > *:first-child {{ margin-top: 0; }}
.bubble > *:last-child {{ margin-bottom: 0; }}
h1,h2,h3,h4 {{ line-height: 1.3; margin: 1.1em 0 .5em; font-weight: 650; }}
h1 {{ font-size: 1.45em; }} h2 {{ font-size: 1.28em; }} h3 {{ font-size: 1.12em; }}
p {{ margin: .6em 0; }}
ul, ol {{ margin: .6em 0; padding-left: 1.5em; }}
li {{ margin: .25em 0; }}
a {{ color: {link}; }}
blockquote {{
  margin: .7em 0; padding: .1em 1em; border-left: 3px solid {border}; color: {muted};
}}
code {{
  background: {code_bg}; border-radius: 5px; padding: .12em .4em;
  font-family: "JetBrains Mono", "Fira Code", ui-monospace, monospace; font-size: .89em;
}}
pre {{
  background: {code_bg}; border: 1px solid {border}; border-radius: 9px;
  padding: 12px 14px; overflow-x: auto; margin: .8em 0;
}}
pre code {{ background: none; padding: 0; font-size: .87em; line-height: 1.5; }}
table {{ border-collapse: collapse; margin: .8em 0; width: 100%; font-size: .94em; }}
th, td {{ border: 1px solid {border}; padding: 7px 10px; text-align: left; }}
th {{ background: {code_bg}; font-weight: 650; }}
hr {{ border: 0; border-top: 1px solid {border}; margin: 1.2em 0; }}
img {{ max-width: 100%; }}
.cursor {{
  display: inline-block; width: 7px; height: 1.05em; vertical-align: text-bottom;
  background: {muted}; animation: blink 1.05s steps(2, start) infinite; margin-left: 2px;
}}
@keyframes blink {{ to {{ visibility: hidden; }} }}
.empty {{ color: {muted}; font-style: italic; }}
.shot {{ margin: 0 0 18px; }}
.shot img {{
  display: block; max-width: 100%; max-height: 220px; cursor: zoom-in;
  border: 1px solid {border}; border-radius: 9px;
}}
.shot-actions {{ margin-top: 7px; }}
.shot-actions button {{
  font: 12px system-ui, sans-serif; padding: 3px 11px; margin-right: 6px;
  border-radius: 6px; border: 1px solid {border}; background: {bot_bg}; color: {fg};
  cursor: pointer;
}}
.shot-actions button:hover {{ background: {user_bg}; }}
.turn.lookup .bubble {{ padding: 6px 15px 10px; }}
.dict {{ margin: 0; padding: 8px 0; line-height: 1.45; }}
.dict + .dict {{ border-top: 1px solid {border}; }}
.dict > summary {{
  cursor: pointer; font-size: 11px; font-weight: 700; letter-spacing: .05em;
  text-transform: uppercase; color: {muted}; margin-bottom: 4px;
}}
.dict .entry + .entry {{ border-top: 1px dashed {border}; margin-top: 8px; padding-top: 8px; }}
.dict .hw {{ font-weight: 700; font-size: 1.08em; }}
.dict .m0, .dict .m1 {{ margin-left: 0; }}
.dict .m2 {{ margin-left: 1.2em; }} .dict .m3 {{ margin-left: 2.4em; }}
.dict .m4 {{ margin-left: 3.6em; }} .dict .m5, .dict .m6, .dict .m7, .dict .m8,
.dict .m9 {{ margin-left: 4.8em; }}
.dict .sub {{ display: block; margin-top: .5em; }}
.dsl-p {{ color: {ok}; font-style: italic; }}
.dsl-ex, .dsl-com {{ color: {muted}; }}
.dsl-t {{ font-family: "Doulos SIL", "Charis SIL", serif; }}
.lookup-note {{ color: {muted}; font-style: italic; }}
{dictionary_dark}.edition {{
  font-size: 10px; letter-spacing: .05em; text-transform: uppercase; color: {muted};
  margin-left: 6px; white-space: nowrap;
}}
.sources {{ margin: .3em 0; padding-left: 1.8em; }}
.sources li {{ margin: .2em 0; }}
.sources .by {{ color: {muted}; font-size: .88em; margin-left: 6px; }}
.gallery .credit {{ display: block; font-size: .9em; font-style: italic; opacity: .85; }}
.sources-more > summary {{ cursor: pointer; color: {muted}; font-size: .92em; }}
.gallery {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }}
.gallery a {{
  width: 150px; text-decoration: none; color: {muted}; font-size: .82em; line-height: 1.35;
}}
.gallery img, .gallery .play {{
  display: block; width: 150px; height: 104px; object-fit: cover;
  object-position: center top; border-radius: 6px; margin-bottom: 4px;
}}
.gallery .play {{
  background: {code_bg}; color: {fg}; font-size: 2.2em; line-height: 104px;
  text-align: center;
}}
.checks {{ color: {muted}; font-size: .9em; margin: .6em 0 .2em; }}
.check-failed {{ color: {bad}; font-size: .9em; margin: .2em 0; }}
.suggestions a {{ margin-right: .7em; white-space: nowrap; }}
{_pygments_css(dark)}
"""


def privacy_html(privacy):
    """The badge on a turn of a private conversation: {label, tone, tip} or None."""
    if not privacy:
        return ""
    tone = privacy.get("tone") if privacy.get("tone") in (
        "ok", "warn", "bad", "pending") else "pending"
    return (f'<span class="privacy {tone}" title="{html.escape(privacy.get("tip", ""))}">'
            f'{html.escape(privacy.get("label", ""))}</span>')


def model_badge(model, is_premium=False, superseded=False):
    """The badge naming the model that wrote an answer (full id on hover), or Premium
    for a turn from before answers recorded their model."""
    if model:
        short = model.rsplit("/", 1)[-1]
        text = f"{short} · earlier answer" if superseded else short
        return (f'<span class="badge" title="{html.escape(model)}">'
                f'{html.escape(text)}</span>')
    return '<span class="badge">Premium</span>' if is_premium else ""


def turn_html(role, content, is_premium=False, streaming=False, privacy=None,
              model=None, superseded=False, links=None):
    label = "You" if role == "user" else "Assistant"
    badge = model_badge(model, is_premium, superseded)
    badge += privacy_html(privacy)
    body = render_markdown(content, links)
    if streaming:
        body += '<span class="cursor"></span>'
    if not content and not streaming:
        body = '<p class="empty">(empty response)</p>'
    classes = html.escape(role)
    if superseded:
        classes += " superseded"
    if privacy:
        classes += " private" + (" bad" if privacy.get("tone") == "bad" else "")
    return (f'<div class="turn {classes}">'
            f'<div class="role">{label}{badge}</div>'
            f'<div class="bubble">{body}</div></div>')


def screenshot_html(png):
    """The capture, above the first turn: a thumbnail with View and Save.

    Not a .turn - streaming replaces the last .turn in the page - and built here rather
    than rendered from Markdown: the sanitiser is for what models write, and these bytes
    are the user's own capture. The buttons post their data-shot to the window; see
    SCREENSHOT_JS in chat.py.
    """
    src = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return ('<div class="shot"><div class="role">Screenshot</div>'
            f'<img src="{src}" alt="Screenshot" title="View full size" data-shot="view">'
            '<div class="shot-actions">'
            '<button type="button" data-shot="view">View</button>'
            '<button type="button" data-shot="save">Save…</button>'
            '</div></div>')


def lookup_html(lookup, hidden=0):
    """A dictionary lookup, as GoldenDict lays one out: each dictionary's entries under
    its name, one after another.

    Built here rather than rendered from Markdown, as the screenshot is: the entries
    are markup of the dictionaries' own, already reduced to safe HTML by their readers
    (see assistant/reference), not text a model wrote. Cross-references are lookup:
    links, which the chat window follows itself.
    """
    from ..reference.markup import attr, escape, lookup_href
    word, results = lookup["word"], lookup["results"]
    parts = []
    if not results and hidden:
        parts.append(f'<p class="lookup-note">Entries for “{escape(word)}” from {hidden} '
                     f'{"dictionary" if hidden == 1 else "dictionaries"}, hidden in this '
                     f"conversation. Switch them back on in the bar above.</p>")
    elif not results:
        parts.append(f'<p class="lookup-note">No entry for “{escape(word)}” in '
                     f'{lookup.get("dictionaries", 0)} dictionaries.</p>')
        if lookup.get("suggestions"):
            links = "".join(f'<a href="{attr(lookup_href(s))}">{escape(s)}</a>'
                            for s in lookup["suggestions"])
            parts.append(f'<p class="suggestions">Nearby: {links}</p>')
    elif lookup["found"] != word:
        parts.append(f'<p class="lookup-note">No entry for “{escape(word)}”; showing '
                     f'“{escape(lookup["found"])}”.</p>')
    for result in results:
        entries = "".join(f'<div class="entry">{entry["html"]}</div>'
                          for entry in result["entries"])
        parts.append(f'<details class="dict" open><summary>'
                     f'{escape(result["dictionary"])}</summary>{entries}</details>')
    count = (f'<span class="badge">{len(results)} of {lookup.get("dictionaries", 0)} '
             f'dictionaries{f" · {hidden} hidden" if hidden else ""}</span>'
             if results or hidden else "")
    return (f'<div class="turn lookup"><div class="role">Look up · {escape(word)}'
            f'{count}</div><div class="bubble">{"".join(parts)}</div></div>')


# The three encyclopedias on the DVD, as search results and sources name them.
EDITION_NAMES = {"EB": "Encyclopedia", "EBI": "Student Encyclopedia",
                 "EBK": "Elementary Encyclopedia"}
# Contributors named beside a source before the rest become "and 20 others".
BYLINE_NAMES = 3


def sources_html(passages, cited, turn, checks=(), limited=False):
    """The encyclopedia passages an Ask Encyclopedia answer was given, under it.

    Those it cited, by their numbers - each a link that opens the article in Search
    Encyclopedia at what was quoted from it - the rest folded away; the result of
    checking its quotations word for word against them; the pictures and films of the
    sections it cited; and, when the encyclopedia had little on the question, that.
    `turn` is the answer's place in the conversation, which the links name. """
    def item(number, passage):
        # .get: answers saved before contributors and credits were kept have neither.
        names = passage.get("contributors") or []
        by = (f'<span class="by" title="{html.escape(byline(names))}">'
              f'{html.escape(byline(names, most=BYLINE_NAMES))}</span>' if names else "")
        return (f'<li value="{number}"><a href="enc://cite/{int(turn)}/{number}">'
                f'{html.escape(passage["title"])}</a> › {html.escape(passage["heading"])}'
                f'<span class="edition">{EDITION_NAMES.get(passage["library"], "")}'
                f'</span>{by}</li>')

    def tile(address, image, label, caption, credit):
        tip = " — ".join(part for part in (caption, credit) if part)
        return (f'<a href="{address}" title="{html.escape(tip)}">{image}'
                f'{html.escape(_short(label))}'
                + (f'<span class="credit">{html.escape(_short(credit, 60))}</span>'
                   if credit else "")
                + "</a>")

    numbered = list(enumerate(passages, 1))
    used = [(n, p) for n, p in numbered if n in cited] or numbered
    rest = [(n, p) for n, p in numbered if (n, p) not in used]
    parts = []
    if limited:
        parts.append('<p class="lookup-note">The encyclopedia has little on this question, '
                     "even after a second search. Strict keeps the answer to what it "
                     "has; switch Strict off for a fuller one, with what is not from "
                     "the encyclopedia set apart.</p>")
    parts.append(f'<ol class="sources">{"".join(item(n, p) for n, p in used)}</ol>')
    if rest:
        parts.append(f'<details class="sources-more"><summary>Also found ({len(rest)})'
                     f'</summary><ol class="sources">'
                     f'{"".join(item(n, p) for n, p in rest)}</ol></details>')
    if checks:
        good = sum(1 for c in checks if c["ok"])
        lines = [f'<p class="checks">Quotations checked against the passages: {good} '
                 f'of {len(checks)} found word for word.</p>']
        for check in checks:
            if check["ok"]:
                continue
            where = (f"found in [{check['found']}] instead"
                     if check["found"] else "not found in any passage")
            cited_as = (" cited as " + ", ".join(f"[{n}]" for n in check["cited"])
                        if check["cited"] else "")
            lines.append(f'<p class="check-failed">⚠ “{html.escape(_short(check["quote"]))}”'
                         f"{cited_as}: {where}.</p>")
        parts.append("".join(lines))
    tiles, seen = [], set()
    for _n, passage in used:
        credits = passage.get("credits") or {}
        for assembly, picture, caption in passage["pictures"]:
            if ("picture", assembly) not in seen:
                seen.add(("picture", assembly))
                # JSON-saved answers key the credits by string.
                credit = credits.get(assembly) or credits.get(str(assembly)) or ""
                tiles.append(tile(f"enc://assembly/{int(assembly)}",
                                  f'<img src="enc://image/{int(picture)}" alt="">',
                                  caption, caption, credit))
        for film in passage["films"]:
            if ("film", film["assembly"]) not in seen:
                seen.add(("film", film["assembly"]))
                still = (f'<img src="enc://image/{int(film["image"])}" alt="">'
                         if film.get("image") else '<span class="play">▶</span>')
                tiles.append(tile(f"enc://film/{int(film['assembly'])}", still + "▶ ",
                                  film["caption"] or film["title"], film["caption"],
                                  film.get("credit") or ""))
    if tiles:
        parts.append(f'<div class="gallery">{"".join(tiles[:12])}</div>')
    parts.append('<p class="lookup-note">From the 2015 encyclopedia DVD.</p>')
    return (f'<div class="turn lookup"><div class="role">Encyclopedia sources</div>'
            f'<div class="bubble">{"".join(parts)}</div></div>')


def _short(text, limit=90):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data: enc:; "
       "form-action 'none'; base-uri 'none'")


def document(turns_html, dark):
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{CSP}\">"
            f"<style>{page_css(dark)}</style></head>"
            f"<body>{turns_html}<div id='end'></div></body></html>")
