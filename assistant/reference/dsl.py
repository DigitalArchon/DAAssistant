"""ABBYY Lingvo DSL dictionaries (.dsl, .dsl.dz), as GoldenDict reads them.

A DSL file is text, usually UTF-16LE - sometimes UTF-16BE or UTF-8, and in an older
dictionary an 8-bit Windows code page: a header of #NAME-style lines, then entries. An
entry is one or more headword lines starting in column 0, followed by body lines that
start with a tab or space. The body carries Lingvo's own markup - [b], [i], [c color],
[m1] for indentation, [ex] for examples, [ref] and <<...>> for cross-references, [s] for
sounds and pictures - and {{...}} comments.

Headwords have markup of their own: {braces} are shown but not indexed, (parentheses)
are optional, so "(to) go" is found under both "go" and "to go", and a backslash escapes
the next character.

Indexing reads the raw bytes rather than decoding the file: the largest dictionary here
is 600 MB of UTF-16, and a regular expression over bytes finds the line starts that are
not body text without turning 300 million characters into a Python string. Only the
headword lines are decoded.
"""

import codecs
import itertools
import mmap
import os
import re

from . import markup
from .dictzip import DictzipFile

# How many spellings one headword's optional parts may expand to.
MAX_VARIANTS = 8


class Encoding:
    def __init__(self, name, newline, line_start, unit, bom):
        self.name = name
        self.newline = newline
        # A newline followed by something other than a tab, space or another line end:
        # where a headword line begins.
        self.line_start = re.compile(line_start)
        self.unit = unit
        self.bom = bom


UTF16LE = Encoding("utf-16-le", b"\n\x00", rb"\n\x00(?![\t \r\n]\x00)", 2,
                   codecs.BOM_UTF16_LE)
UTF16BE = Encoding("utf-16-be", b"\x00\n", rb"\x00\n(?!\x00[\t \r\n])", 2,
                   codecs.BOM_UTF16_BE)
UTF8 = Encoding("utf-8", b"\n", rb"\n(?![\t \r\n])", 1, codecs.BOM_UTF8)

# Lingvo's names for the code page of an 8-bit DSL, which its #SOURCE_CODE_PAGE header
# gives: Windows code pages.
CODE_PAGES = {"latin": "cp1252", "cyrillic": "cp1251", "easterneuropean": "cp1250",
              "greek": "cp1253", "turkish": "cp1254", "hebrew": "cp1255",
              "arabic": "cp1256", "baltic": "cp1257", "vietnamese": "cp1258"}
# What an 8-bit DSL that names no code page, or one of no known name, is read as.
DEFAULT_CODE_PAGE = "cp1252"
_SOURCE_CODE_PAGE = re.compile(rb'^[ \t]*#SOURCE_CODE_PAGE\b[ \t]*"?([^"\r\n]*)',
                               re.M | re.I)
# How much of a DSL is read to tell its encoding by: an 8-bit dictionary can run to a
# page or two of plain ASCII before its first accented letter.
SAMPLE = 64 * 1024
_EIGHT_BIT = {}


def eight_bit(code_page):
    """An 8-bit code page as an Encoding. A byte is a character, so its lines begin
    where UTF-8's do - after a newline byte that no tab, space or other line end
    follows - and there is no byte-order mark to step over."""
    if code_page not in _EIGHT_BIT:
        _EIGHT_BIT[code_page] = Encoding(code_page, b"\n", rb"\n(?![\t \r\n])", 1, b"")
    return _EIGHT_BIT[code_page]


