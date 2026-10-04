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
import gzip
import os
import re
import struct

from . import markup
from .dictzip import DictzipFile
from .dsl import icon_beside

LINKS = ("http:", "https:", "mailto:", markup.LOOKUP_SCHEME)

# The pictures an article may show from the dictionary's res folder: its sense numbers,
# daggers and bullets, mostly, a few hundred bytes each.
PICTURE_TYPES = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".bmp": "image/bmp", ".webp": "image/webp",
                 ".svg": "image/svg+xml", ".ico": "image/x-icon"}
MAX_PICTURE = 512 * 1024


def _companion(stem, *suffixes):
    for suffix in suffixes:
        if os.path.exists(stem + suffix):
            return stem + suffix
    return None


def read_ifo(path):
    info = {}
    with open(path, "rb") as f:
        for raw in f.read().decode("utf-8", "replace").splitlines()[1:]:
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
        self.idx_path = _companion(stem, ".idx", ".idx.gz")
        self.dict_path = _companion(stem, ".dict", ".dict.dz")
        self.syn_path = _companion(stem, ".syn")
        self.types = self.info.get("sametypesequence", "")
        self.icon = icon_beside(stem)
        self.res = os.path.join(os.path.dirname(ifo_path), "res")
        self._dict = None
        self._pictures = {}
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

    def headwords(self):
        """Every (headword, offset, size): the .idx, then the .syn's alternatives."""
        if self.idx_path.endswith(".gz"):
            with gzip.open(self.idx_path, "rb") as f:
                data = f.read()
        else:
            with open(self.idx_path, "rb") as f:
                data = f.read()
        wide = self.info.get("idxoffsetbits") == "64"
        record = struct.Struct(">QI" if wide else ">II")
        entries = []
        position = 0
        while position < len(data):
            end = data.find(b"\x00", position)
            if end == -1 or end + 1 + record.size > len(data):
                break
            word = data[position:end].decode("utf-8", "replace")
            offset, size = record.unpack_from(data, end + 1)
            entries.append((word, offset, size))
            position = end + 1 + record.size
        yield from entries

        if self.syn_path:
            with open(self.syn_path, "rb") as f:
                syn = f.read()
            position = 0
            while position < len(syn):
                end = syn.find(b"\x00", position)
                if end == -1 or end + 5 > len(syn):
                    break
                word = syn[position:end].decode("utf-8", "replace")
                index, = struct.unpack_from(">I", syn, end + 1)
                if index < len(entries):
                    yield (word, entries[index][1], entries[index][2])
                position = end + 5

    # ---- articles ---------------------------------------------------------

    def read(self, offset, size):
        if self.dict_path.endswith(".dz"):
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
        parts = []
        for kind, payload in self.fields(data):
            text = payload.decode("utf-8", "replace")
            if kind in "hg":
                parts.append(markup.balance(_bword_links(text), LINKS, self.picture))
            elif kind == "x":
                parts.append(markup.balance(xdxf_html(text), LINKS, self.picture))
            elif kind in "mtylk":
                parts.append(markup.escape(text).replace("\n", "<br>"))
            # Pictures, sounds and resource lists are not shown.
        body = "".join(parts)
        return body, markup.html_to_text(body)

    def picture(self, name):
        """A picture from the dictionary's res folder, as a data: URI the page can show
        (it loads nothing from disk itself). None for one that is not there, or not a
        picture, or would be outside the folder."""
        if name not in self._pictures:
            self._pictures[name] = self._read_picture(name)
        return self._pictures[name]

    def _read_picture(self, name):
        kind = PICTURE_TYPES.get(os.path.splitext(name)[1].lower())
        res = os.path.realpath(self.res)
        path = os.path.realpath(os.path.join(res, name))
        if kind is None or not path.startswith(res + os.sep):
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
# browser does not know, showing its text.
_XDXF = [
    (re.compile(r"<k>(.*?)</k>", re.S), r'<b class="hw">\1</b>'),
    (re.compile(r"<kref>(.*?)</kref>", re.S), None),
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


def xdxf_html(text):
    for pattern, replacement in _XDXF:
        if replacement is None:
            text = pattern.sub(
                lambda m: f'<a href="{markup.lookup_href(markup.html_to_text(m.group(1)))}">'
                          f"{m.group(1)}</a>", text)
        else:
            text = pattern.sub(replacement, text)
    return text.replace("\n", "<br>")
