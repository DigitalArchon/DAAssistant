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
import heapq
import itertools
import mmap
import os
import re
from array import array

from . import markup
from .dictzip import DictzipFile

# How many spellings one headword's optional parts may expand to.
MAX_VARIANTS = 8
# How many ways of taking them or leaving them are tried for those: all of them, for
# four parts or fewer. Stopping at MAX_VARIANTS spellings found was no stop for parts
# that never make a new one - "word()()()…", thirty-two empty ones, was tried all 2^32
# ways, and indexing never ended.
MAX_TRIES = 16
# No headword runs to more characters than this. A line in column 0 that does is a
# body line that lost its indent, or garbage, and is not indexed - nor, at more than
# four bytes a character, even decoded: a line of a hundred megabytes cost as much
# again as a string, and every spelling of it as much again.
MAX_HEADWORD = 512


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


def companion(stem, *suffixes):
    """The file beside a dictionary that is named `stem` and one of `suffixes`, the
    first of them there is, or None - in whatever case its name is written. Copied
    from Windows, where case does not matter, a dictionary is as often X.IFO beside
    X.IDX and X.DICT.DZ, or LINGVO.DSL beside LINGVO.ANN and LINGVO.BMP."""
    for suffix in suffixes:
        if os.path.isfile(stem + suffix):
            return stem + suffix
    folder, name = os.path.split(stem)
    try:
        entries = sorted(os.listdir(folder or "."))
    except OSError:
        return None
    by_lower = {}
    for entry in entries:
        by_lower.setdefault(entry.lower(), entry)
    for suffix in suffixes:
        entry = by_lower.get((name + suffix).lower())
        if entry is not None and os.path.isfile(os.path.join(folder, entry)):
            return os.path.join(folder, entry)
    return None


def icon_beside(stem):
    """The dictionary's icon, by GoldenDict's convention: a picture beside it under the
    same name. Lingvo dictionaries ship a .bmp."""
    return companion(stem, ".bmp", ".ico", ".png", ".jpg")


class DSL:
    kind = "dsl"

    def __init__(self, path):
        self.path = path
        # In any case: copied from Windows, a dictionary is as often X.DSL.DZ, whose
        # gzip bytes, read as text, were indexed as one garbage headword.
        self.compressed = path.lower().endswith(".dz")
        self._dz = DictzipFile(path) if self.compressed else None
        head = self._read(0, SAMPLE)
        self.encoding = detect(head)
        stem = path[:path.lower().rfind(".dsl")]
        self.name = self._header_name(head) or os.path.basename(stem)
        self.icon = icon_beside(stem)

    def files(self):
        return [self.path]

    def about(self):
        """The annotation Lingvo ships beside a dictionary (its .ann): what the
        dictionary is, and usually whose. "" when there is none."""
        annotation = companion(self.path[:self.path.lower().rfind(".dsl")], ".ann")
        if annotation is None:
            return ""
        try:
            with open(annotation, "rb") as f:
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
            if end - start > 4 * MAX_HEADWORD:
                # Too long to be a headword (see MAX_HEADWORD): it ends the entry
                # before it, and begins none.
                for word in group:
                    yield word, group_start, start - group_start
                group, previous_end = [], None
                continue
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
        return self._read(offset, min(size, markup.MAX_ENTRY))

    def render(self, headword, data):
        """(html, text) of one entry: its headword lines, then its body - as far as
        markup.MAX_HTML of HTML goes, and "[\u2026]" for the rest."""
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
        size = 0
        for line in body:
            html_line, text_line, indent, stack = render_line(
                line.strip(), tilde, stack, markup.MAX_HTML - size)
            if html_line.strip():
                html_lines.append(f'<div class="m{indent}">{html_line}</div>')
                size += len(html_lines[-1])
            if text_line.strip():
                text_lines.append(text_line.strip())
            if size > markup.MAX_HTML:
                html_lines.append('<div class="m1">[\u2026]</div>')
                text_lines.append("[\u2026]")
                break
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


_MARKUP = {"{": re.compile(r"[\\{}]"), "(": re.compile(r"[\\()]")}


def _parts(text, opener):
    """`text` cut where it has an `opener` - "{" or "(" - and the next closer after it,
    neither escaped: ([the pieces between them], [what each pair holds]), one piece
    more than there are parts. An opener with no closer after it is text.

    In one pass, from one markup character to the next. The lazy regular expression
    this was done with searched the rest of the line for a closer from every opener,
    found or not: a headword of twenty thousand "(" took two seconds to index, and
    "{" as long in one being shown as long again."""
    closer = "}" if opener == "{" else ")"
    markup_char = _MARKUP[opener]
    pieces, parts = [], []
    piece_start, opened, position = 0, None, 0
    while True:
        match = markup_char.search(text, position)
        if match is None:
            break
        position = match.start()
        char = text[position]
        if char == "\\":
            position += 2               # it and the character it escapes
            continue
        if opened is None:
            if char == opener:
                opened = position
        elif char == closer:
            pieces.append(text[piece_start:opened])
            parts.append(text[opened + 1:position])
            piece_start, opened = position + 1, None
        position += 1
    pieces.append(text[piece_start:])
    return pieces, parts


