"""A streamed reply's lines, read in linear time and never past a length.

requests' iter_lines joins what is left of a line to each new piece and splits the whole
again, so a line N bytes long costs about N*N/512 to read, and it holds the line however
long it grows: a provider, or anything answering at its address, that sends a line with
no end keeps the app busy and its memory growing for as long as it likes. httpx's is
linear but holds the line all the same, and splits at more than a line's end: at U+2028
and U+2029 too, which JSON leaves unescaped in a string, cutting a reply's piece in two.

read_lines splits each piece once, at CRLF, CR or LF - where a server-sent event's line
ends, and nowhere else - and refuses a line past MAX_LINE_BYTES. From SealedLore's
providers/streaming.py read_lines (Nov 2026).
"""

import re

# A stream's line (one event) is a few kilobytes, a picture sent inline a few megabytes:
# a longer one is no reply, and reading on would hold memory for as long as it came.
MAX_LINE_BYTES = 32 * 1024 * 1024

_NEWLINE = re.compile(rb"\r\n|\r|\n")


class LineTooLong(ValueError):
    """A stream sent a line of more than MAX_LINE_BYTES with no end."""

    def __init__(self):
        super().__init__(f"the reply sent a line of more than "
                         f"{MAX_LINE_BYTES // (1024 * 1024)} MB with no end, which was "
                         f"not read on")


def read_lines(pieces, digest=None):
    """The lines of a stream that arrives in `pieces` (bytes), as bytes without their
    ends, split at CRLF, CR or LF - a CRLF split between two pieces too - and each piece
    fed to `digest` (a hashlib object), if given, as it arrives. Each piece is split once,
    so a long line costs its length and no more; one longer than MAX_LINE_BYTES raises
    LineTooLong, and nothing more is read."""
    line = []                   # the line not yet ended, in the pieces it came in
    length = 0
    held_cr = False             # a CR at a piece's end may be the first half of a CRLF
    for piece in pieces:
        if digest is not None:
            digest.update(piece)
        if not piece:
            continue
        if held_cr:
            piece = b"\r" + piece
        held_cr = piece.endswith(b"\r")
        if held_cr:
            piece = piece[:-1]
        parts = _NEWLINE.split(piece)
        for index, part in enumerate(parts):
            length += len(part)
            if length > MAX_LINE_BYTES:
                raise LineTooLong()
            line.append(part)
            if index < len(parts) - 1:
                yield b"".join(line)
                line.clear()
                length = 0
    if held_cr or length:
        yield b"".join(line)
