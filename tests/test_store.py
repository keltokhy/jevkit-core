import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from jevkit_runtime import AnswerStore, Entry, JevFatal
from jevkit_runtime.store import SCHEMA_VERSION


def test_store_is_private_and_lives_under_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    store = AnswerStore()
    assert store.path == tmp_path / "jev" / "answers.sqlite"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.close()


def test_answers_and_their_origin_are_one_row(tmp_path):
    store = AnswerStore(tmp_path / "answers.sqlite")
    assert store.get("k") is None and store.entry("k") is None
    store.put("k", {"noul": 0.7}, {"resolved_model": "v1"})
    entry = store.entry("k")
    assert isinstance(entry, Entry)
    assert (entry.answer, entry.metadata) == ({"noul": 0.7}, {"resolved_model": "v1"})
    assert entry.at > 0
    store.put("k", {"noul": 0.9})
    assert store.entry("k").metadata is None and store.get("k") == {"noul": 0.9}
    store.close()


def test_earlier_schemas_are_reset_rather_than_misread(tmp_path):
    path = tmp_path / "answers.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE answers "
            "(key TEXT PRIMARY KEY, answer TEXT NOT NULL, at REAL NOT NULL) WITHOUT ROWID"
        )
        db.execute(
            "CREATE TABLE answer_metadata (key TEXT PRIMARY KEY, at REAL NOT NULL, metadata TEXT NOT NULL)"
        )
        db.execute("INSERT INTO answers VALUES ('old', ?, 1)", (json.dumps({"noul": 0.2}),))
    store = AnswerStore(path)
    assert store.get("old") is None
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert [row[1] for row in store.db.execute("PRAGMA table_info(answers)")] == [
        "key",
        "answer",
        "metadata",
        "at",
    ]
    assert not store.db.execute("SELECT name FROM sqlite_master WHERE name='answer_metadata'").fetchall()
    store.put("new", {"noul": 0.4})
    store.close()
    assert AnswerStore(path).get("new") == {"noul": 0.4}


def test_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "answers.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(JevFatal, match="newer JevKit runtime"):
        AnswerStore(path)


def test_concurrent_writers_keep_answer_and_origin_together(tmp_path):
    store = AnswerStore(tmp_path / "answers.sqlite")

    def write(i):
        store.put("same", {"noul": i / 100}, {"i": i})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(100)))
    entry = store.entry("same")
    assert entry.answer["noul"] == entry.metadata["i"] / 100
    store.close()


def test_a_read_only_store_never_creates_migrates_or_writes(tmp_path):
    missing = AnswerStore(tmp_path / "none" / "answers.sqlite", read_only=True)
    assert missing.get("k") is None and not (tmp_path / "none").exists()
    path = tmp_path / "answers.sqlite"
    store = AnswerStore(path)
    store.put("k", {"noul": 0.7})
    store.close()
    reader = AnswerStore(path, read_only=True)
    assert reader.get("k") == {"noul": 0.7}
    with pytest.raises(ValueError, match="read-only"):
        reader.put("k", {"noul": 0.1})
    reader.close()
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    older = AnswerStore(path, read_only=True)
    assert older.get("k") is None
    older.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION - 1
