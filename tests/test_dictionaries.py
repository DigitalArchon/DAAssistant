"""Look up word: the DSL and StarDict readers, the index, and the dictionary window.

Every dictionary here is made up on the spot. No real dictionary text belongs in this
repository - the feature reads the user's own files where they are.
"""

import gzip
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib, Gtk

from assistant import collector

# Windows are made and closed here while lookups and searches run on worker threads; a
# cycle collected on one of those would finalize a WebView there, and WebKit aborts.
collector.install()

from assistant import prompts
from assistant.config import Config
from assistant.reference import dictionaries, dsl, markup, stardict
from assistant.reference import dictzip as dzmodule
from assistant.reference.dictionaries import Library, fold, stems
from assistant.reference.dictzip import DictzipFile
from assistant.reference.stardict import StarDict
from assistant.ui import chat, markdown_html

DSL_TEXT = (
    '#NAME "Invented Dictionary"\n'
    '#INDEX_LANGUAGE "English"\n'
    "\n"
    "glorp\n"
    "glorps\n"
    "\t[m0][b]glorp[/b] [p]n[/p]\n"
    "\t[m1]1. a small [i]imaginary[/i] creature; see <<flimble>>\n"
    "\t[m2][ex]The ~ sang.[/ex] {{a comment the reader never sees}}\n"
    "\t[m1][s]glorp.wav[/s]2. a sound \\[not a tag\\] [ref]zib[/ref]\n"
    "\n"
    "(to) flimble {about}\n"
    "\t[m1]to wander [c darkred]aimlessly\n"
    "\t[m2]across lines[/c] and back\n"
    "\n"
    "café\n"
    # U+0A20 then U+0100: bytes 20 0A 00 01, a newline and a NUL at an odd offset.
    "\t[m1]a place for coffee \u0a20\u0100 here\n"
    "zib\n"
    "\t[m1]last entry, no blank line after it"
)


def dsl_bytes(text=DSL_TEXT):
    """A DSL as Lingvo writes one: UTF-16LE after a byte-order mark, CRLF line ends."""
    return b"\xff\xfe" + text.replace("\n", "\r\n").encode("utf-16-le")


def write_dsl(path, text=DSL_TEXT):
    with open(path, "wb") as f:
        f.write(dsl_bytes(text))