def unsorted_free(line):
    """A headword line less its {unsorted} parts - what ~ in the body stands for."""
    return " ".join(_unescape("".join(_parts(line, "{")[0])).split())


def spellings(line):
    """Every way a headword line can be looked up: optional parts with and without.

    At most MAX_VARIANTS of them, from at most MAX_TRIES ways of taking the parts, and
    none for a line too long to be a headword (MAX_HEADWORD)."""
    text = line.strip()
    if len(text) > MAX_HEADWORD:
        return []
    pieces, optional = _parts("".join(_parts(text, "{")[0]), "(")
    # An empty part, "()", is the same spelling taken or left: it is no part at all.
    kept, rests = [], [pieces[0]]
    for part, rest in zip(optional, pieces[1:]):
        if part:
            kept.append(part)
            rests.append(rest)
        else:
            rests[-1] += rest
    found = []
    for choice in itertools.islice(itertools.product((True, False), repeat=len(kept)),
                                   MAX_TRIES):
        out = [rests[0]]
        for take, part, rest in zip(choice, kept, rests[1:]):
            out.append(part if take else "")
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
# How many of those tags may be open at once; one opened past that is let go. Each line
# closes what is open at its end and opens it again at the next, so tags left open -
# eight thousand lines of "[b]x", 47 KB - had the HTML grow with the square of the
# lines: 224 MB of it. A real entry nests three or four deep.
MAX_OPEN = 8
# Where a token of Lingvo's markup may begin, and what a tag is from its [ to its ].
_TOKEN_START = re.compile(r"[\\{<\[~]")
_TAG = re.compile(r"\[(/?)([a-z*'!0-9]+)(?:\s([^\]]*))?\]")


def _tokens(line):
    """The markup in one body line, in order, as (start, end, kind, what):

        "escaped"    \\x                   what: the character
        "comment"    {{...}}
        "reference"  <<word>>             what: the word, as written
        "tag"        [b], [/b], [c red]   what: ("/" or "", the name, the argument)
        "tilde"      ~

    Anything else is text, and so is an opener with no closer after it.

    In one pass. One regular expression for all of them searched the rest of the line
    for "}}", ">>" or "]" from every "{{", "<<" or "[" that had none after it: a body
    line of twenty thousand "<<" took six seconds to show, under the lock that every
    lookup in every window waits on. Here the closer each kind needs is searched for
    again only past the last one found, and so each part of the line at most once.
    """
    found = {}                          # closer -> where it last was, or -1 for nowhere

    def closer(text, start):
        at = found.get(text)
        if at is None or -1 < at < start:
            at = found[text] = line.find(text, start)
        return at

    position = 0
    while True:
        match = _TOKEN_START.search(line, position)
        if match is None:
            return
        start = match.start()
        char = line[start]
        position = start + 1            # unless it begins a token, it is text
        if char == "\\":
            if start + 1 < len(line):
                yield start, start + 2, "escaped", line[start + 1]
                position = start + 2
        elif char == "~":
            yield start, start + 1, "tilde", None
        elif char == "{" and line.startswith("{{", start):
            end = closer("}}", start + 2)
            if end != -1:
                yield start, end + 2, "comment", None
                position = end + 2
        elif char == "<" and line.startswith("<<", start):
            end = closer(">>", start + 2)
            if end != -1:
                yield start, end + 2, "reference", line[start + 2:end]
                position = end + 2
        elif char == "[":
            end = closer("]", start + 1)
            tag = _TAG.match(line, start, end + 1) if end != -1 else None
            if tag is not None:
                yield start, tag.end(), "tag", tag.groups()
                position = tag.end()


def _opening(tag, argument=None):
    """The HTML that opens Lingvo's `tag`: [c] in the colour it names, as GoldenDict
    shows it, the rest as TAGS has them."""
    if tag == "c":
        return f"<font{markup.coloured(markup.text_colour(argument))}>"
    return TAGS[tag][0]


def _closing(tag):
    return "</font>" if tag == "c" else TAGS[tag][1]


