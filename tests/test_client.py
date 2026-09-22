import asyncio

import httpx
import pytest

from jevkit_core import AnswerStore, Backend, Client, JevBudgetExceeded, JevError, JevFatal

BACKEND = Backend(
    "typesafe", "https://fixture.invalid/v1", "requested-alias", key="test-key", price_per_mtok=0.042
)
QUESTIONS = {
    "q": {"type": "noul", "instructions": "matches"},
    "r": {"type": "noul", "instructions": "relevant"},
}


class Fake:
    def __init__(self, *, model="resolved-v1", usage=None, answer=None, delay=0.0):
        self.bodies, self.model, self.delay = [], model, delay
        self.usage = {"input_tokens": 100, "cost": 0.002} if usage is None else usage
        self.answer = answer or (lambda qid: {"noul": 0.75})

    async def __call__(self, request):
        import json

        body = json.loads(request.content)
        self.bodies.append(body)
        if self.delay:
            await asyncio.sleep(self.delay)
        data = {"answers": {qid: self.answer(qid) for qid in body["questions"]}, "usage": self.usage}
        if self.model is not None:
            data["model"] = self.model
        return httpx.Response(200, json=data)

    def client(self, **kwargs):
        return Client(BACKEND, transport=httpx.MockTransport(self), **kwargs)


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 5))


def test_a_request_is_sent_once_then_answered_from_the_store(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")

    async def exercise():
        async with fake.client(store=store) as client:
            origins = {}
            first = await client.ask("évidence", QUESTIONS, provenance=origins)
            assert first == {"q": {"noul": 0.75}, "r": {"noul": 0.75}}
            assert fake.bodies == [{"model": "requested-alias", "state": "évidence", "questions": QUESTIONS}]
            assert origins["q"]["source"] == "api" and origins["q"]["resolved_model"] == "resolved-v1"
            assert (
                origins["q"]["provider"] == "typesafe"
                and origins["q"]["requested_model"] == "requested-alias"
            )
            again = {}
            assert await client.ask("évidence", QUESTIONS, provenance=again) == first
            assert again["r"]["source"] == "cache" and again["r"]["resolved_model"] == "resolved-v1"
            assert len(fake.bodies) == 1
            meter = client.meter
            assert (meter.calls, meter.cached, meter.input_tokens, meter.cost) == (1, 1, 100, 0.002)
            assert meter.model == "resolved-v1" and meter.cost_sources == {"reported_by_api": 1}
            assert meter.provider == "typesafe" and meter.requested_model == "requested-alias"
            assert meter.summary() == "1 calls, 1 cached; 100 tokens; $0.0020"
            assert store.entry(client.key("évidence", QUESTIONS["q"])).metadata["provider"] == "typesafe"

    run(exercise())
    store.close()


def test_only_missing_questions_are_sent_and_cached_answers_are_validated(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")

    async def exercise():
        async with fake.client(store=store) as client:
            store.put(client.key("text", QUESTIONS["q"]), {"noul": 0.2})
            origins = {}
            answers = await client.ask("text", QUESTIONS, provenance=origins)
            assert answers == {"q": {"noul": 0.2}, "r": {"noul": 0.75}}
            assert list(fake.bodies[0]["questions"]) == ["r"]
            assert origins["q"] == {"source": "cache"}
            assert client.meter.unknown_model_answers == 1 and client.meter.resolved_models == ["resolved-v1"]
            assert client.meter.model == ""
            store.put(client.key("text", QUESTIONS["q"]), {"noul": 2})
            with pytest.raises(JevError, match="question 'q'"):
                await client.ask("text", QUESTIONS)

    run(exercise())
    store.close()


@pytest.mark.parametrize("answers", [None, [], {"q": {"noul": 0.9}}, {"q": {"noul": 0.9}, "r": {}}])
def test_invalid_responses_are_billed_but_never_partially_stored(tmp_path, answers):
    store = AnswerStore(tmp_path / "answers.sqlite")
    charges = []
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"answers": answers, "usage": {"cost": 0.01}})
    )

    async def exercise():
        async with Client(BACKEND, store=store, transport=transport) as client:
            with pytest.raises(JevError, match="answer"):
                await client.ask("text", QUESTIONS, on_cost=charges.append)
            assert client.meter.calls == 1 and client.meter.cost == 0.01
            assert store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0

    run(exercise())
    assert charges == [0.01]
    store.close()


