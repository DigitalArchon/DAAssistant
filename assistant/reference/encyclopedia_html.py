"""An encyclopedia article as a page for the article viewer.

The DVD's pages were made for its own Java browser: scripts, toolbars, note-taking
links and `ebcid:` addresses that only that browser understood. What is kept here is the
article: its text and headings, its tables, its pictures - each with its caption - and
its films. Everything else is dropped, or unwrapped to its text.

Addresses become the viewer's own:

    enc://article/<id>    another article        (the viewer opens it)
    enc://assembly/<id>   a picture or table, on a page of its own
    enc://film/<id>       a film, sound or animation, on a page of its own
    enc://image/<id>      a picture's bytes      (served by assistant/ui/encscheme.py)
    enc://flash/<name>    a Flash animation      (played in a window of its own)

Films and sounds are given to WebKit as file:// addresses into the DVD folder: it plays
those directly, the DVD's FLV videos included, and plays nothing at all from a custom
scheme.

The page is the DVD's own HTML, not a model's output, but it is still filtered through
an allowlist like everything else this app shows.
"""

import html as html_module
import re
from html.parser import HTMLParser
from urllib.parse import quote

from .encyclopedia import body_of, byline, citation_of, with_bylines

KEEP = {
    "p", "br", "hr", "i", "b", "em", "strong", "u", "sub", "sup", "small", "s",
    "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
    "blockquote", "pre", "div", "span", "cite", "q", "abbr",
}
RENAME = {"font": "span", "nobr": "span", "center": "div", "h1": "h2", "big": "span"}
VOID = {"br", "hr", "img", "embed", "input", "param", "col", "area", "source", "wbr",
        "meta", "link"}
DROP = {"script", "style", "noscript", "title", "head", "object", "iframe", "form",
        "select", "textarea", "button"}
ATTRIBUTES = {"colspan", "rowspan"}

_ID = {
    "ArticleIdentifier": ("articleId", "article"),
    "AssemblyIdentifier": ("assemblyId", "assembly"),
    "MultimediaAssemblyIdentifier": ("assemblyId", "film"),
}


def link_for(href):
    """The viewer's address for a link on the DVD, or None to keep only its text."""
    href = (href or "").strip()
    found = re.match(r"ebcid:[\w.]+?\.identifier\.(\w+)\?(.*)", href)
    if found:
        kind, query = found.groups()
        if kind in _ID:
            name, scheme = _ID[kind]
            ident = re.search(rf"(?:^|&){name}=(\d+)", query)
            if ident:
                return f"enc://{scheme}/{ident.group(1)}"
        return None
    if re.match(r"https?://", href, re.I) and "//cdsearch." not in href:
        return href
    return None


def image_for(src):
    found = re.search(r"(?:Thumb|Full)BinaryIdentifier\?binaryId=(\d+)", src or "")
    return f"enc://image/{found.group(1)}" if found else None


