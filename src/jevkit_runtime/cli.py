"""The runtime's side of a tool's command line: shared flags, their meaning, help text and the stats line.

    ap = argparse.ArgumentParser(epilog=providers_help(PROVIDERS))
    add_runtime_args(ap, PROVIDERS, default_budget=1.0)
    args = ap.parse_args()
    client = runtime_from_args(args, PROVIDERS)       # raises JevFatal on a missing key or a bad budget
    ...
    if show_stats(args, sys.stderr): print(stats_line(client, elapsed), file=sys.stderr)

Each tool keeps its own flags and output; these are only the ones every tool has, meaning the same
thing everywhere. `--budget none` is no limit and `--budget 0` spends nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
from collections.abc import Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import httpx

from .budget import Budget
from .client import Client
from .errors import JevFatal
from .providers import Provider, resolve
from .settings import Settings
from .store import AnswerStore

T = TypeVar("T")


class UsageError(Exception):
    """A command line that does not parse; `Parser` raises it instead of printing and exiting."""


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose errors come back to the tool, which prints them where its `err` goes and
    returns 2, rather than argparse writing to the real stderr and exiting mid-test."""

    def error(self, message: str):
        raise UsageError(message)


def parse_budget(text: str) -> float:
    """Dollars, or `none` for no limit."""
    if text.strip().lower() in ("none", "unlimited"):
        return math.inf
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"a number of dollars or none, not {text!r}") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("a finite, nonnegative number of dollars, or none")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"a whole number, not {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("at least 1")
    return value


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"a number of seconds, not {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("a finite number of seconds greater than 0")
    return value


def add_runtime_args(
    parser: argparse.ArgumentParser,
    providers: Mapping[str, Provider],
    *,
    default_budget: float,
    concurrency: int = 32,
    timeout: float = 15.0,
) -> None:
    """--api, --model, --budget, --timeout, -j, --no-cache and --stats, as every tool spells them."""
    budget = "none" if math.isinf(default_budget) else f"{default_budget:.2f}"
    group = parser.add_argument_group("decisions")
    group.add_argument(
        "--api", choices=list(providers), help="which API to call (default: whichever has a key)"
    )
    group.add_argument("--model", metavar="ID", help="model to request (default: the pinned Jev release)")
    group.add_argument(
        "--budget",
        type=parse_budget,
        default=None,
        metavar="DOLLARS",
        help=f"stop sending requests before this much is spent (default {budget}, or $JEV_BUDGET; "
        "none for no limit, 0 to answer only from the cache)",
    )
    group.add_argument(
        "--timeout",
        type=_seconds,
        default=timeout,
        metavar="SECONDS",
        help=f"give up on a request after this long, retries included (default {timeout:g})",
    )
    group.add_argument(
        "-j",
        "--concurrency",
        type=_positive_int,
        default=concurrency,
        metavar="N",
        help=f"requests in flight (default {concurrency})",
    )
    group.add_argument("--no-cache", action="store_true", help="do not read or write the answer cache")
    group.add_argument(
        "--stats",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="print calls, tokens and cost to stderr at the end (default: when stderr is a terminal)",
    )
    parser.set_defaults(default_budget=default_budget)


def providers_help(providers: Mapping[str, Provider], settings: Settings | None = None) -> str:
    """Where each provider's key and endpoint come from, for a tool's --help epilog."""
    if settings is None:
        try:
            settings = Settings.from_env()
        except JevFatal:  # a malformed override must not stop --help from saying where keys live
            unset = ("JEV_BUDGET", "JEV_PRICE_PER_MTOK")
            settings = Settings.from_env({k: v for k, v in os.environ.items() if k not in unset})
    hosted = [p for p in providers.values() if p.requires_key]
    local = [p for p in providers.values() if not p.requires_key]
    lines = []
    if hosted:
        reached = [
            f"{p.title or p.name} ({' and '.join(filter(None, (p.url_env, p.key_env)))})" for p in hosted
        ]
        lines.append(f"Jev is reached through {_either(reached)}.")
        lines.append(
            f"Keys can also live in {settings.config_dir}/" + _either([f"{p.name}.key" for p in hosted]) + "."
        )
    if local:
        servers = [f"--api {p.name} ({p.title or p.name}, {p.url_env})" for p in local]
        lines.append(
            f"Local servers: {_either(servers)}; never chosen automatically, no key needed, $0 API fees. "
            "See the jevkit-runtime docs to run them."
        )
    return "\n".join(lines)


def _either(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def budget_from_args(args: argparse.Namespace, settings: Settings | None = None) -> Budget:
    """--budget, else JEV_BUDGET, else the tool's default."""
    if args.budget is not None:
        return Budget(args.budget)
    return Budget.from_settings(args.default_budget, settings)


def runtime_from_args(
    args: argparse.Namespace,
    providers: Mapping[str, Provider],
    *,
    budget: Budget | None = None,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Client:
    """The client the flags describe: backend, budget, cache, timeout and concurrency.

    A budget of 0 needs no key, since it sends nothing that costs; anything else fails here, before any
    input is read, when no provider is configured.
    """
    settings = settings or Settings.from_env()
    budget = budget if budget is not None else budget_from_args(args, settings)
    backend = resolve(providers, args.api, model=args.model, require_key=budget.limit > 0, settings=settings)
    assert backend is not None  # resolve raises unless asked for missing_ok
    return Client(
        backend,
        timeout=args.timeout,
        concurrency=args.concurrency,
        store=None if args.no_cache else AnswerStore(settings=settings),
        budget=budget,
        transport=transport,
    )


def show_stats(args: argparse.Namespace, err) -> bool:
    return bool(args.stats) if args.stats is not None else err.isatty()


def stats_line(client: Client, elapsed: float | None = None) -> str:
    """Calls, cache hits, retries, tokens and cost, with the budget when it has a limit."""
    parts = [client.meter.summary()]
    if not client.budget.unlimited:
        parts.append(f"budget ${client.budget.limit:.2f}")
    if elapsed is not None:
        parts.append(f"{elapsed:.1f}s")
    return "; ".join(parts)


def run_sync(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion from synchronous code, including inside a notebook's running loop,
    where it runs on a thread of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()
