"""Prove a consumer uses this core; optionally compare requests/results with its pre-extraction commit."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import httpx

import jevkit_core
from jevkit_core import transport, usage


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in ("latencies", "answered_at")}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def catalog_snapshot(module, tool):
    if tool != "jselect":
        # Older adapters implicitly had these capabilities before Backend gained fields.
        defaults = {
            "url_env": None,
            "requires_key": True,
            "auto_select": True,
            "cache_by_request": False,
            "price_per_mtok": module.PRICE_PER_MTOK,
        }
        return {name: defaults | asdict(backend) for name, backend in module.BACKENDS.items()}
    # Resolve with fixture credentials, including jselect's intentionally pinned models.
    with patch.dict(
        os.environ,
        {
            "JEV_API": "",
            "JEV_URL": "",
            "JEV_MODEL": "",
            "TYPESAFE_API_KEY": "fixture",
            "OPENROUTER_API_KEY": "fixture",
            "JEV_GATEWAY_API_KEY": "fixture",
            "JEV_GATEWAY_URL": "https://gateway.invalid",
        },
    ):
        return {
            name: {key: value for key, value in asdict(module.resolve_backend(name)).items() if key != "key"}
            for name in ("typesafe", "openrouter", "gateway")
        }


async def exercise(module, tool, path):
    requests = []

    async def respond(request):
        requests.append(request.content.decode())
        await asyncio.sleep(0.002)
        body = json.loads(request.content)
        answers = {}
        for qid, question in body["questions"].items():
            kind = question["type"]
            answers[qid] = (
                {"noul": 0.75}
                if kind == "noul"
                else {"choice": "yes", "probabilities": {"yes": 0.75, "no": 0.25}}
                if kind == "choice"
                else {"score": 1.0, "confidence": 0.8}
            )
        return httpx.Response(
            200,
            json={"answers": answers, "model": "resolved-v1", "usage": {"input_tokens": 100, "cost": 0.002}},
        )

    if tool == "jselect":
        from jselect.types import Passage

        scorer = module.JevScorer(
            module.Backend("openrouter", "https://fixture.invalid", "v1", "test", "env"),
            cache_path=path,
            transport=httpx.MockTransport(respond),
        )
        passages = [
            Passage("one", "évidence one", [{"source": "fixture", "line": 1}]),
            Passage("two", "évidence two", [{"source": "fixture", "line": 2}]),
        ]
        try:
            answers = [await scorer.score("the task", passages), await scorer.score("the task", passages)]
            stats = normalized(scorer.stats)
        finally:
            scorer.close()
    else:
        cache = module.Cache(path)
        client = module.Jev("test", model="v1", cache=cache, transport=httpx.MockTransport(respond))
        question = {
            "q": {"type": "noul", "instructions": "matches"},
            "r": {"type": "noul", "instructions": "relevant"},
        }
        try:
            answers = [await client.ask("évidence", question), await client.ask("évidence", question)]
            answers.extend(
                await asyncio.gather(client.ask("concurrent", question), client.ask("concurrent", question))
            )
            if tool == "jcol":
                answers.append(
                    await client.ask(
                        "typed",
                        {
                            "choice": {"type": "choice", "criteria": {"yes": "yes", "no": "no"}},
                            "score": {"type": "score", "criteria": ["low", "medium", "high"]},
                        },
                    )
                )
            stats = normalized(asdict(client.meter))
        finally:
            await client.close()
            cache.db.close()
    return {
        "requests": requests,
        "answers": answers,
        "stats": stats,
        "providers": catalog_snapshot(module, tool),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("jgrep", "jsort", "jlink", "jselect", "jcol"))
    parser.add_argument("--baseline-repo", type=Path)
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--expect-core", type=Path)
    args = parser.parse_args()
    if args.expect_core:
        assert Path(jevkit_core.__file__).resolve().parent == args.expect_core.resolve()
    name = "judge" if args.tool == "jselect" else "core"
    consumer = importlib.import_module(f"{args.tool}.{name}")
    assert consumer.backend_catalog is jevkit_core.backend_catalog
    if args.tool != "jselect":
        assert issubclass(consumer.Jev, jevkit_core.DecisionClient)
        assert issubclass(consumer.Cache, jevkit_core.AnswerCache)
    shared_calls = []
    charges, owners, origins = [], [], []
    original = transport.request_json
    original_record = usage.record_usage
    original_share = jevkit_core.DecisionClient.share_request
    original_provenance = jevkit_core.answer_provenance

    async def observed(*values, **kwargs):
        shared_calls.append(kwargs["provider"])
        return await original(*values, **kwargs)

    def recorded(totals, response_usage):
        charges.append(response_usage.cost)
        return original_record(totals, response_usage)

    def shared(client, keys, start):
        result = original_share(client, keys, start)
        owners.append(result[1])
        return result

    def provenance(**kwargs):
        result = original_provenance(**kwargs)
        origins.append(result)
        return result

    with tempfile.TemporaryDirectory(prefix="jevkit-contract-") as temporary:
        temp = Path(temporary)
        with ExitStack() as stack:
            stack.enter_context(patch.object(transport, "request_json", observed))
            stack.enter_context(patch.object(usage, "record_usage", recorded))
            stack.enter_context(patch.object(jevkit_core.DecisionClient, "share_request", shared))
            if args.tool == "jselect":
                stack.enter_context(patch.object(consumer, "record_usage", recorded))
            if args.tool in ("jsort", "jlink"):
                stack.enter_context(patch.object(consumer, "answer_provenance", provenance))
            actual = asyncio.run(exercise(consumer, args.tool, temp / "current.sqlite"))
        assert len(shared_calls) == len(actual["requests"]), "consumer bypassed shared transport"
        assert len(charges) == len(actual["requests"]), "consumer bypassed shared accounting"
        if args.tool != "jselect":
            assert sum(owners) == len(actual["requests"]) and owners.count(False) == 1
        if args.tool in ("jsort", "jlink"):
            assert len(origins) == len(actual["requests"]), "consumer bypassed shared provenance"
        compared = False
        if args.baseline_repo:
            source = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(args.baseline_repo),
                    "show",
                    f"{args.baseline_ref}:src/{args.tool}/{name}.py",
                ],
                text=True,
            )
            file = temp / "baseline.py"
            file.write_text(source)
            module_name = f"{args.tool}._baseline_{name}"
            spec = importlib.util.spec_from_file_location(module_name, file)
            baseline = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = baseline
            spec.loader.exec_module(baseline)
            expected = asyncio.run(exercise(baseline, args.tool, temp / "baseline.sqlite"))
            assert actual == expected, json.dumps({"current": actual, "baseline": expected}, indent=2)
            compared = True
    print(
        json.dumps(
            {
                "tool": args.tool,
                "core": jevkit_core.__file__,
                "requests": len(shared_calls),
                "baseline_matched": compared,
                "shared_transport_verified": True,
                "shared_accounting_verified": True,
                "shared_request_verified": args.tool != "jselect",
                "shared_provenance_verified": args.tool in ("jsort", "jlink"),
            }
        )
    )


if __name__ == "__main__":
    main()
