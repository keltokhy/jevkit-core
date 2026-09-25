"""The System One decision protocol: request bodies, typed answers, usage, and answer identity."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import JevError, JevFatal
from .providers import Backend
from .question import Question

ANSWER_KEY_VERSION = "jevkit/answer/v3"


def digest(parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _who(backend: Backend, scope: str | None) -> list:
    return [ANSWER_KEY_VERSION, backend.name, backend.url, backend.model, scope]


def answer_key(backend: Backend, state, question: Question, *, scope: str | None = None) -> str:
    """One identity for every tool: who answered, at which endpoint, with which model, asked what.

    `scope` lets a tool keep its answers apart from others that ask the same; it is added to the key and
    never replaces it.
    """
    return digest([*_who(backend, scope), state, question.body()])


def answer_keys(
    backend: Backend, state, questions: Mapping[str, Question], *, scope: str | None = None
) -> dict[str, str]:
    """Each question's identity. Under joint reads it is the whole batch, which every answer depends on."""
    if not backend.joint_reads:
        return {qid: answer_key(backend, state, q, scope=scope) for qid, q in questions.items()}
    batch = [[qid, q.body()] for qid, q in questions.items()]
    return {qid: digest([*_who(backend, scope), state, {"slot": qid, "batch": batch}]) for qid in questions}


def packed_keys(
    backend: Backend,
    calls: list[list[tuple[str, object]]],
    question: Question,
    *,
    prefix: str,
    reuse: str,
    scope: str | None = None,
) -> dict[str, str]:
    """Each packed item's identity, by item id.

    `calls` are the items in the calls that will carry them, in order, each in slot `prefix` + position.
    Under `reuse="item"` an item's answer is keyed on the item and the question as written, wherever it
    sits; under `reuse="call"`, and always under joint reads, on its place in the whole call.
    """
    template = [prefix, question.body()]
    if reuse == "item" and not backend.joint_reads:
        return {
            item: digest([*_who(backend, scope), "packed", state, template])
            for call in calls
            for item, state in call
        }
    keys = {}
    for call in calls:
        items = [state for _, state in call]
        for slot, (item, _) in enumerate(call):
            keys[item] = digest(
                [*_who(backend, scope), "packed-call", {"slot": slot, "items": items}, template]
            )
    return keys


def request_body(model: str, state, questions: Mapping[str, Question]) -> dict:
    return {"model": model, "state": state, "questions": {qid: q.body() for qid, q in questions.items()}}


def error_detail(data: dict) -> str:
    found = data.get("error", data.get("detail"))
    if isinstance(found, list):
        found = "; ".join(error_detail({"detail": item}) for item in found)
    elif isinstance(found, dict):
        found = found.get("message") or found.get("msg") or json.dumps(found)
    return " ".join(str(found or "").split())[:200]


def resolved_model(data: dict) -> str | None:
    """The model the API says answered, literally; never the alias that was requested."""
    model = data.get("model")
    return model if isinstance(model, str) and model.strip() else None


def answer_origin(backend: Backend, resolved: str | None) -> dict:
    return {
        "provider": backend.name,
        "requested_model": backend.model,
        "resolved_model": resolved,
        "answered_at": time.time(),
    }


def validate_answer(qid: str, question: Question, answer) -> None:
    """The whole answer, including options and bounds; a malformed one fails the request that brought it."""
    try:
        question.validate(answer)
    except ValueError as exc:
        raise JevError(f"invalid answer returned for question {qid!r}: {exc}") from None


def parse_answers(data: dict, questions: Mapping[str, Question], *, provider: str) -> dict[str, dict]:
    """Every asked question answered and well formed, or nothing; partial responses are errors."""
    answers = data.get("answers")
    if not isinstance(answers, dict):
        detail = error_detail(data)
        raise JevError(f"{provider} returned no answers" + (f": {detail}" if detail else ""))
    for qid, question in questions.items():
        if qid not in answers:
            raise JevError(f"no answer returned for question {qid!r}")
        validate_answer(qid, question, answers[qid])
    return {qid: answers[qid] for qid in questions}


@dataclass(frozen=True)
class Usage:
    tokens: int
    cost: float
    source: str  # reported_by_api | estimated_from_tokens


def parse_usage(raw, *, price_per_mtok: float) -> Usage:
    """Validated metering. Malformed usage stops the run rather than silently under-counting."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise JevFatal("invalid API usage metadata: expected an object; stopped to avoid unmetered calls")
    tokens = raw.get("input_tokens")
    tokens = 0 if tokens is None else tokens
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise JevFatal("invalid API usage metadata: input_tokens must be a nonnegative integer")
    cost = raw.get("cost")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))):
        raise JevFatal("invalid API usage metadata: cost must be a finite nonnegative number")
    try:
        metered = tokens * price_per_mtok / 1e6 if cost is None else float(cost)
        valid = math.isfinite(float(tokens)) and math.isfinite(metered) and metered >= 0
    except OverflowError:
        raise JevFatal("invalid API usage metadata: value exceeds numeric range") from None
    if not valid:
        raise JevFatal("invalid API usage metadata: cost must be a finite nonnegative number")
    return Usage(tokens, metered, "estimated_from_tokens" if cost is None else "reported_by_api")
