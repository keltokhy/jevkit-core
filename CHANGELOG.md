# Changelog

`jevkit-runtime` is 0.x: a minor version may break the API. The JevKit tools pin `<0.(n+1)` so a
new minor release never changes an installed tool.

## Unreleased (0.4)

Part of the run-layer plan ([#14](https://github.com/keltokhy/jevkit-core/issues/14)). Breaking.

- Typed questions: `Noul`, `Choice` and `Score` replace question dicts everywhere. Each validates its
  whole answer, options and scale bounds included, and reads it with `value` and `confidence`.
  `from_body` rebuilds a saved one. ([#6](https://github.com/keltokhy/jevkit-core/issues/6))
- `Client.plan` reads the store without sending or writing: hits, misses, the request they would make,
  and whether it is over the provider's documented token limits (hosted Jev: 64k per request, 32k for
  the state and its longest question). `ask` is `plan` then `send`, and `send` refuses a plan made by
  another client. `AnswerStore(read_only=True)` opens a store for previews without migrating it or
  writing an answer. ([#7](https://github.com/keltokhy/jevkit-core/issues/7))
- Packed requests ([#5](https://github.com/keltokhy/jevkit-core/issues/5)): `ask_packed(items,
  questions, context=...)` asks each question about each item in as few calls as fit, with each
  question naming its slot as `{slot}`, and returns `PackedAnswers` with a failed item's error in
  `errors` rather than failing the rest. Answers are reused only in the same call unless
  `reuse="item"`, which reuses them in any call of the same width; joint-read servers always reuse
  by call. `plan_packed` and `send_packed` split it for estimates and schedulers. `keys=` is gone;
  `scope=` adds to an answer's key and never replaces it.
- Hosted Jev is pinned to `jev-1.13.0` (`typesafe/jev-1.13` on OpenRouter) instead of the
  `jev-latest` alias, and `Meter.mixed_models` names a requested model that several models answered.
  ([#9](https://github.com/keltokhy/jevkit-core/issues/9))
- A stored answer that no longer validates is asked again and overwritten, instead of failing the call.
- `Run` records a run ([#10](https://github.com/keltokhy/jevkit-core/issues/10)): backends, the models
  that answered, every distinct question as asked (the meter now notes them), usage, budget, an input
  `fingerprint` and the tool's own `fields`, as versioned JSON; `run.warnings` names accidental model
  mixing and refused requests.
- `jevkit_runtime.stream` ([#12](https://github.com/keltokhy/jevkit-core/issues/12)): `ordered_map`
  judges records as a reader thread yields them (so `tail -f` works), keeps at most `concurrency`
  records in hand counting those waiting their turn, returns results in input order or as they finish,
  and on leaving early stops the reader, closes it and cancels what is in flight. `open_text` reads a
  file or standard input through a descriptor of its own.
- `Client(workers=N)` ([#13](https://github.com/keltokhy/jevkit-core/issues/13)) sends requests from N
  processes of its own, past one process's ceiling of about 200 calls a second, while identity, the
  store, request sharing, the budget and the meter stay in the client. `ask(..., priority=True)` never
  waits for a worker's slot; `client.start()` starts the processes ahead of the first request. The
  runtime's structured errors now survive pickling.
- `jevkit_runtime.cli` ([#11](https://github.com/keltokhy/jevkit-core/issues/11)): the flags every tool
  shares, with `--budget none` for no limit and `--budget 0` for cache only; provider help generated
  from the catalog (providers gain a `title`); `runtime_from_args`, `stats_line`, `show_stats`, and
  `run_sync`, which runs a coroutine on its own thread inside a notebook's loop.
- `Budget` owns spending ([#8](https://github.com/keltokhy/jevkit-core/issues/8)): each request reserves
  its estimated price (`estimate_tokens`, at the dearest rate charged so far, 1.5 times list price
  before the first charge) and settles its real charge, so concurrent requests cannot overshoot the
  limit together. `Client(budget=...)` replaces `allow_paid=` and `on_cost=`; `Budget(0)` allows only
  what costs nothing. `JEV_BUDGET` (dollars, or `none`) overrides a tool's default. `Plan` carries the
  estimated `tokens` and `cost` of its request.
  Reservations wait in line, first come first served, for money held elsewhere to come back, and are
  refused only when nothing is held and they still do not fit; a waiting request is priced when it is
  granted, at the rate known by then. `await budget.allot(amount)` sets money aside for a unit of work
  that must be done whole (every comparison of one text), and `ask(..., budget=share)` draws on it.
  Hedges take room only if it is free and never count as a refusal.
  Under a limit, a priced backend's first request goes alone until its price is known, so a price far
  from the estimate is learned from one request rather than paid on many. An allotment guarantees its
  unit room rather than capping it: past the share, its requests draw on what the budget has free.
- Requests in this process are bounded by the client's `concurrency` (priority requests excepted), so
  packed fan-out cannot swamp a connection pool; a priority caller joining a background request sends a
  priority copy instead of queueing behind it.
- A cancelled in-process request counts its estimate against the budget, since it may have been billed;
  a worker's request whose caller stopped waiting is metered, charged and stored when it comes back.
- Workers: a process that dies fails the requests waiting on the pool, and the next request starts a
  fresh pool; startup that cannot complete raises instead of hanging; `close` drops queued work and
  joins the workers together.
- Answer keys are v3, and order-sensitive: a state's fields and a question's options are keyed in the
  order they are sent. The store is `answers.v3.sqlite`, beside 0.3's `answers.sqlite`, so tools on
  either runtime share a cache directory; the first run after upgrading re-asks.
- A caller that stops waiting no longer cancels a request other callers share; `Client.close` stops
  requests nobody waits for.
- Questions copy their options and criteria, reject unordered options, fill `{slot}` in criteria too,
  and `from_body` rejects fields it does not know.

## 0.3.2

- Add the local decision server `gliner` (GLiNER2.5-Decide, port 8082) to the catalog: keyless,
  chosen only by name, and priced at zero API fees.
- `scripts/gliner_server.py` serves the model through `gliner2` as a System One endpoint, answering
  `noul`, `choice` and `score` questions; setup in `docs/gliner.md`.
- `jevkit_runtime.__version__` reads the installed package version; it had stayed at 0.3.1.

## 0.3.1

- Documentation and package metadata only; the code is the same as 0.3.0.
- The DiffusionGemma and Laya guides report what to expect from the four tools' benchmarks.
- Add this changelog and a security policy, and list the repository and changelog on PyPI.
- README links point to GitHub, so they also work on PyPI.

## 0.3.0

- Add the local decision servers `diffusiongemma` (OpenJev) and `laya` (laya-mlx) to the catalog.
  They need no key, are chosen only by name and never in place of a configured hosted provider, and
  are priced at zero API fees.
- A provider may carry its own price; `JEV_PRICE_PER_MTOK` overrides it and `Settings.list_price`
  is the fallback.
- Providers with joint reads key every answer on the whole ordered batch and are re-sent whole when
  any slot is missing.
- Setup guides for both servers in `docs/`, and the Laya server adapter in `scripts/`.

## 0.2.0

Breaking redesign: one request pipeline, one answer store, one provider catalog.

- `Client.ask` owns the whole pipeline: request sharing, hedging, cache-only runs, charge callbacks
  and provenance are per-call keyword arguments. It returns `Answers`, a dict with an `origins`
  attribute.
- The import is now `jevkit_runtime`, matching the distribution.
- The answer store keeps answer and provenance in one row under a versioned schema; caches from
  0.1 are reset and re-asked.
- HTTP/2 is used whenever the `http2` extra is installed.
- Removed: the legacy key recipes, the two-table cache, the `DecisionClient` seam, and the unused
  local providers.

## 0.1.0

- First release: shared transport, settings, answer cache and metering for jgrep, jsort, jlink,
  jselect and jcol, published as `jevkit-runtime` (the PyPI name `jevkit-core` belongs to another
  project).
