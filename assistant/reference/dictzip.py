"""Random access into dictzip files (.dict.dz, .dsl.dz).

A dictzip file is an ordinary gzip file whose deflate stream was flushed every CHLEN
bytes of input, with the compressed size of each chunk listed in an "RA" extra field.
Any chunk can then be inflated on its own, so reading one entry from the middle of a
30 MB dictionary costs one or two 58 KB chunks rather than everything before it.

A .dz that lacks the field is still valid gzip - some converters write one. It is
inflated from its start only as far as a read needs, and the inflater is kept every
SPAN bytes on the way, so that a later read starts from the nearest of those places
rather than from the start. It was inflated whole as soon as anything of it was read -
its first 64 KB, as a DSL is opened, which happens on the main loop as a Look up window
or Settings lists the dictionaries - and kept so: an honest 47 MB one stalled the
window half a second and kept 150 MB, and a 3 MB one that unpacks to 3 GB took the
whole app down as Settings opened, before it could be unticked there.

However much a file's header says it holds, nothing here inflates more of it than a
dictionary of its size can (see limit).
"""

import bisect
import os
import struct
import threading
import zlib
from collections import OrderedDict

from .files import open_file

FTEXT, FHCRC, FEXTRA, FNAME, FCOMMENT = 1, 2, 4, 8, 16
CACHED_CHUNKS = 16
# How many times its own size a file may unpack to. A dictionary's text deflates to a
# third of itself or so, a tenth at the very most; a gzip bomb to a thousandth. A small
# file may unpack to FLOOR all the same, and none to more than MAX_INFLATED, which no
# dictionary comes near.
RATIO = 64
FLOOR = 1 << 20
MAX_INFLATED = 2 << 30
# For a file without the RA field: the inflated bytes between two places it is read
# from, what a read may inflate at a time, and the compressed bytes fed in at a time.
SPAN = 4 << 20
PIECE = 256 << 10
STEP = 16 << 10
# The longest name or comment its gzip header may have.
HEADER_TEXT = 64 << 10


class DictzipError(Exception):
    pass


class TooLarge(DictzipError):
    """A file that would unpack to more than limit() lets it."""


def limit(size):
    """How much a file of `size` bytes may inflate to here."""
    return min(MAX_INFLATED, max(FLOOR, RATIO * size))


def _header(f):
    """(chunk length, [compressed chunk sizes], offset of the data), the first two None
    for a gzip file with no RA field. No more of the file is read than the header."""
    head = f.read(10)
    if len(head) < 10 or head[:2] != b"\x1f\x8b" or head[2] != 8:
        raise DictzipError("not a gzip file")
    flags = head[3]
    chunk_length, sizes = None, None
    if flags & FEXTRA:
        raw = f.read(2)
        xlen = struct.unpack("<H", raw)[0] if len(raw) == 2 else -1
        extra = f.read(max(xlen, 0))
        if len(extra) != xlen:
            raise DictzipError("its gzip header is cut short")
        position = 0
        while position + 4 <= len(extra):
            si = extra[position:position + 2]
            length, = struct.unpack("<H", extra[position + 2:position + 4])
            field = extra[position + 4:position + 4 + length]
            position += 4 + length
            if si == b"RA" and len(field) >= 6:
                _version, chunk_length, count = struct.unpack("<HHH", field[:6])
                if len(field) < 6 + 2 * count:
                    raise DictzipError("its table of chunks is cut short")
                sizes = list(struct.unpack(f"<{count}H", field[6:6 + 2 * count]))
    for flag in (FNAME, FCOMMENT):
        if flags & flag:
            start = f.tell()
            end = f.read(HEADER_TEXT).find(b"\x00")
            if end == -1:
                raise DictzipError("its gzip header has no end")
            f.seek(start + end + 1)
    if flags & FHCRC:
        f.read(2)
    if not chunk_length or not sizes:
        chunk_length, sizes = None, None
    return chunk_length, sizes, f.tell()


