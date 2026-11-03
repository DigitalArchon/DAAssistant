"""The user's dictionaries: found in a folder, indexed once, looked up together.

GoldenDict keeps an index file per dictionary; this keeps one SQLite database for all of
them, in ~/.cache/da-assistant. Each dictionary's rows are rebuilt only when one of its
files changes size or modification time, so the first lookup after pointing the app at
a folder is the slow one - about a second per hundred megabytes - and every lookup after
that is an indexed query.

Keys are folded the way GoldenDict folds them: case and diacritics do not matter, and
neither do punctuation and spacing, so "Cafe" finds "café", "email" finds "e-mail", and
"don’t", copied from a web page with its typographic apostrophe, finds "don't". The
marks that make another letter of the one they are on do matter: ガラス is not カラス,
nor мой мои.
"""

import os
import re
import sqlite3
import struct
import threading
import unicodedata
import zlib

from .. import errors, paths
from . import markup
from .dictzip import DictzipError
from .dsl import DSL, inside, spellings
from .stardict import StarDict

CACHE_DIR = paths.CACHE_DIR
INDEX_FILE = "dictionaries.sqlite"
# What a dictionary's own files make go wrong in indexing them, as they will again
# until they change: filed with them in the index, and not tried again before. Anything
# else - a disk full, a file that cannot be read just now - is remembered only until
# the app starts again.
THE_FILES_FAULT = (ValueError, LookupError, EOFError, OverflowError, struct.error,
                   zlib.error, DictzipError)
# Part of every dictionary's signature, so raised whenever the index is made
# differently - 3: keys without punctuation or spacing (fold); 4: with the marks that
# make another letter - and each dictionary is then indexed again, once, at its next
# lookup. Raised too when a reader learns to read what it could not, so that the
# dictionaries filed as failed are tried again.
SCHEMA = 4

# Entries shown per dictionary for one word. A homograph-rich word ("set", "run") has a
# dozen in some dictionaries; past this many they are rarely the one wanted.
MAX_ENTRIES = 12
MAX_SUGGESTIONS = 24
# The most rows one key is read from in one dictionary, to choose those entries from: a
# real one files a few dozen under a key at most, a made one a million, each looked up.
MAX_ROWS = 100 * MAX_ENTRIES

# What text copied from a web page or a PDF has where a dictionary has the plain
# character: typographic apostrophes, quotation marks and dashes.
_TYPOGRAPHY = str.maketrans({**dict.fromkeys("’‘ʼ′´`", "'"),
                             **dict.fromkeys("“”„‟«»", '"'),
                             **dict.fromkeys("‐‑‒–—−", "-")})
# Letters that are not a letter and an accent, so NFKD leaves them whole, which readers
# and other dictionaries write as plain letters - "encyclopaedia" for "encyclopædia",
# "Lodz" for "Łódź". Lower case only: they are spelt out after case folding.
_LETTERS = {"æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ħ": "h", "ı": "i",
            "þ": "th", "ð": "d"}
# What a key leaves out besides accents: punctuation, spacing, and the invisible
# formatting characters - a soft hyphen, a zero-width space - that copied text carries.
_IGNORED = {"Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Zs", "Zl", "Zp", "Cc", "Cf"}
# Typography that Unicode does not count as punctuation: the modifier letter
# apostrophe, the grave accent typed for one, the minus sign typed for a hyphen.
_IGNORED_TOO = set("ʼ`−")
# Marks that make another letter of the one they are on, so that a word is another
# word without them, though NFKD takes them off as it takes off an accent: the kana
# voicing marks - ガラス, glass, is not カラス, crow, nor ばか はか - and the breve on
# a Cyrillic letter - мой, my, is not мои, my in the plural; nor is Belarusian ў у.
# They stay in a key; every other mark goes, the breve on a Latin letter with them.
_VOICING = "\u3099\u309a"
_BREVE = "\u0306"
_KEPT_MARKS = set(_VOICING + _BREVE)
_LATIN_BREVE = re.compile(f"(?<![\u0400-\u052f]){_BREVE}")
# The iota subscript. A mark, but one that casefold() makes a letter of: ᾳ was "αι".
_YPOGEGRAMMENI = "\u0345"


