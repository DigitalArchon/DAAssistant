"""Ask Encyclopedia: the DVD's tables, the import, search, the viewer's pages and enc://.

The DVD here is made up on the spot, in the DVD's own layout - zip archives of AxionDB
tables, the picture store, the media folders - with invented articles. No real
encyclopedia text belongs in this repository.
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
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib, Gtk

from assistant import collector

# Windows are made and closed here while lookups and searches run on worker threads; a
# cycle collected on one of those would finalize a WebView there, and WebKit aborts.
collector.install()

from assistant import prompts
from assistant.config import Config
from assistant.reference import axion, encyclopedia_html, ruffle
from assistant.reference.encyclopedia import (
    Encyclopedia, EncyclopediaError, byline, check_quotes, citation_of, cited,
    contributors_of, fts_query, import_dvd, query_terms, read_json,
    research_context, section_media, sections_of)
from assistant.ui import encscheme

GIF = b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00;"


def page(title, body):
    """A page the way the DVD's are laid out, furniture and all: the body ends in the
    "back to top" link whose id marks where it ends, then the citation box, with the
    gaps the DVD's browser filled in."""
    return (f"<html><head><title>{title}</title><script>furniture()</script></head>"
            f'<body><div id="headerPane"><a href="workspace:addItem">Add</a></div>'
            f'<div id="articleBody">{body}<a onclick="scrollToTop()" '
            f'href="javascript:void(0);" title="back to top" id="articleTop">Back to Top'
            f'</a></div><div id="cite"><h2 class="citationHead"><span class="text">'
            f'To cite this page:</span></h2><ul class="citations"><li><i>MLA Style:</i>'
            f'\r\n  &nbsp;\r\n  <b>"{title}\r\n  ." </b>The Made-up Encyclopedia. \r\n'
            f"  <u>__CITATION_PRODUCT_NAME__</u>.&nbsp;\r\n  Chicago:&nbsp;Made-up Press, "
            f"__CITATION_YEAR__.\r\n</li><li><i>APA Style:</i> &nbsp; <b>{title}. </b>"
            f"(__CITATION_YEAR__). The Made-up Encyclopedia. "
            f"<i>__CITATION_PRODUCT_NAME__.</i>&nbsp; Chicago:&nbsp;Made-up Press.</li></ul>"
            f'</div><div class="clear">&nbsp;</div></body></html>')


AUTHORS = ('<a class="authcr" href="ebcid:com.example.oec2.identifier.AuthorIdentifier?'
           'authorId=1">Ann Writer</a><a class="authcr" href="ebcid:com.example.oec2.'
           'identifier.AuthorIdentifier?authorId=2">Bo Scribe</a>')


ZORBLAX = page("Zorblax", (
    '<a id="9000001.toc"></a><h2>Introduction</h2><div class="h1"><p>'
    '<div class="assembly"><a class="asset" href="ebcid:com.example.oec2.identifier.'
    'AssemblyIdentifier?assemblyId=5&type=A"><img class="thumb" src="ebcid:com.example.'
    'oec2.identifier.ThumbBinaryIdentifier?binaryId=77" alt="Photograph:a zorblax" />'
    '<div class="clear">&nbsp;</div></a><ul class="captions"><li class="thumbcap">A zorblax '
    'at rest.</li></ul></div>a made-up <a href="ebcid:com.example.oec2.identifier.'
    'ArticleIdentifier?articleId=2&library=EB&title=quuxland#9000002.toc">quuxland</a> '
    'creature known for humming.</p></div>'
    '<h2 id="1.toc">Diet</h2><p>Zorblaxes eat moonberries <a href="ebcid:com.example.'
    'oec2.identifier.IndexEntryContentIdentifier?idxStructId=9" name="1.hook"></a>and '
    'nothing else.</p>' + AUTHORS +
    '<h2 id="2.toc">Migration</h2><p>Every winter they cross the glimmer sea. '
    '<a href="ebcid:com.example.oec2.identifier.MultimediaAssemblyIdentifier?'
    'assemblyId=5&type=A">Watch the crossing</a>.</p>'
    '<h2 id="3.toc">Additional Reading</h2><p>A. Author, <i>Zorblaxes</i> (2001).</p>'))
QUUXLAND = page("quuxland", "<p>A country of zorblaxes and moonberries.</p>")
STUDENT = page("Zorblax", "<p>Zorblaxes are small and hum a lot.</p>")
KIDS = page("moonberry", "<p>Moonberries are blue fruit that zorblaxes love.</p>")
PICTURE = page("Zorblax: at rest", (
    '<center><div><img src="ebcid:com.example.oec2.identifier.FullBinaryIdentifier?'
    'binaryId=77"><br><p>A zorblax at rest.</p><br><i>&copy; Made-up Photos</i></div>'
    "</center>"))
FILM = page("Zorblax: the crossing", (
    '<center><div><div id="embedTag"><embed height="240" width="320" '
    'src="file://MEDIA/12.flv" name="jwMediaPlayer"></embed></div><br><p>Zorblaxes '
    "crossing the glimmer sea.&crarr;(5 sec)</p><br><i>Made-up Films</i></div></center>"))
ANIMATION = page("Zorblax: humming", (
    '<embed src="file://MEDIA/13.swf"><p>How a zorblax hums.</p>'))


def write_table(root, group, path, values, compress=True):
    """An AxionDB table of {key: bytes}, unpacked under root/group, or zipped."""
    data, lobs = b"", b""
    for key, value in values.items():
        if compress:
            value = gzip.compress(value.encode() if isinstance(value, str) else value)
        data += struct.pack(">iBqi", key, 1, len(lobs), len(value))
        lobs += value
    name = path.rsplit("/", 1)[-1]
    return {f"{group}/{path}/{name}.data": data,
            f"{group}/{path}/lobs/CACHE_VALUE": lobs}


