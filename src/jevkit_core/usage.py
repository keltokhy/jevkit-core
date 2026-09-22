"""Validated usage and the common meter; budget policy remains with the caller."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .backends import PRICE_PER_MTOK
from .errors import JevFatal


@dataclass(frozen=True)
class Usage:
    tokens: int | float
    cost: float
    source: str


def parse_usage(
    usage, *, price_per_mtok: float = PRICE_PER_MTOK, missing_tokens: int = 0, fractional_tokens: bool = False
) -> Usage:
    usage = {} if usage is None else usage
    if not isinstance(usage, dict):
        raise JevFatal("invalid API usage metadata: expected an object; stopped to avoid unmetered calls")
    tokens = usage.get("input_tokens")
    tokens = missing_tokens if tokens is None else tokens
    valid_types = (int, float) if fractional_tokens else (int,)
    if isinstance(tokens, bool) or not isinstance(tokens, valid_types) or tokens < 0:
        raise JevFatal("invalid API usage metadata: input_tokens must be a nonnegative integer")
    cost = usage.get("cost")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))):
        raise JevFatal("invalid API usage metadata: cost must be a finite nonnegative number")
    try:
        metered_cost = tokens * price_per_mtok / 1e6 if cost is None else float(cost)
        valid = math.isfinite(tokens) and math.isfinite(metered_cost) and metered_cost >= 0
    except OverflowError as exc:
        raise JevFatal("invalid API usage metadata: cost exceeds numeric range") from exc
    if not valid:
        raise JevFatal("invalid API usage metadata: cost must be a finite nonnegative number")
    return Usage(tokens, metered_cost, "estimated_from_tokens" if cost is None else "reported_by_api")


@dataclass
class Meter:
    calls: int = 0
    cached: int = 0
    retries: int = 0
    input_tokens: int = 0
    cost: float = 0.0
    model: str = ""
    latencies: list[float] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.calls:,} calls, {self.cached:,} cached"]
        if self.retries:
            parts.append(f"{self.retries:,} retries")
        if self.calls:
            parts.append(f"{self.input_tokens:,} tokens")
            parts.append(f"${self.cost:.4f}")
        return "; ".join(parts)
