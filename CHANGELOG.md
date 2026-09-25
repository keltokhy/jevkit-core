# Changelog

`jevkit-runtime` is 0.x: a minor version may break the API. The JevKit tools pin `<0.(n+1)` so a
new minor release never changes an installed tool.

## Unreleased (0.4)

Part of the run-layer plan ([#14](https://github.com/keltokhy/jevkit-core/issues/14)). Breaking.

- Typed questions: `Noul`, `Choice` and `Score` replace question dicts everywhere. Each validates its
  whole answer, options and scale bounds included, and reads it with `value` and `confidence`.
  `from_body` rebuilds a saved one. ([#6](https://github.com/keltokhy/jevkit-core/issues/6))
- `Client.plan` reads the store without sending or writing: hits, misses, the request they would make,
  and whether it is over the provider's limits. `ask` is `plan` then `send`. `AnswerStore(read_only=True)`
  opens a store for previews without creating, migrating or writing it. Hosted Jev providers carry
  their request limits. ([#7](https://github.com/keltokhy/jevkit-core/issues/7))
- `Client.ask_packed` asks one question about each of several items in as few calls as fit, reusing
  answers per item, or per call with `reuse="call"` and always on joint-read servers. `keys=` is gone;
  `scope=` adds to an answer's key and never replaces it.
  ([#5](https://github.com/keltokhy/jevkit-core/issues/5))
- Hosted Jev is pinned to `jev-1.13.0` (`typesafe/jev-1.13` on OpenRouter) instead of the
  `jev-latest` alias, and `Meter.mixed_models` names a requested model that several models answered.
  ([#9](https://github.com/keltokhy/jevkit-core/issues/9))
- A stored answer that no longer validates is asked again and overwritten, instead of failing the call.
- Answer keys are v3 and the store schema is 3: the first run after upgrading re-asks.

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
