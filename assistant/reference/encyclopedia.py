"""The 2015 encyclopedia DVD, from the user's own copy.

The DVD's articles are gzip-compressed HTML pages in AxionDB tables (see axion.py),
three encyclopedias of them: the main one (EB, 103,092 articles),
the Student encyclopedia (EBI, 18,934) and the Elementary one (EBK, 3,874). Pictures
are referred to by number - an article's thumbnail and the full picture share it - and
lie back to back in IMAGE_DATA. Videos, sounds and Flash animations are ordinary files
in MEDIA1-4_DATA, reached through "assemblies": the page the DVD showed for a picture,
a table or a film, with its caption and credit. Pictures' and films' assemblies are
numbered separately, and some numbers are used by both.

The DVD's own search index is Lucene's, readable only from Java. So the content is
imported once into a database of our own (import_dvd): every article's compressed
page as it was, split into sections for a full-text index, plus the tables that turn a
picture's number into its bytes and a film's name into its file. Pictures and films are
not copied; they are read from the DVD folder when shown.

A section is what follows one heading in an article, up to the next. The import and
every later reading split articles with the same function, sections_of, so a section's
row number in the index always points at the same text.
"""

import html as html_module
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter
from glob import glob

from .axion import AxionError, Table, unpack  # noqa: F401  (AxionError: for callers)

DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or
                        os.path.expanduser("~/.local/share"), "da-assistant")
DATABASE = "encyclopedia.sqlite"
# Raised whenever what the import stores, or how it splits articles, changes: an older
# database is then reported as needing a new import rather than read wrongly.
SCHEMA = 1


def research_context(passages):
    """Research passages as the model is given them: numbered, for citing."""
    blocks = []
    for number, passage in enumerate(passages, 1):
        blocks.append(f'[{number}] "{passage["title"]}" ({passage["library_name"]}), '
                      f'section "{passage["heading"]}":\n{passage["text"]}')
    return "\n\n".join(blocks)


def read_json(text):
    """The first JSON object in a model's reply, or {} - models wrap it in prose and
    code fences however they are asked."""
    import json
    text = text or ""
    start = text.find("{")
    while start != -1:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        found = json.loads(text[start:end + 1])
                    except ValueError:
                        break
                    return found if isinstance(found, dict) else {}
        start = text.find("{", start + 1)
    return {}


_QUOTED = re.compile(r'[“"]([^”"\n]{3,}?)[”"]((?:\s*\[\d+(?:\s*[,–-]\s*\d+)*\])*)')
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…|\[…\]|\[\.\.\.\])\s*")


