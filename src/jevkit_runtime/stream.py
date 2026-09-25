"""Judge a stream of records as they arrive, a bounded number at a time, and hand results back in order.

    async with ordered_map(read, judge, concurrency=32) as results:
        async for outcome in results:
            if outcome.error: ...
            else: print(outcome.value)
            if enough: break          # leaving the block stops the reader and cancels what is in flight

`read(stop)` runs on a thread of its own, so a blocking source such as `tail -f` never stalls the event
loop; it should return when `stop` is set. `judge(item)` is a coroutine. At most `concurrency` records
are in hand at once, counting both those being judged and those whose results wait their turn, so a
slow record holds back only as much as the window allows. With `ordered=False` results come as they
finish. An exception from `judge` comes back as that record's `error`; one from `read` comes back as a
final outcome with no item.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from concurrent.futures import CancelledError as FutureCancelled
from dataclasses import dataclass
from typing import Generic, TextIO, TypeVar

T = TypeVar("T")
R = TypeVar("R")
_END = object()


@dataclass(frozen=True)
class Outcome(Generic[T, R]):
    item: T | None
    value: R | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _ReadFailed:
    error: BaseException


class ordered_map(Generic[T, R]):  # noqa: N801 - used as a function
    def __init__(
        self,
        read: Callable[[threading.Event], Iterable[T]],
        judge: Callable[[T], Awaitable[R]],
        *,
        concurrency: int,
        ordered: bool = True,
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._read, self._judge, self._ordered = read, judge, ordered
        self._concurrency = concurrency
        self.stop = threading.Event()

    async def __aenter__(self) -> AsyncIterator[Outcome[T, R]]:
        self._loop = asyncio.get_running_loop()
        self._inbox: asyncio.Queue = asyncio.Queue(maxsize=self._concurrency)
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._slots = asyncio.Semaphore(self._concurrency)
        self._tasks: set[asyncio.Task] = set()
        self._finished: dict[int, Outcome] = {}
        self._next = self._delivered = 0
        self._total: int | None = None
        self._put = None  # the reader's pending put, which stopping must cancel
        threading.Thread(target=self._feed, daemon=True).start()
        self._pump_task = asyncio.ensure_future(self._pump())
        self._results_gen = self._results()
        return self._results_gen

    async def __aexit__(self, *exc) -> None:
        self.stop.set()
        await self._results_gen.aclose()
        if (put := self._put) is not None:
            put.cancel()
        self._pump_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(self._pump_task, *self._tasks, return_exceptions=True)

    # ---- the reader's thread ------------------------------------------------------------------------

    def _feed(self) -> None:
        def enqueue(item) -> bool:
            if self.stop.is_set():
                return False
            try:
                put = asyncio.run_coroutine_threadsafe(self._inbox.put(item), self._loop)
            except RuntimeError:  # the loop has closed under a reader that outlived the run
                return False
            self._put = put
            # Stopping may race with creating the put: whichever sees the other cancels it, so a full
            # queue can never strand this thread.
            if self.stop.is_set():
                put.cancel()
            try:
                put.result()
            except (FutureCancelled, RuntimeError):
                return False
            return not self.stop.is_set()

        items = iter(self._read(self.stop))
        try:
            for item in items:
                if not enqueue(item):
                    return
        except Exception as error:  # a reader failure ends the stream as its last outcome
            if not enqueue(_ReadFailed(error)):
                return
        finally:
            if close := getattr(items, "close", None):
                close()  # a generator's own cleanup, such as closing its files, runs now
        enqueue(_END)

    # ---- the event loop -------------------------------------------------------------------------------

    async def _pump(self) -> None:
        seq = 0
        try:
            while (item := await self._inbox.get()) is not _END:
                await self._slots.acquire()
                n, seq = seq, seq + 1
                if isinstance(item, _ReadFailed):
                    self._deliver(n, Outcome(None, error=item.error))
                    break
                task = asyncio.ensure_future(self._run(n, item))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # pragma: no cover - a bug here must still reach the consumer
            self._outbox.put_nowait(_ReadFailed(error))
            return
        self._total = seq
        self._maybe_end()

    async def _run(self, n: int, item: T) -> None:
        try:
            outcome = Outcome(item, value=await self._judge(item))
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            outcome = Outcome(item, error=error)
        self._deliver(n, outcome)

    def _deliver(self, n: int, outcome: Outcome) -> None:
        if not self._ordered:
            self._outbox.put_nowait(outcome)
            self._delivered += 1
        else:
            self._finished[n] = outcome
            while self._next in self._finished:
                self._outbox.put_nowait(self._finished.pop(self._next))
                self._next += 1
                self._delivered += 1
        self._maybe_end()

    def _maybe_end(self) -> None:
        if self._total is not None and self._delivered == self._total:
            self._outbox.put_nowait(_END)
            self._total = None

    async def _results(self) -> AsyncIterator[Outcome[T, R]]:
        while (outcome := await self._outbox.get()) is not _END:
            if isinstance(outcome, _ReadFailed):
                raise outcome.error
            self._slots.release()  # the record leaves the window when its result is taken
            yield outcome


@contextlib.contextmanager
def open_text(name: str) -> Iterator[TextIO]:
    """A file, or standard input for `-`, as text: UTF-8 without a byte-order mark, undecodable bytes
    replaced, newlines left as they are.

    Standard input is read through a reader of its own on the same descriptor, so a reader thread still
    blocked on a live pipe when the run ends never holds `sys.stdin`'s lock against interpreter shutdown.
    """
    if name != "-":
        with open(name, encoding="utf-8-sig", errors="replace", newline="") as stream:
            yield stream
        return
    try:
        descriptor = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        descriptor = None
    if descriptor is None:
        yield sys.stdin  # a stand-in such as a test's StringIO
        return
    with open(descriptor, encoding="utf-8-sig", errors="replace", newline="", closefd=False) as stream:
        yield stream
