import asyncio
import json
import math

import httpx
import pytest

from jevkit_runtime import Backend, Budget, Client, JevBudgetExceeded, JevError, JevFatal, Noul, Settings
from jevkit_runtime.protocol import REQUEST_OVERHEAD_TOKENS, estimate_tokens

HOSTED = Backend("typesafe", "https://fixture.invalid/v1", "jev-1.13.0", key="k", price_per_mtok=0.042)
LOCAL = Backend("laya", "http://127.0.0.1:8081/v1/systemone", "laya-421m", price_per_mtok=0.0)
Q = {"q": Noul("matches")}


def answering(cost=0.00002, delay=0.0, status=200):
    bodies = []

    async def respond(request):
        bodies.append(json.loads(request.content))
        if delay:
            await asyncio.sleep(delay)
        if status != 200:
            return httpx.Response(status, json={"error": "busy"})
        return httpx.Response(200, json={"answers": {"q": {"noul": 0.5}}, "usage": {"cost": cost}})

    return httpx.MockTransport(respond), bodies


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 5))


def test_a_budget_is_dollars_or_no_limit():
    assert Budget().unlimited and Budget().remaining == math.inf
    assert Budget(0.5).remaining == 0.5 and "of $0.50" in Budget(0.5).summary()
    for bad in (-1, float("nan"), True, "1"):
        with pytest.raises(ValueError):
            Budget(bad)


def test_estimates_are_a_token_per_four_bytes_plus_overhead():
    body = {"model": "m", "state": "x" * 400, "questions": {"q": Noul("rule").body()}}
    assert estimate_tokens(body) == math.ceil(len(json.dumps(body).encode()) / 4) + REQUEST_OVERHEAD_TOKENS
    plan = Client(HOSTED).plan("x" * 400, Q)
    assert plan.tokens == estimate_tokens(plan.request) and plan.cost == pytest.approx(plan.tokens * 0.042e-6)
    assert Client(HOSTED).plan("x", {}).tokens == 0


def test_a_zero_budget_sends_nothing_that_costs_but_a_free_server_still_answers():
    transport, bodies = answering()

    async def exercise():
        async with Client(HOSTED, budget=Budget(0), transport=transport) as hosted:
            with pytest.raises(JevBudgetExceeded):
                await hosted.ask("s", Q)
        async with Client(LOCAL, budget=Budget(0), transport=transport) as local:
            assert (await local.ask("s", Q))["q"] == {"noul": 0.5}

    run(exercise())
    assert len(bodies) == 1


def test_requests_in_the_air_hold_their_price_so_concurrency_cannot_overshoot():
    transport, bodies = answering(delay=0.05)
    per_request = Client(HOSTED).plan("text 00", Q).tokens * 0.042e-6 * 1.5
    budget = Budget(per_request * 3.5)

    async def exercise():
        async with Client(HOSTED, budget=budget, transport=transport) as client:
            results = await asyncio.gather(
                *(client.ask(f"text {i:02d}", Q) for i in range(10)), return_exceptions=True
            )
            refused = [r for r in results if isinstance(r, JevBudgetExceeded)]
            assert len(bodies) == 3 and len(refused) == 7 and budget.refused == 7
            assert budget.held == 0

    run(exercise())


def test_the_dearest_charge_sets_the_rate_and_a_jump_counts_as_a_rise():
    async def exercise():
        budget = Budget(1.0)
        hold = await budget.reserve(HOSTED, 1000)
        assert hold.amount == pytest.approx(1000 * 0.042e-6 * 1.5) and budget.held == hold.amount
        hold.settle(1000 * 0.03e-6)
        assert budget.rises == 0 and budget.rates and budget.held == 0
        assert budget.price(HOSTED, 1000) == pytest.approx(1000 * 0.03e-6)
        (await budget.reserve(HOSTED, 1000)).settle(1000 * 0.1e-6)
        assert budget.rises == 1 and budget.price(HOSTED, 1000) == pytest.approx(1000 * 0.1e-6)
        assert budget.price(LOCAL, 1000) == 0
        released = await budget.reserve(HOSTED, 10)
        released.release()
        released.settle(5.0)  # a closed hold stays closed
        assert budget.held == 0 and budget.spent == pytest.approx(1000 * 0.13e-6)

    run(exercise())


def test_a_failed_request_frees_its_hold():
    transport, _ = answering(status=500)

    async def exercise():
        budget = Budget(1.0)
        async with Client(HOSTED, attempts=1, budget=budget, transport=transport) as client:
            with pytest.raises(JevError):
                await client.ask("s", Q)
        assert (budget.held, budget.spent) == (0, 0)

    run(exercise())


def test_a_slow_call_is_not_hedged_when_the_budget_has_no_room_for_a_copy():
    transport, bodies = answering(delay=0.1)
    one = Client(HOSTED).plan("s", Q).tokens * 0.042e-6 * 1.5

    async def exercise():
        async with Client(HOSTED, budget=Budget(one * 1.5), transport=transport) as client:
            await client.ask("s", Q, hedge_after=0.01)
            assert len(bodies) == 1 and client.meter.hedges == 0

    run(exercise())


def test_jev_budget_overrides_a_tool_default():
    assert Budget.from_settings(2.0, Settings.from_env({})).limit == 2.0
    assert Budget.from_settings(2.0, Settings.from_env({"JEV_BUDGET": "0.25"})).limit == 0.25
    assert Budget.from_settings(2.0, Settings.from_env({"JEV_BUDGET": "none"})).unlimited
    assert Budget.from_settings(2.0, Settings.from_env({"JEV_BUDGET": "0"})).limit == 0
    for bad in ("lots", "-1", "inf", "nan"):
        with pytest.raises(JevFatal, match="JEV_BUDGET"):
            Settings.from_env({"JEV_BUDGET": bad})


