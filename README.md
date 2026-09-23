# JevKit core

Distribution **`jevkit-runtime`**, import **`jevkit_runtime`**. The PyPI name `jevkit-core` belongs
to a different project.

One request pipeline, one answer store, one provider catalog for jgrep, jsort, jlink, jselect,
and jcol. Each tool remains its own package and repository; the core imports none of them, and a
tool's adapter is a few lines naming which providers it offers.

## What a tool gets

```python
from jevkit_runtime import AnswerStore, Client, catalog, resolve

PROVIDERS = catalog("typesafe", "openrouter", "gateway")
backend = resolve(PROVIDERS, name=None, model=None)          # or JEV_API / JEV_MODEL, else the first configured
async with Client(backend, store=AnswerStore()) as client:
    answers = await client.ask(state, {"q": {"type": "noul", "instructions": "..."}})
```

`Client.ask` does the whole thing: computes each question's identity, serves what the store already
knows, joins an identical request already in flight, sends only the misses, validates the entire
response before storing any of it, and meters the call before validation so a billed but malformed
answer still counts. It returns `Answers`, a dict by question id whose `origins` say who answered
each one and whether it came from the API, the store, or a shared call. Per-call policy is keyword
arguments: `allow_paid=False` for cache-only runs, `on_cost` for the caller who should be charged,
`hedge_after` to resend a slow call, and `keys` for callers whose reuse unit is not the request.
HTTP/2 is used whenever the `http2` extra is installed.

| Module | Owns |
|---|---|
| `settings.py` | Every environment and filesystem convention, read in one place: `XDG_*`, `JEV_API`, `JEV_URL`, `JEV_MODEL`, `JEV_PRICE_PER_MTOK`, provider keys and URL files |
| `providers.py` | The catalog (`Provider`), a tool's selection of it or its own entries, and `resolve()` to one `Backend`: endpoint, model, key |
| `protocol.py` | Request bodies, typed answer validation (`noul`, `choice`, `score`), usage parsing, answer identity, provenance |
| `transport.py` | One HTTP call with a total deadline, retries with backoff and `Retry-After`, structured status errors |
| `store.py` | SQLite answers with their provenance in one row, one versioned schema |
| `client.py` | The pipeline above, request sharing, hedging |
| `meter.py` | Calls, cache hits, retries, hedges, tokens, cost, and which models actually answered |
| `errors.py` | `JevError`, `JevFatal`, `JevBudgetExceeded`, `RequestExhausted`, `ProviderError`, `ProviderFatal` |

## Conventions every tool shares

- **Answer identity** is `answer_key(backend, state, question)`: provider, endpoint, model, state and
  question. An answer from one provider or model is never served for another.
- **The store** lives at `$XDG_CACHE_HOME/jev/answers.sqlite` (default `~/.cache/jev`), is created
  private to the user, and resets itself when it finds an older schema. Version 0.2 cannot read
  caches written by 0.1 tools; the first run after upgrading re-asks.
- **Credentials** come from the provider's variable, then `$XDG_CONFIG_HOME/jev/<provider>.key`.
  Gateways take their URL from `JEV_GATEWAY_URL` or `<provider>.url`. `JEV_URL` overrides any endpoint.
- **Metering** refuses malformed usage rather than under-counting; a response without a reported
  cost is priced from its tokens at the provider's price, zero for local servers, or the list price.
  `JEV_PRICE_PER_MTOK` overrides both.
- **Errors** keep their wording across tools: a fatal status reads `PROVIDER said 401: detail`, a
  bad request reads `HTTP 400: detail`, and exhaustion reads `gave up after 15s (last failure)`.
  Both status errors carry `provider`, `status` and `detail` for tools that word or redact them.

## Local servers

Two catalog entries point at System One servers on your own machine: `diffusiongemma`, an
[OpenJev](https://github.com/razorback16/openjev) server on port 8080, and `laya`, a
[laya-mlx](https://github.com/mizorewww/laya-mlx) server on port 8081. Every JevKit tool names
them in its catalog, so `--api laya` or `JEV_API=laya` works everywhere. They are never chosen
in place of a configured hosted provider, need no key, and are metered at zero API fees unless
`JEV_PRICE_PER_MTOK` says otherwise. `JEV_LAYA_URL` and `JEV_DIFFUSIONGEMMA_URL`, or the matching
`.url` files, point at a server elsewhere.

DiffusionGemma reads every question in a batch together, so the runtime keys each of its answers
on the whole ordered batch and re-sends a batch whole when any slot is missing.

No package ships the models. [docs/diffusiongemma.md](https://github.com/keltokhy/jevkit-core/blob/main/docs/diffusiongemma.md) and
[docs/laya.md](https://github.com/keltokhy/jevkit-core/blob/main/docs/laya.md) explain how to run the servers, and `scripts/laya_server.py` is the
adapter the Laya guide starts.

## Development

Keep the six checkouts as siblings. Each consumer depends on `jevkit-runtime>=0.3.0,<0.4.0` and
overrides it for development with `jevkit-runtime = { path = "../jevkit-core", editable = true }`
under `[tool.uv.sources]`.

```bash
python3 scripts/dev.py setup          # uv sync every checkout, fetch jselect's tokenizer data
python3 scripts/dev.py check          # core and consumer suites, credentials stripped, sockets blocked
python3 scripts/dev.py wheel-check    # build and exercise real wheel installs in temporary environments
python3 scripts/dev.py run jgrep -- --help
```

`check` also proves each consumer imports this exact source tree; `wheel-check` proves the installed
wheel, not the checkout. `--tool NAME` limits either to one consumer and `--suffix` selects
alternatively named checkouts. The GitHub workflows run the core suite on Python 3.10 and 3.13 and
the downstream matrix against each consumer's main branch.

## Releasing

Tag the verified core `vX.Y.Z` and dispatch the publish workflow with that tag; the workflow checks
the tag matches the package version and publishes through PyPI Trusted Publishing. Then release each
consumer through its own process, bumping its supported core range, lockfile, and CI core reference
together.
