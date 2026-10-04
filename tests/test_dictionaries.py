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
import types
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib, Gtk

from assistant import collector

# Windows are made and closed here while lookups and searches run on worker threads; a
# cycle collected on one of those would finalize a WebView there, and WebKit aborts.
collector.install()

from assistant import prompts
from assistant.config import Config
from assistant.reference import dictionaries, dsl, markup
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


def write_dsl(path, text=DSL_TEXT):
    with open(path, "wb") as f:
        f.write(b"\xff\xfe" + text.replace("\n", "\r\n").encode("utf-16-le"))


def write_dictzip(path, data, chunk=64):
    """A real dictzip: deflate flushed every `chunk` bytes, sizes in an RA field."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    sizes, body = [], b""
    for start in range(0, len(data), chunk):
        piece = compressor.compress(data[start:start + chunk])
        piece += compressor.flush(zlib.Z_FULL_FLUSH)
        if start + chunk >= len(data):
            piece += compressor.flush(zlib.Z_FINISH)
        sizes.append(len(piece))
        body += piece
    ra = struct.pack("<HHH", 1, chunk, len(sizes)) + struct.pack(f"<{len(sizes)}H", *sizes)
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


class DslHeadwords(unittest.TestCase):
    def test_optional_parts_are_found_with_and_without(self):
        self.assertEqual(dsl.spellings("(to) flimble {about}"), ["to flimble", "flimble"])

    def test_escaped_brackets_are_text(self):
        self.assertEqual(dsl.spellings(r"a\(b\) c"), ["a(b) c"])

    def test_braces_are_shown_but_not_indexed(self):
        self.assertEqual(dsl.display_headword("(to) flimble {about}"),
                         "(to) flimble about")
        self.assertEqual(dsl.unsorted_free("(to) flimble {about}"), "(to) flimble")


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

    def test_utf8_dsl(self):
        path = os.path.join(self.dir, "utf8.dsl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#NAME \"Eight\"\n\nwug\n\t[m1]a test creature\n")
        d = dsl.DSL(path)
        index = self._index(d)
        self.assertEqual(list(index), ["wug"])
        self.assertIn("a test creature", d.render("wug", d.read(*index["wug"]))[1])


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

    def test_without_sametypesequence_each_field_has_its_type(self):
        ifo = write_stardict(self.dir, "t", [("zeta", b"mplain\x00hb<b>bold</b>")],
                             types_="")
        d = StarDict(ifo)
        (word, offset, size), = d.headwords()
        self.assertEqual(d.render(word, d.read(offset, size))[0], "plainb<b>bold</b>")


class Folding(unittest.TestCase):
    def test_case_diacritics_and_spacing_do_not_matter(self):
        self.assertEqual(fold("  Café  au   Lait"), "cafe au lait")

    def test_stems_undo_common_inflections(self):
        self.assertIn("study", stems("studies"))
        self.assertIn("stop", stems("stopped"))
        self.assertIn("big", stems("biggest"))
        self.assertIn("make", stems("making"))
        self.assertIn("glorp", stems("glorps"))


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

    def test_paragraphs_and_list_items_end_where_a_browser_ends_them(self):
        self.assertEqual(self.balance("<p>one<p>two<ul><li>a<li>b</ul>"),
                         "<p>one</p><p>two</p><ul><li>a</li><li>b</li></ul>")

    def test_only_what_reaches_past_the_entry_goes(self):
        html = self.balance(
            '<style>body{display:none}</style><meta http-equiv="refresh" content="0">'
            '<span id="end" class="x" onclick="f()">kept</span>'
            '<a href="file:///etc">file</a> <a href="https://a.test/">web</a>'
            '<img src="pic.png">&lt;tag&gt;')
        self.assertEqual(html, '<span class="x">kept</span><a>file</a> '
                               '<a href="https://a.test/">web</a>&lt;tag&gt;')

    def test_a_colour_in_a_style_gets_a_dark_variant_too(self):
        html = self.balance('<span style="background-color:#fff; color: navy">x</span>')
        self.assertIn(f"--dk:{markup.dark_variant('navy')}", html)
        self.assertIn("data-dk", html)
        self.assertNotIn("data-dk", self.balance('<span style="color:#eee">x</span>'))

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
        self.assertIn("var(--dk)", markdown_html.page_css(dark=True))
        self.assertNotIn("var(--dk)", markdown_html.page_css(dark=False))


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
               web_search=None, system=None):
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
            window._on_decide_policy(None, decision,
                                     chat.WebKit2.PolicyDecisionType.NAVIGATION_ACTION)
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
