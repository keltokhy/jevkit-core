"""Prove a consumer uses this core; optionally compare requests/results with its pre-extraction commit."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import httpx

import jevkit_core
from jevkit_core import transport


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in ("latencies", "answered_at")}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


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
    return {"requests": requests, "answers": answers, "stats": stats}


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
    if args.tool != "jselect":
        assert issubclass(consumer.Jev, jevkit_core.DecisionClient)
        assert issubclass(consumer.Cache, jevkit_core.AnswerCache)
    shared_calls = []
    original = transport.request_json

    async def observed(*values, **kwargs):
        shared_calls.append(kwargs["provider"])
        return await original(*values, **kwargs)

    with tempfile.TemporaryDirectory(prefix="jevkit-contract-") as temporary:
        temp = Path(temporary)
        transport.request_json = observed
        try:
            actual = asyncio.run(exercise(consumer, args.tool, temp / "current.sqlite"))
        finally:
            transport.request_json = original
        assert len(shared_calls) == len(actual["requests"]), "consumer bypassed shared transport"
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
            }
        )
    )


if __name__ == "__main__":
    main()
