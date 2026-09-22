"""What a run spent and who answered. Budget policy stays with the tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .protocol import Usage


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
    latencies: list[float] = field(default_factory=list)
    cost_sources: dict[str, int] = field(default_factory=dict)
    answer_provenance: list[dict] = field(default_factory=list)

    def record_call(
        self, usage: Usage, seconds: float, on_cost: Callable[[float], None] | None = None
    ) -> None:
        """Count a paid response before its answers are validated: an invalid answer was still billed."""
        self.calls += 1
        self.input_tokens += usage.tokens
        self.cost += usage.cost
        self.max_call_cost = max(self.max_call_cost, usage.cost)
        self.cost_sources[usage.source] = self.cost_sources.get(usage.source, 0) + 1
        if on_cost is not None:
            on_cost(usage.cost)
        self.latencies.append(seconds)

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
