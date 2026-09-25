"""Worker processes that send requests, for runs that outgrow one process.

One Python process tops out near 200 calls a second however many are in flight: HTTP/2 framing and
JSON decoding are single-threaded. The API takes far more (jcol measured 140 rows a second in one
process and 824 across 12). So a client with workers keeps everything with state in its own process,
identity, the store, request sharing, the budget and the meter, and hands only the sending to workers:
each has its own event loop and connections, posts a body, and returns the decoded response.

Priority requests, for a person waiting on a screen, travel on their own queue and never wait for a
worker's slot; the rest are limited to `per_worker` in flight in each worker.
"""

from __future__ import annotations

import asyncio
import itertools
import multiprocessing as mp
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import transport
from .errors import JevError


def _serve(headers: dict, http2: bool, per_worker: int, priority_q, background_q, out_q) -> None:
    async def main() -> None:
        limits = httpx.Limits(max_connections=per_worker + 4, max_keepalive_connections=per_worker + 4)
        async with httpx.AsyncClient(headers=headers, limits=limits, http2=http2) as http:
            loop = asyncio.get_running_loop()
            slots = asyncio.Semaphore(per_worker)
            threads = ThreadPoolExecutor(2)
            tasks: set[asyncio.Task] = set()

            async def handle(job, limited: bool) -> None:
                job_id, url, body, provider, timeout, attempts = job
                retries = 0

                def retried() -> None:
                    nonlocal retries
                    retries += 1

                try:
                    data = await transport.post(
                        http,
                        url,
                        body,
                        provider=provider,
                        timeout=timeout,
                        attempts=attempts,
                        on_retry=retried,
                    )
                    out_q.put(("ok", job_id, data, retries))
                except Exception as error:
                    out_q.put(("error", job_id, _portable(error), retries))
                finally:
                    if limited:
                        slots.release()

            async def pull(queue, limited: bool) -> None:
                while True:
                    if limited:
                        await slots.acquire()  # a busy worker leaves background jobs to an idle one
                    job = await loop.run_in_executor(threads, queue.get)
                    if job is None:
                        return
                    task = asyncio.ensure_future(handle(job, limited))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)

            out_q.put(("ready", None, None, 0))
            await asyncio.gather(pull(priority_q, False), pull(background_q, True))
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def _portable(error: Exception) -> Exception:
    """The runtime's own errors cross back whole; anything else as a JevError naming it."""
    if type(error).__module__.startswith("jevkit_runtime"):
        return error
    return JevError(f"{type(error).__name__}: {error}")


class Workers:
    """A pool of sending processes owned by one client. Started on first use, or by `start`."""

    def __init__(self, count: int, *, per_worker: int, headers: dict, http2: bool):
        if count < 1 or per_worker < 1:
            raise ValueError("workers and per_worker must be at least 1")
        self.count, self.per_worker, self.headers, self.http2 = count, per_worker, headers, http2
        self._ids = itertools.count()
        self._pending: dict[int, asyncio.Future] = {}
        self._started: asyncio.Future | None = None
        self._processes: list = []

    async def start(self) -> None:
        if self._started is None:
            self._started = asyncio.get_running_loop().create_future()
            try:
                await self._spawn()
            except BaseException as error:
                self._started.set_exception(error)
                raise
            self._started.set_result(None)
        await asyncio.shield(self._started)

    async def _spawn(self) -> None:
        context = mp.get_context("spawn")
        self._priority, self._background, self._out = context.Queue(), context.Queue(), context.Queue()
        loop, ready, seen = asyncio.get_running_loop(), asyncio.Event(), 0

        def deliver(kind, job_id, payload, retries) -> None:
            nonlocal seen
            if kind == "ready":
                seen += 1
                if seen == self.count:
                    ready.set()
                return
            future = self._pending.pop(job_id, None)
            if future is not None and not future.done():  # else its caller stopped waiting
                future.set_result((kind, payload, retries))

        def pump() -> None:
            while (item := self._out.get()) is not None:
                loop.call_soon_threadsafe(deliver, *item)

        args = (self.headers, self.http2, self.per_worker, self._priority, self._background, self._out)
        self._processes = [context.Process(target=_serve, args=args, daemon=True) for _ in range(self.count)]
        for process in self._processes:
            process.start()
        self._pump = threading.Thread(target=pump, daemon=True)
        self._pump.start()
        await ready.wait()

    async def post(
        self,
        url: str,
        body: dict,
        *,
        provider: str,
        timeout: float,
        attempts: int,
        on_retry=None,
        priority: bool = False,
    ) -> dict:
        """`transport.post`, in a worker: the decoded response, or the same error it would raise."""
        await self.start()
        job_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[job_id] = future
        (self._priority if priority else self._background).put(
            (job_id, url, body, provider, timeout, attempts)
        )
        try:
            kind, payload, retries = await future
        finally:
            self._pending.pop(job_id, None)
        for _ in range(retries if on_retry is not None else 0):
            on_retry()
        if kind == "error":
            raise payload
        return payload

    async def close(self) -> None:
        if self._started is None or not self._processes:
            return
        for _ in self._processes:
            self._priority.put(None)
            self._background.put(None)
        loop = asyncio.get_running_loop()
        for process in self._processes:
            await loop.run_in_executor(None, process.join, 2)
            if process.is_alive():
                process.terminate()
        self._out.put(None)
        for future in self._pending.values():
            if not future.done():
                future.set_result(("error", JevError("the client closed while a request was in a worker"), 0))
        self._pending.clear()
        self._processes = []
        self._started = None