def make_dvd(root, articles=None):
    """A made-up DVD folder: what import_dvd reads, in the places it reads it."""
    articles = articles or {"EB": {1: ZORBLAX, 2: QUUXLAND}, "EBI": {101: STUDENT},
                            "EBK": {201: KIDS}}
    with zipfile.ZipFile(os.path.join(root, "ARTICLE_DATA.zip"), "w",
                         zipfile.ZIP_DEFLATED) as z:
        for library, pages in articles.items():
            for name, data in write_table(root, "ARTICLE_DATA", f"articledb/{library}",
                                          pages).items():
                z.writestr(name, data)
    with zipfile.ZipFile(os.path.join(root, "INSTALL_DATA.zip"), "w",
                         zipfile.ZIP_DEFLATED) as z:
        # Pictures' and films' assemblies are numbered separately: both have a 5.
        for table, values in (("ASSEMBLY", {5: PICTURE}),
                              ("MULTIMEDIAASSEMBLY", {5: FILM, 6: ANIMATION})):
            for name, data in write_table(root, "INSTALL_DATA", f"installdb/{table}",
                                          values).items():
                z.writestr(name, data)
    for name, data in write_table(root, "IMAGE_DATA", "imagedb/IMAGE", {77: GIF},
                                  compress=False).items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    media = os.path.join(root, "MEDIA2_DATA", "multimedia")
    os.makedirs(media)
    for name in ("12.flv", "13.swf"):
        with open(os.path.join(media, name), "wb") as f:
            f.write(b"FLV" if name.endswith(".flv") else b"FWS made-up")


class DvdCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dvd = os.path.join(self.dir, "dvd")
        os.makedirs(self.dvd)
        make_dvd(self.dvd)
        self.data = os.path.join(self.dir, "data")
        self.config = types.SimpleNamespace(encyclopedia_dir=self.dvd)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def imported(self):
        encyclopedia = Encyclopedia(self.config, data_dir=self.data)
        encyclopedia.import_from(self.dvd)
        return encyclopedia


class Collector(unittest.TestCase):
    def test_cycles_are_collected_on_the_main_loop_only(self):
        import gc
        collector.install()
        collector.install()                     # twice is once
        self.assertFalse(gc.isenabled())
        self.assertTrue(collector._collect())   # and it keeps running


class Tables(unittest.TestCase):
    def test_rows_are_fixed_width_and_absent_ones_skipped(self):
        data = struct.pack(">iBqi", 7, 1, 0, 3) + struct.pack(">iBqi", 8, 0, 0, 0)
        self.assertEqual(axion.parse_rows(data), [(7, 0, 3)])
        with self.assertRaises(axion.AxionError):
            axion.parse_rows(data[:-1])

    def test_a_table_reads_the_same_zipped_or_unpacked(self):
        root = tempfile.mkdtemp()
        try:
            files = write_table(root, "G", "db/T", {3: "three", 1: "one"})
            with zipfile.ZipFile(os.path.join(root, "G.zip"), "w") as z:
                for name, data in files.items():
                    z.writestr(name, data)
            zipped = dict(axion.Table(root, "G", "db/T").values())
            for name, data in files.items():
                os.makedirs(os.path.dirname(os.path.join(root, name)), exist_ok=True)
                with open(os.path.join(root, name), "wb") as f:
                    f.write(data)
            unpacked = axion.Table(root, "G", "db/T")
            self.assertIsNotNone(unpacked.lob_path())
            self.assertEqual(zipped, dict(unpacked.values()))
            self.assertEqual(axion.unpack(zipped[3]), "three")
            with self.assertRaises(axion.AxionError):
                axion.Table(root, "G", "db/Missing")
        finally:
            shutil.rmtree(root)


class Articles(unittest.TestCase):
    def test_sections_split_at_headings_and_leave_out_pictures_and_furniture(self):
        sections = sections_of(ZORBLAX)
        self.assertEqual([h for h, _t in sections],
                         ["Introduction", "Diet", "Migration", "Additional Reading"])
        intro = sections[0][1]
        self.assertEqual(intro, "a made-up quuxland creature known for humming.")
        self.assertNotIn("cite", " ".join(t for _h, t in sections))

    def test_contributors_end_their_section_as_a_byline(self):
        diet = dict(sections_of(ZORBLAX))["Diet"]
        self.assertEqual(diet, "Zorblaxes eat moonberries and nothing else.\n"
                               "By Ann Writer and Bo Scribe")
        self.assertEqual(contributors_of(ZORBLAX), ["Ann Writer", "Bo Scribe"])

    def test_bylines_read_as_english_and_long_ones_are_cut(self):
        self.assertEqual(byline([]), "")
        self.assertEqual(byline(["A"]), "By A")
        self.assertEqual(byline(["A", "B", "C"]), "By A, B and C")
        self.assertEqual(byline(["A", "B", "C", "D"], most=3), "By A, B, C and D")
        self.assertEqual(byline(list("ABCDEF"), most=3), "By A, B, C and 3 others")

    def test_the_back_to_top_link_the_body_is_cut_inside_is_not_text(self):
        # The end marker is that link's id, so the body ended in half a tag, which
        # every article's last section carried as text.
        last = sections_of(ZORBLAX)[-1][1]
        self.assertEqual(last, "A. Author, Zorblaxes (2001).")
        self.assertNotIn("<", " ".join(t for _h, t in sections_of(QUUXLAND)))

    def test_the_citation_box_drops_what_the_dvds_browser_filled_in(self):
        from assistant.reference.encyclopedia import text_of
        self.assertEqual(text_of(citation_of(ZORBLAX)),
                         "To cite this page:\n"
                         'MLA Style: "Zorblax." The Made-up Encyclopedia. Chicago: '
                         "Made-up Press.\n"
                         "APA Style: Zorblax. The Made-up Encyclopedia. Chicago: "
                         "Made-up Press.")
        self.assertEqual(citation_of("<p>no box</p>"), "")

    def test_query_terms_drop_the_asking(self):
        self.assertEqual(query_terms("Why did the Zorblax migrate, and how?"),
                         ["zorblax", "migrate"])
        self.assertEqual(fts_query(['a"b', "c"], any_of=True), '"a""b" OR "c"')