class DictzipFile:
    """read(offset, size) over the uncompressed contents of a dictzip file."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        # Why read_all() returned less than the whole file, when it did: the file ends
        # early, or there is a chunk of it that will not inflate.
        self.cut_short = None
        with open_file(path) as f:
            self.limit = limit(os.fstat(f.fileno()).st_size)
            self.chunk_length, sizes, start = _header(f)
        if self.chunk_length is None:
            self._offsets = None
            # Where a read may start inflating from: (inflated offset, offset in the
            # file, input taken but not yet used, the inflater there), every SPAN.
            self._places = [(0, start, b"", zlib.decompressobj(-15))]
            self._place_starts = [0]
            self._end = None            # the inflated size, once a read reaches it
            self._ends_early = False    # whether the file ended before its data did
            return
        self._offsets = [start]
        for size in sizes:
            self._offsets.append(self._offsets[-1] + size)

    def _too_large(self):
        most = (f"{self.limit >> 20} MB" if self.limit >= 1 << 20
                else f"{self.limit >> 10} KB")
        return TooLarge(f"it would unpack to more than {most}, more than a dictionary "
                        f"of its size holds")

    # ---- with the RA field --------------------------------------------------

    def _inflate(self, f, index):
        """Chunk `index`, inflated: chunk_length bytes, or for the last no more. One
        that inflates to more is not what the table of chunks says, or no chunk at all,
        and nothing of it is trusted; nor of one that inflates to less, unless it is
        the last - which is that only once it ends the deflate stream."""
        f.seek(self._offsets[index])
        raw = f.read(self._offsets[index + 1] - self._offsets[index])
        inflater = zlib.decompressobj(-15)
        try:
            data = inflater.decompress(raw, self.chunk_length)
            # A chunk that fills its length leaves its flush marker unread: nothing.
            more = inflater.unconsumed_tail and inflater.decompress(
                inflater.unconsumed_tail, 1)
        except zlib.error as e:
            raise DictzipError(f"chunk {index} will not inflate: {e}")
        if more:
            raise DictzipError(f"chunk {index} unpacks to more than its "
                               f"{self.chunk_length} bytes")
        last = index == len(self._offsets) - 2
        if len(data) < self.chunk_length and not last:
            raise DictzipError(f"chunk {index} unpacks to {len(data)} of its "
                               f"{self.chunk_length} bytes")
        return data, inflater.eof

    def _trailer_records(self, size):
        """Whether the file's gzip trailer records `size`, the length its chunks unpacked
        to, as the low 32 bits gzip keeps. dictd's dictzip, which makes nearly every
        .dsl.dz there is, writes the deflate stream's last block after the last chunk,
        outside the table of chunks: a whole file's last chunk does not end the stream,
        and its trailer is what shows it whole. A file that lost its end has some other
        bytes where the trailer was."""
        try:
            with open_file(self.path) as f:
                f.seek(-4, os.SEEK_END)
                recorded = struct.unpack("<I", f.read(4))[0]
        except (OSError, ValueError, struct.error):
            return False
        return recorded == size & 0xFFFFFFFF

    def _chunk(self, f, index):
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        data, _complete = self._inflate(f, index)
        self._cache[index] = data
        if len(self._cache) > CACHED_CHUNKS:
            self._cache.popitem(last=False)
        return data

    # ---- without it ---------------------------------------------------------

    def _pieces(self, place):
        """(inflated offset, bytes) from `place` on, to the end of the file or as far
        as it goes; a place to read from is kept every SPAN on the way, past the last
        one kept. A gzip file may be several, one after another, and is read through."""
        out, position, tail, inflater = place
        inflater = inflater.copy()
        with open_file(self.path) as f:
            f.seek(position)
            while True:
                if not tail:
                    tail = f.read(STEP)
                    position += len(tail)
                    if not tail:
                        self._ends_early = True
                        return
                try:
                    data = inflater.decompress(tail, PIECE)
                except zlib.error as e:
                    raise DictzipError(f"it will not inflate past byte {out}: {e}")
                tail = inflater.unconsumed_tail
                if data:
                    if out + len(data) > self.limit:
                        raise self._too_large()
                    yield out, data
                    out += len(data)
                    if not tail and out >= self._place_starts[-1] + SPAN:
                        self._places.append((out, position, b"", inflater.copy()))
                        self._place_starts.append(out)
                if inflater.eof:
                    # The end of one gzip member, and its 8-byte trailer. What follows
                    # is another, or the end of the file (or padding).
                    f.seek(position - len(inflater.unused_data) + 8)
                    try:
                        _length, _sizes, position = _header(f)
                    except DictzipError:
                        self._end = out
                        return
                    tail, inflater = b"", zlib.decompressobj(-15)

    def _read_stream(self, offset, size):
        if size <= 0 or (self._end is not None and offset >= self._end):
            return b""
        place = self._places[bisect.bisect_right(self._place_starts, offset) - 1]
        out = []
        pieces = self._pieces(place)
        try:
            for start, data in pieces:
                end = start + len(data)
                if end > offset:
                    out.append(data[max(0, offset - start):offset + size - start])
                if end >= offset + size:
                    break
        except TooLarge:
            raise
        except DictzipError:
            if not out:
                raise
        finally:
            pieces.close()
        return b"".join(out)

    # ---- both ---------------------------------------------------------------

    def read(self, offset, size):
        """Up to `size` bytes from `offset`: fewer where the file ends, or where it
        will not inflate from there on - as for a DSL's head, read as it is opened,
        of a file cut short. Nothing at all to be read there raises DictzipError."""
        with self._lock:
            if self._offsets is None:
                return self._read_stream(offset, size)
            first = offset // self.chunk_length
            last = min((offset + max(size, 1) - 1) // self.chunk_length,
                       len(self._offsets) - 2)
            chunks = []
            with open_file(self.path) as f:
                for index in range(first, last + 1):
                    try:
                        chunks.append(self._chunk(f, index))
                    except DictzipError:
                        if not chunks:
                            raise
                        break
            start = offset - first * self.chunk_length
            return b"".join(chunks)[start:start + size]

    def read_all(self, most=None):
        """Everything, or the first `most` bytes, for building an index: in one pass,
        nothing cached.

        Where the file ends early, or will not inflate past some point, what comes
        before is returned, and cut_short says why: a dictzip file missing its last
        two kilobytes is every chunk but the last one or two, and was nothing. More
        than the file may unpack to (limit) is never returned, and raises TooLarge.
        """
        with self._lock:
            self.cut_short = None
            data = bytearray()
            wanted = self.limit if most is None else min(most, self.limit)
            if self._offsets is None:
                self._ends_early = False
                pieces = self._pieces(self._places[0])
                try:
                    for _start, piece in pieces:
                        data += piece
                        if most is not None and len(data) >= most:
                            break
                except TooLarge:
                    raise
                except DictzipError as e:
                    self.cut_short = str(e)
                finally:
                    pieces.close()
                if self._ends_early and self.cut_short is None and len(data) < wanted:
                    self.cut_short = "the file ends before its compressed data does"
                del data[wanted:]
                return data
            count = len(self._offsets) - 1
            # More than limit is refused, as it is without the RA field, unless no more
            # is wanted than that: an .idx.gz, read to its end, that went on past its
            # limit was cut off there, and indexed that far with nothing said.
            whole = most is None or most > self.limit
            # Every chunk but the last is chunk_length bytes, whatever they compress to.
            if whole and (count - 1) * self.chunk_length >= self.limit:
                raise self._too_large()
            with open_file(self.path) as f:
                for index in range(count):
                    if most is not None and len(data) >= most:
                        break
                    try:
                        chunk, complete = self._inflate(f, index)
                    except DictzipError as e:
                        self.cut_short = f"{e}, of {count}"
                        break
                    data += chunk
                    if index == count - 1 and not complete \
                            and not self._trailer_records(len(data)):
                        self.cut_short = f"its last chunk, {index}, is cut short"
            if whole and len(data) > self.limit:
                raise self._too_large()
            del data[wanted:]
            return data
