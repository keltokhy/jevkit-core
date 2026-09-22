"""Thread-safe SQLite answers, compatible with the existing three-column table.

Storage is shared; key identity is explicit. Existing tools retain their exact legacy
key functions in adapters. New consumers may opt into the versioned answer_key.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path


def cache_path() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "jev" / "answers.sqlite"


def digest(parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def answer_key(model: str, state, question: dict, *, provider: str, endpoint: str) -> str:
    """Opt-in v1 identity; no unsafe fallback to ambiguous legacy keys."""
    return digest(["jevkit-answer-v1", provider, endpoint, model, state, question])


class AnswerCache:
    metadata = True

    def __init__(self, path: Path | None = None):
        path = Path(path) if path is not None else cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS answers "
            "(key TEXT PRIMARY KEY, answer TEXT NOT NULL, at REAL NOT NULL) WITHOUT ROWID"
        )
        if self.metadata:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS answer_metadata "
                "(key TEXT PRIMARY KEY, at REAL NOT NULL, metadata TEXT NOT NULL) WITHOUT ROWID"
            )

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT answer FROM answers WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_entry(self, key: str) -> tuple[dict, dict] | None:
        if not self.metadata:
            answer = self.get(key)
            return (answer, {}) if answer is not None else None
        with self._lock:
            row = self.db.execute(
                "SELECT a.answer, m.metadata FROM answers a LEFT JOIN answer_metadata m "
                "ON a.key = m.key AND a.at = m.at WHERE a.key = ?",
                (key,),
            ).fetchone()
        return (json.loads(row[0]), json.loads(row[1]) if row[1] else {}) if row else None

    def put(self, key: str, answer: dict, *, metadata: dict | None = None) -> None:
        encoded = json.dumps(answer)
        encoded_metadata = json.dumps(metadata) if metadata is not None else None
        with self._lock:
            at = time.time()
            if not self.metadata:
                self.db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?)", (key, encoded, at))
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?)", (key, encoded, at))
                if metadata is not None:
                    self.db.execute(
                        "INSERT OR REPLACE INTO answer_metadata VALUES (?, ?, ?)", (key, at, encoded_metadata)
                    )
                else:
                    self.db.execute("DELETE FROM answer_metadata WHERE key = ?", (key,))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self.db.close()