def test_invalid_usage_is_fatal_before_anything_is_counted():
    fake = Fake(usage={"cost": "free"})

    async def exercise():
        async with fake.client() as client:
            with pytest.raises(JevFatal, match="invalid API usage"):
                await client.ask("text", QUESTIONS)
            assert client.meter.calls == 0

    run(exercise())


def test_one_owner_pays_and_a_cache_only_caller_can_join_the_flight():
    fake = Fake(delay=0.05)
    charges = [[], []]

    async def exercise():
        async with fake.client() as client:
            first = asyncio.create_task(client.ask("s", QUESTIONS, on_cost=charges[0].append))
            await asyncio.sleep(0.01)
            origins = {}
            second = asyncio.create_task(
                client.ask("s", QUESTIONS, allow_paid=False, on_cost=charges[1].append, provenance=origins)
            )
            assert await first == await second
            assert len(fake.bodies) == 1 and origins["q"]["source"] == "shared"
            assert (client.meter.calls, client.meter.cached) == (1, 1)
            assert not client._flights
            with pytest.raises(JevBudgetExceeded):
                await client.ask("other", QUESTIONS, allow_paid=False)
            assert len(fake.bodies) == 1

    run(exercise())
    assert charges == [[0.002], []]


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
def test_failed_and_cancelled_flights_release_their_key(outcome):
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        await release.wait()
        return httpx.Response(500, json={"error": "boom"})

    async def exercise():
        async with Client(BACKEND, attempts=1, transport=httpx.MockTransport(respond)) as client:
            first = asyncio.create_task(client.ask("s", QUESTIONS))
            await entered.wait()
            second = asyncio.create_task(client.ask("s", QUESTIONS, allow_paid=False))
            await asyncio.sleep(0)
            assert len(client._flights) == 1
            if outcome == "cancel":
                first.cancel()
            else:
                release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            expected = asyncio.CancelledError if outcome == "cancel" else JevError
            assert all(isinstance(result, expected) for result in results)
            assert not client._flights
            release.set()

    run(exercise())


def test_a_slow_call_is_hedged_and_the_first_answer_wins():
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            await asyncio.sleep(5)
        return httpx.Response(200, json={"answers": {"q": {"noul": 0.9}}, "usage": {"cost": 0.001}})

    async def exercise():
        async with Client(BACKEND, transport=httpx.MockTransport(respond)) as client:
            quick = await client.ask("s", {"q": QUESTIONS["q"]}, hedge_after=0.05)
            assert quick == {"q": {"noul": 0.9}} and client.meter.hedges == 1
            await client.ask("t", {"q": QUESTIONS["q"]}, hedge_after=0.5)
            assert client.meter.hedges == 1 and len(calls) == 3
            for task in list(client._flights.values()):
                task.cancel()

    run(exercise())


def test_callers_may_supply_their_own_answer_identity(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")

    async def exercise():
        async with fake.client(store=store) as client:
            keys = {"q": "passage-one", "r": "passage-two"}
            await client.ask({"q": "one", "r": "two"}, QUESTIONS, keys=keys)
            assert store.get("passage-one") == {"noul": 0.75}
            await client.ask({"q": "one", "r": "two", "s": "three"}, QUESTIONS, keys=keys)
            assert len(fake.bodies) == 1 and client.meter.cached == 1

    run(exercise())
    store.close()


def test_retries_are_counted_and_fatal_statuses_stop_at_once():
    statuses = iter([503, 200])

    def respond(request):
        status = next(statuses)
        return httpx.Response(
            status, json={"answers": {"q": {"noul": 0.5}}} if status == 200 else {"error": "busy"}
        )

    async def exercise():
        async with Client(BACKEND, transport=httpx.MockTransport(respond)) as client:
            await client.ask("s", {"q": QUESTIONS["q"]})
            assert client.meter.retries == 1
        fatal = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "bad key"}))
        async with Client(BACKEND, transport=fatal) as client:
            with pytest.raises(JevFatal, match="typesafe said 401: bad key"):
                await client.ask("s", {"q": QUESTIONS["q"]})

    run(exercise())
