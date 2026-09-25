# JevKit core

Distribution **`jevkit-runtime`**, import **`jevkit_runtime`**. The PyPI name `jevkit-core` belongs
to a different project.

One request pipeline, one answer store, one provider catalog for jgrep, jsort, jlink, jselect,
and jcol. Each tool remains its own package and repository; the core imports none of them, and a
tool's adapter is a few lines naming which providers it offers.

## What a tool gets

```python
from jevkit_runtime import AnswerStore, Client, Noul, catalog, resolve

PROVIDERS = catalog("typesafe", "openrouter", "gateway")
backend = resolve(PROVIDERS, name=None, model=None)          # or JEV_API / JEV_MODEL, else the first configured
question = Noul("The text fits this description: ...")
async with Client(backend, store=AnswerStore()) as client:
    answers = await client.ask(state, {"q": question})
    p = question.value(answers["q"])
```

Questions are typed: `Noul` (a probability), `Choice` (one of its options) and `Score` (a position
on its levels). Each validates its whole answer, options and bounds included, and reads it with
`value` and `confidence`; `text` is the instruction exactly as sent.

`Client.ask` does the whole thing: computes each question's identity, serves what the store already
knows, joins an identical request already in flight, sends only the misses, validates the entire
response before storing any of it, and meters the call before validation so a billed but malformed
answer still counts. It returns `Answers`, a dict by question id whose `origins` say who answered
each one and whether it came from the API, the store, or a shared call. Per-call policy is keyword
arguments: `scope` to keep a tool's answers apart from others that ask the same, and `hedge_after`
to resend a slow call. HTTP/2 is used whenever the `http2` extra is installed.

Spending belongs to a `Budget` shared by the run, `Client(backend, budget=Budget(1.0))`. Before a
request goes out it reserves the request's estimated price, at the dearest rate its backend has
charged so far (1.5 times the list price until the first charge), and when the charge comes back it
settles it. A request that does not fit raises `JevBudgetExceeded`, while the store and a request
already in flight still answer, so requests in the air cannot overshoot the limit together; only a
price rise mid-flight can, and `Budget.rises` counts it. `Budget(0)` allows only what costs nothing,
and `Budget.from_settings(default)` honours `JEV_BUDGET`.

`ask` is `plan` then `send`. `Client.plan` reads the store without sending or writing anything: the
hits, the misses, the request those would make, and whether it is over the provider's limits. An
estimate is a plan; a tool that schedules its own requests plans first and sends later.

`Client.ask_packed(items, question)` asks one question about each of several items, packing items
into as few calls as the provider's limits (and `max_items`) allow, with the question naming its
slot as `{slot}`. With `reuse="item"` each answer is reused on its own wherever the item turns up
again; with `reuse="call"`, and always on a joint-read server, only in the same call.

| Module | Owns |
|---|---|
| `settings.py` | Every environment and filesystem convention, read in one place: `XDG_*`, `JEV_API`, `JEV_URL`, `JEV_MODEL`, `JEV_PRICE_PER_MTOK`, `JEV_BUDGET`, provider keys and URL files |
| `providers.py` | The catalog (`Provider`), a tool's selection of it or its own entries, and `resolve()` to one `Backend`: endpoint, model, key |
| `question.py` | `Noul`, `Choice` and `Score`: request bodies, full answer validation, reading answers |
| `protocol.py` | Request bodies, answer identity (plain, joint and packed), usage parsing, provenance |
| `transport.py` | One HTTP call with a total deadline, retries with backoff and `Retry-After`, structured status errors |
| `budget.py` | `Budget`: reserve a request's estimated price before it goes out, settle its charge when it returns |
| `store.py` | SQLite answers with their provenance in one row, one versioned schema; read-only for previews |
| `client.py` | The pipeline above, plans, packed requests, request sharing, hedging |
| `run.py` | `Run` and its record: tool, backends, models that answered, questions as asked, usage, budget, inputs; `warnings` |
| `cli.py` | The flags every tool shares (`--api --model --budget --timeout -j --no-cache --stats`), help text from the catalog, the stats line, `run_sync` |
| `meter.py` | Calls, cache hits, retries, hedges, tokens, cost, and which models actually answered |
| `errors.py` | `JevError`, `JevFatal`, `JevBudgetExceeded`, `RequestExhausted`, `ProviderError`, `ProviderFatal` |

