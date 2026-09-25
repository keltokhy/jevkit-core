"""The request pipeline: identity, store, sharing, transport, validation, storage, metering."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import transport
from .budget import Budget, Hold
from .errors import JevBudgetExceeded, JevError, JevFatal
from .meter import Meter
from .protocol import (
    REQUEST_OVERHEAD_TOKENS,
    answer_keys,
    answer_origin,
    estimate_tokens,
    packed_keys,
    packed_request,
    parse_answers,
    parse_usage,
    request_body,
    resolved_model,
)
from .providers import Backend
from .question import SLOT, Question
from .store import AnswerStore
from .workers import Workers

Flight = tuple[dict[str, dict], dict]  # answers by key, and the origin they share
REUSE = ("item", "call")


class Answers(dict):
    """Answers by question id, plus where each came from.

    `origins[qid]` carries `provider`, `requested_model`, `resolved_model`, `answered_at`, and a
    `source` of `cache`, `api`, or `shared`. An answer stored without provenance has only `source`.
    """

    def __init__(self, answers: Mapping[str, dict], origins: Mapping[str, dict]):
        super().__init__(answers)
        self.origins: dict[str, dict] = dict(origins)


class PackedAnswers(dict):
    """Answers by item: each item's answers by question id.

    `origins[item]` says where its answers came from; an item answered in a call also names its `slot` and
    the `pack` size. `errors[item]` holds the failure of an item that has no answers: its call failed, the
    budget had no room for it, or it is too large to ask even alone.
    """

    def __init__(self, answers, origins, errors):
        super().__init__(answers)
        self.origins: dict[str, dict] = dict(origins)
        self.errors: dict[str, Exception] = dict(errors)


@dataclass(frozen=True)
class Plan:
    """What asking would do, read from the store without sending or writing anything.

    A plan belongs to the client that made it: `send` refuses a plan made for another backend or scope.
    """

    backend: tuple[str, str, str]  # provider, endpoint and model the keys were computed for
    scope: str | None
    state: Any
    questions: dict[str, Question]
    keys: dict[str, str]
    hits: dict[str, dict]  # answers the store already holds
    origins: dict[str, dict]  # their provenance, each with source "cache"
    misses: dict[str, Question]  # what a request would carry
    whole: bool  # served whole or asked again whole: a joint read, or a packed call reused only as a call
    request: dict | None  # the body it would send; None when nothing is missing
    oversized: str | None  # why that request exceeds the provider's limits, if it does
    tokens: int  # the input tokens that request would bill, estimated
    cost: float  # and their price at the backend's list price

    @property
    def complete(self) -> bool:
        return not self.misses


@dataclass(frozen=True)
class PackedCall:
    plan: Plan
    slots: dict[str, str]  # slot -> item


@dataclass(frozen=True)
class PackedPlan:
    """What asking about several packed items would do, read from the store without sending anything."""

    items: tuple[str, ...]
    questions: dict[str, Question]
    calls: list[PackedCall]  # the calls that would go out
    hits: dict[str, dict[str, dict]]  # items the store answers whole, with their answers by question id
    origins: dict[str, dict]  # their provenance
    twins: dict[str, str] = field(default_factory=dict)  # item -> the item with the same state it takes after
    errors: dict[str, Exception] = field(default_factory=dict)  # items that cannot be asked even alone

    @property
    def tokens(self) -> int:
        return sum(call.plan.tokens for call in self.calls)

    @property
    def cost(self) -> float:
        return sum(call.plan.cost for call in self.calls)


def http2_available() -> bool:
    """HTTP/2 whenever the optional `h2` package is installed; the `http2` extra pulls it in."""
    return importlib.util.find_spec("h2") is not None


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


class Client:
    """Ask System One questions about a state. Every tool gets the same pipeline; policy is per call."""

    def __init__(
        self,
        backend: Backend,
        *,
        timeout: float = 15.0,
        attempts: int = 4,
        concurrency: int = 32,
        store: AnswerStore | None = None,
        budget: Budget | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        workers: int = 0,
        per_worker: int = 64,
    ):
        """`workers` sends requests from that many processes of its own; see `workers.py`."""
        self.backend = backend
        self.budget = budget if budget is not None else Budget()
        self.timeout, self.attempts, self.store = timeout, attempts, store
        self.concurrency, self.transport = concurrency, transport
        self.http2 = transport is None and http2_available()
        self.meter = Meter(provider=backend.name, requested_model=backend.model)
        self._flights: dict[str, asyncio.Task[Flight]] = {}
        self._http: httpx.AsyncClient | None = None
        if workers and transport is not None:
            raise ValueError("a test transport runs in this process; it cannot be used with workers")
        self._workers = (
            Workers(workers, per_worker=per_worker, headers=self._headers(), http2=http2_available())
            if workers
            else None
        )

    @property
    def model(self) -> str:
        return self.backend.model

    @property
    def url(self) -> str:
        return self.backend.url

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.backend.name, self.backend.url, self.backend.model)

    @property
    def http(self) -> httpx.AsyncClient:
        """Opened on first use, so cache-only clients never hold a connection pool."""
        if self._http is None:
            limits = (
                httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=120)
                if self.http2
                else httpx.Limits(
                    max_connections=self.concurrency + 4, max_keepalive_connections=self.concurrency + 4
                )
            )
            self._http = httpx.AsyncClient(
                headers=self._headers(), limits=limits, transport=self.transport, http2=self.http2
            )
        return self._http

    def _headers(self) -> dict:
        headers = {"X-Title": "jev tools"}
        if self.backend.key:
            headers["Authorization"] = f"Bearer {self.backend.key}"
        return headers

    async def start(self) -> None:
        """Start the worker processes now, rather than on the first request that needs them."""
        if self._workers is not None:
            await self._workers.start()

    async def close(self) -> None:
        """Stop requests still in the air that no caller waits for, then the connections and workers."""
        flights = list(self._flights.values())
        for task in flights:
            task.cancel()
        await asyncio.gather(*flights, return_exceptions=True)
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._workers is not None:
            await self._workers.close()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---- planning ---------------------------------------------------------------------------------

    def plan(self, state, questions: Mapping[str, Question], *, scope: str | None = None) -> Plan:
        """Which answers the store holds and what a request for the rest would be. No network, no writes.

        A stored answer that no longer validates is a miss, asked again and overwritten. Under joint reads
        the store serves the whole batch or none of it.
        """
        keys = answer_keys(self.backend, state, questions, scope=scope)
        return self._plan(state, dict(questions), keys, scope, whole=self.backend.joint_reads)

    def _plan(
        self, state, questions: dict[str, Question], keys: dict[str, str], scope, *, whole: bool
    ) -> Plan:
        hits = self._stored(questions, keys)
        if whole and len(hits) != len(questions):
            hits = {}
        misses = {qid: q for qid, q in questions.items() if qid not in hits}
        request = request_body(self.backend.model, state, misses) if misses else None
        tokens = estimate_tokens(request) if request else 0
        return Plan(
            self.identity,
            scope,
            state,
            questions,
            keys,
            {qid: answer for qid, (answer, _) in hits.items()},
            {qid: origin for qid, (_, origin) in hits.items()},
            misses,
            whole,
            request,
            self._oversized(request),
            tokens,
            tokens * self.backend.price_per_mtok / 1e6,
        )

    def _stored(self, questions: Mapping[str, Question], keys: Mapping[str, str]) -> dict[str, tuple]:
        found = {}
        if self.store is None:
            return found
        for qid, question in questions.items():
            entry = self.store.entry(keys[qid])
            if entry is None:
                continue
            try:
                question.validate(entry.answer)
            except ValueError:
                continue
            found[qid] = (entry.answer, dict(entry.metadata or {}) | {"source": "cache"})
        return found

    def _oversized(self, request: dict | None) -> str | None:
        """Why a request is over the provider's documented token limits, by the runtime's estimate."""
        if request is None:
            return None
        name = self.backend.name
        limit, read = self.backend.max_request_tokens, self.backend.max_read_tokens
        if limit is not None and (tokens := estimate_tokens(request)) > limit:
            return f"request of about {tokens:,} tokens is over {name}'s limit of {limit:,}"
        if read is not None and request["questions"]:
            size = _size(request["state"]) + max(_size(q) for q in request["questions"].values())
            tokens = math.ceil(size / 4) + REQUEST_OVERHEAD_TOKENS
            if tokens > read:
                return f"state and question of about {tokens:,} tokens are over {name}'s limit of {read:,}"
        return None

    # ---- asking -----------------------------------------------------------------------------------

    async def ask(
        self,
        state,
        questions: Mapping[str, Question],
        *,
        scope: str | None = None,
        hedge_after: float | None = None,
        priority: bool = False,
    ) -> Answers:
        """Answer every question, sending only those the store cannot answer. `plan` and then `send`."""
        return await self.send(
            self.plan(state, questions, scope=scope), hedge_after=hedge_after, priority=priority
        )

    async def send(self, plan: Plan, *, hedge_after: float | None = None, priority: bool = False) -> Answers:
        """Complete a plan: join an identical request already in flight, or send its misses.

        A new request is sent only if its estimate fits the client's budget; otherwise JevBudgetExceeded,
        though store hits and a request already in flight still answer. Only the caller whose request goes
        out pays. `hedge_after` sends a slow call a second time, budget permitting, and keeps the first
        answer. `priority` sends ahead of other work when the client has workers.
        """
        return await self._send(plan, hedge_after, priority, note=True)

    async def _send(self, plan: Plan, hedge_after, priority, *, note: bool) -> Answers:
        if plan.backend != self.identity:
            raise ValueError(
                f"this plan was made for {plan.backend[0]} {plan.backend[2]}, not {self.backend.name}"
            )
        if plan.whole and plan.misses and len(plan.misses) != len(plan.questions):
            raise ValueError("this plan is answered whole; it cannot send only some of its questions")
        if note:
            for question in plan.questions.values():
                self.meter.note_question(question)
        answers, origins = dict(plan.hits), dict(plan.origins)
        if not plan.misses:
            self.meter.cached += 1
        else:
            if plan.oversized:
                raise JevError(plan.oversized)
            fresh, owner = await self._fetch(plan.state, plan.misses, plan.keys, hedge_after, priority)
            for qid, (answer, origin) in fresh.items():
                answers[qid] = answer
                origins[qid] = origin | {"source": "api" if owner else "shared"}
        for origin in origins.values():
            self.meter.note_answer(origin)
        return Answers({q: answers[q] for q in plan.questions}, {q: origins[q] for q in plan.questions})

    # ---- packed requests --------------------------------------------------------------------------

    def plan_packed(
        self,
        items: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        context=None,
        prefix: str = "p",
        reuse: str = "call",
        max_items: int | None = None,
        scope: str | None = None,
    ) -> PackedPlan:
        """How several items would be asked, packed into as few calls as fit. No network, no writes.

        Each call's state holds its items in slots `prefix` + position (beside `context`, when given), and
        each question, which must name its slot as `{slot}`, is asked once per slot as `slot.qid`.

        `reuse="call"` (the default, and always on joint-read servers) reuses an answer only in the same
        call, since an item's answer can move with the company it is read in. `reuse="item"` reuses each
        item's answers wherever it turns up again, among calls no wider than `max_items`.
        """
        if reuse not in REUSE:
            raise ValueError(f"reuse must be one of {', '.join(REUSE)}")
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be at least 1")
        if not questions:
            raise ValueError("ask at least one question")
        if unslotted := [qid for qid, q in questions.items() if SLOT not in q.instructions]:
            raise ValueError(f"packed questions must name their slot as {SLOT}: {', '.join(unslotted)}")
        questions = dict(questions)
        by_item = reuse == "item" and not self.backend.joint_reads
        entries = list(items.items())
        shape = dict(prefix=prefix, context=context, width=max_items, scope=scope)
        hits: dict[str, dict] = {}
        origins: dict[str, dict] = {}
        twins: dict[str, str] = {}
        if by_item:
            keys = packed_keys(self.backend, [entries], questions, reuse="item", **shape)
            first: dict[tuple, str] = {}
            ask: list[tuple[str, Any]] = []
            for item, state in entries:
                signature = tuple(keys[item].values())
                if signature in first:
                    twins[item] = first[signature]  # the same state, so the same keys: asked once
                    continue
                first[signature] = item
                stored = self._stored(questions, keys[item])
                if len(stored) == len(questions):
                    hits[item] = {qid: answer for qid, (answer, _) in stored.items()}
                    origins[item] = next(iter(stored.values()))[1]
                else:
                    ask.append((item, state))
            calls, errors = self._split(ask, questions, prefix, context, max_items)
        else:
            calls, errors = self._split(entries, questions, prefix, context, max_items)
            keys = packed_keys(self.backend, calls, questions, reuse="call", **shape)
        planned = []
        for call in calls:
            slots, state, wire = packed_request(call, questions, prefix=prefix, context=context)
            wire_keys = {
                f"{slot}.{qid}": keys[item][qid] for slot, item in slots.items() for qid in questions
            }
            plan = self._plan(state, wire, wire_keys, scope, whole=not by_item)
            if plan.complete:
                for slot, item in slots.items():
                    hits[item] = {qid: plan.hits[f"{slot}.{qid}"] for qid in questions}
                    origins[item] = plan.origins[f"{slot}.{next(iter(questions))}"]
            else:
                planned.append(PackedCall(plan, slots))
        return PackedPlan(tuple(items), questions, planned, hits, origins, twins, errors)

    def _split(self, entries, questions, prefix, context, max_items):
        """Items in order, in calls of at most `max_items` whose UTF-8 bytes stay inside the provider's
        token limits (a conservative bound); an item that fits no call even alone is an error."""
        limit, read = self.backend.max_request_tokens, self.backend.max_read_tokens
        asked = [_size(q.at(f"{prefix}000").body()) + len(prefix) + 12 for q in questions.values()]
        base_state = 2 if context is None else _size(context) + 24
        base_request = _size(request_body(self.backend.model, None, {})) + base_state
        calls: list[list] = []
        errors: dict[str, Exception] = {}
        state_bytes = request_bytes = 0
        for item, state in entries:
            item_bytes = _size(state) + len(prefix) + 8
            fits = (
                calls
                and (max_items is None or len(calls[-1]) < max_items)
                and (read is None or state_bytes + item_bytes + max(asked) <= read)
                and (limit is None or request_bytes + item_bytes + sum(asked) <= limit)
            )
            if fits:
                calls[-1].append((item, state))
                state_bytes += item_bytes
                request_bytes += item_bytes + sum(asked)
                continue
            calls.append([(item, state)])
            state_bytes, request_bytes = base_state + item_bytes, base_request + item_bytes + sum(asked)
        kept = []
        for call in calls:
            if len(call) == 1:  # alone and over the byte bound: the token estimate decides
                _, state, wire = packed_request(call, questions, prefix=prefix, context=context)
                if reason := self._oversized(request_body(self.backend.model, state, wire)):
                    errors[call[0][0]] = JevError(f"item {call[0][0]!r} alone is too large: {reason}")
                    continue
            kept.append(call)
        return kept, errors

    async def send_packed(
        self, plan: PackedPlan, *, hedge_after: float | None = None, priority: bool = False
    ) -> PackedAnswers:
        """Send a packed plan's calls together. A failed call's items get its error instead of answers;
        a fatal error stops the run and is raised once every call has finished."""
        for question in plan.questions.values():
            self.meter.note_question(question)
        answers = {item: dict(found) for item, found in plan.hits.items()}
        origins, errors = dict(plan.origins), dict(plan.errors)
        for item in plan.hits:
            for _ in plan.questions:
                self.meter.note_answer(origins[item])
        if not plan.calls:
            self.meter.cached += 1
        results = await asyncio.gather(
            *(self._send(call.plan, hedge_after, priority, note=False) for call in plan.calls),
            return_exceptions=True,
        )
        fatal = next(
            (r for r in results if isinstance(r, BaseException) and not isinstance(r, Exception)), None
        )
        fatal = fatal or next((r for r in results if isinstance(r, JevFatal)), None)
        if fatal is not None:
            raise fatal
        for call, result in zip(plan.calls, results, strict=True):
            for slot, item in call.slots.items():
                if isinstance(result, Exception):
                    errors[item] = result
                    continue
                answers[item] = {qid: result[f"{slot}.{qid}"] for qid in plan.questions}
                origin = result.origins[f"{slot}.{next(iter(plan.questions))}"]
                origins[item] = origin | {"slot": slot, "pack": len(call.slots)}
        for item, twin in plan.twins.items():
            if twin in answers:
                answers[item], origins[item] = answers[twin], origins[twin]
            elif twin in errors:
                errors[item] = errors[twin]
        return PackedAnswers(
            {i: answers[i] for i in plan.items if i in answers},
            {i: origins[i] for i in plan.items if i in origins},
            {i: errors[i] for i in plan.items if i in errors},
        )

    async def ask_packed(
        self,
        items: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        context=None,
        prefix: str = "p",
        reuse: str = "call",
        max_items: int | None = None,
        scope: str | None = None,
        hedge_after: float | None = None,
        priority: bool = False,
    ) -> PackedAnswers:
        """Ask each question about each item, packed into as few calls as fit: `plan_packed`, then
        `send_packed`."""
        plan = self.plan_packed(
            items, questions, context=context, prefix=prefix, reuse=reuse, max_items=max_items, scope=scope
        )
        return await self.send_packed(plan, hedge_after=hedge_after, priority=priority)

    # ---- sending ----------------------------------------------------------------------------------

    async def _fetch(
        self, state, questions: Mapping[str, Question], keys: Mapping[str, str], hedge_after, priority
    ) -> tuple[dict[str, tuple[dict, dict]], bool]:
        """Answers for `questions` by id, each with its origin, and whether this caller sent the request."""
        send_keys = {qid: keys[qid] for qid in questions}
        body = request_body(self.backend.model, state, questions)

        def start() -> Coroutine[Any, Any, Flight]:
            hold = self.budget.reserve(self.backend, estimate_tokens(body))  # raises when it does not fit
            return self._request(body, questions, send_keys, hold, priority)

        task, owner = self._share(send_keys.values(), start)
        if hedge_after is None:
            # Shielded: a caller that stops waiting must not cancel a request others share.
            by_key, origin = await asyncio.shield(task)
        else:
            by_key, origin = await self._hedged(task, hedge_after, body, questions, send_keys, priority)
        return {qid: (by_key[key], dict(origin)) for qid, key in send_keys.items()}, owner

    def _share(self, keys: Iterable[str], start: Callable[[], Coroutine[Any, Any, Flight]]):
        """Join an identical in-flight request, or start one. Only the starter pays."""
        flight = "|".join(sorted(keys))
        if (task := self._flights.get(flight)) is not None:
            self.meter.cached += 1
            return task, False
        task = asyncio.ensure_future(start())
        self._flights[flight] = task

        def discard(done):
            if self._flights.get(flight) is done:
                del self._flights[flight]

        task.add_done_callback(discard)
        return task, True

    async def _hedged(self, first, after, body, questions, keys, priority) -> Flight:
        done, _ = await asyncio.wait({first}, timeout=after)
        if done:
            return first.result()
        try:
            hold = self.budget.reserve(self.backend, estimate_tokens(body))
        except JevBudgetExceeded:
            return await asyncio.shield(first)  # no room for a second copy; wait for the first
        second = asyncio.ensure_future(self._request(body, questions, keys, hold, priority))
        self.meter.hedges += 1
        pending = {first, second}
        error: BaseException | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None:
                        return task.result()
                    error = task.exception()
        finally:
            if not second.done():
                second.cancel()  # never cancel `first`: other callers may be sharing it
        assert error is not None
        raise error

    async def _request(
        self,
        body: dict,
        questions: Mapping[str, Question],
        keys: Mapping[str, str],
        hold: Hold,
        priority: bool,
    ) -> Flight:
        def retry():
            self.meter.retries += 1

        try:
            if self._workers is not None:
                data = await self._workers.post(
                    self.backend.url,
                    body,
                    provider=self.backend.name,
                    timeout=self.timeout,
                    attempts=self.attempts,
                    on_retry=retry,
                    priority=priority,
                )
            else:
                data = await transport.post(
                    self.http,
                    self.backend.url,
                    body,
                    provider=self.backend.name,
                    timeout=self.timeout,
                    attempts=self.attempts,
                    on_retry=retry,
                )
        except BaseException:
            hold.release()  # failed, refused or cancelled before any charge came back
            raise
        try:
            usage = parse_usage(data.get("usage"), price_per_mtok=self.backend.price_per_mtok)
        except JevFatal:
            hold.settle(hold.amount)  # answered, so perhaps billed, but unmetered: count the estimate
            raise
        self.meter.record_call(usage)
        hold.settle(usage.cost)
        answers = parse_answers(data, questions, provider=self.backend.name)
        origin = answer_origin(self.backend, resolved_model(data))
        if self.store is not None:
            for qid, answer in answers.items():
                self.store.put(keys[qid], answer, origin)
        return {keys[qid]: answer for qid, answer in answers.items()}, origin
