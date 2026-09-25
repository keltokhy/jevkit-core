"""Worker processes against a real server on localhost; a mock transport cannot cross a process."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jevkit_runtime import AnswerStore, Backend, Budget, Client, JevError, JevFatal, Noul, ProviderFatal


class Handler(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append((self.path, self.headers.get("Authorization"), body["state"]))
        if self.path == "/denied":
            payload, status = {"error": {"message": "bad key"}}, 401
        else:
            answers = {qid: {"noul": 0.25} for qid in body["questions"]}
            payload, status = {"answers": answers, "model": "served-v1", "usage": {"cost": 0.001}}, 200
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_workers_send_while_the_client_keeps_the_store_budget_and_meter(server, tmp_path):
    Handler.seen.clear()
    backend = Backend("gateway", f"{server}/v1/systemone", "jev-1.13.0", key="secret", price_per_mtok=0.042)
    store, budget = AnswerStore(tmp_path / "answers.sqlite"), Budget(1.0)

    async def exercise():
        async with Client(backend, workers=2, per_worker=4, store=store, budget=budget) as client:
            await client.start()
            answers = await asyncio.gather(*(client.ask(f"text {i}", {"q": Noul("x")}) for i in range(20)))
            urgent = await client.ask("on screen", {"q": Noul("x")}, priority=True)
            again = await client.ask("text 3", {"q": Noul("x")})
            processes = list(client._workers._processes)
            return answers, urgent, again, client.meter, processes

    answers, urgent, again, meter, processes = asyncio.run(asyncio.wait_for(exercise(), 60))
    assert all(a["q"] == {"noul": 0.25} and a.origins["q"]["resolved_model"] == "served-v1" for a in answers)
    assert urgent.origins["q"]["source"] == "api" and again.origins["q"]["source"] == "cache"
    assert meter.calls == 21 and meter.cost == pytest.approx(0.021) and budget.spent == pytest.approx(0.021)
    assert budget.held == 0 and len(Handler.seen) == 21
    assert {auth for _, auth, _ in Handler.seen} == {"Bearer secret"}
    assert not any(p.is_alive() for p in processes)
    store.close()


def test_a_worker_s_error_arrives_whole(server):
    backend = Backend("gateway", f"{server}/denied", "jev-1.13.0", key="k")

    async def exercise():
        async with Client(backend, workers=1) as client:
            with pytest.raises(ProviderFatal) as caught:
                await client.ask("s", {"q": Noul("x")})
            return caught.value

    error = asyncio.run(asyncio.wait_for(exercise(), 60))
    assert isinstance(error, JevFatal) and (error.provider, error.status, error.detail) == (
        "gateway",
        401,
        "bad key",
    )


def test_workers_cannot_use_a_test_transport():
    import httpx

    with pytest.raises(ValueError, match="cannot be used with workers"):
        Client(
            Backend("t", "https://x.invalid", "m"), workers=1, transport=httpx.MockTransport(lambda r: None)
        )


class Slow(BaseHTTPRequestHandler):
    def do_POST(self):
        import time

        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(10)

    def log_message(self, *args):
        pass


def test_a_worker_that_dies_fails_its_callers_and_the_next_request_starts_a_fresh_pool(server):
    import os
    import signal

    slow = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=slow.serve_forever, daemon=True).start()
    hanging = Backend("gateway", f"http://127.0.0.1:{slow.server_address[1]}/v1", "m", key="k")

    async def exercise():
        async with Client(hanging, workers=1, timeout=30) as client:
            await client.start()
            asking = asyncio.create_task(client.ask("s", {"q": Noul("x")}))
            await asyncio.sleep(0.5)
            os.kill(client._workers._processes[0].pid, signal.SIGKILL)
            with pytest.raises(JevError, match="worker process stopped"):
                await asyncio.wait_for(asking, 10)
            assert client.budget.held == 0
            client.backend = Backend("gateway", f"{server}/v1/systemone", "m", key="k")
            again = await asyncio.wait_for(client.ask("t", {"q": Noul("x")}), 30)
            assert again["q"] == {"noul": 0.25}

    asyncio.run(asyncio.wait_for(exercise(), 90))
    slow.shutdown()
