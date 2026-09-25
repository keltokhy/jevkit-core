import argparse
import asyncio
import io
import json
import math

import httpx
import pytest

from jevkit_runtime import (
    AnswerStore,
    Backend,
    Budget,
    Client,
    JevBudgetExceeded,
    JevFatal,
    Noul,
    Settings,
    catalog,
)
from jevkit_runtime.cli import (
    add_runtime_args,
    budget_from_args,
    parse_budget,
    providers_help,
    run_sync,
    runtime_from_args,
    show_stats,
    stats_line,
)
from jevkit_runtime.run import Run, fingerprint, warnings

PROVIDERS = catalog("typesafe", "openrouter", "gateway", "laya")
BACKEND = Backend("typesafe", "https://user:secret@fixture.invalid/v1?token=abc", "jev-1.13.0", key="k")


def answering(model="jev-1.13.0"):
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "answers": {q: {"noul": 0.5} for q in json.loads(request.content)["questions"]},
                "model": model,
                "usage": {"input_tokens": 10, "cost": 0.001},
            },
        )
    )


def test_a_run_record_says_what_was_asked_of_whom_and_at_what_cost(tmp_path):
    run = Run("jtool", "1.2.3", inputs=fingerprint(["a", "b"]))
    budget = Budget(1.0)

    async def exercise():
        store = AnswerStore(tmp_path / "answers.sqlite")
        async with Client(BACKEND, budget=budget, store=store, transport=answering()) as client:
            await client.ask("a", {"q": Noul("matches")})
            await client.ask("b", {"q": Noul("matches"), "r": Noul("{slot} relevant").at("x")})
            await client.ask("a", {"q": Noul("matches")})
            return run.record(client, fields={"threshold": 0.5})

    record = run_sync(exercise())
    assert record["record_version"] == 1 and (record["tool"], record["tool_version"]) == ("jtool", "1.2.3")
    assert record["backends"] == [
        {
            "provider": "typesafe",
            "endpoint": "https://fixture.invalid/v1",
            "requested_model": "jev-1.13.0",
            "joint_reads": False,
        }
    ]
    assert [q["text"] for q in record["questions"]] == ["matches", "x relevant"]
    assert record["usage"]["calls"] == 2 and record["usage"]["cost"] == pytest.approx(0.002)
    assert record["budget"] == [{"limit": 1.0, "spent": pytest.approx(0.002), "refused": 0, "rises": 1}]
    assert {a["source"]: a["count"] for a in record["answered_by"]} == {"api": 3, "cache": 1}
    assert record["inputs"] == fingerprint(["a", "b"]) and record["fields"] == {"threshold": 0.5}
    assert record["mixed_models"] == {} and warnings(record) == []
    json.dumps(record)


def test_mixed_models_and_refused_requests_are_warnings():
    run = Run("jtool", "1")
    first, second = Client(BACKEND, transport=answering("v1")), Client(BACKEND, transport=answering("v2"))

    async def exercise():
        await first.ask("a", {"q": Noul("x")})
        await second.ask("b", {"q": Noul("x")})
        second.budget = first.budget = Budget(0)
        with pytest.raises(JevBudgetExceeded):
            await first.ask("c", {"q": Noul("x")})
        return run.record([first, second])

    record = run_sync(exercise())
    assert record["mixed_models"] == {} and len(record["budget"]) == 1  # each meter saw one model
    record["mixed_models"] = {"typesafe/jev-1.13.0": ["v1", "v2"]}
    found = warnings(record)
    assert len(found) == 1 and "answered by 2 models (v1, v2)" in found[0]
    assert record["budget"][0]["refused"] == 1


def test_fingerprints_follow_order_and_content():
    assert fingerprint([]) == {"count": 0, "sha256": fingerprint([])["sha256"]}
    assert fingerprint(["a", "b"]) != fingerprint(["b", "a"]) and fingerprint([{"x": 1}])["count"] == 1


def test_runtime_flags_mean_the_same_in_every_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "JEV_API", "JEV_BUDGET", "JEV_MODEL", "JEV_URL"):
        monkeypatch.delenv(name, raising=False)
    ap = argparse.ArgumentParser(epilog=providers_help(PROVIDERS))
    add_runtime_args(ap, PROVIDERS, default_budget=1.0)
    args = ap.parse_args([])
    assert budget_from_args(args).limit == 1.0 and (args.timeout, args.concurrency) == (15.0, 32)
    assert budget_from_args(ap.parse_args(["--budget", "none"])).unlimited
    assert budget_from_args(args, Settings.from_env({"JEV_BUDGET": "0.3"})).limit == 0.3
    for bad in (["--budget", "-1"], ["--budget", "lots"], ["-j", "0"], ["--timeout", "0"], ["--api", "nope"]):
        with pytest.raises(SystemExit):
            ap.parse_args(bad)
    with pytest.raises(JevFatal, match="no API key"):
        runtime_from_args(args, PROVIDERS)
    cache_only = runtime_from_args(ap.parse_args(["--budget", "0", "--no-cache"]), PROVIDERS)
    assert cache_only.store is None and cache_only.budget.limit == 0
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = runtime_from_args(ap.parse_args(["-j", "4", "--model", "m"]), PROVIDERS)
    assert (client.backend.name, client.backend.model, client.concurrency) == ("openrouter", "m", 4)
    assert client.store is not None and client.store.path.parent == tmp_path / "cache" / "jev"
    client.store.close()


def test_help_text_and_stats_come_from_the_catalog_and_the_meter():
    text = providers_help(PROVIDERS, Settings.from_env({"XDG_CONFIG_HOME": "/cfg"}))
    assert "TypeSafe's API (TYPESAFE_API_KEY)" in text and "(JEV_GATEWAY_URL and JEV_GATEWAY_API_KEY)" in text
    assert "/cfg/jev/typesafe.key, openrouter.key or gateway.key" in text
    assert "--api laya (laya-mlx, JEV_LAYA_URL)" in text and "diffusiongemma" not in text
    client = Client(BACKEND, budget=Budget(2.0))
    assert stats_line(client, 1.25) == "0 calls, 0 cached; budget $2.00; 1.2s"
    assert stats_line(Client(BACKEND)) == "0 calls, 0 cached"
    tty, pipe = io.StringIO(), io.StringIO()
    tty.isatty = lambda: True
    assert show_stats(argparse.Namespace(stats=None), tty) and not show_stats(
        argparse.Namespace(stats=None), pipe
    )
    assert show_stats(argparse.Namespace(stats=True), pipe)


def test_run_sync_works_inside_a_running_loop():
    async def inner():
        return 7

    async def outer():
        return run_sync(inner())

    assert run_sync(inner()) == 7 and asyncio.run(outer()) == 7
    assert parse_budget("NONE") == math.inf
