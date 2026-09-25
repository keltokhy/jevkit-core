import asyncio
import json

import httpx
import pytest

from jevkit_runtime import Answers, AnswerStore, Backend, Client, JevBudgetExceeded, JevError, JevFatal, Noul
from jevkit_runtime import client as client_module

BACKEND = Backend(
    "typesafe", "https://fixture.invalid/v1", "requested-alias", key="test-key", price_per_mtok=0.042
)
QUESTIONS = {"q": Noul("matches"), "r": Noul("relevant")}
WIRE = {qid: q.body() for qid, q in QUESTIONS.items()}


class Fake:
    def __init__(self, *, model="resolved-v1", usage=None, answer=None, delay=0.0):
        self.bodies, self.model, self.delay = [], model, delay
        self.usage = {"input_tokens": 100, "cost": 0.002} if usage is None else usage
        self.answer = answer or (lambda qid: {"noul": 0.75})

    async def __call__(self, request):
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
            first = await client.ask("évidence", QUESTIONS)
            assert isinstance(first, Answers) and first == {"q": {"noul": 0.75}, "r": {"noul": 0.75}}
            assert fake.bodies == [{"model": "requested-alias", "state": "évidence", "questions": WIRE}]
            origin = first.origins["q"]
            assert (origin["source"], origin["resolved_model"]) == ("api", "resolved-v1")
            assert (origin["provider"], origin["requested_model"]) == ("typesafe", "requested-alias")
            again = await client.ask("évidence", QUESTIONS)
            assert again == first
            assert (
                again.origins["r"]["source"] == "cache"
                and again.origins["r"]["resolved_model"] == "resolved-v1"
            )
            assert len(fake.bodies) == 1
            meter = client.meter
            assert (meter.calls, meter.cached, meter.input_tokens, meter.cost) == (1, 1, 100, 0.002)
            assert meter.model == "resolved-v1" and meter.cost_sources == {"reported_by_api": 1}
            assert meter.provider == "typesafe" and meter.requested_model == "requested-alias"
            assert meter.summary() == "1 calls, 1 cached; 100 tokens; $0.0020"
            key = client.plan("évidence", QUESTIONS).keys["q"]
            assert store.entry(key).metadata["provider"] == "typesafe"

    run(exercise())
    store.close()


