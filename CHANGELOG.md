# Changelog

`jevkit-runtime` is 0.x: a minor version may break the API. The JevKit tools pin `<0.(n+1)` so a
new minor release never changes an installed tool.

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