class _MarkTable(dict):
    """str.translate's table taking every mark off but _KEPT_MARKS."""

    def __missing__(self, code):
        char = chr(code)
        value = None if unicodedata.combining(char) and char not in _KEPT_MARKS else code
        self[code] = value
        return value


_MARKS = _MarkTable()


class _KeyTable(dict):
    """str.translate's table for fold, filled in as characters are first met - Unicode
    has a million of them, and a key is made of every headword in every dictionary.
    One of _LETTERS becomes its plain letters; an accent, a punctuation mark, a space
    or a formatting character goes; anything else - the letters and digits of every
    script, the marks _bare() leaves on, and symbols, which GoldenDict keeps too ("C++")
    - stays as it is."""

    def __missing__(self, code):
        char = chr(code)
        if char in _LETTERS:
            value = _LETTERS[char]
        elif char in _KEPT_MARKS:
            value = code
        elif (char in _IGNORED_TOO or unicodedata.combining(char)
              or unicodedata.category(char) in _IGNORED):
            value = None
        else:
            value = code
        self[code] = value
        return value


_KEY = _KeyTable()


def _bare(text):
    """`text` decomposed (NFKD), without the iota subscript, and with a breve only on a
    Cyrillic letter: where it has one at all, every other mark is taken off first, so
    that each breve follows the letter it is on. What is left of the marks to go goes
    later, with the punctuation (see _KEY). All in str methods and one regular
    expression: a key is made of every headword in every dictionary."""
    text = unicodedata.normalize("NFKD", text).replace(_YPOGEGRAMMENI, "")
    if _BREVE in text:
        text = _LATIN_BREVE.sub("", text.translate(_MARKS))
    return text


def plain(word):
    """`word` with case, accents, typography and spacing evened out, and nothing else:
    "Don’t  Stop" is "don't stop". What a lookup says it found, what tells a word's own
    entries from those it only folds to, and the key of a headword that is nothing but
    punctuation."""
    text = _bare((word or "").translate(_TYPOGRAPHY)).casefold()
    text = "".join(_LETTERS.get(c, c) for c in text
                   if c in _KEPT_MARKS or not unicodedata.combining(c))
    return unicodedata.normalize("NFC", " ".join(text.split()))


def fold(word):
    """The index key for a headword or a query, as GoldenDict makes one: case and
    accents do not matter, and neither do punctuation and spacing.

    So "e-mail" finds "email", and text copied from a web page or a PDF finds what the
    dictionary spells plainly - "don’t" and "o’clock", with their typographic
    apostrophes, find "don't" and "o'clock". The letters NFKD leaves whole are spelt
    out ("encyclopædia" is "encyclopaedia", "Łódź" is "lodz"); letters and digits are
    kept in every script, and so are the marks that make another letter (_KEPT_MARKS),
    composed again with theirs: "ガ", not "カ" and a mark. A headword of punctuation
    alone ("!", "…") would have no key at all that way, and keeps its plain() one.
    """
    text = unicodedata.normalize("NFC", _bare(word or "").casefold().translate(_KEY))
    return text or plain(word)


def stems(word):
    """Likely dictionary forms of an inflected English word, most likely first, spelt
    as plain() spells them - fold each for its key.

    Not a morphological analyser - GoldenDict uses Hunspell for that - but it turns the
    common inflections back into a headword: runs, running, ran is beyond it, but
    "studies", "stopped" and "biggest" are not.
    """
    w = plain(word)
    out = []

    def add(candidate):
        if len(candidate) >= 2 and candidate != w and candidate not in out:
            out.append(candidate)

    if w.endswith("'s"):
        add(w[:-2])
    for suffix, replacements in (
            ("ies", ("y",)), ("ied", ("y",)), ("ier", ("y",)), ("iest", ("y",)),
            ("ing", ("", "e")), ("ed", ("", "e")), ("es", ("", "e")), ("s", ("",)),
            ("er", ("", "e")), ("est", ("", "e")), ("ly", ("",))):
        if w.endswith(suffix) and len(w) > len(suffix) + 1:
            base = w[:-len(suffix)]
            for replacement in replacements:
                add(base + replacement)
            # stopped -> stop, biggest -> big
            if len(base) >= 3 and base[-1] == base[-2] and base[-1] not in "aeiouls":
                add(base[:-1])
    return out


