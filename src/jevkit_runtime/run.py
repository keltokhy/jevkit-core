"""What a run asked, of whom, at what cost: one provenance record for every tool.

    run = Run("jgrep", __version__, inputs=fingerprint(lines))
    ... client.ask(...) ...
    record = run.record(client)          # versioned, JSON-ready
    for warning in warnings(record): print(warning, file=sys.stderr)

The record is built from the clients' meters and budgets, so a tool cannot forget a question it asked
or a model that answered. A tool adds its own settings under `fields`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from importlib.metadata import version
from urllib.parse import urlsplit, urlunsplit

from .client import Client

RECORD_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _endpoint(url: str) -> str:
    """The endpoint without credentials or a query string, which a gateway URL might carry."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def fingerprint(items: Iterable) -> dict:
    """How many items a run read, and a digest of them in order, so a record can say what it was run on."""
    digest, count = hashlib.sha256(), 0
    for item in items:
        digest.update(json.dumps(item, sort_keys=True, ensure_ascii=False, default=str).encode())
        digest.update(b"\n")
        count += 1
    return {"count": count, "sha256": digest.hexdigest()}


class Run:
    """One run of a tool. Start it before asking anything; `record` it at the end."""

    def __init__(self, tool: str, tool_version: str, *, inputs: dict | None = None):
        self.tool, self.tool_version = tool, tool_version
        self.inputs = dict(inputs or {})
        self.started_at = _now()

    def record(self, clients: Client | Sequence[Client], *, fields: dict | None = None) -> dict:
        clients = [clients] if isinstance(clients, Client) else list(clients)
        meters = [c.meter for c in clients]
        budgets = list({id(c.budget): c.budget for c in clients}.values())
        answered: dict[tuple, int] = {}
        for meter in meters:
            for p in meter.answer_provenance:
                key = tuple(p.get(k) for k in ("provider", "requested_model", "resolved_model", "source"))
                answered[key] = answered.get(key, 0) + p["count"]
        questions: dict[str, dict] = {}
        for meter in meters:
            questions.update(meter.questions)
        return {
            "record_version": RECORD_VERSION,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "runtime_version": version("jevkit-runtime"),
            "started_at": self.started_at,
            "finished_at": _now(),
            "backends": [
                {
                    "provider": c.backend.name,
                    "endpoint": _endpoint(c.backend.url),
                    "requested_model": c.backend.model,
                    "joint_reads": c.backend.joint_reads,
                }
                for c in clients
            ],
            "answered_by": [
                dict(zip(("provider", "requested_model", "resolved_model", "source"), key, strict=True))
                | {"count": count}
                for key, count in answered.items()
            ],
            "unknown_model_answers": sum(m.unknown_model_answers for m in meters),
            "mixed_models": {k: v for m in meters for k, v in m.mixed_models.items()},
            "questions": [
                {"digest": digest, "type": body["type"], "text": body["instructions"], "body": body}
                for digest, body in questions.items()
            ],
            "questions_not_listed": sum(m.questions_not_listed for m in meters),
            "usage": {
                "calls": sum(m.calls for m in meters),
                "cached": sum(m.cached for m in meters),
                "retries": sum(m.retries for m in meters),
                "hedges": sum(m.hedges for m in meters),
                "input_tokens": sum(m.input_tokens for m in meters),
                "cost": sum(m.cost for m in meters),
                "cost_sources": _added(m.cost_sources for m in meters),
            },
            "budget": [
                {
                    "limit": None if math.isinf(b.limit) else b.limit,
                    "spent": b.spent,
                    "refused": b.refused,
                    "rises": b.rises,
                }
                for b in budgets
            ],
            "inputs": self.inputs,
            "fields": dict(fields or {}),
        }


def _added(counts: Iterable[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for count in counts:
        for k, v in count.items():
            total[k] = total.get(k, 0) + v
    return total


def warnings(record: dict) -> list[str]:
    """What a person should be told that a tool would not otherwise say: a requested model that more than
    one model answered, and spending past a limit, which happens only when a price rises mid-flight.
    Budget stops and failures are the tool's to word."""
    found = [
        f"{asked} was answered by {len(models)} models ({', '.join(models)}); "
        "clear the cache or pin one model to keep a run on one"
        for asked, models in record["mixed_models"].items()
    ]
    for budget in record["budget"]:
        if budget["limit"] is not None and budget["spent"] > budget["limit"] * (1 + 1e-9):
            found.append(
                f"spent ${budget['spent']:.4f} against a ${budget['limit']:.2f} budget: the price rose "
                "above its estimate while requests were in the air"
            )
    return found