def _reads_as_utf8(sample):
    """Whether `sample` is UTF-8, but for a character it cuts in two at its end."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


def detect(head, code_page=DEFAULT_CODE_PAGE):
    """The Encoding of DSL text beginning with `head` - SAMPLE bytes, or all of a
    shorter file.

    A byte-order mark says. Without one, UTF-16 puts a zero in every other byte of the
    header's ASCII: the odd bytes when little-endian, the even ones when big-endian.
    Otherwise the text is UTF-8 if it reads as UTF-8, and if not, it is in an 8-bit
    code page: the one its #SOURCE_CODE_PAGE header names, as GoldenDict reads it, or
    else `code_page`. Read as UTF-8, a CP1251 dictionary had every Cyrillic headword
    turned into U+FFFD. Plain ASCII reads as UTF-8 too, so a dictionary that names a
    code page and has nothing but ASCII as far as `head` goes is taken at its word.
    """
    if head.startswith(codecs.BOM_UTF16_LE):
        return UTF16LE
    if head.startswith(codecs.BOM_UTF16_BE):
        return UTF16BE
    if head.startswith(codecs.BOM_UTF8):
        return UTF8
    if len(head) >= 4 and head[1] == 0 and head[3] == 0:
        return UTF16LE                  # no BOM, but plainly little-endian UTF-16
    if len(head) >= 4 and head[0] == 0 and head[2] == 0:
        return UTF16BE                  # nor here, and big-endian
    named = _SOURCE_CODE_PAGE.search(head)
    if _reads_as_utf8(head) and not (named and head.isascii()):
        return UTF8
    if named:
        name = re.sub(r"[^a-z]", "", named.group(1).decode("ascii", "ignore").lower())
        code_page = CODE_PAGES.get(name, code_page)
    return eight_bit(code_page)


def icon_beside(stem):
    """The dictionary's icon, by GoldenDict's convention: a picture beside it under the
    same name. Lingvo dictionaries ship a .bmp."""
    for suffix in (".bmp", ".ico", ".png", ".jpg"):
        if os.path.isfile(stem + suffix):
            return stem + suffix
    return None


class DSL:
    kind = "dsl"

    def __init__(self, path):
        self.path = path
        self.compressed = path.endswith(".dz")
        self._dz = DictzipFile(path) if self.compressed else None
        head = self._read(0, SAMPLE)
        self.encoding = detect(head)
        self.name = self._header_name(head) or os.path.basename(path).split(".dsl")[0]
        self.icon = icon_beside(path[:path.lower().rfind(".dsl")])

    def files(self):
        return [self.path]

    def about(self):
        """The annotation Lingvo ships beside a dictionary (its .ann): what the
        dictionary is, and usually whose. "" when there is none."""
        stem = self.path[:self.path.lower().rfind(".dsl")]
        try:
            with open(stem + ".ann", "rb") as f:
                data = f.read(64 * 1024)
        except OSError:
            return ""
        # An 8-bit dictionary's annotation is in its code page, unless it names another.
        eight = self.encoding not in (UTF16LE, UTF16BE, UTF8)
        encoding = detect(data, self.encoding.name if eight else DEFAULT_CODE_PAGE)
        text = data.decode(encoding.name, "replace").lstrip("\ufeff")
        return markup.about(re.sub(r"\A(?:[ \t]*#[^\n]*\n)+", "", text))   # #LANGUAGE lines

    def _read(self, offset, size):
        if self._dz is not None:
            return self._dz.read(offset, size)
        with open(self.path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    def _header_name(self, head):
        # The head may end in the middle of a character; that line is never #NAME.
        text = head.decode(self.encoding.name, "ignore").lstrip("\ufeff")
        for line in text.splitlines():
            if not line.startswith("#"):
                break
            if line.upper().startswith("#NAME"):
                return line[5:].strip().strip('"').strip()
        return None

    # ---- index ------------------------------------------------------------

    def _line_end(self, data, start):
        """Where the line starting at `start` ends: its newline, or the end of data."""
        enc = self.encoding
        position = start
        while True:
            end = data.find(enc.newline, position)
            if end == -1:
                return len(data)
            if (end - start) % enc.unit == 0:
                return end
            position = end + 1          # the bytes of two characters, not a newline

    def _starts(self, data):
        """Byte offsets of every line in column 0 that is not blank."""
        enc = self.encoding
        first = len(enc.bom) if data[:len(enc.bom)] == enc.bom else 0
        yield first
        for match in enc.line_start.finditer(data, first):
            if (match.start() - first) % enc.unit == 0:
                yield match.start() + len(enc.newline)

    def headwords(self):
        """Every (headword, offset, size): one per spelling, the entry's bytes shared
        by all the headword lines that introduce it."""
        if self.compressed:
            data = self._dz.read_all()
            yield from self._scan(data)
            return
        with open(self.path, "rb") as f:
            if os.fstat(f.fileno()).st_size == 0:
                return
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                yield from self._scan(data)

    def _scan(self, data):
        enc = self.encoding
        group, group_start, previous_end = [], None, None
        in_header = True
        for start in self._starts(data):
            end = self._line_end(data, start)
            line = bytes(data[start:end]).decode(enc.name, "replace")
            line = line.lstrip("\ufeff").rstrip("\r")
            if not line.strip():
                continue
            if line[0] in "\t ":
                continue                # the first line of the file, when it is body
            if in_header and line.startswith("#"):
                continue
            if line.startswith("{{") and line.rstrip().endswith("}}"):
                continue                # a comment on a line of its own
            in_header = False
            follows = previous_end is not None and start == previous_end + len(enc.newline)
            if group and not follows:
                for word in group:
                    yield word, group_start, start - group_start
                group = []
            if not group:
                group_start = start
            group.append(line)
            previous_end = end
        if group:
            for word in group:
                yield word, group_start, len(data) - group_start

    # ---- articles ---------------------------------------------------------

    def read(self, offset, size):
        return self._read(offset, size)

    def render(self, headword, data):
        """(html, text) of one entry: its headword lines, then its body."""
        text = data.decode(self.encoding.name, "replace").lstrip("\ufeff")
        heads, body = [], []
        for line in text.replace("\r", "").split("\n"):
            if not line.strip():
                continue
            if line[0] in "\t ":
                body.append(line)
            elif not body:
                heads.append(line)
        shown = [display_headword(h) for h in heads] or [headword]
        tilde = unsorted_free(heads[0]) if heads else headword
        html_lines, text_lines = [], []
        stack = []
        for line in body:
            html_line, text_line, indent, stack = render_line(line.strip(), tilde, stack)
            if html_line.strip():
                html_lines.append(f'<div class="m{indent}">{html_line}</div>')
            if text_line.strip():
                text_lines.append(text_line.strip())
        html = (f'<div class="hw">{" · ".join(markup.escape(s) for s in shown)}</div>'
                + "".join(html_lines))
        return html, markup.tidy("\n".join(text_lines))


# ---- headwords ------------------------------------------------------------

def _unescape(text):
    return re.sub(r"\\(.)", r"\1", text)


def display_headword(line):
    """A headword line as shown: braces and backslashes gone, optional parts kept."""
    out, escaped = [], False
    for char in line.strip():
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char not in "{}":
            out.append(char)
    return " ".join("".join(out).split())


def unsorted_free(line):
    """A headword line less its {unsorted} parts - what ~ in the body stands for."""
    return " ".join(_unescape(re.sub(r"(?<!\\)\{.*?(?<!\\)\}", "", line)).split())


def spellings(line):
    """Every way a headword line can be looked up: optional parts with and without."""
    text = re.sub(r"(?<!\\)\{.*?(?<!\\)\}", "", line.strip())
    pieces, optional = [], []
    position = 0
    for match in re.finditer(r"(?<!\\)\((.*?)(?<!\\)\)", text):
        pieces.append(text[position:match.start()])
        optional.append(match.group(1))
        position = match.end()
    pieces.append(text[position:])
    found = []
    for choice in itertools.product((True, False), repeat=len(optional)):
        out = [pieces[0]]
        for keep, part, rest in zip(choice, optional, pieces[1:]):
            out.append(part if keep else "")
            out.append(rest)
        word = " ".join(_unescape("".join(out)).split())
        if word and word not in found:
            found.append(word)
        if len(found) >= MAX_VARIANTS:
            break
    return found


# ---- body markup ----------------------------------------------------------

# Lingvo tag -> (HTML open, HTML close). [c], which names its colour, opens in _opening.
TAGS = {
    "b": ("<b>", "</b>"), "i": ("<i>", "</i>"), "u": ("<u>", "</u>"),
    "sup": ("<sup>", "</sup>"), "sub": ("<sub>", "</sub>"),
    "p": ('<span class="dsl-p">', "</span>"),
    "ex": ('<span class="dsl-ex">', "</span>"),
    "*": ('<span class="dsl-sec">', "</span>"),
    "com": ('<span class="dsl-com">', "</span>"),
    "t": ('<span class="dsl-t">', "</span>"),
}
TRANSPARENT = {"trn", "trn1", "!trs", "trs", "lang", "preview", "!preview", "'", "url",
               "ref", "video"}
TOKEN = re.compile(r"\\(.)|\{\{.*?\}\}|<<(.*?)>>|\[(/?)([a-z*'!0-9]+)(?:\s([^\]]*))?\]|~",
                   re.S)


def _opening(tag, argument=None):
    """The HTML that opens Lingvo's `tag`: [c] in the colour it names, as GoldenDict
    shows it, the rest as TAGS has them."""
    if tag == "c":
        return f"<font{markup.coloured(markup.text_colour(argument))}>"
    return TAGS[tag][0]


def _closing(tag):
    return "</font>" if tag == "c" else TAGS[tag][1]


def render_line(line, tilde, stack):
    """One body line -> (html, text, indent, tags still open).

    Tags may span lines. Each line is its own <div>, so whatever is open at the end of
    one is closed there and opened again at the start of the next: `stack` holds each
    one open as (tag, the HTML that opened it).
    """
    indent = 1
    html, text = [opening for _t, opening in stack], []
    stack = list(stack)
    skipping = 0                       # inside [s]...[/s]: a media file name
    linking = None                     # inside [ref] or [url]: (kind, collected text)
    position = 0

    def emit(piece):
        if skipping:
            return
        if linking is not None:
            linking[1].append(piece)
            return
        html.append(markup.escape(piece))
        text.append(piece)

    if line.startswith("@"):
        word = line[1:].strip()
        return (f'<b class="sub">{markup.escape(word)}</b>', word, 1, stack)

    for match in TOKEN.finditer(line):
        emit(line[position:match.start()])
        position = match.end()
        escaped, reference, closing, tag, argument = match.groups()
        whole = match.group(0)
        if escaped is not None:
            emit(escaped)
        elif whole.startswith("{{"):
            continue                    # a comment
        elif reference is not None:
            word = _unescape(reference)
            if not skipping:
                html.append(f'<a href="{markup.attr(markup.lookup_href(word))}">'
                            f"{markup.escape(word)}</a>")
                text.append(word)
        elif whole == "~":
            emit(tilde)
        elif tag is not None:
            if re.fullmatch(r"m\d?", tag):
                if not closing and len(tag) == 2:
                    indent = int(tag[1])
            elif tag == "br":
                html.append("<br>")
                text.append("\n")
            elif tag == "s":
                skipping += -1 if closing else 1
                skipping = max(skipping, 0)
            elif tag in ("ref", "url"):
                if not closing:
                    linking = (tag, [])
                elif linking is not None:
                    kind, collected = linking
                    linking = None
                    target = "".join(collected).strip()
                    if kind == "ref":
                        href = markup.lookup_href(target)
                    else:
                        href = target if target.lower().startswith(("http:", "https:")) \
                            else None
                    if href:
                        html.append(f'<a href="{markup.attr(href)}">'
                                    f"{markup.escape(target)}</a>")
                    else:
                        html.append(markup.escape(target))
                    text.append(target)
            elif tag in TAGS or tag == "c":
                tags = [t for t, _opening in stack]
                if closing:
                    if tag in tags:
                        # Close down to it, then reopen what was inside it.
                        index = len(tags) - 1 - tags[::-1].index(tag)
                        inner = stack[index + 1:]
                        html.extend(_closing(t) for t, _o in reversed(stack[index:]))
                        stack = stack[:index] + inner
                        html.extend(opening for _t, opening in inner)
                else:
                    opening = _opening(tag, argument)
                    stack.append((tag, opening))
                    html.append(opening)
            # Anything else ([trn], [lang id=1], [!trs]...) changes nothing shown.
    emit(line[position:])
    html.extend(_closing(t) for t, _opening in reversed(stack))
    return "".join(html), "".join(text), indent, stack