def discover(folder):
    """Every dictionary under `folder`, as unopened (kind, path) pairs.

    Abbreviation lists - the *_abrv files Lingvo dictionaries ship beside themselves -
    are left out: GoldenDict uses them for tooltips, and as dictionaries of their own
    they answer every lookup of "n" or "adj" with a line of nothing. So is a file that
    is a link to one outside the folder: a dictionary is downloaded, and an X.DSL
    unpacked from one as a link to a file of the user's would be read as a dictionary,
    its lines as headwords, and given to the model with the next question. (Folders in
    it that are links are not looked in at all, as os.walk() has it.)
    """
    found = []
    if not folder or not os.path.isdir(folder):
        return found
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            lower = name.lower()
            stem = lower.split(".")[0]
            if stem.endswith("_abrv"):
                continue
            path = os.path.join(root, name)
            if lower.endswith(".ifo"):
                kind = "stardict"
            elif lower.endswith((".dsl", ".dsl.dz")):
                kind = "dsl"
            else:
                continue
            if inside(path, folder):
                found.append((kind, path))
    return found


def open_dictionary(kind, path):
    return StarDict(path) if kind == "stardict" else DSL(path)


def signature(dictionary):
    return _signature(dictionary.files())


def _signature(files):
    """What a dictionary's files are known by, to its index and to the reader kept
    open for it: their names, sizes and modification times."""
    parts = []
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        parts.append(f"{os.path.basename(path)}:{st.st_size}:{int(st.st_mtime)}")
    return f"{SCHEMA}|" + "|".join(parts)


