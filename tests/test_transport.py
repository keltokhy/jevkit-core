import asyncio
import json

import httpx
import pytest

from jevkit_core import JevError, JevFatal, RetryPolicy, request_json


def test_retry_preserves_request_and_accounts_only_retries():
    seen, retries = [], []
    body = {"model": "v1", "state": "évidence", "questions": {"q": {"type": "noul"}}}

    async def fake(request):
        seen.append((str(request.url), json.loads(request.content), request.headers["authorization"]))
        return httpx.Response(503 if len(seen) == 1 else 200, json={"answers": {"q": {"noul": 0.7}}})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fake), headers={"Authorization": "Bearer test"}
        ) as client:
            data, _ = await request_json(
                client,
                "https://fixture.invalid/api",
                body,
                provider="fake",
                on_retry=lambda: retries.append(1),
                policy=RetryPolicy(delay=0, jitter=0),
            )
            return data

    assert asyncio.run(run()) == {"answers": {"q": {"noul": 0.7}}}
    assert seen == [("https://fixture.invalid/api", body, "Bearer test")] * 2
    assert retries == [1]


def test_deadline_covers_a_body_that_keeps_arriving():
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

    calls = []

    async def fake(request):
        calls.append(request)
        return httpx.Response(200, stream=SlowBody())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            with pytest.raises(JevError, match="deadline exceeded") as caught:
                await asyncio.wait_for(
                    request_json(client, "https://fixture.invalid", {}, provider="fake", timeout=0.05), 0.5
                )
            assert caught.value.timed_out is True

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("status,error", [(401, JevFatal), (402, JevFatal), (403, JevFatal), (400, JevError)])
def test_nonretryable_errors_are_redacted_when_requested(status, error):
    calls = []

    def fake(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "secret credential or input"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            with pytest.raises(error) as caught:
                await request_json(
                    client,
                    "https://fixture.invalid",
                    {},
                    provider="fake",
                    policy=RetryPolicy(error_details=False),
                )
        assert str(caught.value) == f"fake returned HTTP {status}"

    asyncio.run(run())
    assert len(calls) == 1


def test_cancellation_does_not_start_another_attempt():
    calls = []
    cancelled = []

    async def fake(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            task = asyncio.create_task(request_json(client, "https://fixture.invalid", {}, provider="fake"))
            while not calls:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert len(calls) == 1 and cancelled == [True]


def test_retry_after_and_strict_json_policy(monkeypatch):
    waits, calls = [], []

    async def sleep(delay):
        waits.append(delay)

    def fake(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.75"})
        return httpx.Response(200, text="not JSON")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            with pytest.raises(JevError, match="invalid JSON"):
                await request_json(
                    client,
                    "https://fixture.invalid",
                    {},
                    provider="fake",
                    policy=RetryPolicy(delay=0, jitter=0, retry_after=True, strict_json=True),
                )

    monkeypatch.setattr("jevkit_core.transport.asyncio.sleep", sleep)
    asyncio.run(run())
    assert waits == [0.75]
    assert len(calls) == 2
