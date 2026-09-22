"""One HTTP retry and deadline implementation for all JevKit tools."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from dataclasses import dataclass

import httpx

from .errors import JevError, JevFatal, RequestExhausted

RETRYABLE = frozenset({408, 429, 500, 502, 503, 504, 529})
FATAL = frozenset({401, 402, 403})


@dataclass(frozen=True)
class RetryPolicy:
    delay: float = 0.2
    jitter: float = 0.1
    retry_after: bool = False
    strict_json: bool = False
    require_answers: bool = True
    error_details: bool = True


DEFAULT_RETRY_POLICY = RetryPolicy()


def json_object(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def error_detail(data: dict) -> str:
    found = data.get("error", data.get("detail"))
    if isinstance(found, list):
        found = "; ".join(error_detail({"detail": item}) for item in found)
    elif isinstance(found, dict):
        found = found.get("message") or found.get("msg") or json.dumps(found)
    return " ".join(str(found or "").split())[:200]


async def request_json(
    client: httpx.AsyncClient,
    url: str,
    body: dict,
    *,
    provider: str,
    timeout: float = 15.0,
    attempts: int = 4,
    on_retry=None,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> tuple[dict, float]:
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
        started = time.perf_counter()
        pause = policy.delay * 2**attempt + random.random() * policy.jitter
        try:
            response = await asyncio.wait_for(client.post(url, json=body, timeout=remaining), remaining)
        except asyncio.TimeoutError:
            last = "deadline exceeded"
            timed_out = True
            break
        except httpx.TransportError as exc:
            last = type(exc).__name__
        else:
            if response.status_code == 200 and policy.strict_json:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise JevError("provider returned invalid JSON") from exc
                if not isinstance(data, dict):
                    raise JevError("provider returned a non-object response")
            else:
                data = json_object(response)
            if response.status_code == 200 and (not policy.require_answers or "answers" in data):
                return data, time.perf_counter() - started
            if response.status_code in FATAL or (
                response.status_code != 200 and response.status_code not in RETRYABLE
            ):
                error = JevFatal if response.status_code in FATAL else JevError
                if not policy.error_details:
                    raise error(f"{provider} returned HTTP {response.status_code}")
                detail = error_detail(data) or response.text[:200]
                message = (
                    f"{provider} said {response.status_code}: {detail}"
                    if error is JevFatal
                    else f"HTTP {response.status_code}: {detail}"
                )
                raise error(message)
            last = f"HTTP {response.status_code}"
            if policy.retry_after:
                try:
                    retry_after = float(response.headers.get("Retry-After", 0))
                    if math.isfinite(retry_after):
                        pause = max(pause, retry_after)
                except ValueError:
                    pass
        if attempt + 1 < attempts:
            if on_retry is not None:
                on_retry()
            await asyncio.sleep(max(0.0, min(pause, deadline - time.monotonic())))
    raise RequestExhausted(timeout, last, timed_out=timed_out)
