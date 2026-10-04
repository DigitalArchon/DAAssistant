"""Random access into dictzip files (.dict.dz, .dsl.dz).

A dictzip file is an ordinary gzip file whose deflate stream was flushed every CHLEN
bytes of input, with the compressed size of each chunk listed in an "RA" extra field.
Any chunk can then be inflated on its own, so reading one entry from the middle of a
30 MB dictionary costs one or two 58 KB chunks rather than everything before it.

A .dz that lacks the field is still valid gzip - some converters write one - and is
read whole, once, instead.
"""

import gzip
import struct
import threading
import zlib
from collections import OrderedDict

FTEXT, FHCRC, FEXTRA, FNAME, FCOMMENT = 1, 2, 4, 8, 16
CACHED_CHUNKS = 16


class DictzipError(Exception):
    pass


def _header(f):
    """(chunk length, [compressed chunk sizes], offset of the data), or None for a
    gzip file with no RA field."""
    head = f.read(10)
    if len(head) < 10 or head[:2] != b"\x1f\x8b" or head[2] != 8:
        raise DictzipError("not a gzip file")
    flags = head[3]
    chunk_length, sizes = None, None
    if flags & FEXTRA:
        xlen, = struct.unpack("<H", f.read(2))
        extra = f.read(xlen)
        position = 0
        while position + 4 <= len(extra):
            si = extra[position:position + 2]
            length, = struct.unpack("<H", extra[position + 2:position + 4])
            field = extra[position + 4:position + 4 + length]
            position += 4 + length
            if si == b"RA" and len(field) >= 6:
                _version, chunk_length, count = struct.unpack("<HHH", field[:6])
                sizes = list(struct.unpack(f"<{count}H", field[6:6 + 2 * count]))
    for flag in (FNAME, FCOMMENT):
        if flags & flag:
            while f.read(1) not in (b"\x00", b""):
                pass
    if flags & FHCRC:
        f.read(2)
    if not chunk_length or sizes is None:
        return None
    return chunk_length, sizes, f.tell()


class DictzipFile:
    """read(offset, size) over the uncompressed contents of a dictzip file."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._whole = None
        with open(path, "rb") as f:
            found = _header(f)
        if found is None:
            self.chunk_length, self._offsets = None, None
            return
        self.chunk_length, sizes, start = found
        self._offsets = [start]
        for size in sizes:
            self._offsets.append(self._offsets[-1] + size)

    def _chunk(self, f, index):
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        f.seek(self._offsets[index])
        raw = f.read(self._offsets[index + 1] - self._offsets[index])
        try:
            data = zlib.decompressobj(-15).decompress(raw)
        except zlib.error as e:
            raise DictzipError(f"chunk {index}: {e}")
        self._cache[index] = data
        if len(self._cache) > CACHED_CHUNKS:
            self._cache.popitem(last=False)
        return data

    def read(self, offset, size):
        with self._lock:
            if self._offsets is None:
                if self._whole is None:
                    with gzip.open(self.path, "rb") as f:
                        self._whole = f.read()
                return self._whole[offset:offset + size]
            first = offset // self.chunk_length
            last = min((offset + max(size, 1) - 1) // self.chunk_length,
                       len(self._offsets) - 2)
            with open(self.path, "rb") as f:
                data = b"".join(self._chunk(f, i) for i in range(first, last + 1))
            start = offset - first * self.chunk_length
            return data[start:start + size]

    def read_all(self):
        """Everything, for building an index: one pass, nothing cached."""
        with gzip.open(self.path, "rb") as f:
            return f.read()
