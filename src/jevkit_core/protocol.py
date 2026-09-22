"""The System One decision protocol: request bodies, typed answers, usage, and answer identity."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass

from .errors import JevError, JevFatal
from .providers import Backend

ANSWER_KEY_VERSION = "jevkit/answer/v2"


def digest(parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def answer_key(backend: Backend, state, question: dict) -> str:
    """One identity for every tool: who answered, at which endpoint, with which model, asked what."""
    return digest([ANSWER_KEY_VERSION, backend.name, backend.url, backend.model, state, question])


def request_body(model: str, state, questions: dict[str, dict]) -> dict:
    return {"model": model, "state": state, "questions": questions}


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


def _probability(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _confidence(answer: dict) -> None:
    if (confidence := answer.get("confidence")) is not None and not _probability(confidence):
        raise ValueError("confidence must be a probability from 0 to 1")


def _noul(answer: dict) -> None:
    if not _probability(answer.get("noul")):
        raise ValueError("noul must be a probability from 0 to 1")


def _choice(answer: dict) -> None:
    if not isinstance(answer.get("choice"), str):
        raise ValueError("choice must be a string")
    probabilities = answer.get("probabilities")
    if probabilities is not None and (
        not isinstance(probabilities, dict)
        or not all(isinstance(k, str) and _probability(v) for k, v in probabilities.items())
    ):
        raise ValueError("probabilities must map choices to probabilities from 0 to 1")
    _confidence(answer)


def _score(answer: dict) -> None:
    score = answer.get("score")
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or score < 0
    ):
        raise ValueError("score must be a finite nonnegative number")
    _confidence(answer)


QUESTION_TYPES = {"noul": _noul, "choice": _choice, "score": _score}


def validate_answer(qid: str, question: dict, answer) -> None:
    """Shape and type only; option membership and scale bounds belong to the tool that asked."""
    if not isinstance(answer, dict):
        raise JevError(f"invalid answer returned for question {qid!r}: expected an object")
    validate = QUESTION_TYPES.get(question.get("type"))
    if validate is None:
        return
    try:
        validate(answer)
    except ValueError as exc:
        raise JevError(f"invalid answer returned for question {qid!r}: {exc}") from None


def parse_answers(data: dict, questions: dict[str, dict], *, provider: str) -> dict[str, dict]:
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
