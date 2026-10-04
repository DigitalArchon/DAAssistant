"""What the dictionary readers share: their links, and their text for a model.

Each entry is rendered twice. The HTML is for the reader, and goes into the chat view.
The text is for the model answering follow-up questions: the same words, the same line
structure, none of the markup - a model reads "1. the act of..." better than the tags
a dictionary wraps it in, and every tag is context window spent on nothing.
"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import quote, unquote

# The scheme a cross-reference links with. The chat window follows these itself - they
# look the word up there - rather than letting the view navigate.
LOOKUP_SCHEME = "lookup:"


def lookup_href(word):
    return LOOKUP_SCHEME + quote(word, safe="")


def word_of(href):
    """The word a lookup: link names, or None for any other link."""
    if not (href or "").startswith(LOOKUP_SCHEME):
        return None
    return unquote(href[len(LOOKUP_SCHEME):]).strip() or None


def escape(text):
    return html.escape(text or "", quote=False)


def attr(text):
    """`text` for inside a double-quoted attribute."""
    return html.escape(text or "", quote=True)


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "blockquote", "dt", "dd", "table", "ul", "ol", "hr"}
    DROP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._suppress = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self._suppress += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in self.DROP:
            self._suppress = max(0, self._suppress - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._suppress:
            self.parts.append(data)


_BLANK_LINES = re.compile(r"\n\s*\n+")


def tidy(text):
    """Lines without trailing space or runs of blank lines between them."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    return _BLANK_LINES.sub("\n", "\n".join(lines)).strip()


# How much of a dictionary's description its tooltip shows.
ABOUT_CHARS = 600


def about(text, limit=ABOUT_CHARS):
    """A dictionary's description, tidied and cut to a tooltip's length."""
    text = tidy(text)
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def html_to_text(raw):
    parser = _Text()
    parser.feed(raw or "")
    parser.close()
    return tidy("".join(parser.parts))


# ---- colours --------------------------------------------------------------

# The dark theme's bubble, which a dictionary's colours are read against there.
DARK_BACKGROUND = (0x26, 0x28, 0x2d)
# WCAG's contrast for body text.
CONTRAST = 4.5


def _luminance(rgb):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def dark_variant(colour):
    """`colour` (a CSS name or #hex) as the dark theme shows it: the same hue made
    light enough to read on a dark background, as "#rrggbb". None when it already
    reads there, or is not a colour.

    Dictionaries pick their colours for a white page - navy authors, maroon parts of
    speech, dark green quotations - and most of them all but vanish on a dark one.
    """
    import colorsys

    from PIL import ImageColor
    try:
        rgb = ImageColor.getrgb((colour or "").strip())[:3]
    except ValueError:
        return None
    if _contrast(rgb, DARK_BACKGROUND) >= CONTRAST:
        return None
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    s *= 0.85                           # a lighter saturated colour glares
    while l < 1:
        l = min(1.0, l + 0.02)
        out = tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, l, s))
        if _contrast(out, DARK_BACKGROUND) >= CONTRAST:
            break
    return "#%02x%02x%02x" % out