class _Page(HTMLParser):
    def __init__(self, media_path):
        super().__init__(convert_charrefs=True)
        self.media_path = media_path
        self.out = []
        self.stack = []             # (source tag, what closes it in the output)
        self.suppress = 0           # inside a <script> or the like, and how deep
        self.spacer = None          # the stack depth of a spacer <div>, text unwanted
        self.media = 0              # films and animations, for the caller

    def _open(self, tag, html, close):
        self.out.append(html)
        self.stack.append((tag, close))

    def handle_starttag(self, tag, attrs):
        if self.suppress:
            if tag in DROP:
                self.suppress += 1
            return
        if tag in DROP:
            self.suppress = 1
            return
        attributes = dict(attrs)
        css = attributes.get("class") or ""
        if tag in VOID:
            self._void(tag, attributes)
            return
        if tag == "div" and css == "assembly":
            self._open(tag, '<figure class="assembly">', "</figure>")
        elif tag == "div" and css == "contributors":
            self._open(tag, '<p class="contributors">', "</p>")
        elif tag == "div" and css == "cite":
            self._open(tag, '<section class="cite">', "</section>")
        elif tag == "ul" and css == "captions":
            self._open(tag, "<figcaption>", "</figcaption>")
        elif tag == "li" and "cap" in css:
            self._open(tag, "<span>", "</span> ")
        elif tag == "div" and css == "clear":
            self.stack.append((tag, ""))    # a spacer: &nbsp; and nothing else
            if self.spacer is None:
                self.spacer = len(self.stack)
        elif tag == "a":
            href = link_for(attributes.get("href"))
            if href:
                kind = ' class="asset"' if href.startswith(("enc://assembly/",
                                                             "enc://film/")) else ""
                self._open(tag, f'<a href="{html_module.escape(href)}"{kind}>', "</a>")
            else:
                self.stack.append((tag, ""))
        elif tag in KEEP or tag in RENAME:
            name = RENAME.get(tag, tag)
            kept = "".join(f' {k}="{html_module.escape(v)}"' for k, v in attrs
                           if k in ATTRIBUTES and v and v.isdigit())
            self._open(tag, f"<{name}{kept}>", f"</{name}>")
        else:
            self.stack.append((tag, ""))

    def handle_startendtag(self, tag, attrs):
        if tag in VOID:
            if not self.suppress:
                self._void(tag, dict(attrs))
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def _void(self, tag, attributes):
        if tag in ("br", "hr", "wbr"):
            self.out.append(f"<{tag}>")
        elif tag == "img":
            src = image_for(attributes.get("src"))
            if src:
                alt = html_module.escape(attributes.get("alt") or "")
                self.out.append(f'<img src="{src}" alt="{alt}">')
        elif tag == "embed":
            self._embed(attributes.get("src") or "")

    def _embed(self, src):
        name = src.rsplit("/", 1)[-1]
        path = self.media_path(name) if name else None
        if not path:
            self.out.append('<p class="missing">This film is not in the DVD folder.</p>')
            return
        self.media += 1
        url = "file://" + quote(path)
        if name.lower().endswith(".flv"):
            self.out.append(f'<video controls preload="metadata" src="{url}"></video>')
        elif name.lower().endswith(".mp3"):
            self.out.append(f'<audio controls preload="metadata" src="{url}"></audio>')
        elif name.lower().endswith(".swf"):
            self.out.append(f'<p><a class="play" href="enc://flash/{quote(name)}">'
                            "▶ Play the animation</a></p>")

    def handle_endtag(self, tag):
        if self.suppress:
            if tag in DROP:
                self.suppress -= 1
            return
        if tag in VOID:
            return
        if not any(open_tag == tag for open_tag, _close in self.stack):
            return                      # a stray close tag
        while self.stack:
            if self.spacer is not None and len(self.stack) <= self.spacer:
                self.spacer = None
            open_tag, close = self.stack.pop()
            self.out.append(close)
            if open_tag == tag:
                break

    def handle_data(self, data):
        if not self.suppress and self.spacer is None:
            # ↵ is how the DVD broke a caption's line; a space reads better here.
            self.out.append(html_module.escape(data.replace("\u21b5", " "), quote=False))

    def result(self):
        while self.stack:
            self.out.append(self.stack.pop()[1])
        return "".join(self.out)


def _byline(names):
    return f'<div class="contributors">{html_module.escape(byline(names))}</div>'


def render(fragment, media_path):
    """(HTML, how many films and animations) of a piece of a DVD page."""
    page = _Page(media_path)
    page.feed(with_bylines(fragment, _byline))
    page.close()
    return page.result(), page.media


def article_body(article, media_path):
    """The article, then its "To cite this page" box, which names the publisher."""
    cite = citation_of(article["page"]).replace('<div id="cite">', '<div class="cite">', 1)
    return render(body_of(article["page"]) + cite, media_path)[0]


def assembly_body(assembly, media_path):
    """A picture's, table's or film's page: what the DVD showed when it was clicked."""
    return render(body_of(assembly["page"]), media_path)[0]


