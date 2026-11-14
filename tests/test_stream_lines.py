"""A streamed reply's lines are read in linear time, and never past MAX_LINE_BYTES: a
provider, or anything answering at its address, that sends a line with no end used to
keep the app reading - and holding it, and splitting it again with every piece - for as
long as it liked (streams.read_lines; SealedLore's streaming.read_lines)."""

import hashlib
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

import requests  # noqa: E402

from assistant import sealed, streams  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.llm import LLMClient  # noqa: E402
from assistant.privacy import PrivacyError  # noqa: E402
from assistant.streams import LineTooLong, read_lines  # noqa: E402

SMALL = 64 * 1024           # the cap, made small enough to pass in a test


def pieces_of(data, cuts):
    cuts = sorted(cuts)
    return [data[a:b] for a, b in zip([0] + cuts, cuts + [len(data)])]


class CountingRaw(io.BytesIO):
    """A response's raw body that counts what was asked of it."""

    def __init__(self, data):
        super().__init__(data)
        self.taken = 0

    def read(self, size=-1, *args, **kwargs):
        chunk = super().read(size)
        self.taken += len(chunk)
        return chunk


def response_of(data):
    response = requests.models.Response()
    response.status_code = 200
    response.raw = CountingRaw(data)
    return response


def event(content):
    return b"data: " + json.dumps({"id": "c1", "choices": [
        {"delta": {"content": content}}]}).encode() + b"\n\n"


class ReadLines(unittest.TestCase):
    def test_lines_end_where_an_events_do_however_the_pieces_fall(self):
        """CRLF, CR or LF, a CRLF split between two pieces too, and nowhere else: the
        lines bytes.splitlines finds in the whole, every byte hashed as it came."""
        rng = random.Random(7)
        for _ in range(2000):
            data = bytes(rng.choice(b"ab\r\n") for _ in range(rng.randint(0, 40)))
            cuts = rng.sample(range(len(data) + 1), min(len(data) + 1, rng.randint(0, 8)))
            digest = hashlib.sha256()
            self.assertEqual(list(read_lines(pieces_of(data, cuts), digest)),
                             data.splitlines(), data)
            self.assertEqual(digest.digest(), hashlib.sha256(data).digest())

    def test_a_line_separator_inside_a_replys_json_is_not_a_line_end(self):
        """httpx's iter_lines, which read the sealed stream, split at U+2028 too."""
        line = 'data: {"choices":[{"delta":{"content":"a\u2028b\u2029c"}}]}'.encode()
        self.assertEqual(list(read_lines([line + b"\n\n"])), [line, b""])

    def test_a_line_with_no_end_is_not_read_past_the_cap(self):
        taken = []

        def endless():
            while True:
                taken.append(1)
                yield b"x" * 512

        with mock.patch.object(streams, "MAX_LINE_BYTES", SMALL):
            lines = read_lines(endless())
            with self.assertRaises(LineTooLong) as caught:
                next(lines)
        self.assertLessEqual(len(taken) * 512, SMALL + 512)
        self.assertIn("with no end", str(caught.exception))
        self.assertIn("32 MB", str(LineTooLong()))
        # A line just under it is read, and the next begins afresh.
        with mock.patch.object(streams, "MAX_LINE_BYTES", SMALL):
            data = b"y" * SMALL + b"\n" + b"z" * SMALL + b"\n"
            self.assertEqual(list(read_lines(pieces_of(data, range(0, len(data), 1000)))),
                             [b"y" * SMALL, b"z" * SMALL])

    def test_a_long_line_costs_its_length_and_no_more(self):
        """Split again with every piece, as requests' iter_lines does, eight megabytes
        in 512-byte pieces took minutes."""
        data = b"data: " + b"x" * (8 * 1024 * 1024) + b"\n"
        pieces = pieces_of(data, range(512, len(data), 512))
        started = time.monotonic()
        lines = list(read_lines(pieces))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([len(line) for line in lines], [len(data) - 1])


class TheStreamsReadThroughIt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"

    def test_a_plain_stream_stops_at_a_line_with_no_end_and_keeps_what_came(self):
        data = event("Hello") + b"data: " + b"x" * (4 * SMALL) + b"\n\n" + event("LATER")
        response = response_of(data + b"data: [DONE]\n\n")
        client = LLMClient(self.config)
        client._post = lambda *_a, **_k: response
        client.search = lambda *_a, **_k: ("", [], None)
        with mock.patch.object(streams, "MAX_LINE_BYTES", SMALL):
            text = client.stream("m", [{"role": "user", "content": "q"}])
        self.assertTrue(text.startswith("Hello\n\n_(Cut off: "), text)
        self.assertIn("a line of more than", text)
        self.assertNotIn("LATER", text)
        self.assertLess(response.raw.taken, 2 * SMALL, "nothing read on past the cap")

    def test_a_tee_streams_lines_are_hashed_and_capped_alike(self):
        data = event("Hi") + b"data: [DONE]\n\n"
        digest = hashlib.sha256()
        lines = list(LLMClient._hashed_lines(response_of(data), digest))
        self.assertEqual(lines, data.splitlines())
        self.assertEqual(digest.hexdigest(), hashlib.sha256(data).hexdigest())
        with mock.patch.object(streams, "MAX_LINE_BYTES", SMALL):
            endless = LLMClient._hashed_lines(response_of(b"x" * (4 * SMALL)),
                                              hashlib.sha256())
            with self.assertRaises(LineTooLong):
                list(endless)
            self.assertFalse(LLMClient._read_to_the_end(
                LLMClient._hashed_lines(response_of(b"x" * (4 * SMALL)),
                                        hashlib.sha256())))

    def test_a_sealed_stream_with_a_line_with_no_end_is_refused(self):
        class Opened:
            def iter_bytes(self):
                yield b'data: {"id":"c1"}\n\n'
                while True:
                    yield b"x" * 4096

        with mock.patch.object(streams, "MAX_LINE_BYTES", SMALL):
            lines = sealed.PrivateModeClient.lines(Opened())
            self.assertEqual(next(lines), b'data: {"id":"c1"}')
            self.assertEqual(next(lines), b"")
            with self.assertRaisesRegex(PrivacyError, "was not read on: .* with no end"):
                next(lines)


if __name__ == "__main__":
    unittest.main()