def test_an_allotment_sets_money_aside_for_a_unit_of_work_and_returns_what_it_did_not_use():
    async def exercise():
        parent = Budget(1.0)
        share = await parent.allot(0.4)
        assert parent.held == pytest.approx(0.4) and parent.remaining == pytest.approx(0.6)
        with pytest.raises(ValueError, match="cannot be divided"):
            await share.allot(0.1)
        hold = await share.reserve(HOSTED, 1000)
        hold.settle(0.1)
        assert (share.spent, parent.spent) == (pytest.approx(0.1), pytest.approx(0.1))
        assert parent.held == pytest.approx(0.3)  # the share's spent part is spending, not held
        assert share.rates is parent.rates and parent.rates
        share.close()
        assert parent.held == 0 and parent.remaining == pytest.approx(0.9)
        with pytest.raises(ValueError, match="closed"):
            await share.reserve(HOSTED, 10)
        with pytest.raises(JevBudgetExceeded):
            await parent.allot(0.95)  # nothing is out that could come back
        assert parent.refused == 1

    run(exercise())


def test_a_share_closed_with_a_request_in_the_air_returns_its_rest_when_that_request_settles():
    async def exercise():
        parent = Budget(1.0)
        with await parent.allot(0.5) as share:
            hold = await share.reserve(HOSTED, 1000)
        assert parent.held == pytest.approx(0.5)  # still set aside: a request is in the air
        hold.settle(0.6)  # a price rise: the request cost more than the share
        assert parent.spent == pytest.approx(0.6) and parent.held == 0 and share.rises == parent.rises == 1
        assert parent.remaining == pytest.approx(0.4)

    run(exercise())


def test_a_backend_s_first_request_goes_alone_until_its_price_is_known():
    async def exercise():
        budget = Budget(1.0)
        probe = await budget.reserve(HOSTED, 1000)
        waiting = asyncio.ensure_future(budget.reserve(HOSTED, 1000))
        await asyncio.sleep(0)
        assert not waiting.done() and budget.try_reserve(HOSTED, 1000) is None
        probe.settle(0.01)  # far dearer than estimated: learned from one request
        hold = await waiting
        assert hold.amount == pytest.approx(0.01) and budget.rates
        hold.release()
        for unguarded in (Budget(), Budget(1.0)):
            backend = HOSTED if unguarded.unlimited else LOCAL  # no limit, or no fees: nothing to learn
            first = await unguarded.reserve(backend, 1000)
            second = await asyncio.wait_for(unguarded.reserve(backend, 1000), 1)
            first.release()
            second.release()

    run(exercise())


def test_a_reservation_that_does_not_fit_waits_for_money_to_come_back_in_turn():
    async def exercise():
        rate = 0.03e-6
        budget = Budget(1000 * rate + 3 * 1000 * rate)
        (await budget.reserve(HOSTED, 1000)).settle(1000 * rate)  # the price is learned first
        first = [await budget.reserve(HOSTED, 1000) for _ in range(3)]
        later = [asyncio.ensure_future(budget.reserve(HOSTED, 1000)) for _ in range(2)]
        await asyncio.sleep(0)
        assert not any(t.done() for t in later) and budget.refused == 0
        first[0].settle(0.0)  # charged nothing: room for exactly the first in line
        await asyncio.sleep(0)
        assert later[0].done() and not later[1].done()
        for hold in first[1:]:
            hold.settle(0.0)
        await asyncio.sleep(0)
        assert later[1].done()
        (await later[0]).settle(0.0)
        (await later[1]).settle(0.0)
        assert budget.refused == 0 and budget.held == 0
        big = asyncio.ensure_future(budget.reserve(HOSTED, 10**6))
        with pytest.raises(JevBudgetExceeded):
            await big  # nothing held that could come back
        assert budget.refused == 1 and budget.try_reserve(HOSTED, 10**6) is None and budget.refused == 1

    run(exercise())


def test_sending_with_a_share_draws_on_it_and_an_unlimited_budget_allots_without_limit():
    transport, bodies = answering(cost=0.001)

    async def exercise():
        parent = Budget(1.0)
        async with Client(HOSTED, budget=parent, transport=transport) as client:
            with await parent.allot(0.01) as share:
                await client.ask("s", Q, budget=share)
            assert share.spent == 0.001 and parent.spent == 0.001 and parent.held == 0
            with await parent.allot(0) as empty:
                await client.ask("t", Q, budget=empty)  # an empty share draws on the budget's free money
            assert parent.spent == pytest.approx(0.002) and parent.held == 0
        assert (await Budget().allot(5)).unlimited

    run(exercise())
    assert len(bodies) == 2


def test_a_share_guarantees_room_and_draws_on_what_the_budget_has_free_when_prices_rise():
    async def exercise():
        parent = Budget(1.0)
        share = await parent.allot(0.001)
        inside = await share.reserve(HOSTED, 1000)
        assert inside.budget is share
        overflow = await share.reserve(HOSTED, 100_000)  # past the share: the budget's free money
        assert overflow.budget is parent and parent.held == pytest.approx(0.001 + overflow.amount)
        overflow.settle(0.5)
        inside.settle(0.0)
        share.close()
        assert parent.spent == pytest.approx(0.5) and parent.held == 0
        greedy = await parent.allot(0.4)
        with pytest.raises(JevBudgetExceeded):
            await greedy.reserve(HOSTED, 10**8)  # more than the share and all the budget has free
        assert greedy.try_reserve(HOSTED, 10**8) is None
        greedy.close()

    run(exercise())
