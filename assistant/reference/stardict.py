"""StarDict dictionaries: .ifo, .idx[.gz], optional .syn, .dict[.dz].

https://github.com/huzheng001/stardict-3/blob/master/dict/doc/StarDictFileFormat

The .idx is a list of (headword, offset, size) into the .dict; the .syn adds alternative
headwords that point at an .idx position. Each article is one or more typed fields:
with `sametypesequence` the types are given once in the .ifo and left out of the data,
otherwise each field starts with its type letter. Lower-case types are NUL-terminated
text (the last field of a sametypesequence article runs to its end instead); upper-case
types are binary, prefixed with their size.
"""

import base64
import os
import re
import struct
from array import array
from collections import OrderedDict

from . import markup
from .dictzip import DictzipFile
from .dsl import MAX_HEADWORD, companion, icon_beside, inside

LINKS = ("http:", "https:", "mailto:", markup.LOOKUP_SCHEME)

# The pictures an article may show from the dictionary's res folder: its sense numbers,
# daggers and bullets, mostly, a few hundred bytes each.
PICTURE_TYPES = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".bmp": "image/bmp", ".webp": "image/webp",
                 ".svg": "image/svg+xml", ".ico": "image/x-icon"}
MAX_PICTURE = 512 * 1024
# How much of them one entry may bring inline, as data: URIs - a few hundred bytes a
# picture is the rule, so this is a great many of them, or six of the largest - and how
# much a dictionary keeps of those it has read, so as not to read them again.
ENTRY_PICTURES = 8 * MAX_PICTURE
PICTURES_KEPT = 8 * MAX_PICTURE
# The most of an .ifo that is read - a dozen lines, one of them a description - and of
# an .idx or a .syn, read or unpacked: the largest StarDict dictionaries' run to a few
# tens of megabytes.
MAX_IFO = 256 * 1024
MAX_INDEX = 512 << 20
# No file has an offset past this, and the index cannot hold one.
LARGEST_OFFSET = (1 << 63) - 1


def read_ifo(path):
    info = {}
    with open(path, "rb") as f:
        for raw in f.read(MAX_IFO).decode("utf-8", "replace").splitlines()[1:]:
            key, sep, value = raw.partition("=")
            if sep:
                info[key.strip()] = value.strip()
    return info


