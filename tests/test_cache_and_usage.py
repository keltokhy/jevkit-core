import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from jevkit_core import AnswerCache, JevFatal, answer_key, parse_usage


def test_versioned_keys_isolate_provider_endpoint_and_preserve_unicode():
    args = ("m", "évidence", {"type": "noul", "instructions": "rule"})
    key = answer_key(*args, provider="one", endpoint="https://one")
    assert key == answer_key(*args, provider="one", endpoint="https://one")
    assert key != answer_key(*args, provider="two", endpoint="https://one")
    assert key != answer_key(*args, provider="one", endpoint="https://two")


def test_legacy_storage_and_metadata_transaction(tmp_path):
    path = tmp_path / "answers.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE answers (key TEXT PRIMARY KEY, answer TEXT NOT NULL, "
            "at REAL NOT NULL) WITHOUT ROWID"
        )
        db.execute("INSERT INTO answers VALUES ('old', ?, 1)", (json.dumps({"noul": 0.2}),))
    cache = AnswerCache(path)
    assert cache.get_entry("old") == ({"noul": 0.2}, {})
    cache.put("new", {"noul": 0.7}, metadata={"resolved_model": "v1"})
    cache.db.execute(
        "CREATE TRIGGER reject_metadata BEFORE INSERT ON answer_metadata "
        "BEGIN SELECT RAISE(FAIL, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        cache.put("new", {"noul": 0.9}, metadata={"resolved_model": "v2"})
    assert cache.get_entry("new") == ({"noul": 0.7}, {"resolved_model": "v1"})
    cache.close()


def test_concurrent_writes_keep_answers_and_origins_together(tmp_path):
    cache = AnswerCache(tmp_path / "answers.sqlite")

    def write(i):
        cache.put("same", {"noul": i / 100}, metadata={"i": i})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(100)))
    answer, metadata = cache.get_entry("same")
    assert answer["noul"] == metadata["i"] / 100
    cache.close()


def test_older_writer_cannot_attribute_an_answer_to_stale_metadata(tmp_path):
    class PlainCache(AnswerCache):
        metadata = False

    path = tmp_path / "answers.sqlite"
    cache, plain = AnswerCache(path), PlainCache(path)
    cache.put("key", {"noul": 0.1}, metadata={"resolved_model": "v1"})
    plain.put("key", {"noul": 0.8})
    assert cache.get_entry("key") == ({"noul": 0.8}, {})
    cache.close()
    plain.close()


@pytest.mark.parametrize(
    "usage",
    [
        [],
        False,
        {"input_tokens": True},
        {"input_tokens": -1},
        {"cost": True},
        {"cost": float("nan")},
        {"cost": float("inf")},
        {"cost": -1},
        {"cost": "0.5"},
        {"input_tokens": 10**400},
    ],
)
def test_invalid_metering_stops_before_any_accounting(usage):
    with pytest.raises(JevFatal, match="invalid API usage"):
        parse_usage(usage)


def test_usage_distinguishes_reported_zero_and_estimated_missing_cost():
    paid = parse_usage({"input_tokens": 100, "cost": 0})
    assert paid.cost == 0 and paid.source == "reported_by_api"
    estimated = parse_usage({}, missing_tokens=100, price_per_mtok=0.042)
    assert estimated.cost == pytest.approx(0.0000042)
    assert estimated.source == "estimated_from_tokens"
    assert parse_usage({"input_tokens": 2.5}, fractional_tokens=True).tokens == 2.5
