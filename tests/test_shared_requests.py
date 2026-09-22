import asyncio

import httpx
import pytest

from jevkit_core import Backend, DecisionClient, JevBudgetExceeded, parse_usage


class Client(DecisionClient):
    def _record(self, state, questions, data, seconds, *, on_cost=None):
        self.meter.record(parse_usage(data.get("usage")), seconds, on_cost=on_cost)
        return data["answers"]


def test_one_owner_pays_and_cache_only_waiter_can_share_without_admission():
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        charges, requests = [], []

        async def respond(request):
            requests.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"answers": {"q": {"noul": 0.8}}, "usage": {"cost": 0.2}})

        client = Client(
            "test",
            Backend("test", "https://test.invalid", "v1", "UNUSED"),
            transport=httpx.MockTransport(respond),
        )

        def disallow_new_request():
            raise JevBudgetExceeded("no new paid request")

        try:
            first, owner = client.share_request(
                ["a", "b"], lambda: client._call("state", {}, on_cost=charges.append)
            )
            await entered.wait()
            second, shared_owner = client.share_request(["b", "a"], disallow_new_request)
            assert owner and not shared_owner
            assert first is second
            release.set()
            assert await asyncio.gather(first, second) == [{"q": {"noul": 0.8}}] * 2
            assert len(requests) == 1 and charges == [0.2]
            assert (client.meter.calls, client.meter.cached, client.meter.cost) == (1, 1, 0.2)
            assert not client._flights
            with pytest.raises(JevBudgetExceeded):
                client.share_request(["a", "b"], disallow_new_request)
            assert not client._flights
        finally:
            await client.close()

    asyncio.run(asyncio.wait_for(exercise(), 5))


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_completed_failed_and_cancelled_requests_release_the_key(outcome):
    async def exercise():
        client = Client("", Backend("test", "https://test.invalid", "v1", "UNUSED"))
        entered, release = asyncio.Event(), asyncio.Event()

        async def work():
            entered.set()
            await release.wait()
            if outcome == "failure":
                raise ValueError("provider failed")
            return "answer"

        async def wait(task):
            return await task

        try:
            task, _ = client.share_request(["key"], work)
            other, owner = client.share_request(["key"], work)
            assert not owner
            await entered.wait()
            waiter = asyncio.create_task(wait(other))
            await asyncio.sleep(0)
            if outcome == "cancel":
                # Preserve direct-await cancellation; callers choose shielding/hedging themselves.
                waiter.cancel()
            else:
                release.set()
            results = await asyncio.gather(task, waiter, return_exceptions=True)
            if outcome == "success":
                assert results == ["answer", "answer"]
            else:
                error = ValueError if outcome == "failure" else asyncio.CancelledError
                assert all(isinstance(result, error) for result in results)
            assert not client._flights
            release.set()
            replacement, owner = client.share_request(["key"], work)
            assert owner and replacement is not task
            await asyncio.gather(replacement, return_exceptions=True)
            assert not client._flights
        finally:
            await client.close()

    asyncio.run(asyncio.wait_for(exercise(), 5))


def test_different_keys_and_clients_do_not_share_tasks():
    async def exercise():
        backend = Backend("test", "https://test.invalid", "v1", "UNUSED")
        clients = [Client("", backend), Client("", backend)]

        async def work():
            return object()

        try:
            a, _ = clients[0].share_request(["a"], work)
            b, _ = clients[0].share_request(["b"], work)
            other, _ = clients[1].share_request(["a"], work)
            results = await asyncio.gather(a, b, other)
            assert len({id(result) for result in results}) == 3
            assert all(client.meter.cached == 0 for client in clients)
        finally:
            for client in clients:
                await client.close()

    asyncio.run(asyncio.wait_for(exercise(), 5))