def write_dictzip(path, data, chunk=64, claimed=None, dictd=False):
    """A real dictzip: deflate flushed every `chunk` bytes, sizes in an RA field - which
    says the chunks are `claimed` bytes long, if given, whatever they are. With `dictd`,
    laid out as dictd's dictzip writes one: the stream's last block after the last
    chunk, outside the table, rather than in it."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    sizes, body = [], b""
    for start in range(0, len(data), chunk):
        piece = compressor.compress(data[start:start + chunk])
        piece += compressor.flush(zlib.Z_FULL_FLUSH)
        if start + chunk >= len(data) and not dictd:
            piece += compressor.flush(zlib.Z_FINISH)
        sizes.append(len(piece))
        body += piece
    if dictd:
        body += compressor.flush(zlib.Z_FINISH)
    ra = (struct.pack("<HHH", 1, claimed or chunk, len(sizes))
          + struct.pack(f"<{len(sizes)}H", *sizes))
    extra = b"RA" + struct.pack("<H", len(ra)) + ra
    header = (b"\x1f\x8b\x08\x04" + b"\x00" * 4 + b"\x02\x03"
              + struct.pack("<H", len(extra)) + extra)
    trailer = struct.pack("<II", zlib.crc32(data) & 0xffffffff, len(data) & 0xffffffff)
    with open(path, "wb") as f:
        f.write(header + body + trailer)


def write_stardict(folder, stem, articles, types_="h", synonyms=(), compress=False,
                   name="Made-up StarDict"):
    """articles: [(headword, bytes)], written in the order given."""
    blob, idx = b"", b""
    for word, data in articles:
        idx += word.encode() + b"\x00" + struct.pack(">II", len(blob), len(data))
        blob += data
    base = os.path.join(folder, stem)
    if compress:
        write_dictzip(base + ".dict.dz", blob)
    else:
        with open(base + ".dict", "wb") as f:
            f.write(blob)
    with open(base + ".idx", "wb") as f:
        f.write(idx)
    if synonyms:
        with open(base + ".syn", "wb") as f:
            for word, index in synonyms:
                f.write(word.encode() + b"\x00" + struct.pack(">I", index))
    with open(base + ".ifo", "w") as f:
        f.write("StarDict's dict ifo file\nversion=2.4.2\n"
                f"bookname={name}\nwordcount={len(articles)}\n"
                f"idxfilesize={len(idx)}\nsametypesequence={types_}\n")
    return base + ".ifo"


class Dictzip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reads_any_span_across_chunk_boundaries(self):
        data = bytes(range(256)) * 9
        path = os.path.join(self.dir, "x.dz")
        write_dictzip(path, data, chunk=100)
        dz = DictzipFile(path)
        self.assertEqual(dz.chunk_length, 100)
        for offset, size in ((0, 10), (95, 10), (250, 300), (2290, 50)):
            self.assertEqual(dz.read(offset, size), data[offset:offset + size])
        self.assertEqual(dz.read_all(), data)

    def test_plain_gzip_without_the_ra_field_still_reads(self):
        path = os.path.join(self.dir, "y.dz")
        with gzip.open(path, "wb") as f:
            f.write(b"hello world")
        self.assertEqual(DictzipFile(path).read(6, 5), b"world")

    def test_without_it_any_span_is_read_from_the_nearest_place_kept(self):
        import random
        rng = random.Random(4)
        data = bytes(rng.randrange(40) for _ in range(300_000))
        path = os.path.join(self.dir, "plain.dz")
        # Two gzip members, one after the other, as gzip reads them: one file.
        with open(path, "wb") as f:
            f.write(gzip.compress(data[:120_000]) + gzip.compress(data[120_000:]))
        with mock.patch.object(dzmodule, "SPAN", 20_000), \
                mock.patch.object(dzmodule, "PIECE", 4096), \
                mock.patch.object(dzmodule, "STEP", 1024):
            dz = DictzipFile(path)
            self.assertIsNone(dz.chunk_length)
            for offset, size in ((0, 10), (299_990, 50), (119_990, 20), (50_000, 70_000),
                                 (5, 0), (400_000, 9), (150_000, 1)):
                self.assertEqual(dz.read(offset, size), data[offset:offset + size])
            self.assertGreater(len(dz._places), 10)
            self.assertEqual(dz.read_all(), data)
            self.assertIsNone(dz.cut_short)
            self.assertEqual(dz.read_all(most=1000), data[:1000])

    def test_opening_one_reads_its_head_and_no_more(self):
        # Settings and every Look up window open the dictionaries on the main loop: one
        # without the field was inflated whole there, and kept so.
        head = dsl_bytes('#NAME "Plain Gzip"\n\nglorp\n\t[m1]an entry\n')
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        deflated = (compressor.compress(head + b"\x00\x00" * 400_000)
                    + compressor.flush(zlib.Z_FULL_FLUSH))
        path = os.path.join(self.dir, "head.dsl.dz")
        with open(path, "wb") as f:      # what matters, then a block of no known type
            f.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + deflated + b"\x06" * 9)
        with mock.patch.object(dzmodule, "PIECE", 4096):
            d = dsl.DSL(path)
            self.assertEqual(d.name, "Plain Gzip")
            data = d._dz.read_all()
        self.assertTrue(data.startswith(head))
        self.assertIn("will not inflate", d._dz.cut_short)

    def test_nothing_unpacks_to_more_than_a_file_of_its_size_holds(self):
        head = dsl_bytes('#NAME "Bomb"\n\nglorp\n\t[m1]an entry\n')
        filler = b"\x00" * 1_000_000
        plain = os.path.join(self.dir, "bomb.dsl.dz")
        with open(plain, "wb") as f:
            f.write(gzip.compress(head + filler))
        chunked = os.path.join(self.dir, "chunked.dsl.dz")
        write_dictzip(chunked, head + filler, chunk=60000)
        with mock.patch.object(dzmodule, "RATIO", 2), \
                mock.patch.object(dzmodule, "FLOOR", 80_000), \
                mock.patch.object(dzmodule, "PIECE", 4096):
            for path in (plain, chunked):
                d = dsl.DSL(path)                  # its head is within the limit
                self.assertEqual(d.name, "Bomb")
                with self.assertRaises(dzmodule.TooLarge):
                    list(d.headwords())
        # A chunk that unpacks to more than its table says is not trusted either.
        path = os.path.join(self.dir, "lying.dz")
        write_dictzip(path, bytes(range(256)) * 4, chunk=128, claimed=64)
        with self.assertRaises(dzmodule.DictzipError):
            DictzipFile(path).read(0, 10)

    def test_dictds_own_layout_is_whole(self):
        # dictd's dictzip ends the deflate stream after the last chunk, outside the
        # table: every .dsl.dz it makes was reported cut short, though read whole.
        data = dsl_bytes('#NAME "Zipped"\n\n' + "".join(
            f"glorp{i}\n\t[m1]invented sense {i}\n" for i in range(300)))
        path = os.path.join(self.dir, "dictd.dsl.dz")
        write_dictzip(path, data, chunk=1000, dictd=True)
        dz = DictzipFile(path)
        self.assertEqual(bytes(dz.read_all()), data)
        self.assertIsNone(dz.cut_short)
        # Without its end it is cut short still, though every chunk is there.
        with open(path, "r+b") as f:
            f.truncate(os.path.getsize(path) - 8)
        dz = DictzipFile(path)
        self.assertEqual(bytes(dz.read_all()), data)
        self.assertIn("is cut short", dz.cut_short)

    def test_a_file_cut_short_reads_as_far_as_it_goes(self):
        import random
        rng = random.Random(2)
        data = bytes(rng.randrange(256) for _ in range(10_240))     # 11 chunks, none small
        path = os.path.join(self.dir, "short.dz")
        write_dictzip(path, data, chunk=1000)
        with open(path, "r+b") as f:
            f.truncate(os.path.getsize(path) - 2000)
        dz = DictzipFile(path)
        got = dz.read_all()
        self.assertGreaterEqual(len(got), 7000)
        self.assertEqual(len(got) % 1000, 0)
        self.assertEqual(bytes(got), data[:len(got)])
        self.assertIn("of 11", dz.cut_short)
        self.assertEqual(dz.read(1500, 100), data[1500:1600])
        # A read that runs into what is missing stops there; one that starts there has
        # nothing to give.
        self.assertEqual(dz.read(len(got) - 10, 50), data[len(got) - 10:len(got)])
        with self.assertRaises(dzmodule.DictzipError):
            dz.read(9500, 50)

    def test_a_header_that_does_not_end_is_no_gzip_file(self):
        path = os.path.join(self.dir, "named.dz")
        with open(path, "wb") as f:
            f.write(b"\x1f\x8b\x08\x08" + b"\x00" * 6 + b"n" * 200_000)
        started = time.monotonic()
        with self.assertRaises(dzmodule.DictzipError):
            DictzipFile(path)
        self.assertLess(time.monotonic() - started, 1)


class DslHeadwords(unittest.TestCase):
    def test_optional_parts_are_found_with_and_without(self):
        self.assertEqual(dsl.spellings("(to) flimble {about}"), ["to flimble", "flimble"])

    def test_escaped_brackets_are_text(self):
        self.assertEqual(dsl.spellings(r"a\(b\) c"), ["a(b) c"])

    def test_braces_are_shown_but_not_indexed(self):
        self.assertEqual(dsl.display_headword("(to) flimble {about}"),
                         "(to) flimble about")
        self.assertEqual(dsl.unsorted_free("(to) flimble {about}"), "(to) flimble")

    def test_parts_that_make_no_new_spelling_cost_nothing(self):
        # Thirty-two empty parts were tried all 2^32 ways, and indexing never ended.
        started = time.monotonic()
        self.assertEqual(dsl.spellings("word" + "()" * 32), ["word"])
        self.assertEqual(dsl.spellings("a()b(c)"), ["abc", "ab"])
        self.assertEqual(dsl.spellings("word" + "( )" * 32 + "(s)"), ["word s", "word"])
        many = dsl.spellings("w" + "(a)" * 32)
        self.assertLessEqual(len(many), dsl.MAX_VARIANTS)
        self.assertEqual(many[0], "w" + "a" * 32)
        self.assertLess(time.monotonic() - started, 1)

    def test_a_run_of_openers_never_closed_is_read_in_one_pass(self):
        started = time.monotonic()
        self.assertEqual(dsl.unsorted_free("{" * 20000 + " x"), "{" * 20000 + " x")
        self.assertEqual(dsl.spellings("(" * 400 + "x"), ["(" * 400 + "x"])
        self.assertEqual(dsl.spellings(r"\{{a} \(b) (c\)d)"), ["{ (b) c)d", "{ (b)"])
        # No headword is that long: it is not indexed at all.
        self.assertEqual(dsl.spellings("(" * 20000), [])
        self.assertLess(time.monotonic() - started, 1)


class DslFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "made-up.dsl")
        write_dsl(self.path)

    def test_the_annotation_beside_it_is_its_description(self):
        self.assertEqual(dsl.DSL(self.path).about(), "")
        # Lingvo's are UTF-16, often headed by a #LANGUAGE line.
        with open(os.path.join(self.dir, "made-up.ann"), "wb") as f:
            f.write('\ufeff#LANGUAGE "English"\nInvented Dictionary, 2nd ed.\n\n\n'
                    "© Nobody Ltd   2001\n".encode("utf-16-le"))
        self.assertEqual(dsl.DSL(self.path).about(),
                         "Invented Dictionary, 2nd ed.\n© Nobody Ltd 2001")

    def test_a_long_annotation_is_cut_to_a_tooltip(self):
        with open(os.path.join(self.dir, "made-up.ann"), "w") as f:
            f.write("word " * 400)
        about = dsl.DSL(self.path).about()
        self.assertLessEqual(len(about), markup.ABOUT_CHARS + 1)
        self.assertTrue(about.endswith("word…"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _index(self, dictionary):
        return {word: (offset, size) for word, offset, size in dictionary.headwords()}

    def test_name_comes_from_the_header(self):
        self.assertEqual(dsl.DSL(self.path).name, "Invented Dictionary")

    def test_every_headword_and_nothing_else_is_indexed(self):
        found = self._index(dsl.DSL(self.path))
        self.assertEqual(sorted(found), ["(to) flimble {about}", "café", "glorp", "glorps",
                                         "zib"])
        # Two headword lines, one entry.
        self.assertEqual(found["glorp"], found["glorps"])

    def test_an_entry_renders_its_markup(self):
        d = dsl.DSL(self.path)
        offset, size = self._index(d)["glorp"]
        html, text = d.render("glorp", d.read(offset, size))
        self.assertIn('<div class="hw">glorp · glorps</div>', html)
        self.assertIn("<i>imaginary</i>", html)
        self.assertIn(f'<a href="{markup.lookup_href("flimble")}">flimble</a>', html)
        self.assertIn(f'<a href="{markup.lookup_href("zib")}">zib</a>', html)
        self.assertIn("The glorp sang.", text)            # ~ is the headword
        self.assertIn("[not a tag]", text)
        self.assertNotIn("comment", html)
        self.assertNotIn("glorp.wav", html)
        self.assertIn('<div class="m2">', html)

    def test_a_tag_left_open_carries_to_the_next_line(self):
        d = dsl.DSL(self.path)
        offset, size = self._index(d)["(to) flimble {about}"]
        html, _text = d.render("flimble", d.read(offset, size))
        opening = f"<font{markup.coloured('darkred')}>"
        self.assertIn(f"{opening}aimlessly</font></div>", html)
        self.assertIn(f'<div class="m2">{opening}across lines</font>', html)

    def test_c_shows_the_colour_it_names_and_green_when_it_names_none(self):
        html, _text, _indent, _stack = dsl.render_line(
            "[c teal]named[/c] [c]plain[/c] [c red;x]odd[/c]", "w", [])
        self.assertIn('<font color="teal"', html)
        self.assertEqual(html.count('<font color="green"'), 2)
        self.assertNotIn("red;x", html)

    def test_a_sound_or_a_link_never_closed_leaves_its_text_shown(self):
        def shown(line):
            html, text, _indent, _stack = dsl.render_line(line, "w", [])
            return html, text

        self.assertEqual(shown("see [ref]house and the rest…"),
                         ("see house and the rest…", "see house and the rest…"))
        self.assertEqual(shown("[url]http://example.com and more"),
                         ("http://example.com and more", "http://example.com and more"))
        self.assertEqual(shown("[s]a.wav kept?"), ("a.wav kept?", "a.wav kept?"))
        # Closed, they are what they were; one opened inside another keeps the text the
        # outer one had so far.
        html, text = shown("[s]a.wav[/s]said [ref]zib[/ref], [ref]a [ref]b[/ref] c")
        self.assertEqual(text, "said zib, a b c")
        self.assertIn(f'<a href="{markup.lookup_href("zib")}">zib</a>', html)
        self.assertIn(f'a <a href="{markup.lookup_href("b")}">b</a> c', html)
        self.assertNotIn("a.wav", html)
        _html, text = shown("[s]one.wav [s]two.wav[/s] three")
        self.assertEqual(text, "one.wav  three")

    def _entry(self, text):
        path = os.path.join(self.dir, "entry.dsl")
        write_dsl(path, text)
        d = dsl.DSL(path)
        (word, offset, size), = d.headwords()
        return d.render(word, d.read(offset, size))

    def test_tags_left_open_line_after_line_cost_no_more_than_the_lines(self):
        # Every line reopened all the tags left open before it: eight thousand lines of
        # "[b]x" made 224 MB of HTML.
        started = time.monotonic()
        html, text = self._entry("glorp\n" + "\t[b]x\n" * 8000)
        self.assertLess(len(html), 2_000_000)
        self.assertEqual(html.count("<b>"), html.count("</b>"))
        self.assertEqual(text, "\n".join(["x"] * 8000))
        self.assertLess(time.monotonic() - started, 5)
        # Up to MAX_OPEN deep, what is open still carries to the next line.
        html, _text = self._entry("glorp\n\t[b][i][u]a\n\tb[/u][/i][/b] c\n")
        self.assertIn('<div class="m1"><b><i><u>b</u></i></b> c</div>', html)

    def test_an_entry_is_shown_as_far_as_its_html_may_go(self):
        with mock.patch.object(markup, "MAX_HTML", 2000):
            html, text = self._entry("glorp\n" + "\t[m1]a line of the entry\n" * 500)
            self.assertLess(len(html), 2200)
            self.assertTrue(html.endswith('<div class="m1">[…]</div>'), html[-80:])
            self.assertTrue(text.endswith("\n[…]"))
            # A line of ~ is the headword over and over: cut short within the line.
            html, text = self._entry("a-long-headword-indeed\n\t" + "~" * 5000 + "\n")
            self.assertLess(len(html), 2200)
            self.assertTrue(text.endswith("[…]"))

    def test_no_more_of_an_entry_is_read_than_any_entry_needs(self):
        d = dsl.DSL(self.path)
        with mock.patch.object(markup, "MAX_ENTRY", 10):
            # And a byte more, where there is more, for render() to tell it cut short.
            self.assertEqual(len(d.read(0, 10 ** 9)), 11)

    def test_an_entry_cut_short_where_its_reading_ends_says_so_too(self):
        # Cut where MAX_ENTRY ends what is read of it, short of MAX_HTML - as a UTF-16
        # entry of text is - an entry was shown without the "[…]" for the rest.
        path = os.path.join(self.dir, "entry.dsl")
        write_dsl(path, "glorp\n" + "\t[m1]a line of the entry\n" * 50)
        d = dsl.DSL(path)
        (word, offset, size), = d.headwords()
        whole = d.render(word, d.read(offset, size))
        with mock.patch.object(markup, "MAX_ENTRY", 400):
            html, text = d.render(word, d.read(offset, size))
            self.assertTrue(html.endswith('<div class="m1">[…]</div>'), html[-80:])
            self.assertTrue(text.endswith("\n[…]"), text[-80:])
            self.assertLess(text.count("a line of the entry"), 10)
        # Read to its end, where that is within the bound, it is shown whole.
        self.assertNotIn("[…]", whole[1])
        self.assertEqual(whole[1].count("a line of the entry"), 50)
        with mock.patch.object(markup, "MAX_ENTRY", size):
            self.assertEqual(d.render(word, d.read(offset, size)), whole)

    def test_markup_never_closed_is_read_in_one_pass(self):
        # Each took seconds to show, under the lock every lookup in every window waits
        # on: twenty thousand "<<" six of them.
        started = time.monotonic()
        for opener in ("{{", "<<", "[b ", "[c "):
            line = opener * 20000
            html, text, _indent, _stack = dsl.render_line(line, "w", [])
            self.assertEqual(text, line, opener)
            self.assertEqual(html, markup.escape(line), opener)
        self.assertLess(time.monotonic() - started, 2)
        # What does close is markup still, however much comes before it.
        html, text, _indent, _stack = dsl.render_line(
            "{{" * 500 + "}} <<" * 500 + "zib>> [b " * 500 + "x]bold[/b]", "w", [])
        self.assertEqual(text, " " + "}} <<" * 499 + "zib" + " " + "bold")
        self.assertIn("<b>bold</b>", html)

    def test_a_misaligned_newline_byte_pair_is_not_a_line(self):
        d = dsl.DSL(self.path)
        offset, size = self._index(d)["café"]
        _html, text = d.render("café", d.read(offset, size))
        self.assertIn("\u0a20\u0100 here", text)

    def test_the_last_entry_runs_to_the_end_of_the_file(self):
        d = dsl.DSL(self.path)
        offset, size = self._index(d)["zib"]
        self.assertIn("no blank line after it", d.render("zib", d.read(offset, size))[1])

    def test_a_dictzipped_dsl_reads_the_same(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        dz = os.path.join(self.dir, "packed.dsl.dz")
        write_dictzip(dz, raw, chunk=50)
        plain, packed = dsl.DSL(self.path), dsl.DSL(dz)
        self.assertEqual(self._index(plain), self._index(packed))
        offset, size = self._index(packed)["glorp"]
        self.assertEqual(packed.render("glorp", packed.read(offset, size)),
                         plain.render("glorp", plain.read(offset, size)))

    def test_headwords_that_would_not_end_are_indexed_and_their_neighbours_too(self):
        folder = os.path.join(self.dir, "hostile")
        os.makedirs(folder)
        write_dsl(os.path.join(folder, "hostile.dsl"),
                  '#NAME "Hostile"\n\n'
                  "glorp" + "()" * 32 + "\n\t[m1]found all the same\n"
                  + "x" * 5000 + "\n"
                  "zib\n\t[m1]after a line too long to be a headword\n")
        library = Library(types.SimpleNamespace(dictionaries_dir=folder,
                                                dictionaries_disabled=[]),
                          cache_dir=os.path.join(self.dir, "cache"))
        started = time.monotonic()
        entry, = library.look_up("glorp")["results"][0]["entries"]
        self.assertEqual(entry["text"], "found all the same")
        entry, = library.look_up("zib")["results"][0]["entries"]
        self.assertEqual(entry["text"], "after a line too long to be a headword")
        self.assertEqual(library.look_up("x" * 5000)["results"], [])
        self.assertLess(time.monotonic() - started, 5)

    def test_utf8_dsl(self):
        path = os.path.join(self.dir, "utf8.dsl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#NAME \"Eight\"\n\nwug\n\t[m1]a test creature\n")
        d = dsl.DSL(path)
        index = self._index(d)
        self.assertEqual(list(index), ["wug"])
        self.assertIn("a test creature", d.render("wug", d.read(*index["wug"]))[1])


CYRILLIC_DSL = (
    '#NAME "Выдуманный словарь"\n'
    '#INDEX_LANGUAGE "Russian"\n'
    '#CONTENTS_LANGUAGE "Russian"\n'
    '#SOURCE_CODE_PAGE "Cyrillic"\n'
    "\n"
    "глорп\n"
    "\t[m1]маленькое [i]выдуманное[/i] существо; см. <<флимбл>>\n"
    "флимбл\n"
    "\t[m1]бродить без цели\n"
)


class DslEncodings(unittest.TestCase):
    """UTF-16 either way round, UTF-8, and the 8-bit code pages of older dictionaries."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _entries(self, dictionary):
        return {word: dictionary.render(word, dictionary.read(offset, size))[1]
                for word, offset, size in dictionary.headwords()}

    def test_an_8_bit_dsl_is_read_in_the_code_page_it_names(self):
        path = self._write("cyrillic.dsl",
                           CYRILLIC_DSL.replace("\n", "\r\n").encode("cp1251"))
        d = dsl.DSL(path)
        self.assertEqual(d.encoding.name, "cp1251")
        self.assertEqual(d.name, "Выдуманный словарь")
        entries = self._entries(d)
        self.assertEqual(sorted(entries), ["глорп", "флимбл"])
        self.assertEqual(entries["глорп"], "маленькое выдуманное существо; см. флимбл")
        self.assertEqual(entries["флимбл"], "бродить без цели")

    def test_and_is_looked_up_like_any_other(self):
        folder = os.path.join(self.dir, "dicts")
        os.makedirs(folder)
        with open(os.path.join(folder, "cyrillic.dsl"), "wb") as f:
            f.write(CYRILLIC_DSL.encode("cp1251"))
        library = Library(types.SimpleNamespace(dictionaries_dir=folder,
                                                dictionaries_disabled=[]),
                          cache_dir=os.path.join(self.dir, "cache"))
        found = library.look_up("ГЛОРП")
        self.assertEqual(found["results"][0]["dictionary"], "Выдуманный словарь")
        entry, = found["results"][0]["entries"]
        self.assertIn("<i>выдуманное</i>", entry["html"])

    def test_lingvos_code_page_names(self):
        for name, codec in (("Latin", "cp1252"), ("Cyrillic", "cp1251"),
                            ("EasternEuropean", "cp1250"), ("Greek", "cp1253"),
                            ("Turkish", "cp1254"), ("Hebrew", "cp1255"),
                            ("Arabic", "cp1256"), ("Baltic", "cp1257"),
                            ("Vietnamese", "cp1258")):
            head = f'#NAME "x"\r\n#SOURCE_CODE_PAGE "{name}"\r\n\r\nw\r\n\t\xe9\r\n'
            self.assertEqual(dsl.detect(head.encode("latin-1")).name, codec, name)

    def test_one_that_names_no_code_page_is_latin(self):
        path = self._write("latin.dsl",
                           '#NAME "Latin One"\n\ncafé\n\ta place for coffee\n'
                           .encode("cp1252"))
        d = dsl.DSL(path)
        self.assertEqual(d.encoding.name, "cp1252")
        self.assertEqual(self._entries(d), {"café": "a place for coffee"})

    def test_utf8_stays_utf8_whatever_its_header_says(self):
        text = CYRILLIC_DSL.encode("utf-8")
        self.assertIs(dsl.detect(text), dsl.UTF8)
        self.assertIs(dsl.detect(b'#NAME "Plain"\n\nwug\n\ta word\n'), dsl.UTF8)

    def test_a_code_page_named_ahead_of_pages_of_ascii_is_believed(self):
        ascii_entries = "".join(f"word{i}\n\tplain sense {i}\n" for i in range(4000))
        text = ('#NAME "Late"\n#SOURCE_CODE_PAGE "Cyrillic"\n\n' + ascii_entries
                + "глорп\n\tвыдуманное\n")
        self.assertGreater(len(ascii_entries), dsl.SAMPLE)
        d = dsl.DSL(self._write("late.dsl", text.encode("cp1251")))
        self.assertEqual(d.encoding.name, "cp1251")
        self.assertEqual(self._entries(d)["глорп"], "выдуманное")

    def test_its_annotation_is_read_in_its_code_page_too(self):
        path = self._write("cyrillic.dsl", CYRILLIC_DSL.encode("cp1251"))
        self._write("cyrillic.ann",
                    "Выдуманный словарь, второе издание\n".encode("cp1251"))
        self.assertEqual(dsl.DSL(path).about(), "Выдуманный словарь, второе издание")

    def test_utf16_big_endian_without_a_byte_order_mark(self):
        path = self._write("big.dsl", DSL_TEXT.replace("\n", "\r\n").encode("utf-16-be"))
        d = dsl.DSL(path)
        self.assertIs(d.encoding, dsl.UTF16BE)
        self.assertEqual(d.name, "Invented Dictionary")
        entries = self._entries(d)
        self.assertEqual(sorted(entries), ["(to) flimble {about}", "café", "glorp",
                                           "glorps", "zib"])
        self.assertIn("The glorp sang.", entries["glorp"])
        self.assertIn("\u0a20\u0100 here", entries["café"])


class StarDictFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_ifo_says_what_the_dictionary_is_and_whose(self):
        ifo = write_stardict(self.dir, "h", [("alpha", b"a")])
        self.assertEqual(StarDict(ifo).about(), "")
        with open(ifo, "a") as f:
            f.write("description=An invented<br>dictionary.\nauthor=Nobody Press\n"
                    "website=https://example.com\n")
        self.assertEqual(StarDict(ifo).about(), "An invented\ndictionary.\n"
                                                "Author: Nobody Press\n"
                                                "Website: https://example.com")

    def test_html_articles_keep_their_markup_and_bword_links(self):
        ifo = write_stardict(self.dir, "h", [
            ("alpha", b'<b>alpha</b> <FONT color=\'#800000\'>noun</FONT> <FONT size=\'1\'>OE.'
                      b'</FONT> see <a href="bword://beta">beta</a>'
                      b'<script>bad()</script><img src="http://tracker/x.gif">'),
            ("beta", b"<i>beta</i>")], synonyms=[("first", 0)])
        d = StarDict(ifo)
        index = {w: (o, s) for w, o, s in d.headwords()}
        self.assertEqual(index["first"], index["alpha"])
        html, text = d.render("alpha", d.read(*index["alpha"]))
        self.assertIn(f'href="{markup.lookup_href("beta")}"', html)
        self.assertIn(f"<font{markup.coloured('#800000')}>noun</font>", html)
        self.assertIn('<font size="1">OE.</font>', html)
        self.assertNotIn("script", html)
        self.assertNotIn("tracker", html)
        self.assertEqual(text, "alpha noun OE. see beta")

    def test_pictures_come_inline_from_the_res_folder_and_only_from_there(self):
        os.mkdir(os.path.join(self.dir, "res"))
        with open(os.path.join(self.dir, "res", "1A.png"), "wb") as f:
            f.write(b"\x89PNG made up")
        with open(os.path.join(self.dir, "secret.png"), "wb") as f:
            f.write(b"outside")
        ifo = write_stardict(self.dir, "h", [
            ("alpha", b"<IMG src='1A.png' width='15'> one <img src='../secret.png'>"
                      b"<img src='missing.png'><img src='/etc/x.png'>")])
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        html, _text = d.render(word, d.read(offset, size))
        self.assertEqual(html.count("<img"), 1)
        self.assertIn('<img src="data:image/png;base64,iVBORyBtYWRlIHVw" width="15">',
                      html)

    def test_an_entry_brings_in_only_so_many_pictures(self):
        os.mkdir(os.path.join(self.dir, "res"))
        for i in range(5):
            with open(os.path.join(self.dir, "res", f"{i}.png"), "wb") as f:
                f.write(b"\x89PNG" + bytes([i]) * 3000)         # 4,030 characters inline
        ifo = write_stardict(self.dir, "h", [
            ("alpha", b"".join(b"<img src='%d.png'>" % i for i in range(5)) + b" text")])
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        with mock.patch.object(stardict, "ENTRY_PICTURES", 9000), \
                mock.patch.object(stardict, "PICTURES_KEPT", 9000):
            html, text = d.render(word, d.read(offset, size))
            self.assertEqual(html.count("<img"), 2)
            self.assertEqual(text, "text")
            self.assertLessEqual(len(d._pictures), 3)
            # Read again, the pictures it let go of are read again.
            self.assertEqual(d.render(word, d.read(offset, size)), (html, text))

    def test_an_entry_longer_than_any_is_shown_so_far_and_said_to_be(self):
        # Read to MAX_ENTRY, an entry was cut short there, and nothing said so.
        entry = b"<b>glorp</b> " + b"zib " * 100
        ifo = write_stardict(self.dir, "long", [("glorp", entry),
                                                ("flimble", b"short")])
        d = StarDict(ifo)
        index = {word: (offset, size) for word, offset, size in d.headwords()}
        with mock.patch.object(markup, "MAX_ENTRY", 100):
            html, text = d.render("glorp", d.read(*index["glorp"]))
            self.assertTrue(html.startswith("<b>glorp</b> zib zib"), html[:40])
            self.assertTrue(html.endswith(" zib<div>[…]</div>"), html[-40:])
            self.assertTrue(text.endswith(" zib\n[…]"), text[-40:])
            self.assertLess(text.count("zib"), 30)
            self.assertEqual(d.render("flimble", d.read(*index["flimble"])),
                             ("short", "short"))
        # One that ends where the bound does is shown whole.
        with mock.patch.object(markup, "MAX_ENTRY", len(entry)):
            html, text = d.render("glorp", d.read(*index["glorp"]))
        self.assertNotIn("[…]", html)
        self.assertEqual(text.count("zib"), 100)

    def test_what_an_entry_hides_is_not_shown_nor_given_to_the_model(self):
        ifo = write_stardict(self.dir, "h", [
            ("glorp", b'<b>glorp</b> <span style="display:none">glorps, glorped</span>'
                      b'a made-up word')])
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        self.assertEqual(d.render(word, d.read(offset, size)),
                         ("<b>glorp</b> a made-up word", "glorp a made-up word"))

    def test_a_nul_in_a_pictures_name_drops_the_picture_not_the_entry(self):
        os.mkdir(os.path.join(self.dir, "res"))
        ifo = write_stardict(self.dir, "h", [
            ("alpha", b"<b>kept</b> <img src='a%00.png'> <IMG SRC='b\x00.png'> too")])
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        html, text = d.render(word, d.read(offset, size))
        self.assertNotIn("<img", html)
        self.assertEqual(text, "kept too")

    def test_xdxf_and_plain_text_articles(self):
        ifo = write_stardict(self.dir, "x", [
            ("gamma", b"<k>gamma</k>\n<abr>n</abr> letter <kref>delta</kref>")],
            types_="x", compress=True)
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        html, text = d.render(word, d.read(offset, size))
        self.assertIn('<span class="dsl-p">n</span>', html)
        self.assertIn(f'href="{markup.lookup_href("delta")}"', html)
        self.assertEqual(text, "gamma\nn letter delta")

        ifo = write_stardict(self.dir, "m", [("eps", b"line one\nline <two>")],
                             types_="m")
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        html, text = d.render(word, d.read(offset, size))
        self.assertEqual(html, "line one<br>line &lt;two&gt;")

    def test_the_index_is_read_to_its_end_whatever_its_ifo_says_of_it(self):
        # As GoldenDict reads it. Read only as far as the .ifo's idxfilesize, an .idx
        # longer than a stale .ifo said lost every headword past that, unsaid; one
        # shorter than its .ifo says is read to its end, as it was.
        ifo = write_stardict(self.dir, "s", [("glorp", b"a glorp"), ("zib", b"a zib"),
                                             ("flimble", b"to flimble")], types_="m",
                             synonyms=[("zibs", 1), ("flimbles", 2)])
        stem = ifo[:-len(".ifo")]
        with open(stem + ".idx", "rb") as f:
            idx = f.read()
        for packed in (False, True):
            if packed:
                os.unlink(stem + ".idx")
                with open(stem + ".idx.gz", "wb") as f:
                    f.write(gzip.compress(idx))
            # The first headword's worth, and far more than there is.
            for claimed in (len(b"glorp\x00") + 8, 100 * len(idx)):
                with open(ifo, "w") as f:
                    f.write("StarDict's dict ifo file\nversion=2.4.2\nbookname=Stale\n"
                            f"idxfilesize={claimed}\nsametypesequence=m\n")
                d = StarDict(ifo)
                self.assertEqual([word for word, _offset, _size in d.headwords()],
                                 ["glorp", "zib", "flimble", "zibs", "flimbles"],
                                 (packed, claimed))
                self.assertEqual(d.problems, [])

    def test_an_index_longer_than_any_is_read_so_far_and_said_to_be(self):
        base = os.path.join(self.dir, "long")
        idx = b"".join(b"w%d\x00" % i + struct.pack(">II", 0, 1) for i in range(80_000))
        self.assertGreater(len(idx), 1 << 20)
        with open(base + ".dict", "wb") as f:
            f.write(b"x")
        with open(base + ".idx", "wb") as f:
            f.write(idx)
        with open(base + ".ifo", "w") as f:
            f.write("StarDict's dict ifo file\nversion=2.4.2\nbookname=Long\n"
                    f"idxfilesize={len(idx)}\nsametypesequence=m\n")
        with mock.patch.object(stardict, "MAX_INDEX", 1 << 20):
            d = StarDict(base + ".ifo")
            words = [word for word, _offset, _size in d.headwords()]
        self.assertTrue(70_000 < len(words) < 80_000, len(words))
        self.assertEqual(words[-1], f"w{len(words) - 1}")
        self.assertEqual(d.problems, ["its .idx goes on past 1 MB, further than any "
                                      "real one does, and is read no further"])

    def test_a_wide_index_leaves_out_what_no_file_has_and_unpacks_within_bounds(self):
        # 64-bit offsets, one of them past any file - which the index could not hold.
        base = os.path.join(self.dir, "wide")
        idx = (b"first\x00" + struct.pack(">QI", 0, 5)
               + b"bogus\x00" + struct.pack(">QI", 1 << 63, 5)
               + b"x" * 5000 + b"\x00" + struct.pack(">QI", 0, 5)
               + b"second\x00" + struct.pack(">QI", 5, 6))
        with open(base + ".dict", "wb") as f:
            f.write(b"firstsecond")
        with open(base + ".idx", "wb") as f:
            f.write(idx)
        with open(base + ".syn", "wb") as f:
            f.write(b"premier\x00" + struct.pack(">I", 0) + b"faux\x00" + struct.pack(">I", 1)
                    + b"deuxieme\x00" + struct.pack(">I", 3))
        with open(base + ".ifo", "w") as f:
            f.write("StarDict's dict ifo file\nversion=3.0.0\nbookname=Wide\n"
                    f"idxfilesize={len(idx)}\nidxoffsetbits=64\nsametypesequence=m\n")
        d = StarDict(base + ".ifo")
        self.assertEqual(list(d.headwords()), [("first", 0, 5), ("second", 5, 6),
                                               ("premier", 0, 5), ("deuxieme", 5, 6)])
        # Gzipped, the same; and an .idx.gz that unpacks as no index does is refused.
        os.unlink(base + ".idx")
        with open(base + ".idx.gz", "wb") as f:
            f.write(gzip.compress(idx))
        self.assertEqual(len(list(StarDict(base + ".ifo").headwords())), 4)
        bomb = (b"w\x00" + struct.pack(">QI", 0, 1)) * 600_000
        with open(base + ".idx.gz", "wb") as f:
            f.write(gzip.compress(bomb, 9))
        started = time.monotonic()
        with self.assertRaises(dzmodule.TooLarge):
            list(StarDict(base + ".ifo").headwords())
        self.assertLess(time.monotonic() - started, 2)
        # So is one in dictzip's layout, which was cut short at that bound, unsaid.
        write_dictzip(base + ".idx.gz", bomb, chunk=60000)
        with self.assertRaises(dzmodule.TooLarge):
            list(StarDict(base + ".ifo").headwords())

    def test_xdxf_pairs_never_closed_are_read_in_one_pass(self):
        # Twenty thousand <k> took four seconds: each searched the rest for its </k>.
        started = time.monotonic()
        for opener in ("<k>", "<kref>"):
            self.assertEqual(stardict.xdxf_html(opener * 20000), opener * 20000)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(stardict.xdxf_html("<k>a</k><k>b"), '<b class="hw">a</b><k>b')

    def test_without_sametypesequence_each_field_has_its_type(self):
        ifo = write_stardict(self.dir, "t", [("zeta", b"mplain\x00hb<b>bold</b>")],
                             types_="")
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        self.assertEqual(d.render(word, d.read(offset, size))[0], "plainb<b>bold</b>")


