import asyncio
import json

import httpx
import pytest

from jevkit_core import JevError, JevFatal, ProviderStatus, RequestExhausted, post


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 5))


def test_retry_resends_the_same_request_and_counts_only_retries():
    seen, retries = [], []
    body = {"model": "v1", "state": "évidence", "questions": {"q": {"type": "noul"}}}

    def fake(request):
        seen.append((str(request.url), json.loads(request.content), request.headers["authorization"]))
        return httpx.Response(503 if len(seen) == 1 else 200, json={"answers": {"q": {"noul": 0.7}}})

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fake), headers={"Authorization": "Bearer test"}
        ) as http:
            data, seconds = await post(
                http,
                "https://fixture.invalid/api",
                body,
                provider="fake",
                delay=0,
                jitter=0,
                on_retry=lambda: retries.append(1),
            )
            assert seconds >= 0
            return data

    assert run(exercise()) == {"answers": {"q": {"noul": 0.7}}}
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

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            with pytest.raises(
                RequestExhausted, match=r"gave up after 0\.05s \(deadline exceeded\)"
            ) as caught:
                await post(http, "https://fixture.invalid", {}, provider="fake", timeout=0.05)
            assert caught.value.timed_out

    run(exercise())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,error,message",
    [
        (401, JevFatal, "fake said 401: secret"),
        (402, JevFatal, "fake said 402: secret"),
        (400, JevError, "HTTP 400: secret"),
    ],
)
def test_non_retryable_statuses_fail_once_with_the_provider_detail(status, error, message):
    calls = []

    def fake(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "secret"})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            with pytest.raises(error, match=message) as caught:
                await post(http, "https://fixture.invalid", {}, provider="fake")
            assert isinstance(caught.value, ProviderStatus)
            assert (caught.value.provider, caught.value.status, caught.value.detail) == (
                "fake",
                status,
                "secret",
            )

    run(exercise())
    assert len(calls) == 1


def test_a_200_that_is_not_a_json_object_is_not_retried():
    calls = []

    def fake(request):
        calls.append(request)
        return httpx.Response(200, text="[]")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            with pytest.raises(JevError, match="not a JSON object"):
                await post(http, "https://fixture.invalid", {}, provider="fake")

    run(exercise())
    assert len(calls) == 1


def test_retry_after_lengthens_the_pause_and_pauses_stay_within_the_deadline(monkeypatch):
    waits, calls = [], []

    async def sleep(delay):
        waits.append(delay)

    def fake(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0.75"})

    monkeypatch.setattr("jevkit_core.transport.asyncio.sleep", sleep)

    async def exercise(timeout):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            with pytest.raises(RequestExhausted, match=r"\(HTTP 429\)"):
                await post(
                    http, "https://fixture.invalid", {}, provider="fake", timeout=timeout, delay=0, jitter=0
                )

    run(exercise(2))
    assert waits == [0.75] * 3 and len(calls) == 4
    waits.clear(), calls.clear()
    run(exercise(0.5))  # the first pause alone would cross the deadline: give up without sleeping
    assert waits == [] and len(calls) == 1


def test_cancellation_does_not_start_another_attempt():
    calls, cancelled = [], []

    async def fake(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            task = asyncio.create_task(post(http, "https://fixture.invalid", {}, provider="fake"))
            while not calls:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(exercise())
    assert len(calls) == 1 and cancelled == [True]
