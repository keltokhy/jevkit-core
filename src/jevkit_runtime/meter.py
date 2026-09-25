"""What a run spent and who answered. Budget policy stays with the tool."""

from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import Usage, digest
from .question import Question

QUESTIONS_LISTED = 100  # distinct questions a run record names in full


@dataclass
class Meter:
    provider: str = ""
    requested_model: str = ""
    calls: int = 0
    cached: int = 0  # asks served without a new request: from the store or a shared in-flight call
    retries: int = 0
    hedges: int = 0
    input_tokens: int = 0
    cost: float = 0.0
    max_call_cost: float = 0.0
    cost_sources: dict[str, int] = field(default_factory=dict)
    answer_provenance: list[dict] = field(default_factory=list)
    questions: dict[str, dict] = field(default_factory=dict)  # distinct questions asked, by digest of body
    questions_not_listed: int = 0  # distinct questions past QUESTIONS_LISTED

    def note_question(self, question: Question) -> None:
        """Remember a question as it was written, so a run record can quote exactly what was asked."""
        body = question.body()
        key = digest(body)
        if key in self.questions:
            return
        if len(self.questions) < QUESTIONS_LISTED:
            self.questions[key] = body
        else:
            self.questions_not_listed += 1

    def record_call(self, usage: Usage) -> None:
        """Count a paid response before its answers are validated: an invalid answer was still billed."""
        self.calls += 1
        self.input_tokens += usage.tokens
        self.cost += usage.cost
        self.max_call_cost = max(self.max_call_cost, usage.cost)
        self.cost_sources[usage.source] = self.cost_sources.get(usage.source, 0) + 1

    def note_answer(self, origin: dict) -> None:
        """Tally where each answer came from, so a run can say which models actually answered."""
        item = {k: origin.get(k) for k in ("provider", "requested_model", "resolved_model", "source")}
        for previous in self.answer_provenance:
            if all(previous.get(k) == v for k, v in item.items()):
                previous["count"] += 1
                return
        self.answer_provenance.append(item | {"count": 1})

    @property
    def resolved_models(self) -> list[str]:
        return sorted({p["resolved_model"] for p in self.answer_provenance if p.get("resolved_model")})

    @property
    def unknown_model_answers(self) -> int:
        return sum(p["count"] for p in self.answer_provenance if not p.get("resolved_model"))

    @property
    def mixed_models(self) -> dict[str, list[str]]:
        """Requested models that more than one model answered, by `provider/requested model`.

        Asking one model and hearing from several is an accident, such as a cache that outlived an alias
        moving to a new version, and a run should say so. Mixing on purpose, across providers, is not.
        """
        seen: dict[str, set[str]] = {}
        for p in self.answer_provenance:
            if p.get("resolved_model"):
                seen.setdefault(f"{p['provider']}/{p['requested_model']}", set()).add(p["resolved_model"])
        return {asked: sorted(models) for asked, models in seen.items() if len(models) > 1}

    @property
    def model(self) -> str:
        """The one model that answered everything, or empty when answers are mixed or unattributed."""
        models = self.resolved_models
        return models[0] if len(models) == 1 and not self.unknown_model_answers else ""

    def summary(self) -> str:
        parts = [f"{self.calls:,} calls, {self.cached:,} cached"]
        if self.retries:
            parts.append(f"{self.retries:,} retries")
        if self.hedges:
            parts.append(f"{self.hedges:,} hedges")
        if self.calls:
            parts.append(f"{self.input_tokens:,} tokens")
            parts.append(f"${self.cost:.4f}")
        return "; ".join(parts)
