"""The user's dictionaries: found in a folder, indexed once, looked up together.

GoldenDict keeps an index file per dictionary; this keeps one SQLite database for all of
them, in ~/.cache/da-assistant. Each dictionary's rows are rebuilt only when one of its
files changes size or modification time, so the first lookup after pointing the app at
a folder is the slow one - about a second per hundred megabytes - and every lookup after
that is an indexed query.

Keys are folded the way GoldenDict folds them: case and diacritics do not matter, and
neither do runs of whitespace, so "Cafe" finds "café".
"""

import os
import sqlite3
import threading
import unicodedata

from .dsl import DSL, spellings
from .stardict import StarDict

CACHE_DIR = os.path.expanduser("~/.cache/da-assistant")
INDEX_FILE = "dictionaries.sqlite"
SCHEMA = 2

# Entries shown per dictionary for one word. A homograph-rich word ("set", "run") has a
# dozen in some dictionaries; past this many they are rarely the one wanted.
MAX_ENTRIES = 12
MAX_SUGGESTIONS = 24


def fold(word):
    """The index key for a headword or a query."""
    text = unicodedata.normalize("NFKD", word or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def stems(word):
    """Likely dictionary forms of an inflected English word, most likely first.

    Not a morphological analyser - GoldenDict uses Hunspell for that - but it turns the
    common inflections back into a headword: runs, running, ran is beyond it, but
    "studies", "stopped" and "biggest" are not.
    """
    w = fold(word)
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
    they answer every lookup of "n" or "adj" with a line of nothing.
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
                found.append(("stardict", path))
            elif lower.endswith((".dsl", ".dsl.dz")):
                found.append(("dsl", path))
    return found


def open_dictionary(kind, path):
    return StarDict(path) if kind == "stardict" else DSL(path)


def signature(dictionary):
    parts = []
    for path in dictionary.files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        parts.append(f"{os.path.basename(path)}:{st.st_size}:{int(st.st_mtime)}")
    return f"{SCHEMA}|" + "|".join(parts)


class Library:
    """Lookups across every enabled dictionary in a folder."""

    def __init__(self, config, cache_dir=CACHE_DIR):
        self.config = config
        self.cache_dir = cache_dir
        self._lock = threading.RLock()
        self._db = None
        self._opened = {}           # path -> dictionary object, or the error opening it

    # ---- the index --------------------------------------------------------

    def _connection(self):
        if self._db is None:
            os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
            db = sqlite3.connect(os.path.join(self.cache_dir, INDEX_FILE),
                                 check_same_thread=False)
            db.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS dictionaries (
                    id INTEGER PRIMARY KEY, path TEXT UNIQUE, signature TEXT,
                    name TEXT, headwords INTEGER);
                CREATE TABLE IF NOT EXISTS headwords (
                    dictionary INTEGER, key TEXT, headword TEXT,
                    offset INTEGER, size INTEGER);
                CREATE INDEX IF NOT EXISTS headwords_key ON headwords (key, dictionary);
            """)
            self._db = db
        return self._db

    def folder(self):
        return os.path.expanduser(getattr(self.config, "dictionaries_dir", "") or "")

    def _all(self):
        """Every dictionary in the folder that opens, in the user's order: the ones
        Settings has placed, as placed, then the rest by name."""
        found = []
        for kind, path in discover(self.folder()):
            dictionary = self._open(kind, path)
            if not isinstance(dictionary, Exception):
                found.append(dictionary)
        order = {path: i for i, path in
                 enumerate(getattr(self.config, "dictionaries_order", None) or [])}
        return sorted(found, key=lambda d: (order.get(d.path, len(order)),
                                            d.name.casefold()))

    def available(self):
        """[(path, name)] of every dictionary in the folder, enabled or not, in order -
        what Settings lists. Opening one reads its header only."""
        return [(d.path, d.name) for d in self._all()]

    def listing(self):
        """[(path, name, icon path or None, about)] of the enabled dictionaries, in
        order - the dictionary window's bar, which shows `about` on hover."""
        return [(d.path, d.name, d.icon, d.about()) for d in self.enabled()]

    def _open(self, kind, path):
        with self._lock:
            if path not in self._opened:
                try:
                    self._opened[path] = open_dictionary(kind, path)
                except Exception as e:      # a broken file must not stop the others
                    self._opened[path] = e
            return self._opened[path]

    def enabled(self):
        disabled = set(getattr(self.config, "dictionaries_disabled", None) or [])
        return [d for d in self._all() if d.path not in disabled]

    def _ensure(self, dictionary, status=None, position=None):
        """The dictionary's id in the index, building its rows if they are stale."""
        db = self._connection()
        sig = signature(dictionary)
        row = db.execute("SELECT id, signature FROM dictionaries WHERE path = ?",
                         (dictionary.path,)).fetchone()
        if row and row[1] == sig:
            return row[0]
        if status:
            status(f"Indexing {dictionary.name}{position or ''}… (once only)")
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
                    db.executemany("INSERT INTO headwords VALUES (?, ?, ?, ?, ?)", batch)
                    count += len(batch)
                    batch = []
            db.executemany("INSERT INTO headwords VALUES (?, ?, ?, ?, ?)", batch)
            count += len(batch)
            # Written last: an index cut short by a crash has no signature, and is
            # rebuilt rather than trusted.
            db.execute("UPDATE dictionaries SET signature = ?, headwords = ? WHERE id = ?",
                       (sig, count, ident))
        return ident

    def prepare(self, status=None):
        """Index whatever needs it; [(dictionary, id)] of everything usable."""
        with self._lock:
            dictionaries = self.enabled()
            ready = []
            for number, dictionary in enumerate(dictionaries, 1):
                try:
                    ident = self._ensure(dictionary, status,
                                         f" ({number} of {len(dictionaries)})")
                except Exception as e:
                    print(f"[dictionaries] {dictionary.path}: {e}")
                    continue
                ready.append((dictionary, ident))
            return ready

    # ---- lookups ----------------------------------------------------------

    def _entries(self, ident, keys):
        marks = ",".join("?" * len(keys))
        rows = self._connection().execute(
            f"SELECT headword, offset, size FROM headwords "
            f"WHERE dictionary = ? AND key IN ({marks}) ORDER BY rowid",
            (ident, *keys)).fetchall()
        seen, out = set(), []
        for headword, offset, size in rows:
            if (offset, size) in seen:
                continue                # one entry under two spellings
            seen.add((offset, size))
            out.append((headword, offset, size))
        return out[:MAX_ENTRIES]

    def _gather(self, ready, keys):
        results = []
        for dictionary, ident in ready:
            entries = []
            for headword, offset, size in self._entries(ident, keys):
                try:
                    html, text = dictionary.render(headword,
                                                   dictionary.read(offset, size))
                except Exception as e:
                    print(f"[dictionaries] {dictionary.name}, {headword!r}: {e}")
                    continue
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
        with self._lock:
            ready = self.prepare(status)
            if status:
                status(f"Looking up “{word}”…")
            results = self._gather(ready, [fold(word)])
            found = word
            if not results:
                for stem in stems(word):
                    results = self._gather(ready, [stem])
                    if results:
                        found = stem
                        break
            suggestions = [] if results else self.suggestions(ready, word)
        return {"word": word, "found": found, "results": results,
                "suggestions": suggestions, "dictionaries": len(ready)}


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
    shows. Each dictionary's text, under its name."""
    if not lookup["results"]:
        lines = [f"_No entry for “{lookup['word']}”._"]
        if lookup["suggestions"]:
            lines.append("Nearby: " + ", ".join(lookup["suggestions"]))
        return "\n\n".join(lines)
    parts = []
    if lookup["found"] != lookup["word"]:
        parts.append(f"_No entry for “{lookup['word']}”; showing “{lookup['found']}”._")
    for result in lookup["results"]:
        body = "\n\n".join(_markdown_escape(entry["text"]) for entry in result["entries"])
        parts.append(f"### {result['dictionary']}\n\n{body}")
    return "\n\n".join(parts)


def _markdown_escape(text):
    out = []
    for char in text:
        if char in "\\`*_[]<>#|":
            out.append("\\")
        out.append(char)
    return "".join(out)