class Library:
    """Lookups across every enabled dictionary in a folder.

    Two locks, so that nothing a window asks for on the main loop waits while an index
    is built. One guards the dictionaries opened - opening one reads its header, a
    moment's work - and the other the index: building it, which takes a quarter of a
    minute for 2 GB of dictionaries, and the lookups in it. Under one lock for both, a
    Look up window opened while another window's first lookup was indexing froze the
    whole app, as it listed its dictionaries' buttons on the main loop.
    """

    def __init__(self, config, cache_dir=CACHE_DIR):
        self.config = config
        self.cache_dir = cache_dir
        self._readers_lock = threading.Lock()       # self._opened
        self._index_lock = threading.RLock()        # the index, and lookups in it
        self._db = None
        # path -> (the dictionary object, or the error opening it; the signature of
        # its files when it was opened)
        self._opened = {}
        # path -> (the signature of its files, what was wrong indexing them, whether
        # nothing of them could be): what the index knows, for listing() to say
        # without waiting for it. Set and read whole, so without a lock.
        self._problems = {}
        self._tidied = False            # of dictionaries no longer there, this run

    # ---- the index --------------------------------------------------------

    def _connection(self):
        if self._db is None:
            os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
            db = sqlite3.connect(os.path.join(self.cache_dir, INDEX_FILE),
                                 check_same_thread=False)
            try:
                db.executescript("""
                    PRAGMA journal_mode = WAL;
                    CREATE TABLE IF NOT EXISTS dictionaries (
                        id INTEGER PRIMARY KEY, path TEXT UNIQUE, signature TEXT,
                        name TEXT, headwords INTEGER, problem TEXT);
                    CREATE TABLE IF NOT EXISTS headwords (
                        dictionary INTEGER, key TEXT, headword TEXT,
                        offset INTEGER, size INTEGER);
                    CREATE INDEX IF NOT EXISTS headwords_key
                        ON headwords (key, dictionary);
                """)
                # An index made before problems were kept has no column for them.
                if "problem" not in {column[1] for column in
                                     db.execute("PRAGMA table_info(dictionaries)")}:
                    db.execute("ALTER TABLE dictionaries ADD COLUMN problem TEXT")
            except sqlite3.Error:
                db.close()
                raise
            self._db = db
        return self._db

    def _set_aside(self, error):
        """Move an index that cannot be read out of the way, and begin a new one.

        It is only a cache, of what the dictionaries' own files hold. One that was not
        a database at all, or a malformed one, was never rebuilt: every lookup said
        "No dictionaries found - see Settings > Reference", with the dictionaries
        listed there all the while. It is kept beside the new one, as
        dictionaries.sqlite.broken, should anyone want to see what became of it."""
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None
        path = os.path.join(self.cache_dir, INDEX_FILE)
        for stale in (path + "-wal", path + "-shm"):
            try:
                os.unlink(stale)
            except OSError:
                pass
        try:
            os.replace(path, path + ".broken")
        except OSError:
            pass                        # then the next open says why, as it did
        self._problems.clear()
        line = (f"[dictionaries] the index {path} could not be read ({error}): set aside "
                f"as {INDEX_FILE}.broken, and made again")
        print(line)
        errors.record(line)

    def _forget_gone(self, db):
        """Delete what the index has of dictionaries no longer there - removed, or
        renamed, which made them new ones - once a run: nothing else ever did, and
        their headwords stayed in it for good. Not while the dictionaries folder is
        not there itself, on a drive not mounted, say: that says nothing of what is
        in it."""
        if self._tidied or not os.path.isdir(self.folder()):
            return
        self._tidied = True
        gone = [ident for ident, path in db.execute("SELECT id, path FROM dictionaries")
                if not os.path.exists(path)]
        if gone:
            marks = ",".join("?" * len(gone))
            with db:
                db.execute(f"DELETE FROM headwords WHERE dictionary IN ({marks})", gone)
                db.execute(f"DELETE FROM dictionaries WHERE id IN ({marks})", gone)

    def folder(self):
        return os.path.expanduser(getattr(self.config, "dictionaries_dir", "") or "")

    def _all(self):
        """Every dictionary in the folder that opens, in the user's order - the ones
        Settings has placed, as placed, then the rest by name - as (dictionary, the
        signature of its files when it was opened)."""
        found = []
        for kind, path in discover(self.folder()):
            dictionary, opened_as = self._open(kind, path)
            if not isinstance(dictionary, Exception):
                found.append((dictionary, opened_as))
        order = {path: i for i, path in
                 enumerate(getattr(self.config, "dictionaries_order", None) or [])}
        return sorted(found, key=lambda pair: (order.get(pair[0].path, len(order)),
                                               pair[0].name.casefold()))

    def available(self):
        """[(path, name)] of every dictionary in the folder, enabled or not, in order -
        what Settings lists. Opening one reads its header only."""
        return [(d.path, d.name) for d, _opened_as in self._all()]

    def listing(self):
        """[(path, name, icon path or None, about)] of the enabled dictionaries, in
        order - the dictionary window's bar, which shows `about` on hover: what the
        dictionary says of itself, and what went wrong indexing it, if anything did."""
        out = []
        for d, opened_as in self._enabled():
            about = d.about()
            known = self._problems.get(d.path)
            if known and known[0] == opened_as:
                note = ("Not indexed: " if known[2] else "Indexed in part: ") + known[1]
                about = f"{about}\n\n{note}" if about else note
            out.append((d.path, d.name, d.icon, about))
        return out

    def _open(self, kind, path):
        """(the dictionary at `path`, or the error opening it; the signature of its
        files when it was opened).

        Opened once and kept - until its files change. What a reader read as it
        opened, it keeps: a .dz's table of chunks, a StarDict's sametypesequence,
        .syn and name, a DSL's encoding and name. A file replaced while the app ran -
        a newer edition copied over it - went on being read through what was read of
        the old one: its entries garbled, zlib errors, the old name. So a reader whose
        files are no longer as they were when it opened them is opened again. A file
        that would not open is tried again once it changes, or once a file appears or
        goes beside it - a StarDict's missing .dict, say.
        """
        with self._readers_lock:
            cached = self._opened.get(path)
            if cached is not None:
                dictionary, opened_as = cached
                if isinstance(dictionary, Exception):
                    now = _signature([path, os.path.dirname(path)])
                else:
                    now = signature(dictionary)
                if now == opened_as:
                    return cached
            try:
                dictionary = open_dictionary(kind, path)
                opened_as = signature(dictionary)
            except Exception as e:      # a broken file must not stop the others
                dictionary, opened_as = e, _signature([path, os.path.dirname(path)])
            self._opened[path] = (dictionary, opened_as)
            return dictionary, opened_as

    def enabled(self):
        return [d for d, _opened_as in self._enabled()]

    def _enabled(self):
        """The enabled dictionaries, as _all() gives them."""
        disabled = set(getattr(self.config, "dictionaries_disabled", None) or [])
        return [(d, opened_as) for d, opened_as in self._all() if d.path not in disabled]

    def _ensure(self, dictionary, sig, status=None, position=None):
        """The dictionary's id in the index, building its rows if they are stale - or
        None if they could not be built from its files as they are.

        `sig` is the signature its files had when it was opened: its rows are read
        through what it read then, so they are filed under that. Should its files
        change even as it is indexed, the next lookup opens it again (see _open), finds
        its rows filed under a signature not its own, and builds them again.

        What went wrong is filed with them: what a reader left out of a dictionary it
        could read only in part (its `problems`), or why nothing of it could be read.
        A dictionary whose files are at fault is not tried again until they change -
        it was rebuilt from scratch at every lookup, each time "(once only)"."""
        db = self._connection()
        row = db.execute("SELECT id, signature, headwords, problem FROM dictionaries "
                         "WHERE path = ?", (dictionary.path,)).fetchone()
        if row and row[1] == sig:
            ident, _sig, count, problem = row
            failed = problem is not None and not count
            if problem is not None:
                self._problems[dictionary.path] = (sig, problem, failed)
            return None if failed else ident
        known = self._problems.get(dictionary.path)
        if known and known[0] == sig and known[2]:
            return None                 # failed this run, for a reason not its files'
        if status:
            status(f"Indexing {dictionary.name}{position or ''}… (once only)")
        try:
            with db:
                if row:
                    db.execute("DELETE FROM headwords WHERE dictionary = ?", (row[0],))
                    db.execute("DELETE FROM dictionaries WHERE id = ?", (row[0],))
                cursor = db.execute(
                    "INSERT INTO dictionaries (path, signature, name, headwords) "
                    "VALUES (?, '', ?, 0)", (dictionary.path, dictionary.name))
                ident = cursor.lastrowid
                count = 0
                batch = []
                for headword, offset, size in dictionary.headwords():
                    for spelling in (spellings(headword) if dictionary.kind == "dsl"
                                     else [headword]):
                        batch.append((ident, fold(spelling), spelling, offset, size))
                    if len(batch) >= 20000:
                        db.executemany("INSERT INTO headwords VALUES (?, ?, ?, ?, ?)",
                                       batch)
                        count += len(batch)
                        batch = []
                db.executemany("INSERT INTO headwords VALUES (?, ?, ?, ?, ?)", batch)
                count += len(batch)
                problem = "; ".join(getattr(dictionary, "problems", None) or ()) or None
                # Written last: an index cut short by a crash has no signature, and is
                # rebuilt rather than trusted.
                db.execute("UPDATE dictionaries SET signature = ?, headwords = ?, "
                           "problem = ? WHERE id = ?", (sig, count, problem, ident))
        except THE_FILES_FAULT as e:
            reason = str(e) or type(e).__name__
            with db:                    # filed as failed, for these files
                db.execute("DELETE FROM headwords WHERE dictionary IN "
                           "(SELECT id FROM dictionaries WHERE path = ?)",
                           (dictionary.path,))
                db.execute("DELETE FROM dictionaries WHERE path = ?", (dictionary.path,))
                db.execute("INSERT INTO dictionaries (path, signature, name, headwords, "
                           "problem) VALUES (?, ?, ?, 0, ?)",
                           (dictionary.path, sig, dictionary.name, reason))
            self._problem(dictionary, sig, reason, failed=True)
            return None
        if problem is not None:
            self._problem(dictionary, sig, problem, failed=False)
        else:
            self._problems.pop(dictionary.path, None)
        return ident

    def _problem(self, dictionary, sig, what, failed):
        """Remember what went wrong indexing `dictionary`, and say so in the error log -
        the window's bar says so too, on its button."""
        self._problems[dictionary.path] = (sig, what, failed)
        line = (f"[dictionaries] {dictionary.path}: "
                f"{'not indexed' if failed else 'indexed in part'}: {what}")
        print(line)
        errors.record(line)

    def prepare(self, status=None):
        """Index whatever needs it; [(dictionary, id)] of everything usable. An index
        that cannot be read raises, for look_up() to begin a new one."""
        with self._index_lock:
            self._forget_gone(self._connection())
            dictionaries = self._enabled()
            ready = []
            for number, (dictionary, opened_as) in enumerate(dictionaries, 1):
                try:
                    ident = self._ensure(dictionary, opened_as, status,
                                         f" ({number} of {len(dictionaries)})")
                except Exception as e:
                    if _unreadable(e):
                        raise
                    # Not the files' fault - the disk full, the file not readable just
                    # now: remembered until the app starts again, or the files change.
                    self._problem(dictionary, opened_as, f"{type(e).__name__}: {e}",
                                  failed=True)
                    continue
                if ident is not None:
                    ready.append((dictionary, ident))
            return ready

    # ---- lookups ----------------------------------------------------------

    def _entries(self, ident, keys, word=None):
        """One dictionary's entries filed under `keys`, in its own order - except that,
        given the `word` looked up, those headed by that very word, as plain() spells
        it, come first. A key leaves out punctuation and spacing, so "well" is filed
        with "we'll", and "resign" with "re-sign": GoldenDict too shows a word's own
        entries before the ones it only folds to."""
        marks = ",".join("?" * len(keys))
        rows = self._connection().execute(
            f"SELECT headword, offset, size FROM headwords "
            f"WHERE dictionary = ? AND key IN ({marks}) ORDER BY rowid LIMIT ?",
            (ident, *keys, MAX_ROWS)).fetchall()
        if word is not None:
            wanted = plain(word)
            rows.sort(key=lambda row: plain(row[0]) != wanted)     # stable
        seen, out = set(), []
        for headword, offset, size in rows:
            if (offset, size) in seen:
                continue                # one entry under two spellings
            seen.add((offset, size))
            out.append((headword, offset, size))
        return out[:MAX_ENTRIES]

    def _gather(self, ready, keys, word=None):
        """Each dictionary's entries filed under `keys`, rendered - as many as fit in
        markup.MAX_HTML of HTML, the most one entry may make: twelve entries of that
        each, all shown on the page, would hold up the window that shows them."""
        results = []
        for dictionary, ident in ready:
            entries, made = [], 0
            for headword, offset, size in self._entries(ident, keys, word):
                if made > markup.MAX_HTML:
                    break
                try:
                    html, text = dictionary.render(headword,
                                                   dictionary.read(offset, size))
                except Exception as e:
                    print(f"[dictionaries] {dictionary.name}, {headword!r}: {e}")
                    continue
                made += len(html)
                entries.append({"headword": headword, "html": html, "text": text})
            if entries:
                results.append({"dictionary": dictionary.name, "path": dictionary.path,
                                "entries": entries})
        return results

    def suggestions(self, ready, word, limit=MAX_SUGGESTIONS):
        """Headwords that start with what was typed, for when nothing matched it."""
        key = fold(word)
        if not key or not ready:
            return []
        ids = [ident for _d, ident in ready]
        marks = ",".join("?" * len(ids))
        rows = self._connection().execute(
            f"SELECT DISTINCT key, headword FROM headwords "
            f"WHERE key >= ? AND key < ? AND dictionary IN ({marks}) "
            f"ORDER BY key LIMIT ?", (key, key + "\U0010ffff", *ids, limit * 4)).fetchall()
        out, keys = [], set()
        for k, headword in rows:
            if k not in keys:
                keys.add(k)
                out.append(headword)
            if len(out) >= limit:
                break
        return out

    def look_up(self, word, status=None):
        """Everything the enabled dictionaries say about `word`.

        {"word", "found": the form that matched (the word itself, or a stem of it),
         "results": [{"dictionary", "entries": [{"headword", "html", "text"}]}],
         "suggestions": [headword...] when nothing matched}
        """
        word = " ".join((word or "").split())
        with self._index_lock:
            try:
                return self._look_up(word, status)
            except sqlite3.DatabaseError as e:
                if not _unreadable(e):
                    raise
                self._set_aside(e)
            return self._look_up(word, status)

    def _look_up(self, word, status):
        ready = self.prepare(status)
        if status:
            status(f"Looking up “{word}”…")
        results = self._gather(ready, [fold(word)], word)
        found = word
        if not results:
            for stem in stems(word):
                results = self._gather(ready, [fold(stem)], stem)
                if results:
                    found = stem
                    break
        suggestions = [] if results else self.suggestions(ready, word)
        return {"word": word, "found": found, "results": results,
                "suggestions": suggestions, "dictionaries": len(ready)}