def _plain(text):
    """Text as quotations are compared: case, quote marks, dashes and spacing aside."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    for curly, straight in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                            ("–", "-"), ("—", "-")):
        text = text.replace(curly, straight)
    return " ".join(text.split())


def _contains(passage, quote):
    """Whether `quote` is in `passage` word for word - each part of it, in order, where
    an ellipsis marks words left out."""
    haystack = _plain(passage)
    position = 0
    for part in _ELLIPSIS.split(quote):
        part = _plain(part).strip(" .,;:!?'\"")
        if not part:
            continue
        found = haystack.find(part, position)
        if found == -1:
            return False
        position = found + len(part)
    return True


# A quotation not word for word in its passage is shown where the passage comes closest
# to it - when some stretch of the passage has at least this share of its words.
NEAREST_SHARE = 0.5


def _words(text):
    """[(word as quotations are compared, start, end)] of `text`."""
    out = []
    for match in re.finditer(r"\S+", text or ""):
        word = _plain(match.group()).strip(".,;:!?'\"()[]-")
        if word:
            out.append((word, match.start(), match.end()))
    return out


def nearest(passage, quote):
    """The words of `passage`, as it has them, that come closest to `quote`, and the
    share of the quotation's words they hold - (None, 0.0) for none with NEAREST_SHARE.

    What a model gets wrong in a quotation is mostly a word changed, added or left out,
    or two sentences run together, so the stretch of the passage the length of the
    quotation, with room for a few words more, that has the most of its words is where
    it was quoting from. Words like "the" and "they" count for little: every stretch has
    some. Found word for word, it is the quotation itself, as the passage writes it.
    """
    wanted = [w for part in _ELLIPSIS.split(quote or "") for w, _s, _e in _words(part)]
    if not wanted:
        return None, 0.0
    need = Counter(wanted)

    def weight(word):
        return 1 if word in STOPWORDS else 10
    total = sum(weight(w) for w in wanted)
    words = _words(passage)
    # Room for words left out, and where an ellipsis says so, for many.
    width = len(wanted) + 3 + len(wanted) // 3
    if _ELLIPSIS.search(quote):
        width += 3 * len(wanted)
    have, score, best, best_score = Counter(), 0, None, 0
    for i, (word, _s, _e) in enumerate(words):
        if have[word] < need[word]:
            score += weight(word)
        have[word] += 1
        if i >= width:
            gone = words[i - width][0]
            have[gone] -= 1
            if have[gone] < need[gone]:
                score -= weight(gone)
        if score > best_score:
            best, best_score = max(0, i - width + 1), score
    if best is None or best_score < NEAREST_SHARE * total:
        return None, 0.0
    # That stretch, less the words at either end it would hold as many without.
    stretch = words[best:best + width]
    have = Counter(word for word, _s, _e in stretch)
    for end in (-1, 0):
        while stretch and have[stretch[end][0]] > need[stretch[end][0]]:
            have[stretch[end][0]] -= 1
            stretch.pop(end)
    return passage[stretch[0][1]:stretch[-1][2]].strip(".,;:!?"), best_score / total


def check_quotes(answer, passages):
    """Every quotation in `answer`, checked against the passages it was given.

    [{quote, cited: [numbers after it], found: the passage it is in or None, ok,
    start, end: where its citations end in `answer`, near}]. ok: word for word in a
    passage it cites - or, citing none, in any. A quotation of a word or two is a term,
    not a quotation of the source, and is left alone. near, for one not ok: where to
    see it - {passage, text: the passage's own words} where it was found, or else came
    closest (see nearest), the passages it cites first; None if none came close.
    """
    out = []
    for match in _QUOTED.finditer(answer or ""):
        quote = match.group(1).strip()
        if len(quote.split()) < 3:
            continue
        numbers = [n for n in cited(match.group(2)) if 1 <= n <= len(passages)]
        found = None
        for n in numbers + [n for n in range(1, len(passages) + 1) if n not in numbers]:
            if _contains(passages[n - 1]["text"], quote):
                found = n
                break
        ok = found is not None and (not numbers or found in numbers)
        out.append({"quote": quote, "cited": numbers, "found": found, "ok": ok,
                    "start": match.start(), "end": match.end(),
                    "near": None if ok else _near(quote, numbers, found, passages)})
    return out


def _near(quote, numbers, found, passages):
    """check_quotes' `near` for a quotation that is not where it says."""
    best, best_share = None, 0.0
    order = [found] if found else (
        numbers + [n for n in range(1, len(passages) + 1) if n not in numbers])
    for n in order:
        text, share = nearest(passages[n - 1]["text"], quote)
        if share > best_share:
            best, best_share = {"passage": n, "text": text}, share
    return best


def cited(answer):
    """The passage numbers an answer cites - [2], [1, 3], [4-6] - in order, once each."""
    out = []
    for group in re.findall(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]", answer or ""):
        numbers = [int(n) for n in re.findall(r"\d+", group)]
        if re.search(r"[–-]", group) and len(numbers) == 2 and numbers[0] < numbers[1]:
            numbers = list(range(numbers[0], numbers[1] + 1))
        out += [n for n in numbers if n not in out]
    return out

LIBRARIES = [
    ("EB", "Encyclopedia"),
    ("EBI", "Student Encyclopedia"),
    ("EBK", "Elementary Encyclopedia"),
]
LIBRARY_NAMES = dict(LIBRARIES)

# What of one article goes with a question about it. Most articles fit whole; the
# longest ("Europe, history of") is 1.6 MB of HTML, and for those the sections that
# best match the question are chosen.
ARTICLE_CONTEXT_CHARS = 40000
SUMMARY_CHARS = 320

# Index weights for bm25: an article whose title matches beats one whose section
# heading does, which beats one that merely mentions the words.
WEIGHTS = (10.0, 4.0, 1.0)

# Research: the passages found for one question - sections from anywhere in the three
# encyclopedias - and how much of them goes to the model. A few from each of several
# articles answer more than many from one.
RESEARCH_PASSAGES = 10
RESEARCH_PER_ARTICLE = 3
RESEARCH_CHARS = 24000
# Sections that list rather than answer: a film article's credits, an article's reading
# list. They match a question's words as well as any other, and answer none of it.
NOT_ANSWERS = ("additional reading", "cast", "production notes", "academy award")

# Who an Ask Encyclopedia answer is for: which encyclopedia is preferred, then which
# next, each weighing EDITION_WEIGHTS in the ranking - the others still count, for
# what the preferred one does not cover.
AUDIENCES = {
    "adult": ("EB", "EBI", "EBK"),
    "student": ("EBI", "EB", "EBK"),
    "young": ("EBK", "EBI", "EB"),
}
AUDIENCE_NAMES = {"adult": "General reader", "student": "Student",
                  "young": "Young reader"}
EDITION_WEIGHTS = (1.0, 0.4, 0.2)


class EncyclopediaError(Exception):
    pass


# ---- reading an article's HTML ---------------------------------------------

_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_BODY_ENDS = ('<div id="cite"', 'id="articleTop"', 'id="sidebarTitleBar"')
# A picture inside the text: its thumbnail and captions, which interrupt the sentence
# around them. Pictures are shown by the viewer; the text does without them.
_ASSEMBLY = re.compile(r'<div class="assembly">.*?</ul>\s*</div>', re.S)
_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.S | re.I)
_BLOCK = re.compile(r"</?(?:p|div|br|li|tr|h[1-6]|ul|ol|table|blockquote|dd|dt|center)"
                    r"\b[^>]*>", re.I)
