"""The request pipeline: identity, cache, sharing, transport, validation, storage, metering."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Mapping
from typing import Any

import httpx

from . import transport
from .errors import JevBudgetExceeded
from .meter import Meter
from .protocol import (
    answer_key,
    answer_origin,
    parse_answers,
    parse_usage,
    request_body,
    resolved_model,
    validate_answer,
)
from .providers import Backend
from .store import AnswerStore

Flight = tuple[dict[str, dict], dict]  # answers by key, and the origin they share


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
        http2: bool = False,
    ):
        self.backend = backend
        self.timeout, self.attempts, self.store = timeout, attempts, store
        self.concurrency, self.transport, self.http2 = concurrency, transport, http2
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
                headers=headers,
                limits=limits,
                transport=self.transport,
                http2=self.http2 and self.transport is None,
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

    def key(self, state, question: dict) -> str:
        return answer_key(self.backend, state, question)

    async def ask(
        self,
        state,
        questions: dict[str, dict],
        *,
        keys: Mapping[str, str] | None = None,
        allow_paid: bool = True,
        on_cost: Callable[[float], None] | None = None,
        hedge_after: float | None = None,
        provenance: dict | None = None,
    ) -> dict[str, dict]:
        """Answer every question, sending only those the store cannot answer.

        `keys` overrides the identity of each question for callers whose reuse unit is not the
        request. `allow_paid=False` still serves store hits and joins an in-flight request.
        `on_cost` is charged only by the caller whose request actually went out. `hedge_after`
        sends a slow call a second time and keeps the first answer. `provenance`, if given,
        receives each question's origin: provider, models, `answered_at`, and a `source` of
        `cache`, `api`, or `shared`.
        """
        keys = dict(keys) if keys is not None else {qid: self.key(state, q) for qid, q in questions.items()}
        answers: dict[str, dict] = {}
        origins: dict[str, dict] = {}
        if self.store is not None:
            for qid, question in questions.items():
                if (entry := self.store.entry(keys[qid])) is not None:
                    validate_answer(qid, question, entry.answer)
                    answers[qid] = entry.answer
                    origins[qid] = dict(entry.metadata or {}) | {"source": "cache"}
        misses = {qid: q for qid, q in questions.items() if qid not in answers}
        if not misses:
            self.meter.cached += 1
        else:
            miss_keys = {qid: keys[qid] for qid in misses}

            def start() -> Coroutine[Any, Any, Flight]:
                if not allow_paid:
                    raise JevBudgetExceeded("a new paid request is not allowed by the budget")
                return self._request(state, misses, miss_keys, on_cost)

            task, owner = self._share(miss_keys.values(), start)
            if hedge_after is None:
                by_key, origin = await task
            else:
                by_key, origin = await self._hedged(task, hedge_after, state, misses, miss_keys, on_cost)
            for qid, key in miss_keys.items():
                answers[qid] = by_key[key]
                origins[qid] = dict(origin) | {"source": "api" if owner else "shared"}
        for origin in origins.values():
            self.meter.note_answer(origin)
        if provenance is not None:
            provenance.update(origins)
        return answers

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

    async def _request(self, state, questions: dict[str, dict], keys: dict[str, str], on_cost) -> Flight:
        def retry():
            self.meter.retries += 1

        data, seconds = await transport.post(
            self.http,
            self.backend.url,
            request_body(self.backend.model, state, questions),
            provider=self.backend.name,
            timeout=self.timeout,
            attempts=self.attempts,
            on_retry=retry,
        )
        usage = parse_usage(data.get("usage"), price_per_mtok=self.backend.price_per_mtok)
        self.meter.record_call(usage, seconds, on_cost)
        answers = parse_answers(data, questions, provider=self.backend.name)
        origin = answer_origin(self.backend, resolved_model(data))
        if self.store is not None:
            for qid, answer in answers.items():
                self.store.put(keys[qid], answer, origin)
        return {keys[qid]: answer for qid, answer in answers.items()}, origin