Every run can say what it did. `Run(tool, version, inputs=fingerprint(texts))` at the start and
`run.record(client)` at the end give one versioned JSON record: the backends, the models that answered
and how often, each distinct question exactly as asked, calls, cache hits, tokens, cost, and the
budget, with the tool's own settings under `fields`. `warnings(record)` names what a person should see,
such as one requested model answered by several.

## Conventions every tool shares

- **Answer identity** is `answer_key(backend, state, question, scope=...)`: provider, endpoint, model,
  scope, state and question. An answer from one provider or model is never served for another, and a
  scope adds to the key without replacing it.
- **Models are pinned.** Hosted Jev is requested as a concrete release (`jev-1.13.0`, or
  `typesafe/jev-1.13` on OpenRouter), bumped deliberately in a release of this package, so a key names
  the model that answers and a moved alias never mixes versions in one cache. `--model jev-latest`
  asks for the alias. `Meter.mixed_models` names any requested model that more than one model answered.
- **The store** lives at `$XDG_CACHE_HOME/jev/answers.sqlite` (default `~/.cache/jev`), is created
  private to the user, and resets itself when it finds an older schema. Version 0.4 cannot read
  caches written by 0.3 tools; the first run after upgrading re-asks.
- **Credentials** come from the provider's variable, then `$XDG_CONFIG_HOME/jev/<provider>.key`.
  Gateways take their URL from `JEV_GATEWAY_URL` or `<provider>.url`. `JEV_URL` overrides any endpoint.
- **Metering** refuses malformed usage rather than under-counting; a response without a reported
  cost is priced from its tokens at the provider's price, zero for local servers, or the list price.
  `JEV_PRICE_PER_MTOK` overrides both.
- **Flags** mean the same in every tool: `--budget none` is no limit and `--budget 0` spends nothing,
  and `JEV_BUDGET` sets a budget for every tool at once.
- **Errors** keep their wording across tools: a fatal status reads `PROVIDER said 401: detail`, a
  bad request reads `HTTP 400: detail`, and exhaustion reads `gave up after 15s (last failure)`.
  Both status errors carry `provider`, `status` and `detail` for tools that word or redact them.

## Local servers

Three catalog entries point at System One servers on your own machine: `diffusiongemma`, an
[OpenJev](https://github.com/razorback16/openjev) server on port 8080, `laya`, a
[laya-mlx](https://github.com/mizorewww/laya-mlx) server on port 8081, and `gliner`, a
[GLiNER2.5-Decide](https://huggingface.co/fastino/GLiNER2.5-Decide) server on port 8082. A tool that
names them in its catalog accepts `--api laya` or `JEV_API=laya`. They are never chosen
in place of a configured hosted provider, need no key, and are metered at zero API fees unless
`JEV_PRICE_PER_MTOK` says otherwise. `JEV_LAYA_URL`, `JEV_DIFFUSIONGEMMA_URL` and `JEV_GLINER_URL`, or the matching
`.url` files, point at a server elsewhere.

DiffusionGemma reads every question in a batch together, so the runtime keys each of its answers
on the whole ordered batch and re-sends a batch whole when any slot is missing.

No package ships the models. [docs/diffusiongemma.md](https://github.com/keltokhy/jevkit-core/blob/main/docs/diffusiongemma.md) and
[docs/laya.md](https://github.com/keltokhy/jevkit-core/blob/main/docs/laya.md) and
[docs/gliner.md](https://github.com/keltokhy/jevkit-core/blob/main/docs/gliner.md) explain how to run the servers, and
`scripts/laya_server.py` and `scripts/gliner_server.py` are the adapters those guides start.

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
