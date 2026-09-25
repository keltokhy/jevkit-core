"""Answers on disk: one table, one versioned schema, keyed by `protocol.answer_key`."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import JevFatal
from .settings import Settings

SCHEMA_VERSION = 3  # 3: answer keys v3 (typed questions, scopes, packed items)


@dataclass(frozen=True)
class Entry:
    answer: dict
    metadata: dict | None
    at: float


class AnswerStore:
    """Thread-safe SQLite storage. Each row carries its answer and who produced it, written together.

    `read_only=True` opens an existing store without creating, migrating or writing it, for previews
    and estimates; a missing store, or one of another schema, simply has no answers.
    """

    def __init__(
        self, path: Path | str | None = None, *, settings: Settings | None = None, read_only: bool = False
    ):
        self.path = Path(path) if path is not None else self.default_path(settings)
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            self.db = None
            if self.path.is_file():
                db = sqlite3.connect(
                    f"{self.path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
                )
                if db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                    self.db = db
                else:
                    db.close()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        try:
            os.chmod(self.path, 0o600)  # answers quote the caller's own text
        except OSError:
            pass
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._prepare()

    @staticmethod
    def default_path(settings: Settings | None = None) -> Path:
        return (settings or Settings.from_env()).cache_dir / "answers.sqlite"

    def _prepare(self) -> None:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise JevFatal(f"{self.path} was written by a newer JevKit runtime (schema {version})")
                if version < SCHEMA_VERSION:
                    # Earlier schemas used keys this version cannot reproduce; start over.
                    self.db.execute("DROP TABLE IF EXISTS answers")
                    self.db.execute("DROP TABLE IF EXISTS answer_metadata")
                    self.db.execute(
                        "CREATE TABLE answers (key TEXT PRIMARY KEY, answer TEXT NOT NULL, "
                        "metadata TEXT, at REAL NOT NULL) WITHOUT ROWID"
                    )
                    self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, key: str) -> dict | None:
        entry = self.entry(key)
        return entry.answer if entry is not None else None

    def entry(self, key: str) -> Entry | None:
        if self.db is None:
            return None
        with self._lock:
            row = self.db.execute("SELECT answer, metadata, at FROM answers WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return Entry(json.loads(row[0]), json.loads(row[1]) if row[1] else None, row[2])

    def put(self, key: str, answer: dict, metadata: dict | None = None) -> None:
        if self.read_only:
            raise ValueError(f"{self.path} was opened read-only")
        values = (
            key,
            json.dumps(answer),
            json.dumps(metadata) if metadata is not None else None,
            time.time(),
        )
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?)", values)

    def close(self) -> None:
        with self._lock:
            if self.db is not None:
                self.db.close()