class UpperCaseNames(unittest.TestCase):
    """Dictionaries copied from Windows, where case does not matter: X.DSL.DZ beside
    X.ANN and X.BMP, and X.IFO beside X.IDX, X.DICT.DZ and X.SYN."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(self.folder)
        self.library = Library(types.SimpleNamespace(dictionaries_dir=self.folder,
                                                     dictionaries_disabled=[]),
                               cache_dir=os.path.join(self.dir, "cache"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_an_upper_case_dsl_dz_is_read_as_dictzip(self):
        path = os.path.join(self.folder, "MADE-UP.DSL.DZ")
        write_dictzip(path, dsl_bytes(), chunk=50)
        with open(os.path.join(self.folder, "MADE-UP.ANN"), "w") as f:
            f.write("Made up, in capitals.\n")
        with open(os.path.join(self.folder, "MADE-UP.BMP"), "wb") as f:
            f.write(b"BM")
        d = dsl.DSL(path)
        self.assertTrue(d.compressed)
        self.assertEqual(sorted(word for word, _offset, _size in d.headwords()),
                         ["(to) flimble {about}", "café", "glorp", "glorps", "zib"])
        self.assertEqual(d.about(), "Made up, in capitals.")
        self.assertEqual(d.icon, os.path.join(self.folder, "MADE-UP.BMP"))
        found = self.library.look_up("glorp")
        self.assertIn("imaginary", found["results"][0]["entries"][0]["text"])

    def test_one_without_a_name_is_named_after_its_file_in_any_case(self):
        path = os.path.join(self.folder, "NAMELESS.DSL")
        write_dsl(path, "wug\n\t[m1]a test creature")
        self.assertEqual(dsl.DSL(path).name, "NAMELESS")

    def test_an_upper_case_stardict_finds_its_files(self):
        write_stardict(self.folder, "sd", [("alpha", b"<b>alpha</b> first"),
                                           ("beta", b"b")],
                       synonyms=[("first", 0)], compress=True)
        for suffix in (".ifo", ".idx", ".dict.dz", ".syn"):
            os.rename(os.path.join(self.folder, "sd" + suffix),
                      os.path.join(self.folder, "SD" + suffix.upper()))
        d = StarDict(os.path.join(self.folder, "SD.IFO"))
        self.assertEqual(d.files(), [os.path.join(self.folder, name) for name in
                                     ("SD.IFO", "SD.IDX", "SD.DICT.DZ", "SD.SYN")])
        index = {word: (offset, size) for word, offset, size in d.headwords()}
        self.assertEqual(index["first"], index["alpha"])
        self.assertEqual(d.render("alpha", d.read(*index["alpha"]))[0],
                         "<b>alpha</b> first")
        found = self.library.look_up("first")
        self.assertEqual(found["results"][0]["dictionary"], "Made-up StarDict")


class Folding(unittest.TestCase):
    def test_case_diacritics_and_spacing_do_not_matter(self):
        self.assertEqual(fold("  Café  au   Lait"), "cafeaulait")
        self.assertEqual(fold("café au lait"), fold("CAFEAULAIT"))

    def test_typographic_apostrophes_quotes_and_dashes_are_the_plain_ones(self):
        # As text copied from a web page or a PDF has them.
        for apostrophe in "’‘ʼ′´`":
            self.assertEqual(fold(f"don{apostrophe}t"), fold("don't"), apostrophe)
        self.assertEqual(fold("o’clock"), fold("o'clock"))
        for dash in "‐‑‒–—−":
            self.assertEqual(fold(f"e{dash}mail"), fold("e-mail"), dash)
        self.assertEqual(fold("“quoted”"), fold('"quoted"'))

    def test_punctuation_and_spacing_do_not_matter(self):
        self.assertEqual(fold("e-mail"), fold("email"))
        self.assertEqual(fold("o'clock"), "oclock")
        self.assertEqual(fold("ice cream"), fold("ice-cream"))
        self.assertEqual(fold("e\u00admail"), "email")          # a soft hyphen
        self.assertEqual(fold("zero\u200bwidth"), "zerowidth")  # a zero-width space

    def test_letters_nfkd_leaves_whole_are_spelt_out(self):
        self.assertEqual(fold("Łódź"), fold("Lodz"))
        self.assertEqual(fold("encyclopædia"), fold("encyclopaedia"))
        self.assertEqual(fold("ŒUVRE"), "oeuvre")
        for letter, spelt in (("ø", "o"), ("đ", "d"), ("Đ", "d"), ("ħ", "h"),
                              ("ı", "i"), ("þ", "th"), ("Þ", "th"), ("ð", "d")):
            self.assertEqual(fold(f"x{letter}y"), f"x{spelt}y", letter)

    def test_letters_and_digits_of_every_script_stay(self):
        self.assertEqual(fold("Глорп-2"), "глорп2")
        self.assertEqual(fold("Ζώο"), "ζωο")
        self.assertEqual(fold("字典"), "字典")
        # A vowel sign is part of the word, not an accent on it.
        self.assertNotEqual(fold("काम"), fold("कम"))
        # Symbols stay, as in GoldenDict.
        self.assertEqual(fold("C++"), "c++")

    def test_marks_that_make_another_letter_count(self):
        # NFKD takes them off as it takes off an accent, and words that differ only by
        # them shared a key: glass and crow, my and my in the plural.
        for one, other in (("ガラス", "カラス"), ("ばか", "はか"), ("パン", "ハン"),
                           ("ぶどう", "ふとう"), ("мой", "мои"), ("ўсё", "усё")):
            self.assertNotEqual(fold(one), fold(other), one)
        self.assertEqual(fold("ГЛАВНЫЙ"), fold("главный"))
        self.assertEqual(fold("ｶﾞﾗｽ"), fold("ガラス"))            # half-width kana
        self.assertEqual(fold("ёж"), fold("еж"))                   # ё is often written е
        self.assertEqual(fold("Română"), fold("romana"))           # a Latin breve is an accent
        self.assertEqual(fold("й\u0323"), fold("й"))               # and so is a dot below
        # The iota subscript is a mark too, which case folding made a letter of.
        self.assertEqual(fold("ᾳ"), "α")
        self.assertEqual(fold("ᾼ"), "α")
        self.assertEqual(dictionaries.plain("Мой  ГЛАЗ"), "мой глаз")
        for word in ("ガラス", "Мой", "ᾼ", "ｶﾞﾗｽ", "Română"):
            self.assertEqual(fold(fold(word)), fold(word), word)
            self.assertEqual(fold(dictionaries.plain(word)), fold(word), word)

    def test_a_headword_of_punctuation_alone_keeps_a_key(self):
        self.assertEqual(fold("!"), "!")
        self.assertEqual(fold("?!"), "?!")
        self.assertEqual(fold("…"), fold("..."))
        self.assertEqual(fold("—"), fold("-"))
        self.assertEqual(fold("’"), fold("'"))
        self.assertEqual(fold("  "), "")

    def test_a_key_folds_to_itself(self):
        for word in ("don’t", "Łódź", "?!", "Café au lait", "Глорп-2"):
            self.assertEqual(fold(fold(word)), fold(word), word)
            self.assertEqual(fold(dictionaries.plain(word)), fold(word), word)

    def test_stems_undo_common_inflections(self):
        self.assertIn("study", stems("studies"))
        self.assertIn("stop", stems("stopped"))
        self.assertIn("big", stems("biggest"))
        self.assertIn("make", stems("making"))
        self.assertIn("glorp", stems("glorps"))
        # Spelt as written, for saying what was found instead.
        self.assertIn("ice cream", stems("Ice Creams"))
        self.assertIn("e-mail", stems("e-mails"))
        self.assertIn("john", stems("John’s"))


TYPOGRAPHY_DSL = (
    '#NAME "Typography"\n'
    "\n"
    "don't\n"
    "\t[m1]made-up sense of do not\n"
    "o'clock\n"
    "\t[m1]made-up sense of the hour\n"
    "email\n"
    "\t[m1]made-up sense of mail sent electronically\n"
    "Lodz\n"
    "\t[m1]made-up sense of a place\n"
    "encyclopaedia\n"
    "\t[m1]made-up sense of a reference work\n"
    "?!\n"
    "\t[m1]made-up sense of surprise\n"
    "we'll\n"
    "\t[m1]made-up sense of we will\n"
    "well\n"
    "\t[m1]made-up sense of in a good way\n"
)


class TypographyLookups(unittest.TestCase):
    """What is copied from a web page or a PDF finds what a dictionary spells plainly."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(self.folder)
        write_dsl(os.path.join(self.folder, "typography.dsl"), TYPOGRAPHY_DSL)
        self.config = types.SimpleNamespace(dictionaries_dir=self.folder,
                                            dictionaries_disabled=[])
        self.library = Library(self.config, cache_dir=os.path.join(self.dir, "cache"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def headwords(self, word):
        return [entry["headword"] for result in self.library.look_up(word)["results"]
                for entry in result["entries"]]

    def test_typographic_apostrophes_find_the_plain_ones(self):
        self.assertEqual(self.headwords("don’t"), ["don't"])
        self.assertEqual(self.headwords("o’clock"), ["o'clock"])
        self.assertEqual(self.headwords("dont"), ["don't"])

    def test_hyphens_and_letters_nfkd_leaves_alone_do_not_matter(self):
        self.assertEqual(self.headwords("e-mail"), ["email"])
        self.assertEqual(self.headwords("e–mail"), ["email"])
        self.assertEqual(self.headwords("Łódź"), ["Lodz"])
        self.assertEqual(self.headwords("encyclopædia"), ["encyclopaedia"])

    def test_the_stem_fallback_works_through_them_too(self):
        found = self.library.look_up("encyclopædias")
        self.assertEqual(found["found"], "encyclopaedia")
        self.assertTrue(found["results"])
        found = self.library.look_up("e-mails")
        self.assertEqual(found["found"], "e-mail")
        self.assertEqual([entry["headword"] for entry in found["results"][0]["entries"]],
                         ["email"])

    def test_and_so_do_the_suggestions(self):
        for typed, expected in (("e-ma", "email"), ("o’clo", "o'clock"),
                                ("encyclopæ", "encyclopaedia")):
            found = self.library.look_up(typed)
            self.assertEqual(found["results"], [], typed)
            self.assertIn(expected, found["suggestions"], typed)

    def test_a_headword_of_punctuation_alone_is_found(self):
        self.assertEqual(self.headwords("?!"), ["?!"])

    def test_a_word_is_not_found_under_one_that_differs_by_a_letters_mark(self):
        with open(os.path.join(self.folder, "marks.dsl"), "w", encoding="utf-8") as f:
            f.write('#NAME "Marks"\n\nмой\n\t[m1]made-up sense of my\n'
                    "мои\n\t[m1]made-up sense of my, in the plural\n"
                    "ガラス\n\t[m1]made-up sense of glass\n"
                    "カラス\n\t[m1]made-up sense of crow\n")
        self.assertEqual(self.headwords("МОЙ"), ["мой"])
        self.assertEqual(self.headwords("мои"), ["мои"])
        self.assertEqual(self.headwords("ｶﾞﾗｽ"), ["ガラス"])
        self.assertEqual(self.headwords("カラス"), ["カラス"])

    def test_a_words_own_entries_come_before_those_it_only_folds_to(self):
        self.assertEqual(self.headwords("well"), ["well", "we'll"])
        self.assertEqual(self.headwords("we’ll"), ["we'll", "well"])

    def test_an_index_made_the_old_way_is_built_again_once(self):
        def old_fold(word):
            return " ".join((word or "").casefold().split())

        with mock.patch.object(dictionaries, "SCHEMA", 2), \
                mock.patch.object(dictionaries, "fold", old_fold):
            self.library.look_up("email")
        statuses = []
        self.assertEqual([e["headword"] for r in self.library.look_up(
            "e-mail", statuses.append)["results"] for e in r["entries"]], ["email"])
        self.assertTrue(any(s.startswith("Indexing") for s in statuses))
        statuses.clear()
        self.library.look_up("e-mail", statuses.append)
        self.assertFalse(any(s.startswith("Indexing") for s in statuses))


class Lookups(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(os.path.join(self.folder, "sub"))
        write_dsl(os.path.join(self.folder, "made-up.dsl"))
        write_dsl(os.path.join(self.folder, "made-up_abrv.dsl"), '#NAME "Abbr"\n\nn\n\tnoun')
        write_stardict(os.path.join(self.folder, "sub"), "sd",
                       [("glorp", b"<b>Glorp</b>: a stardict view"),
                        ("cafe", b"coffee house")])
        self.config = types.SimpleNamespace(dictionaries_dir=self.folder,
                                            dictionaries_disabled=[])
        self.library = Library(self.config, cache_dir=os.path.join(self.dir, "cache"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_abbreviation_files_are_not_dictionaries(self):
        names = sorted(name for _path, name in self.library.available())
        self.assertEqual(names, ["Invented Dictionary", "Made-up StarDict"])

    def test_a_word_is_found_in_every_dictionary_with_it(self):
        statuses = []
        found = self.library.look_up("Glorp", statuses.append)
        self.assertEqual([r["dictionary"] for r in found["results"]],
                         ["Invented Dictionary", "Made-up StarDict"])
        self.assertTrue(any(s.startswith("Indexing") for s in statuses))
        self.assertEqual(found["dictionaries"], 2)
        # Indexed once: the second lookup builds nothing.
        statuses.clear()
        self.library.look_up("glorp", statuses.append)
        self.assertFalse(any(s.startswith("Indexing") for s in statuses))

    def test_folding_finds_accented_headwords(self):
        found = self.library.look_up("CAFE")
        self.assertEqual(len(found["results"]), 2)

    def test_an_optional_part_is_found_either_way(self):
        self.assertTrue(self.library.look_up("to flimble")["results"])
        self.assertTrue(self.library.look_up("flimble")["results"])

    def test_an_inflection_falls_back_to_its_stem(self):
        found = self.library.look_up("zibs")
        self.assertEqual(found["found"], "zib")
        self.assertTrue(found["results"])

    def test_nothing_found_suggests_nearby_headwords(self):
        found = self.library.look_up("gl")
        self.assertEqual(found["results"], [])
        self.assertIn("glorp", found["suggestions"])

    def test_a_disabled_dictionary_is_not_searched(self):
        path = os.path.join(self.folder, "sub", "sd.ifo")
        self.config.dictionaries_disabled = [path]
        found = self.library.look_up("glorp")
        self.assertEqual([r["dictionary"] for r in found["results"]],
                         ["Invented Dictionary"])

    def test_a_changed_file_is_indexed_again(self):
        self.library.look_up("glorp")
        path = os.path.join(self.folder, "made-up.dsl")
        write_dsl(path, DSL_TEXT + "\nnewword\n\t[m1]added later\n")
        os.utime(path, (1, 1))
        self.assertTrue(self.library.look_up("newword")["results"])

    def test_listing_does_not_wait_for_an_index_being_built(self):
        # A Look up window lists its dictionaries on the main loop as it opens. While
        # another window's first lookup was indexing, that waited for the index - 15
        # seconds for 2 GB of dictionaries - and the whole app froze.
        started, release = threading.Event(), threading.Event()
        headwords = dsl.DSL.headwords

        def slow(dictionary):
            started.set()
            release.wait(30)
            yield from headwords(dictionary)

        with mock.patch.object(dsl.DSL, "headwords", slow):
            looking = threading.Thread(target=self.library.look_up, args=("glorp",))
            looking.start()
            try:
                self.assertTrue(started.wait(10), "the indexing never started")
                listed = []
                lister = threading.Thread(target=lambda: listed.append(
                    (self.library.listing(), self.library.available())))
                lister.start()
                lister.join(5)
                self.assertFalse(lister.is_alive(), "listing waited for the indexing")
                listing, available = listed[0]
                self.assertEqual(sorted(name for _path, name, _icon, _about in listing),
                                 ["Invented Dictionary", "Made-up StarDict"])
                self.assertEqual(len(available), 2)
            finally:
                release.set()
                looking.join(30)
        self.assertEqual(len(self.library.look_up("glorp")["results"]), 2)

    def test_one_lookup_gathers_only_so_much_of_one_dictionary(self):
        write_dsl(os.path.join(self.folder, "many.dsl"), '#NAME "Many"\n\n' + "\n".join(
            f"zork\n\t[m1]sense {i} " + "x" * 60 for i in range(30)))
        entries = lambda: self.library.look_up("zork")["results"][0]["entries"]  # noqa: E731
        self.assertEqual(len(entries()), dictionaries.MAX_ENTRIES)
        with mock.patch.object(markup, "MAX_HTML", 300):
            self.assertEqual(len(entries()), 3)
        with mock.patch.object(dictionaries, "MAX_ROWS", 2):
            self.assertEqual([e["text"][:7] for e in entries()], ["sense 0", "sense 1"])

    def test_context_shares_its_budget_and_names_each_dictionary(self):
        found = self.library.look_up("glorp")
        context = dictionaries.as_context(found, 60)
        self.assertIn('Dictionary entries for "glorp"', context)
        self.assertIn("## Invented Dictionary", context)
        self.assertIn("## Made-up StarDict", context)
        self.assertIn("[…]", context)
        found = self.library.look_up("zibs")
        self.assertIn('(found under "zib")', dictionaries.as_context(found, 9999))

    def test_markdown_escapes_what_markdown_would_eat(self):
        found = self.library.look_up("glorp")
        text = dictionaries.as_markdown(found)
        self.assertIn("### Invented Dictionary", text)
        self.assertIn(r"\[not a tag\]", text)

    def test_markdown_makes_no_link_of_a_name_or_a_suggestion(self):
        from assistant.ui.markdown_text import INLINE
        trap = "[Oxford](https://phish.test/x)"
        found = {"word": "<w>", "found": "*w*", "dictionaries": 1, "suggestions": [],
                 "results": [{"dictionary": trap, "entries": [
                     {"headword": "w", "html": "", "text": "an entry"}]}]}
        nothing = dict(found, results=[], suggestions=[trap, "plain"])
        for text in (dictionaries.as_markdown(found), dictionaries.as_markdown(nothing)):
            self.assertNotIn(trap, text)
            links = [m.group(2) for m in INLINE.finditer(text) if m.group(1)]
            self.assertEqual(links, [], text)
        self.assertIn(r"### \[Oxford\](https://phish.test/x)",
                      dictionaries.as_markdown(found))
        self.assertIn(r"“\<w\>”; showing “\*w\*”", dictionaries.as_markdown(found))
        self.assertIn(r"Nearby: \[Oxford\](https://phish.test/x), plain",
                      dictionaries.as_markdown(nothing))


class ReplacedFiles(unittest.TestCase):
    """A dictionary replaced while the app runs - a newer edition copied over it - is
    read afresh, not through what was read of the old one as it opened."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(self.folder)
        self.config = types.SimpleNamespace(dictionaries_dir=self.folder,
                                            dictionaries_disabled=[])
        self.library = Library(self.config, cache_dir=os.path.join(self.dir, "cache"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _touch_all(self, when):
        for name in os.listdir(self.folder):
            os.utime(os.path.join(self.folder, name), (when, when))

    def test_a_dictzipped_dsl_comes_back_right(self):
        path = os.path.join(self.folder, "edition.dsl.dz")
        write_dictzip(path, dsl_bytes(), chunk=50)
        found = self.library.look_up("glorp")
        self.assertIn("imaginary", found["results"][0]["entries"][0]["text"])

        # Other entries, another name, and chunks of another size: read through the
        # old file's table of chunks, the new one gave garbage and zlib errors.
        second = '#NAME "Second Edition"\n\n' + "\n".join(
            f"word{i}\n\t[m1]sense {i} of the second edition" for i in range(40))
        write_dictzip(path, dsl_bytes(second), chunk=37)
        self._touch_all(2)
        found = self.library.look_up("word30")
        self.assertEqual([r["dictionary"] for r in found["results"]], ["Second Edition"])
        self.assertEqual(found["results"][0]["entries"][0]["text"],
                         "sense 30 of the second edition")
        self.assertEqual(self.library.look_up("glorp")["results"], [])
        self.assertEqual([name for _path, name in self.library.available()],
                         ["Second Edition"])

    def test_a_stardict_comes_back_right(self):
        write_stardict(self.folder, "sd", [("alpha", b"<b>first</b> edition")],
                       types_="m", compress=True, name="First Edition")
        found = self.library.look_up("alpha")
        self.assertIn("&lt;b&gt;first", found["results"][0]["entries"][0]["html"])

        # HTML now, not plain text, with more in its .dict.dz.
        write_stardict(self.folder, "sd", [("beta", b"filler " * 60),
                                           ("alpha", b"<b>second</b> edition")],
                       types_="h", compress=True, name="Second Edition")
        self._touch_all(2)
        found = self.library.look_up("alpha")
        self.assertEqual(found["results"][0]["dictionary"], "Second Edition")
        self.assertEqual(found["results"][0]["entries"][0]["html"],
                         "<b>second</b> edition")

    def test_one_that_would_not_open_is_tried_again_once_it_can(self):
        ifo = write_stardict(self.folder, "sd", [("alpha", b"a")])
        dict_path = ifo[:-len(".ifo")] + ".dict"
        with open(dict_path, "rb") as f:
            data = f.read()
        os.unlink(dict_path)
        self.assertEqual(self.library.available(), [])
        with open(dict_path, "wb") as f:
            f.write(data)
        os.utime(self.folder, (2, 2))
        self.assertEqual([name for _path, name in self.library.available()],
                         ["Made-up StarDict"])


class IndexProblems(unittest.TestCase):
    """What went wrong indexing a dictionary is filed with it in the index and said, and
    it is not tried again until its files change - not at every lookup, "(once only)"
    each time."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(self.folder)
        write_dsl(os.path.join(self.folder, "good.dsl"))
        self.config = types.SimpleNamespace(dictionaries_dir=self.folder,
                                            dictionaries_disabled=[])
        self.cache = os.path.join(self.dir, "cache")
        self.library = Library(self.config, cache_dir=self.cache)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def look_up(self, library, word="glorp"):
        statuses = []
        found = library.look_up(word, statuses.append)
        return found, [s for s in statuses if s.startswith("Indexing")]

    def about(self, library):
        return {name: about for _path, name, _icon, about in library.listing()}

    def test_one_that_cannot_be_indexed_is_not_tried_again_until_it_changes(self):
        bomb = os.path.join(self.folder, "bomb.dsl.dz")
        with open(bomb, "wb") as f:
            f.write(gzip.compress(dsl_bytes('#NAME "Bomb"\n\nglorp\n\t[m1]boom\n')
                                  + b"\x00" * 2_000_000, 9))
        with mock.patch.object(dzmodule, "FLOOR", 300_000):
            found, indexing = self.look_up(self.library)
            self.assertEqual([r["dictionary"] for r in found["results"]],
                             ["Invented Dictionary"])
            self.assertEqual(found["dictionaries"], 1)
            self.assertEqual(len(indexing), 2)
            self.assertEqual(self.look_up(self.library)[1], [])
            self.assertTrue(self.about(self.library)["Bomb"].startswith(
                "Not indexed: it would unpack to more than"), self.about(self.library))
            # Filed with the index, it is not tried again when the app starts again...
            again = Library(self.config, cache_dir=self.cache)
            self.assertEqual(self.look_up(again)[1], [])
            self.assertIn("Not indexed", self.about(again)["Bomb"])
            # ...until it changes.
            os.utime(bomb, (5, 5))
            self.assertEqual(self.look_up(again)[1], ["Indexing Bomb (1 of 2)… (once only)"])

    def test_one_cut_short_is_indexed_as_far_as_it_goes(self):
        import random
        rng = random.Random(3)
        text = '#NAME "Cut Short"\n\n' + "\n".join(
            f"word{i}\n\t[m1]sense {i}: " + "".join(rng.choice("abcdefghij") for _ in range(30))
            for i in range(300))
        path = os.path.join(self.folder, "short.dsl.dz")
        write_dictzip(path, dsl_bytes(text), chunk=1000)
        with open(path, "r+b") as f:          # missing its last two kilobytes
            f.truncate(os.path.getsize(path) - 2000)
        found, indexing = self.look_up(self.library, "word200")
        self.assertTrue(found["results"][0]["entries"][0]["text"].startswith("sense 200:"))
        self.assertEqual(len(indexing), 2)
        self.assertEqual(self.library.look_up("word299")["results"], [])
        self.assertEqual(self.look_up(self.library, "word3")[1], [])
        self.assertRegex(self.about(self.library)["Cut Short"],
                         r"^Indexed in part: read as far as it goes, [\d,]+ bytes unpacked: "
                         r"chunk \d+ unpacks to \d+ of its 1000 bytes, of \d+$")

    def test_a_headword_no_file_has_leaves_the_others_indexed(self):
        base = os.path.join(self.folder, "wide")
        idx = (b"glorp\x00" + struct.pack(">QI", 0, 4)
               + b"bogus\x00" + struct.pack(">QI", 1 << 63, 4))
        with open(base + ".dict", "wb") as f:
            f.write(b"wide")
        with open(base + ".idx", "wb") as f:
            f.write(idx)
        with open(base + ".ifo", "w") as f:
            f.write("StarDict's dict ifo file\nversion=3.0.0\nbookname=Wide\n"
                    f"idxfilesize={len(idx)}\nidxoffsetbits=64\nsametypesequence=m\n")
        found, _indexing = self.look_up(self.library)
        self.assertEqual([r["dictionary"] for r in found["results"]],
                         ["Invented Dictionary", "Wide"])
        self.assertEqual(self.look_up(self.library)[1], [])
        self.assertEqual(self.about(self.library)["Wide"],
                         "Indexed in part: 1 of its headwords are too long to be any, or "
                         "point past the end of any file, and are left out")

    def test_a_failure_not_of_the_files_is_tried_again_when_the_app_starts(self):
        with mock.patch.object(dsl.DSL, "headwords", side_effect=OSError("not just now")):
            found, indexing = self.look_up(self.library)
            self.assertEqual((found["results"], len(indexing)), ([], 1))
            self.assertEqual((found["dictionaries"], found["unusable"]),
                             (0, ["Invented Dictionary"]))
            self.assertEqual(self.look_up(self.library)[1], [])
            self.assertEqual(self.about(self.library)["Invented Dictionary"],
                             "Not indexed: OSError: not just now")
        found, indexing = self.look_up(Library(self.config, cache_dir=self.cache))
        self.assertEqual(len(indexing), 1)
        self.assertEqual(len(found["results"]), 1)


class TheIndexItself(unittest.TestCase):
    """The index is only a cache, of what the dictionaries' files hold."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(self.folder)
        write_dsl(os.path.join(self.folder, "made-up.dsl"))
        self.config = types.SimpleNamespace(dictionaries_dir=self.folder,
                                            dictionaries_disabled=[])
        self.cache = os.path.join(self.dir, "cache")
        self.index = os.path.join(self.cache, dictionaries.INDEX_FILE)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def look_up(self, word="glorp"):
        """A lookup by a library of its own, as at a start of the app, closed after."""
        statuses = []
        library = Library(self.config, cache_dir=self.cache)
        try:
            found = library.look_up(word, statuses.append)
        finally:
            if library._db is not None:
                library._db.close()     # all of it in the file, none in a -wal
        return found, [s for s in statuses if s.startswith("Indexing")]

    def test_one_that_is_no_database_is_set_aside_and_made_again(self):
        # Every lookup said "No dictionaries found - see Settings > Reference".
        os.makedirs(self.cache)
        with open(self.index, "wb") as f:
            f.write(bytes(range(256)) * 32)
        found, indexing = self.look_up()
        self.assertEqual([r["dictionary"] for r in found["results"]],
                         ["Invented Dictionary"])
        self.assertEqual(len(indexing), 1)
        self.assertTrue(os.path.exists(self.index + ".broken"))
        self.assertEqual(self.look_up()[1], [])

    def test_and_so_is_a_malformed_one(self):
        self.look_up()
        size = os.path.getsize(self.index)
        with open(self.index, "r+b") as f:     # its first page sound, the rest not
            f.seek(4096)
            f.write(bytes(range(256)) * ((size - 4096) // 256 + 1))
        found, indexing = self.look_up()
        self.assertEqual(len(found["results"]), 1)
        self.assertEqual(len(indexing), 1)
        self.assertEqual(self.look_up()[1], [])

    def test_what_it_has_of_a_dictionary_no_longer_there_goes(self):
        second = os.path.join(self.folder, "second.dsl")
        write_dsl(second, '#NAME "Second"\n\nglorp\n\t[m1]a second account')
        self.assertEqual(len(self.look_up()[0]["results"]), 2)
        os.unlink(second)
        self.assertEqual(len(self.look_up()[0]["results"]), 1)
        import sqlite3
        db = sqlite3.connect(self.index)
        try:
            self.assertEqual(db.execute("SELECT path FROM dictionaries").fetchall(),
                             [(os.path.join(self.folder, "made-up.dsl"),)])
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM headwords WHERE dictionary NOT IN "
                "(SELECT id FROM dictionaries)").fetchone(), (0,))
        finally:
            db.close()


class LinksOutOfTheFolder(unittest.TestCase):
    """A dictionary is downloaded, and an archive of one can carry links to the user's
    own files. None of them is read as any part of a dictionary."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(self.folder)
        self.secret = os.path.join(self.dir, "home", "secretdir")
        os.makedirs(self.secret)
        self.config_file = os.path.join(self.secret, "config.toml")
        with open(self.config_file, "wb") as f:
            f.write(b"api_key = 'never for the model'\n" * 4)
        self.library = Library(types.SimpleNamespace(dictionaries_dir=self.folder,
                                                     dictionaries_disabled=[]),
                               cache_dir=os.path.join(self.dir, "cache"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _link(self, target, name):
        os.symlink(os.path.relpath(target, self.folder), os.path.join(self.folder, name))

    def test_a_stardict_whose_dict_is_a_link_out_is_not_opened(self):
        ifo = write_stardict(self.folder, "sd", [("alpha", b"x" * 40)])
        os.unlink(ifo[:-len(".ifo")] + ".dict")
        self._link(self.config_file, "sd.dict")
        with self.assertRaises(ValueError):
            StarDict(ifo)
        self.assertEqual(self.library.available(), [])
        self.assertEqual(self.library.look_up("alpha")["results"], [])

    def test_nor_a_dsl_that_is_one_nor_an_annotation_or_icon(self):
        write_dsl(os.path.join(self.secret, "mine.dsl"), '#NAME "Not Mine"\n\nalpha\n\tb')
        self._link(os.path.join(self.secret, "mine.dsl"), "x.dsl")
        self.assertEqual(self.library.available(), [])
        path = os.path.join(self.folder, "real.dsl")
        write_dsl(path)
        self._link(self.config_file, "real.ann")
        self._link(self.config_file, "real.bmp")
        d = dsl.DSL(path)
        self.assertEqual(d.about(), "")
        self.assertIsNone(d.icon)
        self.assertEqual([name for _path, name in self.library.available()],
                         ["Invented Dictionary"])

    def test_nor_pictures_through_a_res_that_is_a_link_out(self):
        with open(os.path.join(self.secret, "1A.png"), "wb") as f:
            f.write(b"\x89PNG not the dictionary's")
        self._link(self.secret, "res")
        ifo = write_stardict(self.folder, "sd", [("alpha", b"<img src='1A.png'> a")])
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        self.assertEqual(d.render(word, d.read(offset, size))[0], " a")

    def test_links_within_the_folder_are_followed(self):
        os.makedirs(os.path.join(self.folder, "store"))
        ifo = write_stardict(os.path.join(self.folder, "store"), "sd",
                             [("alpha", b"<b>within</b>")])
        os.rename(ifo, os.path.join(self.folder, "sd.ifo"))
        for suffix in (".idx", ".dict"):
            self._link(ifo[:-len(".ifo")] + suffix, "sd" + suffix)
        found = self.library.look_up("alpha")
        self.assertEqual(found["results"][0]["entries"][0]["html"], "<b>within</b>")

    def test_but_a_file_beside_one_only_within_that_dictionarys_own_folder(self):
        # Within the folder chosen in Settings is not enough: a dictionary is one of
        # several there, each from somewhere else. Refused, its .dict was "missing".
        for sub in ("a", "b"):
            os.makedirs(os.path.join(self.folder, sub))
        ifo = write_stardict(os.path.join(self.folder, "a"), "sd", [("alpha", b"x")])
        stem = ifo[:-len(".ifo")]
        os.rename(stem + ".dict", os.path.join(self.folder, "b", "sd.dict"))
        os.symlink(os.path.join("..", "b", "sd.dict"), stem + ".dict")
        with self.assertRaisesRegex(ValueError, "its .idx or .dict file is missing, or is "
                                                "a link to a file outside its folder"):
            StarDict(ifo)
        self.assertEqual(self.library.available(), [])


class Ordering(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.folder = os.path.join(self.dir, "dicts")
        os.makedirs(self.folder)
        for stem, name in (("b", "Bravo"), ("a", "Alpha"), ("c", "Charlie")):
            write_dsl(os.path.join(self.folder, f"{stem}.dsl"),
                      f'#NAME "{name}"\n\nglorp\n\t[m1]said by {name}')
        with open(os.path.join(self.folder, "c.bmp"), "wb") as f:
            f.write(b"BM")
        self.config = types.SimpleNamespace(dictionaries_dir=self.folder,
                                            dictionaries_disabled=[],
                                            dictionaries_order=[])
        self.library = Library(self.config, cache_dir=os.path.join(self.dir, "cache"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def names(self, found):
        return [r["dictionary"] for r in found["results"]]

    def test_by_name_until_ordered_then_as_ordered(self):
        self.assertEqual(self.names(self.library.look_up("glorp")),
                         ["Alpha", "Bravo", "Charlie"])
        path = lambda stem: os.path.join(self.folder, f"{stem}.dsl")   # noqa: E731
        # Only some placed: the rest follow, by name.
        self.config.dictionaries_order = [path("c")]
        self.assertEqual(self.names(self.library.look_up("glorp")),
                         ["Charlie", "Alpha", "Bravo"])
        self.assertEqual([n for _p, n in self.library.available()],
                         ["Charlie", "Alpha", "Bravo"])

    def test_the_listing_carries_each_icon(self):
        icons = {name: icon for _path, name, icon, _about in self.library.listing()}
        self.assertEqual(icons["Charlie"], os.path.join(self.folder, "c.bmp"))
        self.assertIsNone(icons["Alpha"])

    def test_settings_moves_a_dictionary_and_saves_the_order(self):
        from assistant.ui.dialogs import SettingsDialog
        config = Config(os.path.join(self.dir, "c.json"))
        config.dictionaries_dir = self.folder
        dialog = SettingsDialog(None, config, type("C", (), {"config": config}))
        try:
            order = dialog.values()["dictionaries_order"]
            self.assertEqual([os.path.basename(p) for p in order], ["a.dsl", "b.dsl", "c.dsl"])
            last = dialog.dictionary_list.get_children()[2].get_child()
            dialog._move_dictionary(None, last, -1)
            dialog._move_dictionary(None, last, -1)
            dialog._move_dictionary(None, last, -1)          # already first: stays
            order = dialog.values()["dictionaries_order"]
            self.assertEqual([os.path.basename(p) for p in order], ["c.dsl", "a.dsl", "b.dsl"])
        finally:
            dialog.destroy()

    def _settings(self):
        from assistant.ui.dialogs import SettingsDialog
        config = Config(os.path.join(self.dir, "c.json"))
        config.dictionaries_dir = self.folder
        return SettingsDialog(None, config, type("C", (), {"config": config}))

    def _order(self, dialog):
        return [os.path.basename(p) for p in dialog.values()["dictionaries_order"]]

    def test_dragging_moves_a_dictionary_any_distance_in_one_go(self):
        dialog = self._settings()
        path = lambda stem: os.path.join(self.folder, f"{stem}.dsl")   # noqa: E731
        try:
            dialog._place_dictionary(path("c"), 0)         # last to first
            self.assertEqual(self._order(dialog), ["c.dsl", "a.dsl", "b.dsl"])
            dialog._place_dictionary(path("c"), 3)         # first to after the last
            self.assertEqual(self._order(dialog), ["a.dsl", "b.dsl", "c.dsl"])
            dialog._place_dictionary(path("a"), 2)         # between b and c
            self.assertEqual(self._order(dialog), ["b.dsl", "a.dsl", "c.dsl"])
            dialog._place_dictionary(path("a"), 1)         # onto itself: stays
            self.assertEqual(self._order(dialog), ["b.dsl", "a.dsl", "c.dsl"])
        finally:
            dialog.destroy()

    def test_a_drop_lands_before_or_after_the_row_it_is_over(self):
        dialog = self._settings()
        try:
            dialog.show_all()
            dialog.notebook.set_current_page(3)            # Reference, so it is laid out
            for _ in range(50):
                while Gtk.events_pending():
                    Gtk.main_iteration()
                if dialog.dictionary_list.get_children()[1].get_allocation().height > 1:
                    break
            middle = dialog.dictionary_list.get_children()[1].get_allocation()
            self.assertEqual(dialog._drop_index(middle.y + 2), 1)
            self.assertEqual(dialog._drop_index(middle.y + middle.height - 2), 2)
            self.assertEqual(dialog._drop_index(10 ** 6), 3)

            dropped = types.SimpleNamespace(
                get_data=lambda: os.path.join(self.folder, "c.dsl").encode())
            dialog._on_dictionary_dropped(dialog.dictionary_list, None, 0, middle.y + 2,
                                          dropped, 0, 0)
            self.assertEqual(self._order(dialog), ["a.dsl", "c.dsl", "b.dsl"])
        finally:
            dialog.destroy()

    def test_initials_stand_in_for_a_missing_icon(self):
        self.assertEqual(chat.dictionary_initials("Collins (En-En)"), "Co")
        self.assertEqual(chat.dictionary_initials("The New Oxford Thesaurus Dictionary"),
                         "OT")
        self.assertEqual(chat.dictionary_initials("WordNet® 3.0 (En-En)"), "Wo")


class LookupHtml(unittest.TestCase):
    def _lookup(self, **overrides):
        base = {"word": "w<x>", "found": "w<x>", "dictionaries": 3, "suggestions": [],
                "results": [{"dictionary": "D&D", "entries": [
                    {"headword": "w", "html": "<b>entry</b>", "text": "entry"}]}]}
        base.update(overrides)
        return base

    def test_names_are_escaped_and_entries_kept(self):
        html = markdown_html.lookup_html(self._lookup())
        self.assertIn("w&lt;x&gt;", html)
        self.assertIn("D&amp;D", html)
        self.assertIn('<div class="entry"><b>entry</b></div>', html)
        self.assertIn("1 of 3 dictionaries", html)
        self.assertTrue(html.startswith('<div class="turn lookup">'))

    def test_suggestions_are_lookup_links(self):
        word = 'a "b"'
        html = markdown_html.lookup_html(self._lookup(results=[], suggestions=[word]))
        self.assertIn(f'href="{markup.attr(markup.lookup_href(word))}"', html)
        self.assertIn("No entry for", html)

    def test_sanitise_allows_extra_link_schemes_only_when_asked(self):
        raw = '<a href="lookup:x">x</a>'
        self.assertNotIn("href", markdown_html.sanitise(raw))
        self.assertIn('href="lookup:x"',
                      markdown_html.sanitise(raw, ("http:", markup.LOOKUP_SCHEME)))

    def test_word_of_reads_back_only_lookup_links(self):
        self.assertEqual(markup.word_of(markup.lookup_href("café au lait")),
                         "café au lait")
        self.assertIsNone(markup.word_of("https://a.test/"))


class DictionaryHTML(unittest.TestCase):
    """A dictionary's own HTML: kept as written, but unable to run past its entry."""

    def balance(self, raw):
        return markup.balance(raw, ("https:", markup.LOOKUP_SCHEME))

    def test_tags_closed_out_of_order_are_closed_and_reopened(self):
        self.assertEqual(self.balance("<I><B>noun</I>.</B> rest"),
                         "<i><b>noun</b></i><b>.</b> rest")

    def test_whatever_is_left_open_is_closed_and_stray_closers_dropped(self):
        self.assertEqual(self.balance("</div><b>bold <font size=1>small</span>"),
                         '<b>bold <font size="1">small</font></b>')

    def test_end_tags_cost_no_more_than_what_is_open(self):
        # Each </i> was looked for down forty thousand <b>: 49 seconds.
        started = time.monotonic()
        html = self.balance("<i>" + "<b>" * 40000 + "x" + "</i>" * 40000)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(html.count("<b>"), html.count("</b>"))
        self.assertEqual(html.count("<i>"), html.count("</i>"))
        self.assertIn("x", html)
        # Nested deeper than MAX_DEPTH, the extra ones are let go, and so are their ends.
        depth = markup._Balancer.MAX_DEPTH
        self.assertEqual(self.balance("<span>" * 100 + "x" + "</span>" * 100 + "<b>y</b>"),
                         "<span>" * depth + "x" + "</span>" * depth + "<b>y</b>")

    def test_paragraphs_and_list_items_end_where_a_browser_ends_them(self):
        self.assertEqual(self.balance("<p>one<p>two<ul><li>a<li>b</ul>"),
                         "<p>one</p><p>two</p><ul><li>a</li><li>b</li></ul>")

    def test_what_reaches_past_the_entry_goes(self):
        html = self.balance(
            '<style>body{display:none}</style><meta http-equiv="refresh" content="0">'
            '<span id="end" class="x" onclick="f()">kept</span>'
            '<a href="file:///etc">file</a> <a href="https://a.test/">web</a>'
            '<img src="pic.png">&lt;tag&gt;')
        self.assertEqual(html, '<span>kept</span><a>file</a> '
                               '<a href="https://a.test/">web</a>&lt;tag&gt;')

    def test_only_how_its_text_looks_is_kept_of_an_entrys_attributes(self):
        # Over the whole window, this had every click anywhere open the link.
        html = self.balance(
            '<a href="https://phish.test/" style="position:fixed;top:0;left:0;'
            'width:100vw;height:100vh;z-index:99;color:navy !important;'
            'font-weight:bold">x</a>')
        self.assertEqual(html, '<a href="https://phish.test/" data-dk style="color:navy;'
                               f'font-weight:bold;--dk:{markup.dark_variant("navy")}">x</a>')
        # The window's own classes drew its badges and turns; the readers' own stay.
        self.assertEqual(self.balance('<span class="privacy ok">signed</span>'
                                      '<div class="turn user">asked</div>'
                                      '<span class="dsl-p x">n</span>'),
                         '<span>signed</span><div>asked</div><span class="dsl-p">n</span>')
        # Nothing that would fetch, whatever the page's policy lets through.
        html = self.balance(
            '<img src="data:image/png;base64,AA==" srcset="https://t.test/a 2x" '
            'width="15" height="99999" ping="https://t.test/" data-dk="1">'
            '<video poster="https://t.test/p.png"></video>'
            '<table background="https://t.test/b.png"><tr><td colspan="2">c</td></tr>'
            '</table><svg><a xlink:href="https://t.test/">s</a></svg>'
            '<span style="background:url(https://t.test/c.png);color:url(x);'
            'font-size:99em;font-family:\'Charis SIL\', serif;text-align:center;'
            'vertical-align:-999px;font-size:larger">t</span>')
        self.assertNotIn("t.test", html)
        self.assertIn('<img src="data:image/png;base64,AA==" width="15">', html)
        self.assertIn('<td colspan="2">c</td>', html)
        self.assertIn('<span style="font-family:&#x27;Charis SIL&#x27;, serif;'
                      'text-align:center;font-size:larger">t</span>', html)
        # What sets type stays as written.
        self.assertEqual(self.balance('<font size="-1" face="Doulos SIL" color="red">a'
                                      '</font><ol start="3" type="a"><li>b</ol>'
                                      '<p dir="rtl" lang="he" title="t" align="right">c'),
                         f'<font size="-1" face="Doulos SIL"{markup.coloured("red")}>a'
                         '</font><ol start="3" type="a"><li>b</li></ol>'
                         '<p dir="rtl" lang="he" title="t" align="right">c</p>')

    def test_what_a_browser_does_not_show_is_not_kept_or_told(self):
        # WebKit shows nothing of these; the text of them went to the model escaped.
        raw = ("a<noembed>&lt;b&gt;x</noembed> b<noframes><i>y</i></noframes> c"
               "<noscript>z</noscript>")
        self.assertEqual(self.balance(raw), "a b c")
        self.assertEqual(markup.html_to_text(raw), "a b c")

    def test_what_an_entry_hides_is_neither_shown_nor_told(self):
        # Keeping no display or visibility of a style, the balancer showed what an
        # entry hid, and its text went to the model with the rest - as it always had.
        html = self.balance(
            '<b>a</b> <span style="display:none">glorp</span>'
            '<div style="color:red; Visibility : Hidden !important">zib</div>'
            '<p hidden>flimble</p><i style="visibility:collapse">x</i>'
            '<br style="display:none"><img src="data:image/png;base64,AA==" '
            'style="display: NONE">b')
        self.assertEqual(html, "<b>a</b> b")
        self.assertEqual(markup.html_to_text(html), "a b")
        # What shows still keeps neither, nor any other display.
        self.assertEqual(self.balance('<span style="display:none;display:block;'
                                      'visibility:visible">a</span>'), "<span>a</span>")

    def test_a_hidden_element_ends_where_a_browser_ends_it(self):
        hide = ' style="display:none"'
        # With all it holds, to its own end tag, and no picture is read for it.
        asked = []
        self.assertEqual(markup.balance(f"<span{hide}>a<span>b</span>c<img src='1.png'>"
                                        f"</span>d<img{hide} src='2.png'>", (),
                                        asked.append), "d")
        self.assertEqual(asked, [])
        # A <p> ended by the next, an <li> by the next, one left open by the entry's
        # end - and one closed by a stray end tag and opened again is hidden again.
        self.assertEqual(self.balance(f"<p{hide}>a<p>b"), "<p>b</p>")
        self.assertEqual(self.balance(f"<ul><li{hide}>a<li>b</ul>"),
                         "<ul><li>b</li></ul>")
        self.assertEqual(self.balance(f"a<div{hide}>b<b>c"), "a")
        self.assertEqual(self.balance(f"<b>a<i{hide}>b</b>c</i>d"), "<b>a</b>d")

    def test_plaintext_xmp_and_listing_are_written_as_pre(self):
        # A browser never closes <plaintext>: let through, it took the rest of the page
        # - every entry and answer after it - for text.
        html = self.balance("<b>a</b><plaintext>x <i>y</i> &amp; z")
        self.assertTrue(html.startswith("<b>a</b><pre>"), html)
        self.assertTrue(html.endswith("</pre>"), html)
        for tag in ("plaintext", "xmp", "listing", "XMP"):
            html = self.balance(f"<{tag}>text</{tag}> after")
            self.assertNotIn(f"<{tag.lower()}", html)
            self.assertTrue(html.startswith("<pre>text"), html)
        self.assertEqual(self.balance("<p>one<listing>two\n<b>three</b></listing>"),
                         "<p>one</p><pre>two\n<b>three</b></pre>")

    @unittest.skipUnless("xmp" in getattr(HTMLParser, "CDATA_CONTENT_ELEMENTS", ()),
                         "this Python's HTML parser reads what is in <xmp> as markup")
    def test_what_plaintext_and_xmp_hold_is_text_escaped_once(self):
        self.assertEqual(self.balance("<b>a</b><plaintext>x <i>y</i> &amp; z"),
                         "<b>a</b><pre>x &lt;i&gt;y&lt;/i&gt; &amp;amp; z</pre>")
        # Escaped inside an <xmp>, which shows its text as it stands, it was shown
        # escaped: "&lt;b&gt;".
        self.assertEqual(self.balance("<xmp><b>x</b> &amp;</xmp> after"),
                         "<pre>&lt;b&gt;x&lt;/b&gt; &amp;amp;</pre> after")

    def test_a_colour_in_a_style_gets_a_dark_variant_too(self):
        html = self.balance('<span style="background-color:#fff; color: navy">x</span>')
        self.assertIn(f"--dk:{markup.dark_variant('navy')}", html)
        self.assertIn("data-dk ", html)
        self.assertNotIn("data-dk", self.balance('<span style="color:#eee">x</span>'))
        # Navy lifted to a light blue on a white box: as unreadable as it was dark.
        self.assertIn(f"--dkbg:{markup.dark_background('#fff')}", html)
        self.assertIn("data-dkbg", html)

    def test_a_pale_background_is_darkened_for_the_dark_theme(self):
        """A usage note's panel, #efefdf, kept as it was under the dark theme's own
        #e6e6e6 text: almost nothing to read (Nov 2026)."""
        html = self.balance('<div style="background-color:#efefdf">'
                            '<b>USAGE:</b> the h- is sounded</div>')
        self.assertEqual(html, '<div data-dkbg style="background-color:#efefdf;'
                               f'--dkbg:{markup.dark_background("#efefdf")}">'
                               '<b>USAGE:</b> the h- is sounded</div>')
        self.assertNotIn("data-dkbg", self.balance(
            '<div style="background-color:#1b1b1b">dark already</div>'))
        self.assertNotIn("data-dkbg", self.balance(
            '<div style="background-color:transparent">none</div>'))
        # A <font> keeps its own colour's attributes beside the background's.
        font = self.balance('<font color="navy" style="background-color:yellow">a</font>')
        self.assertIn("data-dkbg", font)
        self.assertIn(f"--dk:{markup.dark_variant('navy')}", font)

    def test_dark_backgrounds_keep_the_hue_and_carry_what_the_dark_theme_writes(self):
        import colorsys

        from PIL import ImageColor
        theme_text = (0xe6, 0xe6, 0xe6)
        lifted = [markup.dark_variant(c) for c in ("navy", "maroon", "#008000", "gray")]
        for colour in ("#efefdf", "white", "yellow", "beige", "#ffcc00", "lightblue",
                       "rgb(240, 240, 220)", "hsl(60, 30%, 90%)", "#336699"):
            dark = markup.dark_background(colour)
            rgb = tuple(int(dark[i:i + 2], 16) for i in (1, 3, 5))
            self.assertLessEqual(markup._luminance(rgb),
                                 markup._luminance(markup.DARK_BACKGROUND), colour)
            self.assertGreaterEqual(markup._contrast(rgb, theme_text), markup.CONTRAST)
            for text in lifted:
                text = tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))
                self.assertGreaterEqual(markup._contrast(rgb, text), markup.CONTRAST,
                                        (colour, text))
            before = colorsys.rgb_to_hls(*(c / 255 for c in ImageColor.getrgb(colour)[:3]))
            after = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
            if before[2] > 0.1:
                self.assertAlmostEqual(before[0], after[0], delta=0.03, msg=colour)
        for colour in ("black", "#1b1b1b", "transparent", "not a colour", ""):
            self.assertIsNone(markup.dark_background(colour), colour)

    def test_dark_variants_keep_the_hue_and_read_on_the_dark_theme(self):
        import colorsys
        for colour in ("#0000C0", "#008000", "#800000", "gray", "darkslategray"):
            dark = markup.dark_variant(colour)
            rgb = tuple(int(dark[i:i + 2], 16) for i in (1, 3, 5))
            self.assertGreaterEqual(markup._contrast(rgb, markup.DARK_BACKGROUND),
                                    markup.CONTRAST, colour)
            from PIL import ImageColor
            before = colorsys.rgb_to_hls(*(c / 255 for c in ImageColor.getrgb(colour)))
            after = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
            if before[2] > 0.1:
                self.assertAlmostEqual(before[0], after[0], delta=0.02)
        self.assertIsNone(markup.dark_variant("yellow"))     # already reads there
        self.assertIsNone(markup.dark_variant("not a colour"))

    def test_the_dark_theme_alone_reads_the_dark_variants(self):
        dark, light = markdown_html.page_css(dark=True), markdown_html.page_css(dark=False)
        for rule in ("var(--dk)", "var(--dkbg)", ".dict img { background-color: #ffffff"):
            self.assertIn(rule, dark)
            self.assertNotIn(rule, light)


class LookupsStayOutOfHistory(unittest.TestCase):
    def test_a_lookup_turn_is_not_sent_as_a_message(self):
        history = chat.history_for([
            {"role": "lookup", "content": "", "display": "x", "context": "c"},
            {"role": "user", "content": "what does it mean?"}])
        self.assertEqual(history, [{"role": "user", "content": "what does it mean?"}])


class StubClient:
    def __init__(self):
        self.calls = []

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None, **_kwargs):
        self.calls.append({"messages": list(messages), "system": system,
                           "web_search": web_search})
        return "It is made up."


class StubApp:
    def __init__(self, config, client):
        self.config, self.client, self.notices = config, client, []

    def notify(self, message, urgency="low"):
        self.notices.append(message)


class DictionaryWindow(unittest.TestCase):
    """The real ChatWindow, looking up in a made-up folder."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        folder = os.path.join(self.dir, "dicts")
        os.makedirs(folder)
        write_dsl(os.path.join(folder, "made-up.dsl"))
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "test"
        self.config.dictionaries_dir = folder
        self.library = Library(self.config, cache_dir=os.path.join(self.dir, "cache"))
        self.client = StubClient()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_until(self, predicate, timeout=20):
        """Run the main loop until `predicate()`, or `timeout` seconds. Both timers are
        removed however the loop ended: one left behind quits a later test's loop."""
        state = {"expired": False, "done": False}

        def expire():
            state["expired"] = True
            Gtk.main_quit()
            return False

        def poll():
            if predicate():
                state["done"] = True
                Gtk.main_quit()
                return False
            return True

        source = GLib.timeout_add_seconds(timeout, expire)
        poller = GLib.timeout_add(50, poll)
        Gtk.main()
        if not state["expired"]:
            GLib.source_remove(source)
        if not state["done"]:
            GLib.source_remove(poller)
        return not state["expired"] and (state["done"] or bool(predicate()))

    def _window(self, word="glorp"):
        return chat.ChatWindow(StubApp(self.config, self.client), "Look up", [],
                               web_search=False, system=prompts.lookup_system("English"),
                               lookup=self.library.look_up, lookup_word=word)

    def test_the_word_is_looked_up_and_a_question_carries_the_entries(self):
        window = self._window()
        try:
            self.assertTrue(self._run_until(lambda: window.conversation), "no lookup")
            turn = window.conversation[0]
            self.assertEqual(turn["role"], "lookup")
            self.assertIn("imaginary", turn["context"])
            self.assertEqual(window.lookup_entry.get_text(), "glorp")

            window.entry.get_buffer().set_text("Is it real?")
            window._send()
            self.assertTrue(self._run_until(lambda: window._cancel is None
                                            and len(window.conversation) == 3))
            call, = self.client.calls
            self.assertEqual(call["messages"], [{"role": "user", "content": "Is it real?"}])
            self.assertIn(prompts.LOOKUP_CONTEXT, call["system"])
            self.assertIn("a small imaginary creature", call["system"])
            self.assertFalse(call["web_search"])
            self.assertIn("## Look up: glorp", window._markdown())
        finally:
            window.destroy()

    def test_a_dictionary_switched_off_is_hidden_and_left_out_of_questions(self):
        second = os.path.join(self.dir, "dicts", "other.dsl")
        write_dsl(second, '#NAME "Second Opinion"\n\nglorp\n\t[m1]a rival account')
        with open(os.path.join(self.dir, "dicts", "other.ann"), "w") as f:
            f.write("A rival's account of glorp.\n")
        window = chat.ChatWindow(StubApp(self.config, self.client), "Look up", [],
                                 web_search=False, lookup=self.library.look_up,
                                 dictionary_list=self.library.listing, lookup_word="glorp")
        try:
            self.assertTrue(self._run_until(lambda: window.conversation), "no lookup")
            buttons = [child.get_child() for child in window.dictionary_bar.get_children()]
            self.assertEqual([b.get_tooltip_text() for b in buttons],
                             ["Invented Dictionary",
                              "Second Opinion\n\nA rival's account of glorp."])
            turn = window.conversation[0]
            self.assertIn("a rival account", turn["context"])

            buttons[1].set_active(False)
            self.assertNotIn("a rival account", turn["context"])
            self.assertNotIn("Second Opinion", turn["html"])
            self.assertIn("1 hidden", turn["html"])
            self.assertNotIn("Second Opinion", window._markdown())
            self.assertNotIn("a rival account", window._request_system())

            buttons[0].set_active(False)
            self.assertIn("hidden in this conversation", turn["html"])
            self.assertEqual(turn["context"], "")

            buttons[0].set_active(True)
            buttons[1].set_active(True)
            self.assertIn("a rival account", turn["context"])
            self.assertIn("imaginary", turn["context"])
        finally:
            window.destroy()

    def test_the_button_the_status_points_to_says_why_in_the_first_window(self):
        """The bar is made before the first lookup, which is what finds a dictionary
        that cannot be read; the status line then sends the reader to its button."""
        with mock.patch.object(dsl.DSL, "headwords", side_effect=OSError("not just now")):
            window = chat.ChatWindow(StubApp(self.config, self.client), "Look up", [],
                                     web_search=False, lookup=self.library.look_up,
                                     dictionary_list=self.library.listing,
                                     lookup_word="glorp")
            try:
                self.assertTrue(self._run_until(lambda: window.conversation), "no lookup")
                self.assertEqual(window.status.get_text(),
                                 "Invented Dictionary could not be read: its button says why")
                button, = [child.get_child()
                           for child in window.dictionary_bar.get_children()]
                self.assertEqual(button.get_tooltip_text(),
                                 "Invented Dictionary\n\nNot indexed: OSError: not just now")
            finally:
                window.destroy()

    def _looked_up(self, window, count):
        self.assertTrue(self._run_until(lambda: not window._looking and len(
            [t for t in window.conversation if t["role"] == "lookup"]) >= count))

    def _ask(self, window, question):
        window.entry.get_buffer().set_text(question)
        window._send()
        self.assertTrue(self._run_until(lambda: window._cancel is None and
                                        window.conversation[-1]["role"] == "assistant"))

    def test_each_word_has_a_page_of_its_own_with_its_questions(self):
        window = self._window()
        try:
            self._looked_up(window, 1)
            self.assertFalse(window.back_button.get_sensitive())
            self._ask(window, "Is it real?")
            self.assertEqual(len(window.conversation), 3)

            window._look_up("zib")
            self._run_until(lambda: window.conversation[0]["word"] == "zib")
            self.assertEqual([t["role"] for t in window.conversation], ["lookup"])
            self.assertNotIn("imaginary", window._request_system())
            self.assertTrue(window.back_button.get_sensitive())
            self.assertFalse(window.forward_button.get_sensitive())

            window._turn_page(-1)
            self.assertEqual(window.lookup_entry.get_text(), "glorp")
            self.assertEqual([t["role"] for t in window.conversation],
                             ["lookup", "user", "assistant"])
            self.assertIn("imaginary", window._request_system())
            self.assertTrue(window.forward_button.get_sensitive())
            window._turn_page(1)
            self.assertEqual(window.conversation[0]["word"], "zib")

            # A new word from the middle drops what was ahead of it.
            window._turn_page(-1)
            window._look_up("café")
            self._run_until(lambda: window.conversation[0]["word"] == "café")
            self.assertEqual([p[0]["word"] for p in window._pages], ["glorp", "café"])
        finally:
            window.destroy()

    def test_shift_enter_adds_a_word_to_the_page(self):
        from gi.repository import Gdk
        window = self._window()
        try:
            self._looked_up(window, 1)
            window.lookup_entry.set_text("zib")
            event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
            event.keyval, event.state = Gdk.KEY_Return, Gdk.ModifierType.SHIFT_MASK
            self.assertTrue(window._on_lookup_key(window.lookup_entry, event))
            self._looked_up(window, 2)
            self.assertEqual([t["word"] for t in window.conversation], ["glorp", "zib"])
            self.assertEqual(len(window._pages), 1)
            self.assertIn("last entry", window._request_system())
            self.assertIn("imaginary", window._request_system())
        finally:
            window.destroy()

    def _scroll(self, window):
        """(scrollY, the top of each lookup on the page), once the page has settled."""
        import json
        result = {}

        def got(view, res):     # _run_until notices; quitting here would leak its poll
            result["v"] = json.loads(view.run_javascript_finish(res).get_js_value()
                                     .to_string())

        self._run_until(lambda: window._loaded, timeout=10)
        self._run_until(lambda: False, timeout=1)
        window.webview.run_javascript(
            "JSON.stringify([Math.round(window.scrollY), Array.from("
            "document.querySelectorAll('.turn.lookup')).map(function(t){"
            "return Math.round(t.getBoundingClientRect().top + window.scrollY)})])",
            None, got)
        self._run_until(lambda: "v" in result, timeout=10)
        return result["v"]

    def test_a_lookup_is_shown_from_its_top_not_its_end(self):
        long_entry = "\n".join(f"\t[m1]sense {i} of a very long entry" for i in range(200))
        write_dsl(os.path.join(self.dir, "dicts", "long.dsl"),
                  f'#NAME "Long One"\n\nglorp\n{long_entry}\nzib\n{long_entry}')
        window = self._window()
        try:
            self._looked_up(window, 1)
            scroll, tops = self._scroll(window)
            self.assertEqual(scroll, 0)
            window._look_up("zib", add=True)
            self._looked_up(window, 2)
            scroll, tops = self._scroll(window)
            self.assertGreater(scroll, 0)
            self.assertLessEqual(abs(scroll - tops[1]), 2)    # the added word, from its top
        finally:
            window.destroy()

    def test_a_cross_reference_opens_a_page_of_its_own(self):
        window = self._window()
        try:
            self._looked_up(window, 1)

            class Navigation:
                def get_navigation_type(self):
                    return chat.WebKit2.NavigationType.LINK_CLICKED

                def get_request(self):
                    return types.SimpleNamespace(get_uri=lambda: markup.lookup_href("zib"))

            decision = types.SimpleNamespace(get_navigation_action=Navigation,
                                             ignore=lambda: None)
            window.guard._on_decide_policy(
                None, decision, chat.WebKit2.PolicyDecisionType.NAVIGATION_ACTION)
            self._run_until(lambda: window.conversation[0]["word"] == "zib")
            self.assertEqual(len(window._pages), 2)
        finally:
            window.destroy()

    def test_clear_starts_over_and_undo_brings_it_all_back(self):
        window = self._window()
        try:
            self._looked_up(window, 1)
            self._ask(window, "Is it real?")
            window._look_up("zib")
            self._run_until(lambda: window.conversation[0]["word"] == "zib")

            window._clear()
            self.assertEqual(window.conversation, [])
            self.assertEqual(window._pages, [[]])
            self.assertFalse(window.back_button.get_sensitive())
            self.assertNotIn(prompts.LOOKUP_CONTEXT, window._request_system() or "")
            self.assertTrue(window.undo_button.get_visible())

            window._undo_clear()
            self.assertEqual(window.conversation[0]["word"], "zib")
            self.assertEqual(len(window._pages), 2)
            self.assertTrue(window.back_button.get_sensitive())
            self.assertFalse(window.undo_button.get_visible())
        finally:
            window.destroy()

    def test_undo_lasts_until_something_new_happens_or_time_runs_out(self):
        window = self._window()
        try:
            self._looked_up(window, 1)
            window._clear()
            window._look_up("zib")
            self._looked_up(window, 1)
            self.assertIsNone(window._undo)
            self.assertFalse(window.undo_button.get_visible())

            window._clear()
            window._expire_undo()               # as the timer would, UNDO_SECONDS on
            window._undo_clear()                # too late: nothing comes back
            self.assertEqual(window.conversation, [])
            self.assertFalse(window.undo_button.get_visible())
        finally:
            window.destroy()

    def test_ctrl_l_clears_and_alt_arrows_turn_pages(self):
        from gi.repository import Gdk
        window = self._window()
        try:
            self._looked_up(window, 1)
            window._look_up("zib")
            self._run_until(lambda: window.conversation[0]["word"] == "zib")
            event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
            event.keyval, event.state = Gdk.KEY_Left, Gdk.ModifierType.MOD1_MASK
            self.assertTrue(window._on_window_key(window, event))
            self.assertEqual(window.conversation[0]["word"], "glorp")
            event.keyval, event.state = Gdk.KEY_l, Gdk.ModifierType.CONTROL_MASK
            self.assertTrue(window._on_window_key(window, event))
            self.assertEqual(window.conversation, [])
        finally:
            window.destroy()

    def test_clearing_a_capture_conversation_keeps_the_capture(self):
        window = chat.ChatWindow(StubApp(self.config, self.client), "Query image",
                                 [{"role": "user", "content": "q"},
                                  {"role": "assistant", "content": "a"}],
                                 image_base64="aW1hZ2U=")
        try:
            window.reading, window._reading_in_history = "a red box", True
            window._clear()
            self.assertEqual(window.image_base64, "aW1hZ2U=")
            self.assertEqual(window.reading, "a red box")
            # Out of the history with the turns, so it rides with the instructions.
            self.assertFalse(window._reading_in_history)
            window._undo_clear()
            self.assertTrue(window._reading_in_history)
            self.assertEqual(len(window.conversation), 2)
        finally:
            window.destroy()

    def test_clear_during_an_answer_stops_it_first(self):
        window = self._window(word=None)
        try:
            window.conversation.append({"role": "user", "content": "q"})
            window._cancel = threading.Event()
            window._streaming_text = "partial"
            window._clear()
            self.assertIsNone(window._cancel)
            self.assertEqual(window.conversation, [])
        finally:
            window.destroy()

    def test_only_the_latest_lookups_go_with_a_request(self):
        window = self._window(word=None)
        try:
            for i in range(chat.LOOKUPS_IN_CONTEXT + 1):
                window.conversation.append({"role": "lookup", "word": str(i),
                                            "context": f"entries {i}", "html": "",
                                            "display": ""})
            system = window._request_system()
            self.assertNotIn("entries 0", system)
            self.assertIn(f"entries {chat.LOOKUPS_IN_CONTEXT}", system)
            self.assertTrue(system.startswith(prompts.lookup_system("English")))
        finally:
            window.destroy()

    def test_a_lookup_waits_for_an_answer_in_progress(self):
        window = self._window(word=None)
        try:
            window._cancel = object()           # an answer is streaming
            window._look_up("glorp")
            self.assertFalse(window._looking)
            self.assertIn("Wait", window.status.get_text())
        finally:
            window._cancel = None
            window.destroy()


if __name__ == "__main__":
    unittest.main()
