import asyncio
import io
import itertools
import random
import sys
import threading

import pytest

from jevkit_runtime.stream import open_text, ordered_map


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 5))


def reading(items):
    def read(stop):
        for item in items:
            if stop.is_set():
                return
            yield item

    return read


def test_results_come_back_in_input_order_and_the_window_is_never_exceeded():
    in_hand = peak = 0
    rng = random.Random(1)

    async def judge(n):
        nonlocal in_hand, peak
        in_hand += 1
        peak = max(peak, in_hand)
        await asyncio.sleep(rng.random() * 0.01)
        return n * n

    async def exercise():
        nonlocal in_hand
        seen = []
        async with ordered_map(reading(range(40)), judge, concurrency=4) as results:
            async for outcome in results:
                in_hand -= 1
                seen.append((outcome.item, outcome.value))
        return seen

    assert run(exercise()) == [(n, n * n) for n in range(40)]
    assert peak <= 4


def test_a_slow_record_holds_back_only_the_window_and_unordered_results_do_not_wait():
    started = []

    async def judge(n):
        started.append(n)
        await asyncio.sleep(0.2 if n == 0 else 0)
        return n

    async def first_three(ordered):
        started.clear()
        async with ordered_map(reading(range(10)), judge, concurrency=3, ordered=ordered) as results:
            return [outcome.item async for outcome in results][:3], list(started)

    order, _ = run(first_three(ordered=True))
    assert order == [0, 1, 2]
    order, _ = run(first_three(ordered=False))
    assert order == [1, 2, 3]


def test_a_failed_record_and_a_failed_reader_come_back_as_outcomes():
    async def judge(n):
        if n == 2:
            raise ValueError("bad record")
        return n

    def read(stop):
        yield from range(4)
        raise OSError("disk went away")

    async def exercise():
        async with ordered_map(read, judge, concurrency=2) as results:
            return [outcome async for outcome in results]

    outcomes = run(exercise())
    assert [o.item for o in outcomes] == [0, 1, 2, 3, None]
    assert isinstance(outcomes[2].error, ValueError) and outcomes[3].value == 3
    assert isinstance(outcomes[4].error, OSError)


def test_leaving_early_stops_the_reader_and_cancels_what_is_in_flight():
    cancelled, stopped = [], threading.Event()

    def endless(stop):
        try:
            for n in itertools.count():
                if stop.is_set():
                    return
                yield n
        finally:
            stopped.set()

    async def judge(n):
        try:
            await asyncio.sleep(0.05 if n < 3 else 5)  # record 3 is running when the consumer leaves
        except asyncio.CancelledError:
            cancelled.append(n)
            raise
        return n

    async def exercise():
        taken = []
        async with ordered_map(endless, judge, concurrency=4) as results:
            async for outcome in results:
                taken.append(outcome.value)
                if len(taken) == 3:
                    break
        return taken

    assert run(exercise()) == [0, 1, 2]
    assert 3 in cancelled and stopped.wait(2)


def test_a_reader_blocked_forever_does_not_keep_the_run_open():
    never = threading.Event()

    def blocked(stop):
        yield 1
        never.wait()

    async def judge(n):
        return n

    async def exercise():
        async with ordered_map(blocked, judge, concurrency=2) as results:
            async for outcome in results:
                return outcome.value

    assert run(exercise()) == 1
    never.set()


def test_open_text_drops_a_byte_order_mark_and_reads_a_stand_in_stdin(tmp_path, monkeypatch):
    path = tmp_path / "in.txt"
    path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\xff\n")
    with open_text(str(path)) as stream:
        assert stream.read() == "one\r\ntwo�\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped\n"))
    with open_text("-") as stream:
        assert stream.read() == "piped\n"
    with pytest.raises(ValueError):
        ordered_map(reading([]), None, concurrency=0)