_CELL = re.compile(r"</t[dh]\s*>", re.I)
_TAG = re.compile(r"<[^>]*>")
_UNFINISHED = re.compile(r"<[^>]*$")
_BINARY = re.compile(r"binaryId=(\d+)")
_MEDIA = re.compile(r"file://MEDIA/([\w.-]+)", re.I)


def title_of(page):
    found = _TITLE.search(page)
    return " ".join(html_module.unescape(found.group(1)).split()) if found else ""


def body_of(page):
    """The article itself: what lies between the page furniture above and the
    citation box below."""
    start = page.find('id="articleBody"')
    if start == -1:
        return ""
    start = page.find(">", start) + 1
    ends = [i for i in (page.find(marker, start) for marker in _BODY_ENDS) if i != -1]
    # The end markers fall after the "back to top" link has begun, leaving
    # `<a onclick="scrollToTop()" ... title="back to top"` cut off, which no parser
    # closes: as text it ended every article, and as HTML it swallowed what followed.
    return _UNFINISHED.sub("", page[start:min(ends) if ends else len(page)])


def text_of(fragment):
    text = _CELL.sub(" \t ", fragment)
    text = _BLOCK.sub("\n", text)
    text = html_module.unescape(_TAG.sub("", text))
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


# A section's contributors: links the DVD set side by side with nothing between them,
# which as text read "Blake EhrlichJohn Foot".
_AUTHORS = re.compile(r'(?:<a\b[^>]*class="authcr"[^>]*>.*?</a>\s*)+', re.S)
_AUTHOR = re.compile(r'<a\b[^>]*class="authcr"[^>]*>(.*?)</a>', re.S)


def byline(names, most=None):
    """"By A", "By A and B", "By A, B and C"; "" for nobody. Given `most`, a longer
    list ends "and 20 others" - an article on Western painting credits 23 people."""
    if not names:
        return ""
    if len(names) == 1:
        return f"By {names[0]}"
    if most and len(names) > most + 1:
        return f"By {', '.join(names[:most])} and {len(names) - most} others"
    return f"By {', '.join(names[:-1])} and {names[-1]}"


def with_bylines(fragment, wrap):
    """`fragment` with each run of contributor links replaced by wrap(names)."""
    def replace(match):
        names = [name for name in (text_of(n) for n in _AUTHOR.findall(match.group(0)))
                 if name]
        return wrap(names) if names else ""
    return _AUTHORS.sub(replace, fragment)


_CITE = re.compile(r'<div id="cite">.*?</ul>', re.S)
# What the DVD's own browser filled in - the product's name and the year - and the
# punctuation that went with it. Left unfilled rather than guessed.
_CITE_GAPS = (
    (re.compile(r"\s+"), " "),
    (re.compile(r"<(u|i)>\s*__CITATION_PRODUCT_NAME__\s*\.?\s*</\1>\s*\.?"), ""),
    (re.compile(r"__CITATION_PRODUCT_NAME__\s*\."), ""),
    (re.compile(r",\s*__CITATION_YEAR__"), ""),
    (re.compile(r"\(\s*__CITATION_YEAR__\s*\)\s*\.?"), ""),
    (re.compile(r"__CITATION_\w+__"), ""),
    (re.compile(r"\s+(?=[.,])"), ""),
)


def citation_of(page):
    """The page's own "To cite this page" box, as HTML, its gaps closed; "" if none."""
    found = _CITE.search(page or "")
    if not found:
        return ""
    fragment = found.group(0) + "</div>"
    for pattern, replacement in _CITE_GAPS:
        fragment = pattern.sub(replacement, fragment)
    return fragment


def contributors_of(page):
    """Everyone credited anywhere in an article, in the order first credited."""
    names = (text_of(n) for n in _AUTHOR.findall(body_of(page)))
    return list(dict.fromkeys(name for name in names if name))


