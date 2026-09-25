"""A run's spending: reserve an estimate before a request goes out, settle the charge when it returns.

A request is priced before it is sent, from its own size (`protocol.estimate_tokens`), at the dearest
rate per estimated token its backend has charged so far, and at MARGIN times the list price until a
charge has been seen. It goes out only if that fits beside what is spent and what the requests already
in the air hold. So a budget is overshot only when a price rises while requests are in the air, and
`rises` counts that.

The limit is the tool's to choose; `math.inf` is no limit, and 0 allows only answers that cost nothing:
the store, a request already in flight, or a server that charges no fees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import JevBudgetExceeded
from .providers import Backend
from .settings import Settings

MARGIN = 1.5  # on the list price, while a backend has charged nothing yet
RISE = 1.25  # a charge this much dearer per estimated token than the rate reserved at is a rise


def _rate_key(backend: Backend) -> str:
    return f"{backend.name}|{backend.url}|{backend.model}"


@dataclass
class Hold:
    """Money set aside for one request until its charge is known."""

    budget: Budget
    backend: Backend
    tokens: int
    rate: float
    amount: float
    open: bool = True

    def settle(self, cost: float) -> None:
        """The request was charged `cost`: spend it, free the hold, and learn the rate."""
        if self.open:
            self.open = False
            self.budget._settle(self, cost)

    def release(self) -> None:
        """The request was never charged."""
        if self.open:
            self.open = False
            self.budget._free(self)


class Budget:
    """A spending limit in dollars, shared by every request of a run, across clients."""

    def __init__(self, limit: float = math.inf):
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or math.isnan(limit) or limit < 0:
            raise ValueError("a budget is a nonnegative number of dollars, or math.inf for no limit")
        self.limit = float(limit)
        self.spent = 0.0
        self.held = 0.0
        self._open = 0  # holds not yet settled or released
        self.rates: dict[str, float] = {}  # dollars per estimated token, the dearest each backend charged
        self.rises = 0
        self.refused = 0

    @classmethod
    def from_settings(cls, default: float, settings: Settings | None = None) -> Budget:
        """`JEV_BUDGET` when it is set, else the tool's default."""
        settings = settings or Settings.from_env()
        return cls(default if settings.budget is None else settings.budget)

    @property
    def unlimited(self) -> bool:
        return math.isinf(self.limit)

    @property
    def remaining(self) -> float:
        if self.unlimited:
            return math.inf
        left = self.limit - self.spent - self.held
        # Roundoff at the limit must not buy an extra request.
        return 0.0 if left <= 0 or math.isclose(left, 0.0, abs_tol=1e-12) else left

    @property
    def exhausted(self) -> bool:
        """Whether a request has been turned away."""
        return self.refused > 0

    def price(self, backend: Backend, tokens: int) -> float:
        return tokens * self._rate(backend)

    def _rate(self, backend: Backend) -> float:
        learned = self.rates.get(_rate_key(backend))
        return learned if learned is not None else backend.price_per_mtok / 1e6 * MARGIN

    def reserve(self, backend: Backend, tokens: int) -> Hold:
        """Set aside the price of a request, or raise JevBudgetExceeded when it does not fit."""
        rate = self._rate(backend)
        amount = tokens * rate
        if amount > 0 and amount > self.remaining:
            self.refused += 1
            raise JevBudgetExceeded(
                f"the ${self.limit:.2f} budget has ${self.remaining:.4f} left; "
                f"the next request would need about ${amount:.4f}"
            )
        self.held += amount
        self._open += 1
        return Hold(self, backend, tokens, rate, amount)

    def _free(self, hold: Hold) -> None:
        self._open -= 1
        self.held = self.held - hold.amount if self._open else 0.0  # no drift once nothing is held

    def _settle(self, hold: Hold, cost: float) -> None:
        self._free(hold)
        self.spent += cost
        if hold.tokens:
            charged = cost / hold.tokens
            if hold.rate and charged > RISE * hold.rate:
                self.rises += 1
            key = _rate_key(hold.backend)
            self.rates[key] = max(self.rates.get(key, 0.0), charged)

    def summary(self) -> str:
        if self.unlimited:
            return f"${self.spent:.4f} spent, no limit"
        return f"${self.spent:.4f} of ${self.limit:.2f} spent"
