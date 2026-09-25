"""A run's spending: reserve an estimate before a request goes out, settle the charge when it returns.

A request is priced before it is sent, from its own size (`protocol.estimate_tokens`), at the dearest
rate per estimated token its backend has charged so far, and at MARGIN times the list price until a
charge has been seen. It goes out only when that fits beside what is spent and what the requests already
in the air hold. Reservations wait their turn, first come first served: one that does not fit waits for
money held elsewhere to come back, and is refused only when nothing is held and it still does not fit.
So the limit is passed only when a price rises while requests are in the air, and `rises` counts that.

The limit is the tool's to choose; `math.inf` is no limit, and 0 allows only answers that cost nothing:
the store, a request already in flight, or a server that charges no fees.

A unit of work that must be done whole or not at all, such as every comparison of one text, takes an
allotment first: `share = await budget.allot(amount)` sets `amount` aside, waiting for room like a
request, and requests sent with `budget=share` draw on it. An allotment guarantees its unit room, not a
ceiling: if prices rise, its requests go on to draw on whatever the budget has free, and are refused only
when that is gone too. What is left goes back when the share closes.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
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
            self.budget._free(self.amount)


class Budget:
    """A spending limit in dollars, shared by every request of a run, across clients."""

    def __init__(self, limit: float = math.inf):
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or math.isnan(limit) or limit < 0:
            raise ValueError("a budget is a nonnegative number of dollars, or math.inf for no limit")
        self.limit = float(limit)
        self.spent = 0.0
        self.held = 0.0
        self._open = 0  # holds and shares not yet settled, released or returned
        self.rates: dict[str, float] = {}  # dollars per estimated token, the dearest each backend charged
        self.rises = 0
        self.refused = 0
        self._waiting: deque[tuple[asyncio.Future, object, object]] = deque()  # (future, price, make)
        self._parent: Budget | None = None
        self._closing = False

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
        """Whether a request or an allotment has been turned away."""
        return self.refused > 0

    def price(self, backend: Backend, tokens: int) -> float:
        """What a request of `tokens` estimated tokens would set aside now."""
        return tokens * self._rate(backend)

    def _rate(self, backend: Backend) -> float:
        learned = self.rates.get(_rate_key(backend))
        return learned if learned is not None else backend.price_per_mtok / 1e6 * MARGIN

    def _fits(self, amount: float) -> bool:
        remaining = self.remaining  # an amount equal to what is left, but for roundoff, fits
        return amount <= 0 or amount <= remaining or math.isclose(amount, remaining, rel_tol=1e-9)

    # ---- reserving ----------------------------------------------------------------------------------

    async def reserve(self, backend: Backend, tokens: int) -> Hold:
        """Set aside the price of a request, waiting in line for room; JevBudgetExceeded when nothing is
        held that could come back and it still does not fit."""
        if self._closing:
            raise ValueError("this share of the budget has been closed")
        if self._parent is not None:
            return self._draw(backend, tokens, refuse=True)
        # Priced when granted, not when it joins the line, so a charge seen meanwhile sets the rate.
        return await self._take(
            lambda: self.price(backend, tokens),
            lambda amount: Hold(self, backend, tokens, self._rate(backend), amount),
        )

    def try_reserve(self, backend: Backend, tokens: int) -> Hold | None:
        """A hold now if there is room and nobody is waiting, else None, never a refusal: for extras such
        as a hedge, which a run can do without."""
        if self._parent is not None:
            return None if self._closing else self._draw(backend, tokens, refuse=False)
        rate = self._rate(backend)
        amount = tokens * rate
        if self._closing or self._waiting or not self._fits(amount):
            return None
        self._grant(amount)
        return Hold(self, backend, tokens, rate, amount)

    def _draw(self, backend: Backend, tokens: int, *, refuse: bool) -> Hold | None:
        """A share's request: from the share while it lasts, then from what the budget has free right now.
        It never queues behind other allotments, which may be waiting for this very share to close."""
        rate = self._rate(backend)
        amount = tokens * rate
        for budget in (self, self._parent):
            if budget._fits(amount):
                budget._grant(amount)
                return Hold(budget, backend, tokens, rate, amount)
        if refuse:
            self._refuse(amount)
        return None

    async def allot(self, amount: float) -> Budget:
        """A share of this budget for one unit of work, waiting in line for room like a request.

        Requests sent with `budget=share` draw on the share, share this budget's learned prices, and are
        spending here too. Closing the share (or leaving its `with` block) returns what it did not use,
        once its last request has settled. A share cannot itself be divided.
        """
        if self._parent is not None:
            raise ValueError("a share of a budget cannot be divided further")
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or math.isnan(amount)
            or amount < 0
        ):
            raise ValueError("an allotment is a nonnegative number of dollars")

        def share(amount: float) -> Budget:
            child = Budget(math.inf if self.unlimited else amount)
            child.rates, child._parent = self.rates, self
            return child

        return await self._take(lambda: float(amount), share)

    async def _take(self, price, make):
        amount = price()
        if not self._waiting and self._fits(amount):
            self._grant(amount)
            return make(amount)
        if not self._waiting and not self._open:
            self._refuse(amount)
        future = asyncio.get_running_loop().create_future()
        self._waiting.append((future, price, make))
        try:
            return await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled() and future.exception() is None:
                granted = future.result()  # granted just as its caller stopped waiting: give it back
                if isinstance(granted, Hold):
                    granted.release()
                else:
                    granted.close()
            raise
        finally:
            self._wake()

    def _grant(self, amount: float) -> None:
        if not self.unlimited:
            self.held += amount
        self._open += 1

    def _refuse(self, amount: float) -> None:
        self.refused += 1
        raise JevBudgetExceeded(
            f"the ${self.limit:.2f} budget has ${self.remaining:.4f} left and nothing in the air to come "
            f"back; the next request would need about ${amount:.4f}"
        )

    def _wake(self) -> None:
        """Serve the line in order: grant what fits, refuse what cannot fit with nothing left to come back."""
        while self._waiting:
            future, price, make = self._waiting[0]
            if future.done():
                self._waiting.popleft()
                continue
            amount = price()
            if self._fits(amount):
                self._waiting.popleft()
                self._grant(amount)
                future.set_result(make(amount))
                continue
            if self._open:
                return  # money is in the air; the head of the line waits for it
            self._waiting.popleft()
            try:
                self._refuse(amount)
            except JevBudgetExceeded as refusal:
                future.set_exception(refusal)

    # ---- returning ----------------------------------------------------------------------------------

    def close(self) -> None:
        """Give an allotment's unused part back to its budget, now or when its last request settles."""
        if self._parent is not None and not self._closing:
            self._closing = True
            if not self._open:
                self._return()

    def __enter__(self) -> Budget:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _return(self) -> None:
        parent = self._parent
        parent._free(0.0 if parent.unlimited else max(0.0, self.limit - self.spent))

    def _free(self, amount: float) -> None:
        self._open -= 1
        self.held = self.held - amount if self._open else 0.0  # no drift once nothing is held
        if self._closing and not self._open:
            self._return()
        self._wake()

    def _settle(self, hold: Hold, cost: float) -> None:
        before = self.spent
        self.spent += cost
        if hold.tokens:
            charged = cost / hold.tokens
            if hold.rate and charged > RISE * hold.rate:
                self.rises += 1
                if self._parent is not None:
                    self._parent.rises += 1
            key = _rate_key(hold.backend)
            self.rates[key] = max(self.rates.get(key, 0.0), charged)
        if (parent := self._parent) is not None:
            parent.spent += cost
            if not parent.unlimited:  # what the share had set aside is spent, not held
                parent.held -= min(cost, max(0.0, self.limit - before))
        self._free(hold.amount)

    def summary(self) -> str:
        if self.unlimited:
            return f"${self.spent:.4f} spent, no limit"
        return f"${self.spent:.4f} of ${self.limit:.2f} spent"