# What a dictionary's [c] or <c> shows when it names no colour, as GoldenDict has it.
DEFAULT_COLOUR = "green"
_COLOUR_NAME = re.compile(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?|[a-z]+", re.I)


def text_colour(name):
    """The colour a dictionary named for its text - a CSS name or #hex - or the
    default when it named none, or something that is not one."""
    name = (name or "").strip()
    return name if _COLOUR_NAME.fullmatch(name) else DEFAULT_COLOUR


def coloured(colour, style=""):
    """The attributes that give an element `colour`, and its dark variant: a
    `--dk` custom property the dark theme's stylesheet reads, flagged by data-dk."""
    out = f' color="{attr(colour)}"'
    dark = dark_variant(colour)
    if dark:
        style = f"{style.rstrip().rstrip(';')};--dk:{dark}".lstrip(";")
        out += " data-dk"
    if style:
        out += f' style="{attr(style)}"'
    return out


# ---- a dictionary's own HTML ----------------------------------------------

_STYLE_COLOUR = re.compile(r"(?:^|;)\s*color\s*:\s*([^;!]+)", re.I)


class _Balancer(HTMLParser):
    """A dictionary's HTML, kept as it is but closed off: every tag it opens is closed
    by its end, and the ones it closes out of order are closed and reopened, as a
    browser would. Not an allowlist - these are the user's own dictionary files, and
    the page's Content-Security-Policy runs no script and fetches nothing remote.

    What balancing guards against is one entry's markup running on into the next: a
    <b> left open in one, or a stray </div>, would carry into the next dictionary and
    the conversation under it. Besides that, only what reaches past the entry goes:
    <style> (it would restyle the whole window), <meta> (a refresh would navigate it
    away), ids (the window's own elements have some), and links the window could not
    follow anyway.
    """

    VOID = {"area", "br", "col", "embed", "hr", "img", "input", "source", "track", "wbr"}
    # Formatting that a misplaced closing tag ends and the browser picks up again.
    REOPEN = {"b", "big", "cite", "em", "font", "i", "s", "small", "span", "strike",
              "strong", "sub", "sup", "tt", "u", "var", "abbr", "dfn", "q", "mark"}
    DROP_CONTENT = {"script", "style", "head", "title", "template", "iframe", "object",
                    "noscript", "textarea", "select"}
    SKIP = {"html", "body", "meta", "link", "base", "form", "frame", "frameset"}
    # A new one of these ends the last one still open, inside the same list or table.
    SIBLINGS = {"li": {"ul", "ol"}, "dt": {"dl"}, "dd": {"dl"},
                "tr": {"table", "tbody", "thead", "tfoot"}, "td": {"tr"}, "th": {"tr"},
                "option": {"select"}}
    # Blocks that end a <p> still open.
    ENDS_P = {"p", "div", "ul", "ol", "dl", "table", "blockquote", "pre", "hr", "h1",
              "h2", "h3", "h4", "h5", "h6", "section", "article", "center"}
    WALLS = {"div", "td", "th", "li", "dd", "blockquote", "table"}

    def __init__(self, links, picture):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._open = []                 # (tag, opening tag as written)
        self._suppress = 0
        self._links = tuple(links)
        self._picture = picture

    def _attrs(self, tag, attrs):
        """The attributes as written out, or None for a picture that cannot be shown."""
        kept = {}
        for name, value in attrs:
            if name.startswith("on") or name in ("id", "name") or value is None:
                continue
            if name == "href" and not value.strip().lower().startswith(self._links):
                continue
            if name == "src":
                value = self._source(value)
                if value is None:
                    continue
            kept.setdefault(name, value)
        if tag == "img" and "src" not in kept:
            return None
        style = kept.pop("style", "")
        colour = kept.pop("color", None) if tag == "font" else None
        if colour is None:
            match = _STYLE_COLOUR.search(style)
            dark = dark_variant(match.group(1)) if match else None
            if dark:
                kept["data-dk"] = None
                style = f"{style.rstrip().rstrip(';')};--dk:{dark}"
        out = "".join(f" {name}" if value is None else f' {name}="{attr(value)}"'
                      for name, value in kept.items())
        if colour is not None:
            return out + coloured(colour, style)
        return out + (f' style="{attr(style)}"' if style else "")

    def _source(self, value):
        """An image's src: inline already, or one of the dictionary's own pictures,
        brought inline. None for anything else."""
        value = value.strip()
        if value.lower().startswith("data:image/"):
            return value
        if "://" in value or value.startswith(("/", "\\")) or self._picture is None:
            return None
        return self._picture(unquote(value))

    def _close_to(self, index, reopen):
        inner = self._open[index + 1:]
        for tag, _opening in reversed(self._open[index:]):
            self.parts.append(f"</{tag}>")
        del self._open[index:]
        if reopen:
            for tag, opening in inner:
                if tag in self.REOPEN:
                    self._open.append((tag, opening))
                    self.parts.append(opening)

    def _last(self, tag, walls):
        """Where the innermost open `tag` is, unless one of `walls` is inside it."""
        for index in range(len(self._open) - 1, -1, -1):
            current = self._open[index][0]
            if current == tag:
                return index
            if current in walls:
                return None
        return None

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP_CONTENT:
            self._suppress += 1
            return
        if self._suppress or tag in self.SKIP:
            return
        attributes = self._attrs(tag, attrs)
        if attributes is None:
            return                      # a picture that cannot be shown
        opening = f"<{tag}{attributes}>"
        if tag in self.ENDS_P:
            index = self._last("p", self.WALLS)
            if index is not None:
                self._close_to(index, reopen=False)
        if tag in self.VOID:
            self.parts.append(opening)
            return
        if tag in self.SIBLINGS:
            index = self._last(tag, self.SIBLINGS[tag])
            if index is not None:
                self._close_to(index, reopen=False)
        self._open.append((tag, opening))
        self.parts.append(opening)

    def handle_startendtag(self, tag, attrs):
        if tag in self.VOID:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.DROP_CONTENT:
            self._suppress = max(0, self._suppress - 1)
            return
        if self._suppress or tag in self.SKIP or tag in self.VOID:
            return
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                self._close_to(index, reopen=tag in self.REOPEN)
                return
        # Closes nothing open: dropped.

    def handle_data(self, data):
        if not self._suppress:
            self.parts.append(escape(data))

    def result(self):
        self._close_to(0, reopen=False)
        return "".join(self.parts)


def balance(raw, links, picture=None):
    """A dictionary's own HTML, closed off so it cannot run past its entry (see
    _Balancer). `links`: the href schemes kept. `picture(name)`: a picture of the
    dictionary's, as a data: URI, or None."""
    parser = _Balancer(links, picture)
    parser.feed(raw or "")
    parser.close()
    return parser.result()