def sections_of(page):
    """[(heading, text)] of an article, in order, the empty ones left out. Each
    section's contributors end it as a line of their own: "By A and B"."""
    body = _ASSEMBLY.sub(" ", body_of(page))
    body = with_bylines(body, lambda names: f"<p>{html_module.escape(byline(names))}</p>")
    parts, heading, last = [], "Introduction", 0
    for match in _HEADING.finditer(body):
        parts.append((heading, body[last:match.start()]))
        heading = text_of(match.group(2)) or heading
        last = match.end()
    parts.append((heading, body[last:]))
    out = []
    for heading, fragment in parts:
        text = text_of(fragment)
        if text:
            out.append((heading, text))
    return out


_PICTURE = re.compile(r"AssemblyIdentifier\?assemblyId=(\d+).*?binaryId=(\d+)", re.S)
_FILM = re.compile(r"MultimediaAssemblyIdentifier\?assemblyId=(\d+)")
_CAPTION = re.compile(r'<li class="thumbcap">(.*?)</li>', re.S)


def section_media(page):
    """[(pictures, films)] for each of sections_of(page)'s sections: the pictures
    shown in it, as [(assembly, picture, caption)], and the films it links to, as
    [assembly]. [] if the article cannot be split the same way twice."""
    body = body_of(page)
    parts, last = [], 0
    for match in _HEADING.finditer(body):
        parts.append(body[last:match.start()])
        last = match.end()
    parts.append(body[last:])
    out = []
    for fragment in parts:
        if not text_of(_ASSEMBLY.sub(" ", fragment)):
            continue                    # sections_of leaves the empty ones out too
        pictures = []
        for block in _ASSEMBLY.finditer(fragment):
            found = _PICTURE.search(block.group(0))
            if found and "MultimediaAssemblyIdentifier" not in found.group(0):
                caption = _CAPTION.search(block.group(0))
                pictures.append((int(found.group(1)), int(found.group(2)),
                                 text_of(caption.group(1)) if caption else ""))
        films = list(dict.fromkeys(int(i) for i in _FILM.findall(fragment)))
        out.append((pictures, films))
    return out if len(out) == len(sections_of(page)) else []