class Import(DvdCase):
    def test_everything_is_imported_and_counted(self):
        meta = import_dvd(self.dvd, os.path.join(self.data, "b.sqlite"))
        self.assertEqual((meta["EB"], meta["EBI"], meta["EBK"]), (2, 1, 1))
        self.assertEqual((meta["assembly"], meta["multimediaassembly"]), (1, 2))
        self.assertEqual((meta["images"], meta["media"]), (1, 2))

    def test_an_article_keeps_its_page_title_summary_and_picture(self):
        article = self.imported().article(1)
        self.assertEqual(article["title"], "Zorblax")
        self.assertEqual(article["library_name"], "Encyclopedia")
        self.assertEqual(article["image"], 77)
        self.assertEqual(article["summary"], "a made-up quuxland creature known for humming.")
        self.assertEqual(article["page"], ZORBLAX)
        self.assertEqual(article["sections"], 4)

    def test_pictures_and_films_with_the_same_number_are_both_kept(self):
        encyclopedia = self.imported()
        picture = encyclopedia.assembly(5)
        film = encyclopedia.assembly(5, "media")
        self.assertEqual((picture["image"], picture["caption"], picture["credit"]),
                         (77, "A zorblax at rest.", "© Made-up Photos"))
        self.assertEqual((film["media"], film["caption"]),
                         ("12.flv", "Zorblaxes crossing the glimmer sea. (5 sec)"))

    def test_pictures_and_films_are_read_from_the_dvd_folder(self):
        encyclopedia = self.imported()
        self.assertEqual(encyclopedia.image(77), (GIF, "image/gif"))
        self.assertIsNone(encyclopedia.image(78))
        self.assertTrue(encyclopedia.media_path("12.flv").endswith(
            os.path.join("MEDIA2_DATA", "multimedia", "12.flv")))
        self.assertIsNone(encyclopedia.media_path("../12.flv"))

    def test_a_failed_import_leaves_the_last_one_in_use(self):
        encyclopedia = self.imported()
        os.unlink(os.path.join(self.dvd, "INSTALL_DATA.zip"))
        with self.assertRaises(axion.AxionError):
            encyclopedia.import_from(self.dvd)
        self.assertEqual(encyclopedia.article(2)["title"], "quuxland")
        self.assertFalse(os.path.exists(encyclopedia.path + ".new"))
        self.assertIsNone(encyclopedia.importing)

    def test_a_cancelled_import_says_so_and_changes_nothing(self):
        cancel = threading.Event()
        cancel.set()
        encyclopedia = Encyclopedia(self.config, data_dir=self.data)
        with self.assertRaises(EncyclopediaError):
            encyclopedia.import_from(self.dvd, cancel=cancel)
        self.assertFalse(encyclopedia.available())

    def test_an_import_from_an_older_version_is_not_read(self):
        encyclopedia = self.imported()
        import sqlite3
        encyclopedia.close()
        db = sqlite3.connect(encyclopedia.path)
        db.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
        db.commit()
        db.close()
        self.assertFalse(encyclopedia.available())
        with self.assertRaises(EncyclopediaError):
            encyclopedia.article(1)


class Search(DvdCase):
    def test_an_exact_title_comes_first_in_every_edition(self):
        found = self.imported().search("zorblax")
        self.assertEqual([r["id"] for r in found[:2]], [1, 101])

    def test_of_several_titled_alike_the_longest_comes_first(self):
        root = tempfile.mkdtemp()
        try:
            make_dvd(root, {"EB": {7: page("Rome", "<p>A small town.</p>"),
                                   8: page("Rome", "<p>" + "A great city. " * 200 + "</p>")},
                            "EBI": {}, "EBK": {}})
            encyclopedia = Encyclopedia(types.SimpleNamespace(encyclopedia_dir=root),
                                        data_dir=os.path.join(root, "data"))
            encyclopedia.import_from(root)
            self.assertEqual([r["id"] for r in encyclopedia.search("rome")[:2]], [8, 7])
        finally:
            shutil.rmtree(root)

    def test_every_word_before_any_word(self):
        found = self.imported().search("moonberries quuxland")
        self.assertEqual(found[0]["id"], 2)            # has both
        self.assertIn(201, [r["id"] for r in found])   # has one, still offered

    def test_a_question_finds_the_article_by_its_section(self):
        found = self.imported().search("Where do zorblaxes migrate in winter?")
        self.assertEqual(found[0]["id"], 1)

    def test_nothing_to_search_for_finds_nothing(self):
        self.assertEqual(self.imported().search("why is it?"), [])


class Context(DvdCase):
    def test_a_short_article_goes_whole(self):
        text = self.imported().context(1, "what do they eat?")
        self.assertTrue(text.startswith('Article "Zorblax" from the Encyclopedia (2015):'))
        self.assertIn("## Diet\nZorblaxes eat moonberries and nothing else.", text)
        self.assertIn("## Migration", text)

    def test_a_long_one_keeps_its_introduction_and_what_matches(self):
        text = self.imported().context(1, "Where do they migrate?", budget=170)
        self.assertIn("## Introduction", text)
        self.assertIn("## Migration", text)
        self.assertNotIn("## Diet", text)
        self.assertIn("Sections left out: Diet; Additional Reading", text)


