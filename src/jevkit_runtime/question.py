"""The three System One question types: what each asks, and what a well-formed answer to it is.

A question validates its whole answer, including membership in its options and the bounds of its scale,
so a tool reads an answer through `value` and `confidence` and never checks it again. `text` is the
instruction exactly as sent, for the methods paragraphs and run records that quote it.

A question used in a packed request (`Client.ask_packed`) may name its slot as `{slot}`; each item's copy
is asked with the slot filled in, and its answer is keyed on the question as written.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

SLOT = "{slot}"


def _probability(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _optional_confidence(answer: dict) -> None:
    if (confidence := answer.get("confidence")) is not None and not _probability(confidence):
        raise ValueError("confidence must be a probability from 0 to 1")


@dataclass(frozen=True, eq=False)
class Question:
    """One question about a state. Subclasses fix the answer type; equal questions ask the same thing."""

    type: ClassVar[str]
    instructions: str

    def __post_init__(self) -> None:
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("a question needs instructions")

    def __eq__(self, other) -> bool:
        return isinstance(other, Question) and self.body() == other.body()

    def __hash__(self) -> int:
        return hash(json.dumps(self.body(), sort_keys=True))

    @property
    def text(self) -> str:
        return self.instructions

    def body(self) -> dict:
        return {"type": self.type, "instructions": self.instructions}

    def at(self, slot: str) -> Question:
        """This question for one slot of a packed request."""
        return replace(self, instructions=self.instructions.replace(SLOT, slot))

    def validate(self, answer: Any) -> None:
        """Raise ValueError unless `answer` is a complete, well-formed answer to this question."""
        if not isinstance(answer, dict):
            raise ValueError("expected an object")
        self._check(answer)

    def _check(self, answer: dict) -> None:
        raise NotImplementedError

    def value(self, answer: dict) -> Any:
        raise NotImplementedError

    def confidence(self, answer: dict) -> float | None:
        raise NotImplementedError


@dataclass(frozen=True, eq=False)
class Noul(Question):
    """Is the statement true of the state? Answered with a probability."""

    type: ClassVar[str] = "noul"
    criteria: Mapping[str, str] | None = field(default=None)  # what "true" and "false" mean, when spelled out

    def body(self) -> dict:
        body = super().body()
        if self.criteria:
            body["criteria"] = dict(self.criteria)
        return body

    def _check(self, answer: dict) -> None:
        if not _probability(answer.get("noul")):
            raise ValueError("noul must be a probability from 0 to 1")

    def value(self, answer: dict) -> float:
        return float(answer["noul"])

    def confidence(self, answer: dict) -> float:
        p = self.value(answer)
        return max(p, 1 - p)


@dataclass(frozen=True, eq=False)
class Choice(Question):
    """Which option fits the state? Answered with a label, and optionally probabilities for every option."""

    type: ClassVar[str] = "choice"
    options: Mapping[str, str] = field(default_factory=dict)  # label -> what it means

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.options, Sequence) and not isinstance(self.options, str):
            object.__setattr__(self, "options", {label: label for label in self.options})
        if len(self.options) < 2 or not all(isinstance(label, str) and label for label in self.options):
            raise ValueError("a choice needs at least two named options")

    def body(self) -> dict:
        return super().body() | {"criteria": dict(self.options)}

    def _check(self, answer: dict) -> None:
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise ValueError("choice must be a string")
        if choice not in self.options:
            raise ValueError(f"choice {choice!r} is not one of the options")
        probabilities = answer.get("probabilities")
        if probabilities is not None and (
            not isinstance(probabilities, dict)
            or not all(k in self.options and _probability(v) for k, v in probabilities.items())
        ):
            raise ValueError("probabilities must map options to probabilities from 0 to 1")
        _optional_confidence(answer)

    def value(self, answer: dict) -> str:
        return answer["choice"]

    def confidence(self, answer: dict) -> float | None:
        probabilities = answer.get("probabilities") or {}
        found = probabilities.get(answer["choice"], answer.get("confidence"))
        return None if found is None else float(found)


@dataclass(frozen=True, eq=False)
class Score(Question):
    """Where does the state sit on an ordered scale? Answered with a position from 0 to len(levels) - 1."""

    type: ClassVar[str] = "score"
    levels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "levels", tuple(self.levels))
        if len(self.levels) < 2 or not all(isinstance(level, str) and level for level in self.levels):
            raise ValueError("a score needs at least two named levels")

    def body(self) -> dict:
        return super().body() | {"criteria": list(self.levels)}

    def _check(self, answer: dict) -> None:
        score = answer.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= len(self.levels) - 1
        ):
            raise ValueError(f"score must be a number from 0 to {len(self.levels) - 1}")
        _optional_confidence(answer)

    def value(self, answer: dict) -> float:
        return float(answer["score"])

    def confidence(self, answer: dict) -> float | None:
        found = answer.get("confidence")
        return None if found is None else float(found)


def from_body(body: Mapping) -> Question:
    """The question a saved request body describes: the inverse of `Question.body`."""
    kind = body.get("type")
    if kind == "noul":
        return Noul(body.get("instructions"), body.get("criteria"))
    if kind == "choice":
        return Choice(body.get("instructions"), body.get("criteria") or {})
    if kind == "score":
        return Score(body.get("instructions"), tuple(body.get("criteria") or ()))
    raise ValueError(f"unknown question type {kind!r}")