def summary_of(sections, limit=SUMMARY_CHARS):
    """The opening of an article, cut at a sentence where one ends in time."""
    if not sections:
        return ""
    text = " ".join(sections[0][1].split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = cut.rfind(". ")
    if stop > limit // 2:
        return cut[:stop + 1]
    return cut.rsplit(" ", 1)[0] + "…"


# ---- search queries --------------------------------------------------------

STOPWORDS = set("""
a about above after again against all am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for
from further had has have having he her here hers him his how i if in into is it its
itself me more most my no nor not of off on once only or other our out over own same she
should so some such than that the their them then there these they this those through
to too under until up very was we were what when where which while who whom why will
with would you your tell explain describe please know much many
""".split())


def query_terms(text):
    """The words of `text` worth searching for, lower-cased, in order, once each."""
    out = []
    for word in re.findall(r"\w+", (text or "").lower()):
        if (len(word) > 1 or word.isdigit()) and word not in STOPWORDS and word not in out:
            out.append(word)
    return out


def fts_query(terms, any_of=False):
    """An FTS5 query for `terms`: all of them, or with any_of, any."""
    quoted = ['"' + term.replace('"', '""') + '"' for term in terms]
    return (" OR " if any_of else " ").join(quoted)


# ---- the import ------------------------------------------------------------

def _create(db):
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY, library TEXT, title TEXT, summary TEXT,
            image INTEGER, first_section INTEGER, sections INTEGER, page BLOB);
        CREATE INDEX articles_title ON articles (title COLLATE NOCASE);
        CREATE TABLE sections (id INTEGER PRIMARY KEY, article INTEGER, heading TEXT);
        CREATE TABLE assemblies (
            id INTEGER, kind TEXT, title TEXT, caption TEXT, credit TEXT,
            image INTEGER, media TEXT, page BLOB, PRIMARY KEY (kind, id));
        CREATE TABLE images (id INTEGER PRIMARY KEY, offset INTEGER, length INTEGER);
        CREATE TABLE media (name TEXT PRIMARY KEY, path TEXT);
        CREATE VIRTUAL TABLE text USING fts5(
            title, heading, body, content='',
            tokenize='porter unicode61 remove_diacritics 2');
    """)


def _assembly_row(key, kind, value):
    page = unpack(value)
    body = body_of(page)
    image = _BINARY.search(body)
    media = _MEDIA.search(body)
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I)
    credits = re.findall(r"<i\b[^>]*>(.*?)</i>", body, re.S | re.I)
    caption = text_of(paragraphs[0]).replace("↵", " ") if paragraphs else ""
    return (key, kind, title_of(page), " ".join(caption.split()),
            text_of(credits[-1]) if credits else "",
            int(image.group(1)) if image else None,
            media.group(1) if media else None, value)


def import_dvd(dvd, dest=None, progress=None, cancel=None):
    """Build the database from the DVD folder `dvd`. Returns what it holds.

    `progress(fraction, message)` is called as it goes; `cancel`, a threading.Event,
    stops it. The database is built beside its final name and moved into place only
    when complete, so an import stopped half way leaves the previous one in use.
    """
    dest = dest or os.path.join(DATA_DIR, DATABASE)
    progress = progress or (lambda fraction, message: None)
    dvd = os.path.abspath(os.path.expanduser(dvd))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    building = dest + ".importing"
    if os.path.exists(building):
        os.unlink(building)

    tables = {lib: Table(dvd, "ARTICLE_DATA", f"articledb/{lib}") for lib, _ in LIBRARIES}
    pictures = Table(dvd, "IMAGE_DATA", "imagedb/IMAGE")
    if pictures.lob_path() is None:
        raise EncyclopediaError("IMAGE_DATA must be a folder, as it is on the DVD, so "
                                "that pictures can be read from it directly")

    db = sqlite3.connect(building)
    try:
        db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;")
        _create(db)
        counts = {}

        progress(0.0, "Reading the picture index…")
        rows = pictures.rows()
        db.executemany("INSERT INTO images VALUES (?, ?, ?)", rows)
        counts["images"] = len(rows)

        found = sorted(glob(os.path.join(dvd, "MEDIA*_DATA", "multimedia", "*")))
        db.executemany("INSERT OR IGNORE INTO media VALUES (?, ?)",
                       [(os.path.basename(p), os.path.relpath(p, dvd)) for p in found])
        counts["media"] = len(found)

        progress(0.01, "Reading pictures' and films' captions…")
        for kind, name in (("image", "ASSEMBLY"), ("media", "MULTIMEDIAASSEMBLY")):
            table = Table(dvd, "INSTALL_DATA", f"installdb/{name}")
            batch = [_assembly_row(key, kind, value) for key, value in table.values()]
            db.executemany("INSERT INTO assemblies VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
            counts[name.lower()] = len(batch)

        planned = {lib: table.rows() for lib, table in tables.items()}
        total = sum(length for rows in planned.values() for _k, _o, length in rows) or 1
        done, section_id = 0, 0
        started = time.monotonic()
        for library, label in LIBRARIES:
            articles, sections, text = [], [], []
            count = 0
            for key, value in tables[library].values(planned[library]):
                if cancel is not None and cancel.is_set():
                    raise EncyclopediaError("Import cancelled")
                page = unpack(value)
                title = title_of(page)
                parts = sections_of(page)
                image = _BINARY.search(body_of(page))
                articles.append((key, library, title, summary_of(parts),
                                 int(image.group(1)) if image else None,
                                 section_id + 1, len(parts), value))
                for heading, body in parts:
                    section_id += 1
                    sections.append((section_id, key, heading))
                    text.append((section_id, title, heading, body))
                count += 1
                done += len(value)
                if len(articles) >= 500:
                    _flush(db, articles, sections, text)
                    elapsed = time.monotonic() - started
                    left = elapsed * (total - done) / done
                    progress(0.02 + 0.93 * done / total,
                             f"{label}: {count:,} articles… about "
                             f"{max(1, round(left / 60))} min left")
            _flush(db, articles, sections, text)
            counts[library] = count

        progress(0.96, "Optimising the search index…")
        db.execute("INSERT INTO text (text) VALUES ('optimize')")
        meta = dict(counts, schema=SCHEMA, dvd=dvd,
                    imported=time.strftime("%Y-%m-%d %H:%M"))
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [(k, str(v)) for k, v in meta.items()])
        db.commit()
    except BaseException:
        db.close()
        if os.path.exists(building):
            os.unlink(building)
        raise
    db.close()
    os.replace(building, dest)
    progress(1.0, "Done")
    return meta


def _flush(db, articles, sections, text):
    db.executemany("INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, ?)", articles)
    db.executemany("INSERT INTO sections VALUES (?, ?, ?)", sections)
    db.executemany("INSERT INTO text (rowid, title, heading, body) VALUES (?, ?, ?, ?)",
                   text)
    articles.clear()
    sections.clear()
    text.clear()


# ---- reading ---------------------------------------------------------------

def _sniff(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/octet-stream"


class Encyclopedia:
    """The imported encyclopedia, and the DVD folder its pictures and films are in."""

    def __init__(self, config, data_dir=DATA_DIR):
        self.config = config
        self.path = os.path.join(data_dir, DATABASE)
        self._lock = threading.RLock()
        self._db = None
        self._meta = None
        self.importing = None       # (fraction, message) while an import runs

    # ---- the database -----------------------------------------------------

    def _connection(self):
        with self._lock:
            if self._db is None:
                if not os.path.exists(self.path):
                    raise EncyclopediaError("The encyclopedia has not been imported yet "
                                            "- see Settings > Reference")
                db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     check_same_thread=False)
                meta = dict(db.execute("SELECT key, value FROM meta"))
                if meta.get("schema") != str(SCHEMA):
                    db.close()
                    raise EncyclopediaError("The encyclopedia import is from an older "
                                            "version of the app - import it again in "
                                            "Settings > Reference")
                self._db, self._meta = db, meta
            return self._db

    def close(self):
        """Let go of the database, so a new import can take its place."""
        with self._lock:
            if self._db is not None:
                self._db.close()
            self._db, self._meta = None, None

    def status(self):
        """What has been imported, or None."""
        try:
            self._connection()
        except (EncyclopediaError, sqlite3.Error):
            return None
        return dict(self._meta)

    def available(self):
        return self.status() is not None

    def describe(self):
        """One line: what has been imported, and whether Ruffle can play the
        animations - for Settings and Diagnostics."""
        from . import ruffle
        status = self.status()
        if status is None:
            text = "not imported"
        else:
            articles = sum(int(status.get(k, 0)) for k, _name in LIBRARIES)
            text = f"{articles:,} articles, imported {status.get('imported', '?')}"
        flash = ruffle.version()
        return text + (f"; Ruffle {flash}" if flash else "; Ruffle not installed")

    def _query(self, sql, args=()):
        with self._lock:
            return self._connection().execute(sql, args).fetchall()

    def dvd(self):
        return os.path.expanduser(getattr(self.config, "encyclopedia_dir", "") or
                                  (self._meta or {}).get("dvd", ""))

    def import_from(self, dvd, progress=None, cancel=None):
        """Import the DVD at `dvd`, replacing any earlier import once it is complete."""
        def report(fraction, message):
            self.importing = (fraction, message)
            if progress:
                progress(fraction, message)

        try:
            report(0.0, "Starting…")
            meta = import_dvd(dvd, self.path + ".new", report, cancel)
            self.close()
            os.replace(self.path + ".new", self.path)
            return meta
        finally:
            self.importing = None

    # ---- articles ---------------------------------------------------------

    def article(self, ident):
        """{id, library, title, summary, image, first_section, sections, page}."""
        rows = self._query("SELECT id, library, title, summary, image, first_section, "
                           "sections, page FROM articles WHERE id = ?", (int(ident),))
        if not rows:
            return None
        keys = ("id", "library", "title", "summary", "image", "first_section",
                "sections", "page")
        article = dict(zip(keys, rows[0]))
        article["page"] = unpack(article["page"])
        article["library_name"] = LIBRARY_NAMES.get(article["library"], "Encyclopedia")
        return article

    def search(self, query, limit=10):
        """The articles that best answer `query`: [{id, library, title, summary,
        image}], an article titled exactly that first, then by how well its best
        section matches - every word, and failing that, any."""
        terms = query_terms(query)
        wanted = " ".join(query.split())
        ranked = []
        # Several articles can share a title - Rome is a city in Italy, Georgia and New
        # York - and the one wanted is nearly always the one with the most to say.
        for row in self._query("SELECT id FROM articles WHERE title = ? COLLATE NOCASE "
                               "ORDER BY length(page) DESC", (wanted,)):
            ranked.append(row[0])
        for any_of in (False, True):
            if not terms or len(ranked) >= limit:
                break
            # bm25() cannot be used in an aggregate: rank sections first, then
            # keep each article's best.
            rows = self._query(
                "SELECT sections.article, min(found.score) AS best FROM ("
                "  SELECT rowid, bm25(text, ?, ?, ?) AS score FROM text"
                "  WHERE text MATCH ? ORDER BY score LIMIT ?) AS found "
                "JOIN sections ON sections.id = found.rowid "
                "GROUP BY sections.article ORDER BY best LIMIT ?",
                (*WEIGHTS, fts_query(terms, any_of), limit * 20, limit * 3))
            for article, _score in rows:
                if article not in ranked:
                    ranked.append(article)
        out = []
        for ident in ranked[:limit]:
            row = self._query("SELECT id, library, title, summary, image FROM articles "
                              "WHERE id = ?", (ident,))[0]
            out.append(dict(zip(("id", "library", "title", "summary", "image"), row),
                            library_name=LIBRARY_NAMES.get(row[1], "Encyclopedia")))
        return out

    def sections(self, article):
        return sections_of(article["page"])

    def gather(self, question, also=None, queries=(), titles=(), audience="adult",
               exclude=(), limit=RESEARCH_PASSAGES, budget=RESEARCH_CHARS):
        """The sections anywhere in the encyclopedia that best answer `question`.

        Several searches, their rankings fused: the question's own words; `queries`,
        the searches a model planned for it; and `titles`, the articles it expects to
        cover it, whose opening and best-matching section count as found. Given `also`,
        the question before, sections matching words of both are searched for first, so
        a follow-up like "and how did it end?" still finds its subject. Each search
        ranks by any of its words: bm25 already prefers sections with more of them, and
        rarer ones, and the title weighs most - requiring all of them favoured long
        sections that happen to use every word, six yearbook summaries of "London in
        194x" ahead of the Great Fire of London's own article.

        `audience` decides which encyclopedia is preferred (AUDIENCES); the others count
        for less, not nothing. At most RESEARCH_PER_ARTICLE sections from one article,
        none of `exclude` - (article, section) pairs already found.

        [{article, title, library, library_name, contributors, heading, section, text,
        pictures: [(assembly, picture, caption)], credits: {assembly: credit}, films:
        [{assembly, title, caption, credit, media, image}]}], best first, the texts
        sharing `budget` characters.
        """
        terms = query_terms(question)
        searches = []
        earlier = [t for t in query_terms(also) if t not in terms]
        if terms and earlier:
            searches.append(f"({fts_query(terms, any_of=True)}) AND "
                            f"({fts_query(earlier, any_of=True)})")
        if terms:
            searches.append(fts_query(terms, any_of=True))
        for query in queries:
            words = query_terms(query)
            if words:
                searches.append(fts_query(words, any_of=True))
        weights = dict(zip(AUDIENCES.get(audience, AUDIENCES["adult"]), EDITION_WEIGHTS))
        scores, about = {}, {}

        def score(key, rank, library, heading):
            if key in exclude or (heading or "").lower().startswith(NOT_ANSWERS):
                return
            scores[key] = scores.get(key, 0.0) + weights.get(library, 0.3) / (10 + rank)
            about[key] = library

        for search in searches:
            rows = self._query(
                "SELECT sections.article, found.id - articles.first_section, "
                "sections.heading, articles.library FROM ("
                "  SELECT rowid AS id, bm25(text, ?, ?, ?) AS score FROM text"
                "  WHERE text MATCH ? ORDER BY score LIMIT ?) AS found "
                "JOIN sections ON sections.id = found.id "
                "JOIN articles ON articles.id = sections.article ORDER BY found.score",
                (*WEIGHTS, search, limit * 6))
            for rank, (article, section, heading, library) in enumerate(rows):
                score((article, section), rank, library, heading)
        any_word = fts_query(terms + earlier, any_of=True) if terms + earlier else None
        for title in titles:
            for ident, library, first, count in self._query(
                    "SELECT id, library, first_section, sections FROM articles "
                    "WHERE title = ? COLLATE NOCASE ORDER BY length(page) DESC LIMIT 2",
                    (" ".join((title or "").split()),)):
                score((ident, 0), 0, library, "")
                if any_word and count > 1:
                    best = self._query(
                        "SELECT rowid FROM text WHERE text MATCH ? AND rowid BETWEEN ? "
                        "AND ? ORDER BY bm25(text, ?, ?, ?) LIMIT 1",
                        (any_word, first + 1, first + count - 1, *WEIGHTS))
                    if best:
                        heading = self._query("SELECT heading FROM sections WHERE id = ?",
                                              (best[0][0],))[0][0]
                        score((ident, best[0][0] - first), 1, library, heading)

        chosen, per_article = [], {}
        for key in sorted(scores, key=scores.get, reverse=True):
            if per_article.get(key[0], 0) >= RESEARCH_PER_ARTICLE:
                continue
            chosen.append(key)
            per_article[key[0]] = per_article.get(key[0], 0) + 1
            if len(chosen) >= limit:
                break
        return self._passages(chosen, budget)

    def _passages(self, chosen, budget):
        """The (article, section) pairs `chosen`, as passages sharing `budget`."""
        from ..websearch import _cut, _share
        passages, pages = [], {}
        for ident, section in chosen:
            if ident not in pages:
                article = self.article(ident)
                pages[ident] = (article, self.sections(article),
                                section_media(article["page"]))
            article, sections, media = pages[ident]
            if section >= len(sections):
                continue
            pictures, films = media[section] if media else ([], [])
            passages.append({
                "article": ident, "title": article["title"] or "(untitled)",
                "library": article["library"], "library_name": article["library_name"],
                "contributors": contributors_of(article["page"]),
                "heading": sections[section][0], "section": section,
                "text": sections[section][1], "pictures": pictures,
                "credits": {a: self.credit(a) for a, _picture, _caption in pictures},
                "films": [film for film in (self._film(a) for a in films) if film]})
        for passage, share in zip(passages, _share([len(p["text"]) for p in passages],
                                                   budget)):
            cut = _cut(passage["text"], share)
            passage["text"] = cut + (" […]" if len(cut) < len(passage["text"]) else "")
        return passages

    def _film(self, ident):
        film = self.assembly(ident, "media")
        if film is None or not film["media"]:
            return None
        return {"assembly": ident, "title": film["title"], "caption": film["caption"],
                "credit": film["credit"], "media": film["media"], "image": film["image"]}

    def context(self, ident, question=None, budget=ARTICLE_CONTEXT_CHARS):
        """An article as text for the model. Whole when it fits in `budget`; otherwise
        its introduction and the sections that best match `question`, in the order
        they come in the article, with the headings of the rest listed."""
        article = self.article(ident)
        if article is None:
            return ""
        sections = self.sections(article)
        heading = (f'Article "{article["title"]}" from the {article["library_name"]} '
                   f"(2015):")
        blocks = [f"## {name}\n{text}" for name, text in sections]
        if sum(len(b) + 2 for b in blocks) <= budget:
            return heading + "\n\n" + "\n\n".join(blocks)

        order = [0]
        terms = query_terms(question)
        if terms:
            first = article["first_section"]
            rows = self._query(
                "SELECT rowid FROM text WHERE text MATCH ? AND rowid BETWEEN ? AND ? "
                "ORDER BY bm25(text, ?, ?, ?)",
                (fts_query(terms, any_of=True), first, first + len(sections) - 1,
                 *WEIGHTS))
            order += [rowid - first for (rowid,) in rows]
        order += range(len(sections))
        chosen, used = set(), 0
        for index in order:
            if index in chosen or index >= len(blocks):
                continue
            if used + len(blocks[index]) > budget:
                if not chosen:
                    blocks[index] = blocks[index][:budget] + " […]"
                else:
                    continue
            chosen.add(index)
            used += len(blocks[index]) + 2
        kept = [blocks[i] for i in sorted(chosen)]
        left = [sections[i][0] for i in range(len(sections)) if i not in chosen]
        note = ("\n\n(Too long to give whole. Sections left out: "
                + "; ".join(left) + ")") if left else ""
        return heading + "\n\n" + "\n\n".join(kept) + note

    # ---- pictures, films, assemblies --------------------------------------

    def assembly(self, ident, kind="image"):
        """A picture or table's page (kind "image"), or a film's or animation's
        ("media"): the DVD numbers the two separately."""
        rows = self._query("SELECT id, kind, title, caption, credit, image, media, page "
                           "FROM assemblies WHERE kind = ? AND id = ?", (kind, int(ident)))
        if not rows:
            return None
        keys = ("id", "kind", "title", "caption", "credit", "image", "media", "page")
        assembly = dict(zip(keys, rows[0]))
        assembly["page"] = unpack(assembly["page"])
        return assembly

    def credit(self, ident, kind="image"):
        """Whom a picture or film is credited to, e.g. "© Bryan and Cherry Alexander";
        "" when the DVD names nobody."""
        rows = self._query("SELECT credit FROM assemblies WHERE kind = ? AND id = ?",
                           (kind, int(ident)))
        return (rows[0][0] or "") if rows else ""

    def image(self, ident):
        """(bytes, MIME type) of picture `ident`, read from the DVD folder."""
        rows = self._query("SELECT offset, length FROM images WHERE id = ?", (int(ident),))
        if not rows:
            return None
        offset, length = rows[0]
        path = os.path.join(self.dvd(), "IMAGE_DATA", "imagedb", "IMAGE", "lobs",
                            "CACHE_VALUE")
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(length)
        except OSError:
            return None
        return data, _sniff(data)

    def media_path(self, name):
        """The file a film, sound or animation is in, or None."""
        rows = self._query("SELECT path FROM media WHERE name = ?", (name,))
        if not rows:
            return None
        path = os.path.join(self.dvd(), rows[0][0])
        return path if os.path.isfile(path) else None

