"""A small local database shared by every run: translation memory + termbase.

- Translation memory (TM): every translated paragraph, so a paragraph that was already
  translated - in this document or any later one - is reused instead of paid for again.
- Termbase: agreed translations of key terms, with the votes that chose them.

Human corrections always win: a row marked human=1 (TM) or status='human' (terms)
is never overwritten by a model.

SQLite is a full SQL database stored in one file; it is built into Python.
"""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .docx_io import normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS tm (
    key      TEXT PRIMARY KEY,        -- hash of language + normalized source text
    lang     TEXT NOT NULL,
    source   TEXT NOT NULL,
    target   TEXT NOT NULL,
    provider TEXT,
    human    INTEGER NOT NULL DEFAULT 0,
    updated  TEXT
);
CREATE TABLE IF NOT EXISTS terms (
    source    TEXT NOT NULL,
    lang      TEXT NOT NULL,
    target    TEXT NOT NULL,
    votes     TEXT,                   -- JSON: {"gemini": "...", "claude": "...", ...}
    agreement REAL,                   -- share of the vote weight the winner got, 0..1
    status    TEXT NOT NULL,          -- agreed | single | disputed | human
    updated   TEXT,
    PRIMARY KEY (source, lang)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = "translator.db", lang: str = "kk"):
        self.path, self.lang = path, lang
        self._conn = sqlite3.connect(path, check_same_thread=False)  # we guard it with a lock
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.lang}\n{normalize(text)}".encode("utf-8")).hexdigest()

    # ---------------------------------------------------- translation memory

    def tm_get_many(self, texts: list) -> dict:
        """{source text: translation} for the texts already in memory."""
        found = {}
        with self._lock:
            for text in texts:
                row = self._conn.execute("SELECT target FROM tm WHERE key = ?", (self._key(text),)).fetchone()
                if row:
                    found[text] = row[0]
        return found

    def tm_put_many(self, pairs: list, provider: str = "", human: bool = False) -> None:
        """pairs: [(source, target, provider_or_None)]. A machine translation never replaces a human one."""
        rows = [(self._key(s), self.lang, normalize(s), t, p or provider, int(human), _now()) for s, t, p in pairs]
        with self._lock, self._conn:  # `with conn` = one transaction, committed at the end
            self._conn.executemany(
                """INSERT INTO tm (key, lang, source, target, provider, human, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       target = excluded.target, provider = excluded.provider,
                       human = excluded.human, updated = excluded.updated
                   WHERE tm.human = 0 OR excluded.human = 1""", rows)

    # ---------------------------------------------------- termbase

    def terms(self) -> dict:
        """{source term: (target, status)}"""
        with self._lock:
            rows = self._conn.execute("SELECT source, target, status FROM terms WHERE lang = ?",
                                      (self.lang,)).fetchall()
        return {s: (t, st) for s, t, st in rows}

    def term_rows(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, target, votes, agreement, status FROM terms WHERE lang = ? ORDER BY status, source",
                (self.lang,)).fetchall()
        return [{"source": s, "target": t, "votes": json.loads(v or "{}"), "agreement": a, "status": st}
                for s, t, v, a, st in rows]

    def term_put(self, source: str, target: str, votes: dict = None, agreement: float = 1.0,
                 status: str = "agreed") -> None:
        """Save a term decision. A model decision never replaces a human one."""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO terms (source, lang, target, votes, agreement, status, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, lang) DO UPDATE SET
                       target = excluded.target, votes = excluded.votes, agreement = excluded.agreement,
                       status = excluded.status, updated = excluded.updated
                   WHERE terms.status != 'human' OR excluded.status = 'human'""",
                (source, self.lang, target, json.dumps(votes or {}, ensure_ascii=False),
                 agreement, status, _now()))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