class Gather(DvdCase):
    def test_a_question_finds_its_sections_with_their_pictures_and_films(self):
        passages = self.imported().gather("Where do zorblaxes migrate in winter?")
        first = passages[0]
        self.assertEqual((first["title"], first["heading"]), ("Zorblax", "Migration"))
        self.assertEqual(first["text"], "Every winter they cross the glimmer sea. Watch "
                                        "the crossing.")
        film, = first["films"]
        self.assertEqual((film["assembly"], film["media"]), (5, "12.flv"))
        self.assertEqual(first["pictures"], [])
        intro = next(p for p in passages if p["heading"] == "Introduction"
                     and p["article"] == 1)
        self.assertEqual(intro["pictures"], [(5, 77, "A zorblax at rest.")])

    def test_passages_name_their_contributors_and_credit_their_pictures(self):
        passages = self.imported().gather("What do zorblaxes eat?")
        diet = next(p for p in passages if p["heading"] == "Diet")
        self.assertEqual(diet["contributors"], ["Ann Writer", "Bo Scribe"])
        intro = next(p for p in self.imported().gather("zorblax humming creature")
                     if p["heading"] == "Introduction" and p["article"] == 1)
        self.assertEqual(intro["credits"], {5: "© Made-up Photos"})
        film, = next(p for p in self.imported().gather("zorblaxes migrate winter")
                     if p["heading"] == "Migration")["films"]
        self.assertEqual(film["credit"], "Made-up Films")

    def test_reading_lists_are_never_passages(self):
        passages = self.imported().gather("zorblaxes author 2001 reading")
        self.assertNotIn("Additional Reading", [p["heading"] for p in passages])

    def test_a_follow_up_keeps_to_the_subject_before(self):
        passages = self.imported().gather("and what do they eat?",
                                          also="Tell me about the zorblax")
        self.assertEqual(passages[0]["heading"], "Diet")

    def test_planned_searches_and_titles_find_what_the_words_alone_would_not(self):
        encyclopedia = self.imported()
        self.assertEqual(encyclopedia.gather("what is the meaning of life?"), [])
        passages = encyclopedia.gather("what is the meaning of life?",
                                       queries=["moonberries"], titles=["quuxland"])
        self.assertEqual({p["title"] for p in passages} & {"quuxland", "Zorblax"},
                         {"quuxland", "Zorblax"})

    def test_the_audience_decides_which_encyclopedia_comes_first(self):
        encyclopedia = self.imported()
        adult = encyclopedia.gather("zorblax", titles=["Zorblax"])
        student = encyclopedia.gather("zorblax", titles=["Zorblax"], audience="student")
        self.assertEqual(adult[0]["library"], "EB")
        self.assertEqual(student[0]["library"], "EBI")
        self.assertIn("EB", [p["library"] for p in student])     # still there, later

    def test_what_was_found_already_is_not_found_again(self):
        encyclopedia = self.imported()
        first = encyclopedia.gather("zorblax moonberries winter")
        found = {(p["article"], p["section"]) for p in first}
        again = encyclopedia.gather("zorblax moonberries winter", exclude=found)
        self.assertFalse(found & {(p["article"], p["section"]) for p in again})

    def test_the_budget_is_shared_and_cuts_are_marked(self):
        passages = self.imported().gather("zorblax moonberries winter", budget=60)
        self.assertTrue(any(p["text"].endswith(" […]") for p in passages))

    def test_media_lines_up_with_sections(self):
        self.assertEqual(len(section_media(ZORBLAX)), len(sections_of(ZORBLAX)))

    def test_passages_are_numbered_for_citing(self):
        text = research_context([{"title": "T", "library_name": "L", "heading": "H",
                                  "text": "words"}])
        self.assertEqual(text, '[1] "T" (L), section "H":\nwords')

    def test_citations_are_read_back_in_any_form(self):
        self.assertEqual(cited("as [3] says, and [1, 2]; also [4-6] and [3] again"),
                         [3, 1, 2, 4, 5, 6])
        self.assertEqual(cited("no citations, just [brackets]"), [])


class Quotes(unittest.TestCase):
    PASSAGES = [{"text": "Every winter they cross the glimmer sea, humming as they go."},
                {"text": "Zorblaxes eat moonberries and nothing else."}]

    def check(self, answer):
        return [(c["quote"], c["cited"], c["found"], c["ok"])
                for c in check_quotes(answer, self.PASSAGES)]

    def test_a_quotation_found_where_it_is_cited(self):
        self.assertEqual(self.check('They "cross the glimmer sea" [1].'),
                         [("cross the glimmer sea", [1], 1, True)])

    def test_case_curly_quotes_dashes_and_spacing_do_not_matter(self):
        self.assertTrue(self.check('“Zorblaxes  EAT moonberries” [2]')[0][3])

    def test_an_ellipsis_marks_words_left_out_in_order(self):
        self.assertTrue(self.check('"Every winter … humming as they go" [1]')[0][3])
        self.assertFalse(self.check('"humming as they go … Every winter" [1]')[0][3])

    def test_found_but_under_the_wrong_number_is_not_ok(self):
        self.assertEqual(self.check('"eat moonberries and nothing else" [1]'),
                         [("eat moonberries and nothing else", [1], 2, False)])

    def test_made_up_and_uncited(self):
        self.assertEqual(self.check('It says "they fly to the moon" [2], and '
                                    '"eat moonberries and nothing" with no number.'),
                         [("they fly to the moon", [2], None, False),
                          ("eat moonberries and nothing", [], 2, True)])

    def test_a_word_or_two_in_quotes_is_a_term_not_a_quotation(self):
        self.assertEqual(self.check('the "Eternal City" [1]'), [])

    def test_a_models_json_is_found_wherever_it_put_it(self):
        self.assertEqual(read_json('Sure!\n```json\n{"enough": false, "queries": '
                                   '["a"]}\n```'), {"enough": False, "queries": ["a"]})
        self.assertEqual(read_json("no json here"), {})
        self.assertEqual(read_json('{"broken": } then {"ok": 1}'), {"ok": 1})