class StarDict:
    kind = "stardict"

    def __init__(self, ifo_path):
        self.path = ifo_path
        stem = ifo_path[:-len(".ifo")]
        self.info = read_ifo(ifo_path)
        self.name = self.info.get("bookname") or os.path.basename(stem)
        # In any case: an X.IFO copied from Windows comes with X.IDX and X.DICT.DZ, and
        # looked for in lower case only, they were missing - and so was the dictionary.
        self.idx_path = companion(stem, ".idx", ".idx.gz")
        self.dict_path = companion(stem, ".dict", ".dict.dz")
        self.syn_path = companion(stem, ".syn")
        self.types = self.info.get("sametypesequence", "")
        self.icon = icon_beside(stem)
        self.res = os.path.join(os.path.dirname(ifo_path), "res")
        self.problems = []              # what indexing could not read: see headwords
        self._dict = None
        self._pictures = OrderedDict()      # name -> data: URI or None, the latest last
        self._pictures_size = 0
        if not self.idx_path or not self.dict_path:
            raise ValueError(f"{self.name}: its .idx or .dict file is missing")

    def files(self):
        return [p for p in (self.path, self.idx_path, self.dict_path, self.syn_path) if p]

    def about(self):
        """The .ifo's own account of the dictionary: its description, then whose it is
        and where from. "" when it gives none."""
        lines = [markup.html_to_text(self.info.get("description", ""))]
        for key, label in (("author", "Author"), ("copyright", "Copyright"),
                           ("website", "Website"), ("date", "Date")):
            if self.info.get(key):
                lines.append(f"{label}: {self.info[key]}")
        return markup.about("\n".join(line for line in lines if line))

    # ---- index ------------------------------------------------------------

    def _index(self):
        """The .idx, read or unpacked as far as its .ifo's idxfilesize says it goes, as
        StarDict reads it, and no further than MAX_INDEX. An .idx.gz of 85 KB that
        unpacked to 44 MB was unpacked, and then its headwords listed, to half a
        gigabyte; unpacked now, it is bounded as any gzip file is here (see
        dictzip.limit)."""
        claimed = self.info.get("idxfilesize", "")
        most = min(int(claimed), MAX_INDEX) if claimed.isdigit() else MAX_INDEX
        if self.idx_path.lower().endswith(".gz"):
            packed = DictzipFile(self.idx_path)
            data = packed.read_all(most)
            if packed.cut_short:
                self.problems.append(f"its .idx.gz is read as far as it goes: "
                                     f"{packed.cut_short}")
            return data
        with open(self.idx_path, "rb") as f:
            return f.read(most)

    def headwords(self):
        """Every (headword, offset, size): the .idx, then the .syn's alternatives -
        one at a time, from the .idx as read, and not a list of them all. None longer
        than any headword is (dsl.MAX_HEADWORD), nor one at an offset no file has: how
        many of those there were is in `problems` once they are all given."""
        self.problems = []
        data = self._index()
        wide = self.info.get("idxoffsetbits") == "64"
        record = struct.Struct(">QI" if wide else ">II")
        # Where each headword's offset and size are, for the .syn, which names the
        # headword it is another spelling of by its number.
        numbers = array("Q") if self.syn_path else None
        left_out = 0
        position = 0
        while position < len(data):
            end = data.find(b"\x00", position)
            if end == -1 or end + 1 + record.size > len(data):
                break
            if numbers is not None:
                numbers.append(end + 1)
            offset, size = record.unpack_from(data, end + 1)
            if end - position <= 4 * MAX_HEADWORD and offset <= LARGEST_OFFSET:
                yield data[position:end].decode("utf-8", "replace"), offset, size
            else:
                left_out += 1
            position = end + 1 + record.size
        if left_out:
            self.problems.append(f"{left_out} of its headwords are too long to be any, "
                                 f"or point past the end of any file, and are left out")

        if self.syn_path:
            with open(self.syn_path, "rb") as f:
                syn = f.read(MAX_INDEX)
            position = 0
            while position < len(syn):
                end = syn.find(b"\x00", position)
                if end == -1 or end + 5 > len(syn):
                    break
                index, = struct.unpack_from(">I", syn, end + 1)
                if index < len(numbers) and end - position <= 4 * MAX_HEADWORD:
                    offset, size = record.unpack_from(data, numbers[index])
                    if offset <= LARGEST_OFFSET:
                        yield syn[position:end].decode("utf-8", "replace"), offset, size
                position = end + 5

    # ---- articles ---------------------------------------------------------

    def read(self, offset, size):
        size = min(size, markup.MAX_ENTRY)
        if self.dict_path.lower().endswith(".dz"):
            if self._dict is None:
                self._dict = DictzipFile(self.dict_path)
            return self._dict.read(offset, size)
        with open(self.dict_path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    def fields(self, data):
        """[(type, bytes)] of one article."""
        out = []
        position = 0
        if self.types:
            for i, kind in enumerate(self.types):
                last = i == len(self.types) - 1
                if kind.islower():
                    end = len(data) if last else data.find(b"\x00", position)
                    end = len(data) if end == -1 else end
                    out.append((kind, data[position:end]))
                    position = end + 1
                else:
                    if last:
                        out.append((kind, data[position:]))
                    else:
                        size, = struct.unpack_from(">I", data, position)
                        out.append((kind, data[position + 4:position + 4 + size]))
                        position += 4 + size
            return out
        while position < len(data):
            kind = chr(data[position])
            position += 1
            if kind.islower():
                end = data.find(b"\x00", position)
                end = len(data) if end == -1 else end
                out.append((kind, data[position:end]))
                position = end + 1
            else:
                if position + 4 > len(data):
                    break
                size, = struct.unpack_from(">I", data, position)
                out.append((kind, data[position + 4:position + 4 + size]))
                position += 4 + size
        return out

    def render(self, headword, data):
        """(html, text) of one article."""
        room = [ENTRY_PICTURES]

        def picture(name):
            uri = self.picture(name)
            if uri is None or len(uri) > room[0]:
                return None
            room[0] -= len(uri)
            return uri

        parts = []
        for kind, payload in self.fields(data):
            text = payload.decode("utf-8", "replace")
            if kind in "hg":
                parts.append(markup.balance(_bword_links(text), LINKS, picture))
            elif kind == "x":
                parts.append(markup.balance(xdxf_html(text), LINKS, picture))
            elif kind in "mtylk":
                parts.append(markup.escape(text).replace("\n", "<br>"))
            # Pictures, sounds and resource lists are not shown.
        body = "".join(parts)
        return body, markup.html_to_text(body)

    def picture(self, name):
        """A picture from the dictionary's res folder, as a data: URI the page can show
        (it loads nothing from disk itself). None for one that is not there, or not a
        picture, or would be outside the folder."""
        if name in self._pictures:
            self._pictures.move_to_end(name)
            return self._pictures[name]
        uri = self._pictures[name] = self._read_picture(name)
        self._pictures_size += len(name) + len(uri or "")
        while self._pictures_size > PICTURES_KEPT and len(self._pictures) > 1:
            old_name, old = self._pictures.popitem(last=False)
            self._pictures_size -= len(old_name) + len(old or "")
        return uri

    def _read_picture(self, name):
        kind = PICTURE_TYPES.get(os.path.splitext(name)[1].lower())
        # No file's name holds a NUL. One written %00 into an entry's <img> made
        # realpath() raise, and took the whole entry with it, not just the picture.
        if kind is None or "\x00" in name:
            return None
        try:
            res = os.path.realpath(self.res)
            path = os.path.realpath(os.path.join(res, name))
        except (OSError, ValueError):
            return None
        # In the res folder, and that in the dictionary's own: a res that is a link to
        # a folder elsewhere showed the pictures there.
        if not path.startswith(res + os.sep) or not inside(res, os.path.dirname(self.path)):
            return None
        try:
            if os.path.getsize(path) > MAX_PICTURE:
                return None
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        return f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}"


