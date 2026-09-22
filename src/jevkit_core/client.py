"""Shared client lifecycle and transport; adapters own answer/request reuse semantics."""

from __future__ import annotations

import asyncio
import math
import os

import httpx

from . import transport
from .backends import Backend
from .errors import JevError
from .usage import Meter


def validate_answer(qid: str, question: dict, answer) -> None:
    if not isinstance(answer, dict):
        raise JevError(f"invalid answer returned for question {qid!r}: expected an object")
    if question.get("type") == "noul":
        p = answer.get("noul")
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise JevError(
                f"invalid answer returned for question {qid!r}: noul must be a probability from 0 to 1"
            )


class DecisionClient:
    def __init__(
        self,
        key: str,
        backend: Backend,
        *,
        model: str | None = None,
        timeout: float = 15.0,
        attempts: int = 4,
        concurrency: int = 32,
        cache=None,
        transport=None,
        meter: Meter | None = None,
        http2: bool = False,
    ):
        self.backend = backend
        self.model = model or os.environ.get("JEV_MODEL") or backend.model
        self.url = backend.endpoint()
        self.timeout, self.attempts, self.cache = timeout, attempts, cache
        self.meter = meter if meter is not None else Meter()
        self._flights: dict[str, asyncio.Task] = {}
        headers = {"X-Title": "jev tools"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        limits = (
            httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=120)
            if http2
            else httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency + 4)
        )
        self.http = httpx.AsyncClient(
            headers=headers, limits=limits, transport=transport, http2=http2 and transport is None
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, state, questions: dict[str, dict], **kwargs):
        body = {"model": self.model, "state": state, "questions": questions}

        def retry():
            self.meter.retries += 1

        data, seconds = await transport.request_json(
            self.http,
            self.url,
            body,
            provider=self.backend.name,
            timeout=self.timeout,
            attempts=self.attempts,
            on_retry=retry,
        )
        return self._record(state, questions, data, seconds, **kwargs)

    def _record(self, state, questions, data, seconds, **kwargs):
        raise NotImplementedError("the product adapter must record and validate its answer contract")
