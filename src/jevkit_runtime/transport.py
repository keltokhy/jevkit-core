"""One HTTP retry and total-deadline implementation for every tool."""

from __future__ import annotations

import asyncio
import math
import random
import time

import httpx

from .errors import JevError, ProviderError, ProviderFatal, RequestExhausted
from .protocol import error_detail

RETRYABLE = frozenset({408, 429, 500, 502, 503, 504, 529})
FATAL = frozenset({401, 402, 403})


def _json_object(response: httpx.Response) -> dict | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", 0))
    except ValueError:
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


async def post(
    http: httpx.AsyncClient,
    url: str,
    body: dict,
    *,
    provider: str,
    timeout: float = 15.0,
    attempts: int = 4,
    delay: float = 0.2,
    jitter: float = 0.1,
    on_retry=None,
) -> dict:
    """POST `body` and return the JSON object it answers with.

    `timeout` bounds the whole call: every attempt, pause, and drip-fed body. Transport
    failures and retryable statuses are retried with backoff and Retry-After; other statuses
    and malformed 200 bodies fail at once.
    """
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("attempts must be a positive integer")
    deadline = time.monotonic() + timeout
    last = "no attempt made"
    timed_out = False
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        pause = delay * 2**attempt + random.random() * jitter
        try:
            response = await asyncio.wait_for(http.post(url, json=body, timeout=remaining), remaining)
        except asyncio.TimeoutError:
            last, timed_out = "deadline exceeded", True
            break
        except httpx.TransportError as exc:
            last = type(exc).__name__
        else:
            if response.status_code == 200:
                data = _json_object(response)
                if data is None:
                    raise JevError(f"{provider} returned a response that is not a JSON object")
                return data
            detail = error_detail(_json_object(response) or {}) or response.text[:200]
            if response.status_code in FATAL:
                raise ProviderFatal(provider, response.status_code, detail)
            if response.status_code not in RETRYABLE:
                raise ProviderError(provider, response.status_code, detail)
            last = f"HTTP {response.status_code}"
            pause = max(pause, _retry_after(response))
        if attempt + 1 == attempts or pause >= deadline - time.monotonic():
            break
        if on_retry is not None:
            on_retry()
        await asyncio.sleep(pause)
    raise RequestExhausted(timeout, last, timed_out=timed_out)