_BWORD = re.compile(r"""href\s*=\s*(["'])bword://([^"']*)\1""", re.I)


def _bword_links(text):
    """StarDict's cross-references, bword://word, as this app's lookup links."""
    return _BWORD.sub(lambda m: f'href="{markup.lookup_href(m.group(2))}"', text)


# XDXF article markup -> HTML. Anything not listed stays as it is: an element the
# browser does not know, showing its text. The headword and a cross-reference are
# pairs, made by _pairs; the rest are tags, one for one.
_XDXF = [
    (re.compile(r'<c(?:\s+c="([^"]*)")?>', re.S),
     lambda m: f"<font{markup.coloured(markup.text_colour(m.group(1)))}>"),
    (re.compile(r"</c>"), "</font>"),
    (re.compile(r"<abr>"), '<span class="dsl-p">'),
    (re.compile(r"</abr>"), "</span>"),
    (re.compile(r"<ex>"), '<span class="dsl-ex">'),
    (re.compile(r"</ex>"), "</span>"),
    (re.compile(r"<co>"), '<span class="dsl-com">'),
    (re.compile(r"</co>"), "</span>"),
    (re.compile(r"<tr>"), "["),
    (re.compile(r"</tr>"), "]"),
]


def _pairs(text, opener, closer, make):
    """`text` with each `opener`...`closer` in it made make(what is between) - the
    first closer after each opener, and an opener with none after it left as it is.

    As the lazy regular expressions <k>(.*?)</k> and <kref>(.*?)</kref> did it, but
    without searching the rest of the article for a closer from every opener that has
    none: twenty thousand <k> took four seconds."""
    out, position = [], 0
    while True:
        start = text.find(opener, position)
        end = -1 if start == -1 else text.find(closer, start + len(opener))
        if end == -1:
            break
        out.append(text[position:start])
        out.append(make(text[start + len(opener):end]))
        position = end + len(closer)
    out.append(text[position:])
    return "".join(out)


def xdxf_html(text):
    text = _pairs(text, "<k>", "</k>", lambda word: f'<b class="hw">{word}</b>')
    text = _pairs(text, "<kref>", "</kref>", lambda word: (
        f'<a href="{markup.lookup_href(markup.html_to_text(word))}">{word}</a>'))
    for pattern, replacement in _XDXF:
        text = pattern.sub(replacement, text)
    return text.replace("\n", "<br>")