def _unclosed(line):
    """The numbers, among the tokens of `line`, of the [s], [ref] and [url] tags that
    nothing on it closes - [/s] for [s], [/ref] or [/url] for either of the others,
    each closing the last one still open - in order.

    Read in a pass of its own over the line, keeping the numbers only: a list of the
    tokens themselves, for a line of a few megabytes of markup, was hundreds."""
    still_open = {"s": array("q"), "link": array("q")}
    for number, (_start, _end, kind, what) in enumerate(_tokens(line)):
        if kind != "tag" or what[1] not in ("s", "ref", "url"):
            continue
        opened = still_open["s" if what[1] == "s" else "link"]
        if not what[0]:
            opened.append(number)
        elif opened:
            opened.pop()
    return heapq.merge(still_open["s"], still_open["link"])


def render_line(line, tilde, stack, room=markup.MAX_HTML):
    """One body line -> (html, text, indent, tags still open).

    Tags may span lines. Each line is its own <div>, so whatever is open at the end of
    one is closed there and opened again at the start of the next: `stack` holds each
    one open as (tag, the HTML that opened it) - at most MAX_OPEN of them.

    A sound's [s], a cross-reference's [ref] and a link's [url] are closed on their own
    line, or they are no tags at all: what follows one left open is text. Taken for a
    sound's file name or a link's address, it went - the rest of the line with it -
    and "see [ref]house and the rest" was shown as "see ".

    The line is cut short once it has made more than `room` characters: each ~ is the
    headword again, and a line of them a thousand times the line.
    """
    indent = 1
    stack = list(stack)
    html, text = [], []
    made = 0                            # characters of HTML, and of a link collected
    skipping = 0                        # inside [s]...[/s]: a media file name
    linking = None                      # inside [ref] or [url]: (kind, collected text)
    position = 0

    def out(piece, words=""):
        nonlocal made
        html.append(piece)
        made += len(piece)
        if words:
            text.append(words)

    def emit(piece):
        nonlocal made
        if skipping or not piece:
            return
        if linking is not None:
            linking[1].append(piece)
            made += len(piece)
            return
        out(markup.escape(piece), piece)

    if line.startswith("@"):
        word = line[1:].strip()
        return (f'<b class="sub">{markup.escape(word)}</b>', word, 1, stack)

    for _tag, opening in stack:
        out(opening)
    unclosed = _unclosed(line)
    next_unclosed = next(unclosed, None)
    cut = False
    for number, (start, end, kind, what) in enumerate(_tokens(line)):
        if made > room:
            cut = True
            break
        emit(line[position:start])
        position = end
        if number == next_unclosed:
            next_unclosed = next(unclosed, None)
            continue
        if kind == "escaped":
            emit(what)
        elif kind == "comment":
            continue
        elif kind == "reference":
            word = _unescape(what)
            if not skipping:
                out(f'<a href="{markup.attr(markup.lookup_href(word))}">'
                    f"{markup.escape(word)}</a>", word)
        elif kind == "tilde":
            emit(tilde)
        else:
            closing, tag, argument = what
            if re.fullmatch(r"m\d?", tag):
                if not closing and len(tag) == 2:
                    indent = int(tag[1])
            elif tag == "br":
                out("<br>", "\n")
            elif tag == "s":
                skipping += -1 if closing else 1
                skipping = max(skipping, 0)
            elif tag in ("ref", "url"):
                if not closing:
                    if linking is not None:
                        # One opened inside another: what the outer one has so far is
                        # text, not to be lost to the inner one.
                        collected = "".join(linking[1])
                        out(markup.escape(collected), collected)
                    linking = (tag, [])
                elif linking is not None:
                    link, collected = linking
                    linking = None
                    target = "".join(collected).strip()
                    if link == "ref":
                        href = markup.lookup_href(target)
                    else:
                        href = target if target.lower().startswith(("http:", "https:")) \
                            else None
                    if href:
                        out(f'<a href="{markup.attr(href)}">{markup.escape(target)}</a>',
                            target)
                    else:
                        out(markup.escape(target), target)
            elif tag in TAGS or tag == "c":
                tags = [t for t, _opening in stack]
                if closing:
                    if tag in tags:
                        # Close down to it, then reopen what was inside it.
                        index = len(tags) - 1 - tags[::-1].index(tag)
                        inner = stack[index + 1:]
                        for t, _o in reversed(stack[index:]):
                            out(_closing(t))
                        stack = stack[:index] + inner
                        for _t, opening in inner:
                            out(opening)
                elif len(stack) < MAX_OPEN:
                    opening = _opening(tag, argument)
                    stack.append((tag, opening))
                    out(opening)
            # Anything else ([trn], [lang id=1], [!trs]...) changes nothing shown.
    if not cut:
        emit(line[position:])
    if linking is not None:
        # _unclosed() leaves none open by the end of the line; were one to be, what it
        # holds would be text all the same.
        collected = "".join(linking[1])
        out(markup.escape(collected), collected)
    for t, _o in reversed(stack):
        out(_closing(t))
    return "".join(html), "".join(text), indent, stack