class Sources(unittest.TestCase):
    def passage(self, ident, title, pictures=(), films=()):
        return {"article": ident, "title": title, "library": "EB", "heading": "H",
                "pictures": list(pictures), "films": list(films)}

    def test_cited_first_the_rest_folded_and_their_pictures_and_films(self):
        from assistant.ui import markdown_html
        passages = [self.passage(1, "One", pictures=[(5, 77, "a <caption>")]),
                    self.passage(2, "Two", pictures=[(6, 78, "not cited")]),
                    self.passage(3, "Three", films=[{"assembly": 9, "title": "F",
                                                     "caption": "a film", "image": None,
                                                     "media": "9.flv"}])]
        html = markdown_html.sources_html(passages, [3, 1], 4)
        cited_part, folded = html.split("Also found (1)")
        self.assertIn('<li value="1"><a href="enc://cite/4/1">One</a>', cited_part)
        self.assertIn('<li value="3"><a href="enc://cite/4/3">Three</a>', cited_part)
        self.assertIn('<li value="2">', folded)
        self.assertIn('<a href="enc://assembly/5" title="a &lt;caption&gt;">'
                      '<img src="enc://image/77" alt="">a &lt;caption&gt;</a>', html)
        self.assertIn('href="enc://film/9"', html)
        self.assertNotIn("enc://image/78", html)       # not cited, no picture
        self.assertIn("2015", html)

    def test_contributors_and_credits_are_named(self):
        from assistant.ui import markdown_html
        from assistant.ui.encyclopedia_windows import sources_markdown
        one = dict(self.passage(1, "One", pictures=[(5, 77, "a zorblax")],
                                films=[{"assembly": 9, "title": "F", "caption": "a film",
                                        "credit": "Made-up Films", "image": None,
                                        "media": "9.flv"}]),
                   contributors=["Ann", "Bo", "Cy", "Di", "Ed"],
                   credits={5: "© Made-up Photos"})
        html = markdown_html.sources_html([one], [1], 0)
        self.assertIn('<span class="by" title="By Ann, Bo, Cy, Di and Ed">'
                      "By Ann, Bo, Cy and 2 others</span>", html)
        self.assertIn('title="a zorblax — © Made-up Photos"', html)
        self.assertIn('<span class="credit">© Made-up Photos</span>', html)
        self.assertIn('<span class="credit">Made-up Films</span>', html)
        text = sources_markdown([one], [1], None)
        self.assertIn("One › H - Encyclopedia · By Ann, Bo, Cy and 2 others", text)
        self.assertIn("a zorblax (© Made-up Photos)", text)
        self.assertIn("▶ a film (Made-up Films)", text)

    def test_answers_saved_before_credits_were_kept_still_show(self):
        from assistant.ui import markdown_html
        from assistant.ui.encyclopedia_windows import sources_markdown
        old = self.passage(1, "One", pictures=[(5, 77, "a zorblax")])
        self.assertNotIn("class=\"by\"", markdown_html.sources_html([old], [1], 0))
        self.assertIn("a zorblax", sources_markdown([old], [1], None))
        # Saved as JSON, the credits come back keyed by string.
        saved = dict(old, credits={"5": "© Made-up Photos"})
        self.assertIn("© Made-up Photos", markdown_html.sources_html([saved], [1], 0))

    def test_the_quotation_check_and_how_little_was_found(self):
        from assistant.ui import markdown_html
        checks = [{"quote": "found it", "cited": [1], "found": 1, "ok": True},
                  {"quote": "made <up>", "cited": [1], "found": None, "ok": False}]
        html = markdown_html.sources_html([self.passage(1, "One")], [1], 2, checks,
                                          limited=True)
        self.assertIn("1 of 2 found word for word", html)
        self.assertIn("⚠ “made &lt;up&gt;” cited as [1]: not found in any passage", html)
        self.assertIn("switch Strict off", html)

    def test_an_answer_citing_nothing_shows_every_source(self):
        from assistant.ui import markdown_html
        html = markdown_html.sources_html([self.passage(1, "One")], [], 0)
        self.assertNotIn("Also found", html)
        self.assertNotIn("switch Strict off", html)
        self.assertIn('<li value="1">', html)


class ViewerPages(unittest.TestCase):
    def media(self, name):
        return {"12.flv": "/dvd/MEDIA2_DATA/multimedia/12.flv",
                "13.swf": "/dvd/x/13.swf", "9.mp3": "/dvd/9 a.mp3"}.get(name)

    def test_links_become_the_viewers_own(self):
        ebcid = "ebcid:com.example.oec2.identifier."
        link_for = encyclopedia_html.link_for
        self.assertEqual(link_for(f"{ebcid}ArticleIdentifier?articleId=12&library=EB"),
                         "enc://article/12")
        self.assertEqual(link_for(f"{ebcid}AssemblyIdentifier?assemblyId=5"),
                         "enc://assembly/5")
        self.assertEqual(link_for(f"{ebcid}MultimediaAssemblyIdentifier?assemblyId=5"),
                         "enc://film/5")
        self.assertIsNone(link_for(f"{ebcid}AuthorIdentifier?authorId=1"))
        self.assertIsNone(link_for("javascript:void(0)"))
        self.assertEqual(link_for("https://a.test/x"), "https://a.test/x")
        # The DVD's own search pages, which went with the product's website.
        self.assertIsNone(link_for("http://cdsearch.example.com/?q=zorblax"))

    def test_an_article_keeps_its_text_pictures_and_links(self):
        html, films = encyclopedia_html.render(
            encyclopedia_html.body_of(ZORBLAX), self.media)
        self.assertIn('<figure class="assembly"><a href="enc://assembly/5" class="asset">'
                      '<img src="enc://image/77" alt="Photograph:a zorblax"></a>'
                      "<figcaption><span>A zorblax at rest.</span> </figcaption></figure>",
                      html)
        self.assertIn('<a href="enc://article/2">quuxland</a>', html)
        self.assertIn('<a href="enc://film/5" class="asset">Watch the crossing</a>', html)
        self.assertIn("moonberries and nothing else", html)
        self.assertNotIn("&nbsp;", html)
        self.assertNotIn("\xa0", html)
        self.assertNotIn("ebcid", html)
        self.assertNotIn("hook", html)
        self.assertEqual(films, 0)

    def test_an_article_ends_with_its_bylines_and_how_to_cite_it(self):
        html = encyclopedia_html.article_body({"page": ZORBLAX}, self.media)
        self.assertIn('nothing else.</p><p class="contributors">By Ann Writer and Bo Scribe'
                      "</p>", html)
        cite = html[html.index('<section class="cite">'):]
        self.assertIn("To cite this page:", cite)
        self.assertIn("The Made-up Encyclopedia.", cite)
        self.assertNotIn("__CITATION", html)
        self.assertNotIn("back to top", html)

    def test_scripts_and_furniture_go(self):
        html, _films = encyclopedia_html.render(
            '<p>kept<script>alert(1)</script></p><form><input value="x">gone</form>'
            '<a href="annotate:viewNotes">notes</a><img src="img/icon.png">', self.media)
        self.assertEqual(html, "<p>kept</p>notes")

    def test_films_play_in_place_and_animations_open_their_own_window(self):
        html, films = encyclopedia_html.render(
            '<embed src="file://MEDIA/12.flv"><embed src="file://MEDIA/9.mp3">'
            '<embed src="file://MEDIA/13.swf"><embed src="file://MEDIA/404.flv">',
            self.media)
        self.assertIn('<video controls preload="metadata" '
                      'src="file:///dvd/MEDIA2_DATA/multimedia/12.flv"></video>', html)
        self.assertIn('src="file:///dvd/9%20a.mp3"', html)
        self.assertIn('href="enc://flash/13.swf"', html)
        self.assertIn("not in the DVD folder", html)
        self.assertEqual(films, 3)

    def test_the_page_allows_no_scripts_of_its_own(self):
        document = encyclopedia_html.document("T<", "Lib", "<p>x</p>", dark=False)
        self.assertIn("default-src 'none'", document)
        self.assertNotIn("script-src", encyclopedia_html.CSP)
        self.assertIn("<h1>T&lt;</h1>", document)