CSP = ("default-src 'none'; img-src enc: data:; media-src file:; "
       "style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'")


def page_css(dark):
    if dark:
        bg, fg, muted, border, link, panel = ("#1e1f22", "#e6e6e6", "#a0a4ab",
                                              "#3a3d44", "#7cb7ff", "#26282d")
    else:
        bg, fg, muted, border, link, panel = ("#ffffff", "#1b1c1e", "#6a6f76",
                                              "#e0e2e6", "#1a5fd0", "#f5f6f8")
    return f"""
:root {{ color-scheme: {'dark' if dark else 'light'}; }}
body {{
  margin: 0; padding: 22px 28px 40px; background: {bg}; color: {fg};
  font: 16px/1.62 "Charis SIL", "Source Serif 4", Georgia, serif;
}}
main {{ max-width: 46em; margin: 0 auto; }}
header {{ border-bottom: 1px solid {border}; margin-bottom: 1.2em; }}
header h1 {{ font-size: 1.9em; line-height: 1.2; margin: 0 0 .2em; }}
header .library {{
  font: 12px system-ui, sans-serif; letter-spacing: .06em; text-transform: uppercase;
  color: {muted}; margin-bottom: .8em;
}}
h2, h3, h4, h5, h6 {{ font-family: system-ui, sans-serif; line-height: 1.3; }}
h2 {{ font-size: 1.3em; margin: 1.6em 0 .5em; }}
h3 {{ font-size: 1.12em; margin: 1.3em 0 .4em; }}
a {{ color: {link}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
figure.assembly {{
  float: right; clear: right; width: 240px; margin: .3em 0 1em 1.4em;
  font: 13px/1.45 system-ui, sans-serif; color: {muted};
}}
figure.assembly img {{
  display: block; max-width: 100%; max-height: 320px; width: auto; height: auto;
  margin: 0 auto; border-radius: 6px;
}}
figure.assembly figcaption {{ margin-top: .35em; }}
p.contributors {{
  margin: -.2em 0 1em; font: italic 13px/1.5 system-ui, sans-serif; color: {muted};
}}
section.cite {{
  clear: both; margin-top: 2.5em; padding: .2em 1em .4em; border-radius: 8px;
  background: {panel}; font: 13px/1.5 system-ui, sans-serif;
}}
section.cite h2 {{ font-size: 1em; margin: .6em 0 .3em; }}
section.cite ul {{ margin: 0; padding-left: 1.2em; }}
.assembly-page {{ text-align: center; font-family: system-ui, sans-serif; }}
.assembly-page img {{ max-width: 100%; height: auto; border-radius: 8px; }}
.assembly-page p {{ max-width: 40em; margin: .8em auto; }}
video, audio {{ display: block; max-width: 100%; margin: .6em auto; }}
a.play {{
  display: inline-block; padding: .5em 1.1em; border: 1px solid {border};
  border-radius: 8px; background: {panel}; font-family: system-ui, sans-serif;
}}
table {{ border-collapse: collapse; margin: 1em 0; font: 14px/1.45 system-ui, sans-serif; }}
td, th {{ border: 1px solid {border}; padding: 4px 8px; vertical-align: top; }}
blockquote {{ margin: 1em 0; padding-left: 1em; border-left: 3px solid {border}; }}
.missing {{ color: {muted}; font-style: italic; }}
footer {{
  clear: both; margin-top: 2.5em; padding-top: .8em; border-top: 1px solid {border};
  font: 12px system-ui, sans-serif; color: {muted};
}}
"""


def document(title, library, body, dark):
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{CSP}\">"
            f"<title>{html_module.escape(title)}</title>"
            f"<style>{page_css(dark)}</style></head><body><main>"
            f"<header><h1>{html_module.escape(title)}</h1>"
            f"<div class='library'>{html_module.escape(library)}</div></header>"
            f"{body}"
            f"<footer>From the 2015 encyclopedia DVD, read from your "
            f"own copy.</footer></main></body></html>")