def _unreadable(error):
    """Whether `error` says the index itself is broken - not a database, or a malformed
    one - rather than that it is busy or the disk is full."""
    return (isinstance(error, sqlite3.DatabaseError)
            and (getattr(error, "sqlite_errorname", None) or "").startswith(
                ("SQLITE_CORRUPT", "SQLITE_NOTADB")))


def shown(lookup, hidden=()):
    """`lookup` without the dictionaries in `hidden` (their paths): what a window that
    has switched them off shows, and gives the model."""
    if not hidden:
        return lookup
    return dict(lookup, results=[r for r in lookup["results"]
                                 if r.get("path") not in hidden])


def as_context(lookup, budget):
    """A lookup as text for the model, its dictionaries sharing `budget` characters."""
    from ..websearch import _cut, _share
    if not lookup["results"]:
        return ""
    blocks = []
    for result in lookup["results"]:
        text = "\n\n".join(entry["text"] for entry in result["entries"])
        blocks.append((result["dictionary"], text))
    shares = _share([len(text) for _name, text in blocks], budget)
    heading = f'Dictionary entries for "{lookup["word"]}"'
    if lookup["found"] != lookup["word"]:
        heading += f' (found under "{lookup["found"]}")'
    parts = [heading + ":"]
    for (name, text), share in zip(blocks, shares):
        cut = _cut(text, share)
        parts.append(f"## {name}\n{cut}" + (" […]" if len(cut) < len(text) else ""))
    return "\n\n".join(parts)


