"""Reading the AxionDB tables on the encyclopedia DVD.

AxionDB is an open-source Java database, and the DVD keeps its content in AxionDB disk
tables of one shape: an integer key and a large object. A table is a directory - TABLE/
TABLE.data, TABLE/lobs/CACHE_VALUE - and the .data file is fixed-width rows:

    key       4 bytes, big-endian
    present   1 byte
    offset    8 bytes, big-endian, into lobs/CACHE_VALUE
    length    4 bytes, big-endian

The large objects lie back to back in CACHE_VALUE: gzip-compressed HTML for articles
and assemblies, the image bytes themselves for pictures. Nothing is encrypted.

The DVD ships most tables inside zip archives (ARTICLE_DATA.zip holds
ARTICLE_DATA/articledb/EB/...), and the installed product unpacks some of them; either
is read here. A deflated zip member cannot be read from the middle, so a table in a zip
is read in one pass in order of offset, which is how the importer wants it anyway.

What the tables say of themselves is not taken on trust: a row's length, and what a page
unpacks to, are bounded (MAX_VALUE, MAX_PAGE), so that a damaged or made-up DVD is
refused rather than read into memory. Nothing else of how it is read changes.
"""

import os
import struct
import zipfile
import zlib

ROW = struct.Struct(">iBqi")
# The most one large object is read as, and what one may unpack to: an article's page
# is a few tens of kilobytes gzipped and a megabyte or two unpacked, a picture a few
# megabytes. A row's length is a signed number, and -1 read the whole of the gigabytes
# after it; gzip says nothing of how much a page unpacks to until it has.
MAX_VALUE = 64 << 20
MAX_PAGE = 64 << 20
# The most a .data file is read as: 17 bytes a row, a few hundred thousand rows.
MAX_DATA = 64 << 20


class AxionError(Exception):
    pass


def parse_rows(data):
    """[(key, offset, length)] of a .data file, the absent ones left out."""
    if len(data) % ROW.size:
        raise AxionError(f"a .data file of {len(data)} bytes is not whole rows")
    rows = []
    for start in range(0, len(data), ROW.size):
        key, present, offset, length = ROW.unpack_from(data, start)
        if present:
            rows.append((key, offset, length))
    return rows


def unpack(value, most=MAX_PAGE):
    """A large object as text when it is gzip-compressed HTML, else as it is - and
    AxionError for one that unpacks to more than `most`, or will not unpack. Several
    gzip members one after another are one page, as gzip.decompress reads them."""
    if value[:2] != b"\x1f\x8b":
        return value
    pages, size, rest = [], 0, value
    while rest:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            page = inflater.decompress(rest, most - size + 1)
        except zlib.error as e:
            raise AxionError(f"a page will not unpack: {e}") from e
        size += len(page)
        if size > most:
            raise AxionError(f"a page unpacks to more than {most >> 20} MB, more than "
                             f"any article")
        if not inflater.eof:
            raise AxionError("a page is cut short")
        pages.append(page)
        rest = inflater.unused_data.lstrip(b"\x00")
    return b"".join(pages).decode("utf-8", "replace")


class _Member:
    """A zip member that closes its archive with it."""

    def __init__(self, archive, name):
        self._archive = zipfile.ZipFile(archive)
        try:
            self._file = self._archive.open(name)
        except KeyError:
            self._archive.close()
            raise AxionError(f"{name} is missing from {archive}")

    def read(self, size=-1):
        return self._file.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._file.close()
        self._archive.close()


class Table:
    """One table, wherever it is: `group` is ARTICLE_DATA, INSTALL_DATA...; `path` the
    rest, e.g. "articledb/EB"."""

    def __init__(self, dvd, group, path):
        self.name = path.rsplit("/", 1)[-1]
        self._zip = None
        folder = os.path.join(dvd, group, *path.split("/"))
        archive = os.path.join(dvd, group + ".zip")
        if os.path.isdir(folder):
            self._dir = folder
        elif os.path.isfile(archive):
            self._dir = None
            self._zip = archive
            self._member = f"{group}/{path}"
            with zipfile.ZipFile(archive) as z:
                if f"{self._member}/{self.name}.data" not in z.NameToInfo:
                    raise AxionError(f"{group}/{path} is not in {archive}")
        else:
            raise AxionError(f"{group}/{path} is not on the DVD at {dvd}")

    def _open(self, suffix):
        if self._dir is not None:
            return open(os.path.join(self._dir, suffix), "rb")
        return _Member(self._zip, f"{self._member}/{suffix}")

    def rows(self):
        with self._open(f"{self.name}.data") as f:
            data = f.read(MAX_DATA + 1)
        if len(data) > MAX_DATA:
            raise AxionError(f"{self.name}: its .data file goes on past "
                             f"{MAX_DATA >> 20} MB, further than any of the DVD's")
        return parse_rows(data)

    def values(self, rows=None):
        """(key, bytes) of every row, read in one pass in order of offset."""
        rows = sorted(rows if rows is not None else self.rows(), key=lambda r: r[1])
        with self._open("lobs/CACHE_VALUE") as f:
            position = 0
            for key, offset, length in rows:
                if not 0 <= length <= MAX_VALUE:
                    raise AxionError(f"{self.name}: key {key} has a length of "
                                     f"{length:,} bytes")
                if offset < position:
                    raise AxionError(f"{self.name}: rows overlap at key {key}")
                skip = offset - position
                while skip:
                    skipped = len(f.read(min(skip, 1 << 20)))
                    if not skipped:
                        raise AxionError(f"{self.name}: ends before key {key}")
                    skip -= skipped
                value = f.read(length)
                if len(value) != length:
                    raise AxionError(f"{self.name}: key {key} is cut short")
                position = offset + length
                yield key, value

    def lob_path(self):
        """The CACHE_VALUE file itself, for reading by offset - unpacked tables only."""
        if self._dir is None:
            return None
        return os.path.join(self._dir, "lobs", "CACHE_VALUE")