class Scheme(DvdCase):
    def test_pictures_animations_and_ruffle_are_served(self):
        encyclopedia = self.imported()
        self.assertEqual(encscheme.resolve("enc://image/77", encyclopedia),
                         (GIF, "image/gif"))
        swf, mime = encscheme.resolve("enc://flash/swf/13.swf", encyclopedia)
        self.assertEqual((swf, mime), (b"FWS made-up", "application/x-shockwave-flash"))
        page, mime = encscheme.resolve("enc://flash/13.swf", encyclopedia)
        self.assertEqual(mime, "text/html")
        if ruffle.location():
            self.assertIn(b'player.load({ url: "enc://flash/swf/13.swf" })', page)
        else:
            self.assertIn(b"tools/install-ruffle.sh", page)

    def test_nothing_outside_the_dvd_and_ruffle_is_reachable(self):
        encyclopedia = self.imported()
        for uri in ("enc://image/../77", "enc://image/x", "enc://flash/swf/../../etc.swf",
                    "enc://flash/ruffle/../../../etc/passwd", "enc://flash/ruffle/x.html",
                    "enc://flash/a/b.swf", "enc://article/1", "http://image/77"):
            self.assertIsNone(encscheme.resolve(uri, encyclopedia), uri)

    def test_ruffle_serves_only_its_own_kinds_of_file(self):
        self.assertIsNone(ruffle.file("../ruffle.js"))
        self.assertIsNone(ruffle.file("LICENSE_MIT"))


class StubClient:
    """Plans, checks and answers the way a model would, as scripted."""

    def __init__(self, answer="They hum.", plan=None, check=None):
        self.answer, self.calls = answer, []
        self.plan = plan if plan is not None else {"queries": [], "titles": []}
        self.check = check if check is not None else {"enough": True}

    def complete(self, model, messages, image_base64=None, cancel=None, web_search=None,
                 system=None, max_tokens=None, **kwargs):
        import json
        kind = "plan" if system == prompts.ENCYCLOPEDIA_PLAN else "check"
        self.calls.append({"kind": kind, "content": messages[-1]["content"]})
        return "Here you are:\n" + json.dumps(self.plan if kind == "plan" else self.check)

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None):
        self.calls.append({"kind": "answer", "messages": list(messages), "system": system})
        return self.answer


class EncyclopediaWindowCase(DvdCase):
    def setUp(self):
        super().setUp()
        self.encyclopedia = self.imported()
        self.client = StubClient()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "test"
        self.config.encyclopedia_dir = self.dvd
        self.opened = []
        self.app = types.SimpleNamespace(
            config=self.config, client=self.client, encyclopedia=self.encyclopedia,
            notify=lambda *a, **k: None,
            search_encyclopedia=lambda target=None, highlight=None:
                self.opened.append((target, highlight)))

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

    def _answered(self, window, turns):
        return self._run_until(lambda: window._cancel is None
                               and len(window.conversation) >= turns)


class SearchWindow(EncyclopediaWindowCase):
    def _window(self, **kwargs):
        from assistant.ui.encyclopedia_windows import SearchEncyclopediaWindow
        return SearchEncyclopediaWindow(self.app, self.encyclopedia, **kwargs)

    def _searched(self, window, query):
        window.search_entry.set_text(query)
        window.search_entry.emit("activate")
        self.assertTrue(self._run_until(lambda: not window._looking
                                        and window.pane.current is not None))

    def test_the_clipboard_waits_in_the_search_box_until_enter(self):
        window = self._window(query="Zorblax")
        try:
            self.assertEqual(window.search_entry.get_text(), "Zorblax")
            self.assertEqual(window.search_entry.get_selection_bounds(), (0, 7))
            self._run_until(lambda: False, timeout=1)
            self.assertIsNone(window.pane.current)
            self.assertFalse(window.tag_button.get_sensitive())
        finally:
            window.destroy()

    def test_the_best_match_is_read_and_the_rest_wait_under_results(self):
        window = self._window()
        try:
            self._searched(window, "Zorblax")
            self.assertEqual(window.pane.current, ("article", 1))
            self.assertEqual(window.header.get_title(), "Zorblax")
            self.assertEqual(window.results_button.get_label(), "Results (4)")
            student = window.results.get_children()[1]
            window._on_result(window.results, student)
            self.assertEqual(window.pane.current, ("article", 101))
            self.assertTrue(window.back_button.get_sensitive())
            window._go(-1)
            self.assertEqual(window.pane.current, ("article", 1))
        finally:
            window.destroy()

    def test_nothing_is_sent_until_tagged_and_then_only_what_is_tagged(self):
        window = self._window()
        try:
            self._searched(window, "Zorblax")
            window.message_box_text("What do they eat?")
            window._send()
            self.assertIn("Tag an article first", window.status.get_text())
            self.assertEqual(window.conversation, [])
            self.assertEqual(self.client.calls, [])

            window.tag_button.set_active(True)      # tagging sends nothing either
            self.assertEqual(window._tags, [1])
            self.assertEqual(self.client.calls, [])
            self.assertTrue(window.chips.get_visible())
            self.assertFalse(window.untagged_hint.get_visible())

            window.open(("article", 2))             # read something else, untagged
            self.assertFalse(window.tag_button.get_active())
            window._send()
            self.assertTrue(self._answered(window, 2))
            call, = self.client.calls
            self.assertEqual(call["messages"], [{"role": "user",
                                                 "content": "What do they eat?"}])
            self.assertIn("Zorblaxes eat moonberries", call["system"])
            self.assertNotIn('Article "quuxland"', call["system"])

            window.untag(1)
            self.assertEqual(window._tags, [])
            self.assertTrue(window.untagged_hint.get_visible())
        finally:
            window.destroy()

    def test_several_tagged_articles_go_together(self):
        window = self._window(target=("article", 1))
        try:
            window.tag_button.set_active(True)
            window.open(("article", 201))
            window.tag_button.set_active(True)
            system = window._request_system()
            self.assertIn('Article "Zorblax"', system)
            self.assertIn('Article "moonberry"', system)
            window.open(("article", 1))
            self.assertTrue(window.tag_button.get_active())  # it knows what is tagged
        finally:
            window.destroy()

    def test_clear_keeps_what_is_tagged(self):
        window = self._window(target=("article", 1))
        try:
            window.tag_button.set_active(True)
            window.message_box_text("What do they eat?")
            window._send()
            self.assertTrue(self._answered(window, 2))
            window._clear()
            self.assertEqual(window.conversation, [])
            self.assertEqual(window._tags, [1])
        finally:
            window.destroy()

    def test_hiding_the_answers_keeps_the_question_box(self):
        window = self._window(target=("article", 1))
        try:
            window.answers_toggle.set_active(True)
            self.assertFalse(window.view.get_visible())
            self.assertTrue(window.entry.get_visible())
            window.answers_toggle.set_active(False)
            self.assertTrue(window.view.get_visible())
        finally:
            window.destroy()