def as_markdown(lookup):
    """A lookup as Markdown: what Copy and Save write, and what the plain-text view
    shows. Each dictionary's text, under its name.

    Everything in it that the dictionaries or the reader wrote is escaped, not only
    the entries: a dictionary is named by its own #NAME or bookname, and one named
    "[Oxford](https://…)" was a live link under a name of its choosing, in the
    plain-text view and in what Copy and Save wrote. Its suggestions are its
    headwords, and the word is what was typed or selected."""
    word, found = _markdown_escape(lookup["word"]), _markdown_escape(lookup["found"])
    if not lookup["results"]:
        lines = [f"_No entry for “{word}”._"]
        if lookup["suggestions"]:
            lines.append("Nearby: " + ", ".join(_markdown_escape(suggestion)
                                                for suggestion in lookup["suggestions"]))
        return "\n\n".join(lines)
    parts = []
    if lookup["found"] != lookup["word"]:
        parts.append(f"_No entry for “{word}”; showing “{found}”._")
    for result in lookup["results"]:
        body = "\n\n".join(_markdown_escape(entry["text"]) for entry in result["entries"])
        parts.append(f"### {_markdown_escape(result['dictionary'])}\n\n{body}")
    return "\n\n".join(parts)


def _markdown_escape(text):
    out = []
    for char in text:
        if char in "\\`*_[]<>#|":
            out.append("\\")
        out.append(char)
    return "".join(out)
