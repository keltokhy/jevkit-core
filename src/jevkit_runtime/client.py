"""The request pipeline: identity, store, sharing, transport, validation, storage, metering."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from . import transport
from .errors import JevBudgetExceeded, JevError
from .meter import Meter
from .protocol import (
    answer_keys,
    answer_origin,
    packed_keys,
    parse_answers,
    parse_usage,
    request_body,
    resolved_model,
)
from .providers import Backend
from .question import Question
from .store import AnswerStore

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


@dataclass(frozen=True)
class Plan:
    """What asking would do, read from the store without sending or writing anything."""

    state: Any
    questions: dict[str, Question]
    keys: dict[str, str]
    hits: dict[str, dict]  # answers the store already holds
    origins: dict[str, dict]  # their provenance, each with source "cache"
    misses: dict[str, Question]  # what a request would carry
    request: dict | None  # the body it would send; None when nothing is missing
    oversized: str | None  # why that request exceeds the provider's limits, if it does

    @property
    def complete(self) -> bool:
        return not self.misses


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
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.backend = backend
        self.timeout, self.attempts, self.store = timeout, attempts, store
        self.concurrency, self.transport = concurrency, transport
        self.http2 = transport is None and http2_available()
        self.meter = Meter(provider=backend.name, requested_model=backend.model)
        self._flights: dict[str, asyncio.Task[Flight]] = {}
        self._http: httpx.AsyncClient | None = None

    @property
    def model(self) -> str:
        return self.backend.model

    @property
    def url(self) -> str:
        return self.backend.url

    @property
    def http(self) -> httpx.AsyncClient:
        """Opened on first use, so cache-only clients never hold a connection pool."""
        if self._http is None:
            headers = {"X-Title": "jev tools"}
            if self.backend.key:
                headers["Authorization"] = f"Bearer {self.backend.key}"
            limits = (
                httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=120)
                if self.http2
                else httpx.Limits(
                    max_connections=self.concurrency + 4, max_keepalive_connections=self.concurrency + 4
                )
            )
            self._http = httpx.AsyncClient(
                headers=headers, limits=limits, transport=self.transport, http2=self.http2
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

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
        hits = self._stored(questions, keys)
        if self.backend.joint_reads and len(hits) != len(questions):
            hits = {}
        misses = {qid: q for qid, q in questions.items() if qid not in hits}
        request = request_body(self.backend.model, state, misses) if misses else None
        return Plan(
            state,
            dict(questions),
            keys,
            {qid: answer for qid, (answer, _) in hits.items()},
            {qid: origin for qid, (_, origin) in hits.items()},
            misses,
            request,
            self._oversized(request),
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
        if request is None:
            return None
        name, limit, read = self.backend.name, self.backend.max_request_bytes, self.backend.max_read_bytes
        if limit is not None and (size := _size(request)) > limit:
            return f"request of {size:,} bytes is over {name}'s limit of {limit:,}"
        if read is not None and request["questions"]:
            size = _size(request["state"]) + max(_size(q) for q in request["questions"].values())
            if size > read:
                return f"state and question of {size:,} bytes are over {name}'s limit of {read:,}"
        return None

    # ---- asking -----------------------------------------------------------------------------------

    async def ask(
        self,
        state,
        questions: Mapping[str, Question],
        *,
        scope: str | None = None,
        allow_paid: bool = True,
        on_cost: Callable[[float], None] | None = None,
        hedge_after: float | None = None,
    ) -> Answers:
        """Answer every question, sending only those the store cannot answer. `plan` and then `send`."""
        return await self.send(
            self.plan(state, questions, scope=scope),
            allow_paid=allow_paid,
            on_cost=on_cost,
            hedge_after=hedge_after,
        )

    async def send(
        self,
        plan: Plan,
        *,
        allow_paid: bool = True,
        on_cost: Callable[[float], None] | None = None,
        hedge_after: float | None = None,
    ) -> Answers:
        """Complete a plan: join an identical request already in flight, or send its misses.

        `allow_paid=False` still serves store hits and joins an in-flight request. `on_cost` is charged
        only by the caller whose request actually went out. `hedge_after` sends a slow call a second
        time and keeps the first answer.
        """
        answers, origins = dict(plan.hits), dict(plan.origins)
        if not plan.misses:
            self.meter.cached += 1
        else:
            if plan.oversized:
                raise JevError(plan.oversized)
            fresh, owner = await self._fetch(
                plan.state, plan.misses, plan.keys, allow_paid, on_cost, hedge_after
            )
            for qid, (answer, origin) in fresh.items():
                answers[qid] = answer
                origins[qid] = origin | {"source": "api" if owner else "shared"}
        for origin in origins.values():
            self.meter.note_answer(origin)
        return Answers({q: answers[q] for q in plan.questions}, {q: origins[q] for q in plan.questions})

    async def ask_packed(
        self,
        items: Mapping[str, Any],
        question: Question,
        *,
        prefix: str = "p",
        reuse: str = "item",
        max_items: int | None = None,
        scope: str | None = None,
        allow_paid: bool = True,
        on_cost: Callable[[float], None] | None = None,
        hedge_after: float | None = None,
    ) -> Answers:
        """Ask one question about each of several items, packing items into as few calls as fit.

        Each call's state holds its items in slots `prefix` + position, and `question` is asked once per
        slot with `{slot}` filled in. Answers come back by item id.

        `reuse="item"` keys each answer on its item and the question as written, so an item answered
        in one call is served from the store in any other. `reuse="call"` keys it on its place in the
        whole call, which is served whole or asked again whole; joint-read backends always use it.

        Calls go out together. If one fails, its error is raised once every call has finished, and the
        answers that did arrive are already stored.
        """
        if reuse not in REUSE:
            raise ValueError(f"reuse must be one of {', '.join(REUSE)}")
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be at least 1")
        by_item = reuse == "item" and not self.backend.joint_reads
        entries = list(items.items())
        if by_item:
            keys = packed_keys(self.backend, [entries], question, prefix=prefix, reuse="item", scope=scope)
            stored = self._stored(dict.fromkeys(items, question), keys)
            first: dict[str, str] = {}  # an item whose key another item already carries is asked once
            for item, _ in entries:
                if item not in stored:
                    first.setdefault(keys[item], item)
            calls = self._calls([(i, items[i]) for i in first.values()], question, prefix, max_items)
        else:
            calls = self._calls(entries, question, prefix, max_items)
            keys = packed_keys(self.backend, calls, question, prefix=prefix, reuse="call", scope=scope)
            stored = self._stored(dict.fromkeys(items, question), keys)
            whole = [call for call in calls if all(item in stored for item, _ in call)]
            stored = {item: stored[item] for call in whole for item, _ in call}
            calls = [call for call in calls if call not in whole]

        answers = {item: answer for item, (answer, _) in stored.items()}
        origins = {item: origin for item, (_, origin) in stored.items()}
        if not calls:
            self.meter.cached += 1
        results = await asyncio.gather(
            *(
                self._packed_call(call, question, prefix, keys, allow_paid, on_cost, hedge_after)
                for call in calls
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
            for item, (answer, origin) in result.items():
                answers[item], origins[item] = answer, origin
        if by_item:
            for item, _ in entries:  # items that shared a key take the answer their twin was given
                if item not in answers:
                    twin = first[keys[item]]
                    answers[item], origins[item] = answers[twin], origins[twin]
        for item in items:
            self.meter.note_answer(origins[item])
        return Answers({i: answers[i] for i in items}, {i: origins[i] for i in items})

    def _slots(self, call: list[tuple[str, Any]], question: Question, prefix: str):
        slots = [f"{prefix}{position}" for position in range(len(call))]
        state = {slot: item_state for slot, (_, item_state) in zip(slots, call, strict=True)}
        return slots, state, {slot: question.at(slot) for slot in slots}

    def _calls(self, entries, question: Question, prefix: str, max_items: int | None) -> list[list]:
        """Items in order, in calls of at most `max_items` that each fit the provider's limits."""
        calls: list[list] = []
        for entry in entries:
            if calls and (max_items is None or len(calls[-1]) < max_items):
                _, state, questions = self._slots(calls[-1] + [entry], question, prefix)
                if not self._oversized(request_body(self.backend.model, state, questions)):
                    calls[-1].append(entry)
                    continue
            _, state, questions = self._slots([entry], question, prefix)
            if reason := self._oversized(request_body(self.backend.model, state, questions)):
                raise JevError(f"item {entry[0]!r} alone is too large: {reason}")
            calls.append([entry])
        return calls

    async def _packed_call(self, call, question, prefix, keys, allow_paid, on_cost, hedge_after) -> dict:
        slots, state, questions = self._slots(call, question, prefix)
        slot_keys = {slot: keys[item] for slot, (item, _) in zip(slots, call, strict=True)}
        fresh, owner = await self._fetch(state, questions, slot_keys, allow_paid, on_cost, hedge_after)
        source = "api" if owner else "shared"
        return {
            item: (fresh[slot][0], fresh[slot][1] | {"source": source})
            for slot, (item, _) in zip(slots, call, strict=True)
        }

    # ---- sending ----------------------------------------------------------------------------------

    async def _fetch(
        self,
        state,
        questions: Mapping[str, Question],
        keys: Mapping[str, str],
        allow_paid,
        on_cost,
        hedge_after,
    ) -> tuple[dict[str, tuple[dict, dict]], bool]:
        """Answers for `questions` by id, each with its origin, and whether this caller sent the request."""
        send_keys = {qid: keys[qid] for qid in questions}

        def start() -> Coroutine[Any, Any, Flight]:
            if not allow_paid:
                raise JevBudgetExceeded("a new paid request is not allowed by the budget")
            return self._request(state, questions, send_keys, on_cost)

        task, owner = self._share(send_keys.values(), start)
        if hedge_after is None:
            by_key, origin = await task
        else:
            by_key, origin = await self._hedged(task, hedge_after, state, questions, send_keys, on_cost)
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

    async def _hedged(self, first, after, state, questions, keys, on_cost) -> Flight:
        done, _ = await asyncio.wait({first}, timeout=after)
        if done:
            return first.result()
        second = asyncio.ensure_future(self._request(state, questions, keys, on_cost))
        self.meter.hedges += 1
        pending = {first, second}
        error: BaseException | None = None
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    if second in pending:
                        second.cancel()  # never cancel `first`: other callers may be sharing it
                    return task.result()
                error = task.exception()
        assert error is not None
        raise error

    async def _request(
        self, state, questions: Mapping[str, Question], keys: Mapping[str, str], on_cost
    ) -> Flight:
        def retry():
            self.meter.retries += 1

        data = await transport.post(
            self.http,
            self.backend.url,
            request_body(self.backend.model, state, questions),
            provider=self.backend.name,
            timeout=self.timeout,
            attempts=self.attempts,
            on_retry=retry,
        )
        usage = parse_usage(data.get("usage"), price_per_mtok=self.backend.price_per_mtok)
        self.meter.record_call(usage, on_cost)
        answers = parse_answers(data, questions, provider=self.backend.name)
        origin = answer_origin(self.backend, resolved_model(data))
        if self.store is not None:
            for qid, answer in answers.items():
                self.store.put(keys[qid], answer, origin)
        return {keys[qid]: answer for qid, answer in answers.items()}, origin