class AskWindow(EncyclopediaWindowCase):
    def _window(self, **kwargs):
        from assistant.ui.encyclopedia_windows import AskEncyclopediaWindow
        return AskEncyclopediaWindow(self.app, self.encyclopedia, **kwargs)

    def test_a_copied_question_waits_selected_and_the_web_is_not_asked(self):
        window = self._window(question="Where do they go?")
        try:
            buf = window.entry.get_buffer()
            start, end = buf.get_selection_bounds()
            self.assertEqual(buf.get_text(start, end, False), "Where do they go?")
            self.assertFalse(window.search_toggle.get_visible())
            self.assertTrue(window.strict_switch.get_active())
            self.assertEqual(window.audience.get_active_id(), "adult")
        finally:
            window.destroy()

    def test_planned_searched_checked_answered_and_its_quotes_checked(self):
        self.client.answer = ('They "cross the glimmer sea" [1], and "they fly to the '
                              'moon each spring" [1].')
        self.client.plan = {"queries": ["glimmer sea crossing"], "titles": ["Zorblax"]}
        window = self._window(question="Where do zorblaxes migrate?")
        try:
            window._send()
            self.assertTrue(self._answered(window, 3))
            self.assertEqual([c["kind"] for c in self.client.calls],
                             ["plan", "check", "answer"])
            self.assertIn("Question: Where do zorblaxes migrate?",
                          self.client.calls[0]["content"])
            self.assertIn("Zorblax › Migration", self.client.calls[1]["content"])
            system = self.client.calls[2]["system"]
            self.assertIn("Strict: these passages are your only source", system)
            self.assertIn("Write for an adult general reader", system)
            self.assertIn('[1] "Zorblax" (Encyclopedia), section "Migration"',
                          system)

            question, answer, sources = window.conversation
            self.assertEqual(answer["content"], self.client.answer)   # history as said
            self.assertIn("[\\[1\\]](enc://cite/1/1)", answer["display"])
            self.assertIn("each spring\" [1] ⚠", answer["export"])
            self.assertEqual([c["ok"] for c in answer["checks"]], [True, False])
            self.assertIn("1 of 2 found word for word", sources["html"])
            self.assertIn('href="enc://film/5"', sources["html"])
            self.assertIn("⚠", window.status.get_text())
            self.assertIn("each spring\" [1] ⚠", window._markdown())
            self.assertNotIn("enc://cite", window._markdown())
        finally:
            window.destroy()

    def test_a_citation_opens_the_reader_at_what_was_quoted(self):
        self.client.answer = 'They "cross the glimmer sea" [1].'
        window = self._window(question="Where do zorblaxes migrate?")
        try:
            window._send()
            self.assertTrue(self._answered(window, 3))
            window._follow_encyclopedia("enc://cite/1/1")
            window._follow_encyclopedia("enc://cite/1/99")         # no such passage
            window._follow_encyclopedia("enc://cite/0/1")          # not an answer
            window._follow_encyclopedia("enc://film/5")
            self.assertEqual(self.opened, [(("article", 1), "cross the glimmer sea"),
                                           (("film", 5), None)])
        finally:
            window.destroy()

    def test_not_enough_searches_again_and_little_more_is_flagged_in_strict(self):
        self.client.check = {"enough": False, "queries": ["glimmerless void"]}
        window = self._window(question="Where do zorblaxes migrate?")
        try:
            window._send()
            self.assertTrue(self._answered(window, 3))
            self.assertIn("switch Strict off", window.conversation[2]["html"])
        finally:
            window.destroy()

    def test_labelled_and_for_a_young_reader(self):
        window = self._window(question="Where do zorblaxes migrate?")
        try:
            window.strict_switch.set_active(False)
            window.audience.set_active_id("young")
            window._send()
            self.assertTrue(self._answered(window, 3))
            system = self.client.calls[-1]["system"]
            self.assertIn('headed "Not from the encyclopedia"', system)
            self.assertNotIn("Strict:", system)
            self.assertIn("child of about eight to eleven", system)
        finally:
            window.destroy()

    def test_nothing_found_says_so_strictly_or_labelled(self):
        for strict, said in ((True, prompts.ASK_ENCYCLOPEDIA_NOTHING),
                             (False, prompts.ASK_ENCYCLOPEDIA_NOTHING_LABELLED)):
            self.client.calls.clear()
            window = self._window(question="glimmerless void?")
            try:
                window.strict_switch.set_active(strict)
                window._send()
                self.assertTrue(self._answered(window, 2))
                self.assertIn(said, self.client.calls[-1]["system"])
            finally:
                window.destroy()

    def test_a_follow_up_carries_the_passages_and_question_before(self):
        self.client.answer = 'They "cross the glimmer sea" [1].'
        window = self._window(question="Where do zorblaxes migrate?")
        try:
            window._send()
            self.assertTrue(self._answered(window, 3))
            window.message_box_text("and what do they eat?")
            window._send()
            self.assertTrue(self._answered(window, 6))
            plan = [c for c in self.client.calls if c["kind"] == "plan"][1]
            self.assertIn("Earlier question: Where do zorblaxes migrate?", plan["content"])
            system = self.client.calls[-1]["system"]
            self.assertIn('section "Diet"', system)
            self.assertIn('section "Migration"', system)
            self.assertEqual(len(self.client.calls[-1]["messages"]), 3)
        finally:
            window.destroy()

    def test_in_the_plain_text_view_too(self):
        # Where WebKit cannot load - the AppImage on CachyOS - the answer, its citations
        # and its sources must still read, and their links still work.
        from unittest import mock
        from assistant.ui import chat
        self.client.answer = ('They "cross the glimmer sea" [1], and "they fly to the '
                              'moon each spring" [1].')
        with mock.patch.object(chat, "WebKit2", None):
            window = self._window(question="Where do zorblaxes migrate?")
        try:
            self.assertIsNone(window.webview)
            window._send()
            self.assertTrue(self._answered(window, 3))
            shown = window.bubbles.transcript_text()
            self.assertIn('They "cross the glimmer sea" [1], and', shown)
            self.assertNotIn("enc://", shown)
            self.assertNotIn("\\[", shown)
            self.assertIn("⚠ “they fly to the moon each spring” cited as [1]: not found "
                          "in any passage.", shown)
            self.assertIn("Pictures and films: ▶ Zorblaxes crossing the glimmer sea.",
                          shown)
            self.assertTrue(window._on_text_link("enc://cite/1/1"))
            self.assertTrue(window._on_text_link("enc://film/5"))
            self.assertFalse(window._on_text_link("https://a.test"))
            self.assertEqual(self.opened, [(("article", 1), "cross the glimmer sea"),
                                           (("film", 5), None)])
            saved = window._markdown()
            self.assertNotIn("enc://", saved)
            self.assertIn("Quotations checked against the passages: 1 of 2", saved)
        finally:
            window.destroy()

    def test_a_plan_that_fails_still_searches_with_the_question(self):
        class Failing(StubClient):
            def complete(self, *args, **kwargs):
                from assistant.llm import LLMError
                raise LLMError("HTTP 503: busy")

        self.client = Failing(answer="Fine.")
        self.app.client = self.client
        window = self._window(question="Where do zorblaxes migrate?")
        try:
            window._send()
            self.assertTrue(self._answered(window, 2))
            self.assertIn('section "Migration"', self.client.calls[-1]["system"])
        finally:
            window.destroy()