def test_only_missing_questions_are_sent_and_a_malformed_stored_answer_is_asked_again(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")

    async def exercise():
        async with fake.client(store=store) as client:
            key = client.plan("text", QUESTIONS).keys["q"]
            store.put(key, {"noul": 0.2})
            answers = await client.ask("text", QUESTIONS)
            assert answers == {"q": {"noul": 0.2}, "r": {"noul": 0.75}}
            assert list(fake.bodies[0]["questions"]) == ["r"]
            assert answers.origins["q"] == {"source": "cache"}
            assert client.meter.unknown_model_answers == 1 and client.meter.resolved_models == ["resolved-v1"]
            assert client.meter.model == ""
            store.put(key, {"noul": 2})
            again = await client.ask("text", QUESTIONS)
            assert again["q"] == {"noul": 0.75} and list(fake.bodies[1]["questions"]) == ["q"]
            assert store.get(key) == {"noul": 0.75}

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
            second = asyncio.create_task(
                client.ask("s", QUESTIONS, allow_paid=False, on_cost=charges[1].append)
            )
            assert await first == await second
            assert len(fake.bodies) == 1
            assert first.result().origins["q"]["source"] == "api"
            assert second.result().origins["q"]["source"] == "shared"
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


def test_a_joint_read_is_reused_whole_or_repeated_whole(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")
    joint = Backend(
        "diffusiongemma", "http://127.0.0.1:8080/v1/systemone", "openjev-latest", joint_reads=True
    )

    async def exercise():
        async with Client(joint, store=store, transport=httpx.MockTransport(fake)) as client:
            first = await client.ask("s", QUESTIONS)
            assert await client.ask("s", QUESTIONS) == first and len(fake.bodies) == 1
            await client.ask("s", dict(reversed(QUESTIONS.items())))
            assert len(fake.bodies) == 2  # the same questions in another order are another read
            await client.ask("s", {"q": QUESTIONS["q"]})
            assert len(fake.bodies) == 3  # and so is one of them alone
            store.db.execute("DELETE FROM answers WHERE key = ?", (client.plan("s", QUESTIONS).keys["r"],))
            await client.ask("s", QUESTIONS)
            assert len(fake.bodies) == 4 and list(fake.bodies[-1]["questions"]) == ["q", "r"]
            assert client.meter.cached == 1

    run(exercise())
    store.close()


def test_a_scope_keeps_a_tool_s_answers_apart_but_never_replaces_the_key(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")

    async def exercise():
        async with fake.client(store=store) as client:
            plain, scoped = client.plan("s", QUESTIONS), client.plan("s", QUESTIONS, scope="tool/v1")
            assert set(plain.keys.values()).isdisjoint(scoped.keys.values())
            await client.ask("s", QUESTIONS, scope="tool/v1")
            assert client.plan("s", QUESTIONS, scope="tool/v1").complete
            assert not client.plan("s", QUESTIONS).hits and len(fake.bodies) == 1

    run(exercise())
    store.close()


def test_a_plan_reads_the_store_and_sends_nothing(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")

    async def exercise():
        async with fake.client(store=store) as client:
            await client.ask("s", {"q": QUESTIONS["q"]})
            plan = client.plan("s", QUESTIONS)
            assert plan.hits == {"q": {"noul": 0.75}} and plan.origins["q"]["source"] == "cache"
            assert list(plan.misses) == ["r"] and not plan.complete and plan.oversized is None
            assert plan.request == {"model": "requested-alias", "state": "s", "questions": {"r": WIRE["r"]}}
            assert len(fake.bodies) == 1 and client.meter.cached == 0
            answers = await client.send(plan)
            assert answers.origins["q"]["source"] == "cache" and answers.origins["r"]["source"] == "api"

    run(exercise())
    store.close()


def test_a_request_over_the_provider_s_limits_is_refused_before_it_is_sent():
    fake = Fake()
    limited = Backend(
        "typesafe", "https://fixture.invalid/v1", "m", key="k", max_request_bytes=400, max_read_bytes=200
    )

    async def exercise():
        async with Client(limited, transport=httpx.MockTransport(fake)) as client:
            assert client.plan("short", QUESTIONS).oversized is None
            plan = client.plan("x" * 180, QUESTIONS)
            assert "state and question" in plan.oversized
            with pytest.raises(JevError, match="over typesafe's limit"):
                await client.send(plan)
            assert "request of" in client.plan("s", {f"q{i}": Noul(f"rule {i}") for i in range(20)}).oversized
            assert not fake.bodies

    run(exercise())


def test_packed_items_share_calls_and_are_reused_one_by_one(tmp_path):
    fake = Fake(answer=lambda slot: {"noul": 0.9})
    store = AnswerStore(tmp_path / "answers.sqlite")
    question = Noul("Passage {slot} is relevant")

    async def exercise():
        async with fake.client(store=store) as client:
            items = {"a": "one", "b": "two", "c": "three", "d": "one"}
            first = await client.ask_packed(items, question, max_items=2)
            assert first == {item: {"noul": 0.9} for item in items}
            # "d" repeats "a", so three distinct items go out, two to a call
            assert [list(b["state"].values()) for b in fake.bodies] == [["one", "two"], ["three"]]
            assert fake.bodies[0]["questions"]["p1"]["instructions"] == "Passage p1 is relevant"
            assert first.origins["d"] == first.origins["a"]
            again = await client.ask_packed({"x": "three", "y": "one", "z": "four"}, question, max_items=2)
            assert [list(b["state"].values()) for b in fake.bodies[2:]] == [["four"]]
            assert again.origins["x"]["source"] == "cache" and again.origins["z"]["source"] == "api"

    run(exercise())
    store.close()


def test_packed_items_under_call_reuse_or_joint_reads_are_reused_only_in_the_same_call(tmp_path):
    fake = Fake()
    store = AnswerStore(tmp_path / "answers.sqlite")
    question = Noul("Passage {slot} is relevant")
    joint = Backend(
        "diffusiongemma", "http://127.0.0.1:8080/v1/systemone", "openjev-latest", joint_reads=True
    )

    async def exercise():
        async with Client(joint, store=store, transport=httpx.MockTransport(fake)) as client:
            items = {"a": "one", "b": "two", "c": "three"}
            await client.ask_packed(items, question, max_items=2)
            assert len(fake.bodies) == 2
            await client.ask_packed(items, question, max_items=2)
            assert len(fake.bodies) == 2  # the same calls again
            await client.ask_packed({"b": "two", "c": "three"}, question, max_items=2)
            assert len(fake.bodies) == 3  # "two" and "three" never shared a call before
            await client.ask_packed({"c": "three"}, question)
            assert len(fake.bodies) == 3  # but "three" alone did
        async with fake.client(store=store) as client:
            await client.ask_packed(items, question, max_items=2, reuse="call")
            await client.ask_packed({"a": "one"}, question, reuse="call")
            assert len(fake.bodies) == 6

    run(exercise())
    store.close()


def test_a_packed_item_too_large_alone_is_named():
    limited = Backend("typesafe", "https://fixture.invalid/v1", "m", key="k", max_read_bytes=100)

    async def exercise():
        async with Client(limited, transport=httpx.MockTransport(Fake())) as client:
            with pytest.raises(JevError, match="item 'big' alone is too large"):
                await client.ask_packed({"ok": "x", "big": "y" * 200}, Noul("{slot} fits"))

    run(exercise())


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


def test_http2_follows_the_installed_extra_and_never_applies_to_a_test_transport(monkeypatch):
    monkeypatch.setattr(client_module, "http2_available", lambda: True)
    assert Client(BACKEND).http2 is True
    assert Client(BACKEND, transport=httpx.MockTransport(lambda request: httpx.Response(200))).http2 is False
    monkeypatch.setattr(client_module, "http2_available", lambda: False)
    assert Client(BACKEND).http2 is False


def test_one_requested_model_answered_by_several_is_flagged():
    from jevkit_runtime import Meter

    meter = Meter(provider="typesafe", requested_model="jev-1.13.0")
    for resolved in ("jev-1.13.0", "jev-1.13.0", None):
        meter.note_answer(
            {"provider": "typesafe", "requested_model": "jev-1.13.0", "resolved_model": resolved}
        )
    meter.note_answer({"provider": "gliner", "requested_model": "g", "resolved_model": "g"})
    assert meter.mixed_models == {}
    meter.note_answer(
        {"provider": "typesafe", "requested_model": "jev-1.13.0", "resolved_model": "jev-1.14.0"}
    )
    assert meter.mixed_models == {"typesafe/jev-1.13.0": ["jev-1.13.0", "jev-1.14.0"]}