class Pane(DvdCase):
    def test_back_and_forward_through_articles_pictures_and_films(self):
        from assistant.ui.article_view import ArticlePane, find_text, target_of
        self.assertEqual(target_of("enc://film/5"), ("film", 5))
        self.assertEqual(target_of("enc://flash/13.swf"), ("flash", "13.swf"))
        self.assertIsNone(target_of("enc://article/x"))
        self.assertEqual(find_text("Every winter … humming as they go."),
                         "humming as they go")
        shown = []
        pane = ArticlePane(self.imported(), on_change=lambda *a: shown.append(a[2]))
        window = Gtk.Window()
        window.add(pane)
        try:
            pane.open(("article", 1))
            pane.open(("assembly", 5))
            self.assertEqual(shown, ["Zorblax", "Zorblax: at rest"])
            pane.go(-1)
            self.assertEqual(pane.current, ("article", 1))
            self.assertTrue(pane.can_go(1))
            pane.open(("film", 5))
            self.assertFalse(pane.can_go(1))
            self.assertEqual(shown[-1], "Zorblax: the crossing")
            pane.open(("article", 999))
            self.assertEqual(shown[-1], "Not found")
        finally:
            window.destroy()


class TurnedOff(unittest.TestCase):
    """Off by default: no tray items, no hotkeys, and an action that arrives anyway says
    where to turn it on rather than opening anything."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))

    def _app(self):
        from assistant.app import Assistant
        app = Assistant.__new__(Assistant)        # the parts under test, not a tray app
        app.config = self.config
        app.hotkeys = types.SimpleNamespace(mode="off")
        app.notices, app.opened = [], []
        app.notify = lambda message, urgency="low": app.notices.append(message)
        app.ask_encyclopedia = lambda: app.opened.append("ask")
        return app

    def test_off_unless_turned_on(self):
        from assistant.hotkeys import enabled_actions
        self.assertFalse(self.config.encyclopedia_enabled)
        names = [name for name, _, _ in enabled_actions(self.config)]
        self.assertNotIn("search_encyclopedia", names)
        self.assertNotIn("ask_encyclopedia", names)
        self.config.encyclopedia_enabled = True
        names = [name for name, _, _ in enabled_actions(self.config)]
        self.assertEqual(names[-2:], ["search_encyclopedia", "ask_encyclopedia"])

    def test_an_action_while_off_only_says_so(self):
        app = self._app()
        app.dispatch("ask_encyclopedia")
        self.assertEqual(app.opened, [])
        self.assertIn("Settings > Reference", app.notices[-1])
        self.config.encyclopedia_enabled = True
        app.dispatch("ask_encyclopedia")
        self.assertEqual(app.opened, ["ask"])

    def test_the_tray_shows_its_items_only_while_on(self):
        app = self._app()
        menu = app._build_menu()
        try:
            labels = [item.get_label() for item in menu.get_children()
                      if item.get_visible() and isinstance(item, Gtk.MenuItem)]
            self.assertIn("Look up word", labels)
            self.assertNotIn("Search Encyclopedia", labels)
            self.config.encyclopedia_enabled = True
            app._sync_encyclopedia_items()
            labels = [item.get_label() for item in menu.get_children()
                      if item.get_visible()]
            self.assertIn("Search Encyclopedia", labels)
            self.assertIn("Ask Encyclopedia", labels)
        finally:
            menu.destroy()

    def test_settings_greys_out_the_section_until_it_is_ticked(self):
        from assistant.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(None, self.config, types.SimpleNamespace(config=self.config))
        try:
            self.assertFalse(dialog.encyclopedia_section.get_sensitive())
            self.assertFalse(dialog.values()["encyclopedia_enabled"])
            dialog.encyclopedia_enabled.set_active(True)
            self.assertTrue(dialog.encyclopedia_section.get_sensitive())
            self.assertTrue(dialog.values()["encyclopedia_enabled"])
        finally:
            dialog.destroy()


if __name__ == "__main__":
    unittest.main()
