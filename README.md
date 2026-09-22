# JevKit core

Distribution: **`jevkit-runtime`**. Python import: **`jevkit_core`**.
The PyPI name `jevkit-core` belongs to a different project.

Shared provider definitions, backend configuration, HTTP transport, retries, deadlines,
SQLite answer storage, in-flight requests, usage accounting, and answer provenance
for jgrep, jsort, jlink, jselect, and jcol.

Each product remains a separate repository and package. This core imports none of them.
Product adapters retain prompts, cache identities, reuse policies, budget policies, and public APIs.

Version 0.1.0 is available on [PyPI](https://pypi.org/project/jevkit-runtime/0.1.0/)
and as [GitHub release artifacts](https://github.com/keltokhy/jevkit-core/releases/tag/v0.1.0).
Ordinary source edits in an editable core installation apply on the next run;
already-running Python processes need to restart.

## Development

Keep the six checkouts as siblings. Each consumer declares a normal versioned
dependency on `jevkit-runtime` and a uv development override:

```toml
[tool.uv.sources]
jevkit-runtime = { path = "../jevkit-core", editable = true }
```

Clone `keltokhy/jevkit-core` beside `keltokhy/jgrep`, `keltokhy/jsort`,
`keltokhy/jlink`, `keltokhy/jselect`, and `keltokhy/jcol`. All six repositories
remain independently versioned. For isolated development checkouts named
`jgrep-jevkit` and so on, add `--suffix=-jevkit` before the action.

From this directory:

```bash
python3 scripts/dev.py setup
python3 scripts/dev.py check
python3 scripts/dev.py wheel-check
python3 scripts/dev.py run jgrep -- --help
```

`run` uses the selected worktree's virtual environment and preserves the caller's
working directory. To use it with data, replace `--help` with the normal tool arguments.
`--repos-root` selects the checkouts' parent;
`--tool jgrep` limits setup and checks to one consumer.

`setup` installs dependencies and prepares the public tiktoken encoding files needed
by jselect. It performs no model inference. `check` runs each suite in its own
environment, strips model credentials, isolates configuration/cache directories,
and blocks outbound sockets while permitting local fake HTTP servers. It also proves
that every consumer calls the shared transport and imports this exact source tree,
and compares its fixture behavior to the commits in `consumer-baselines.json`.
Those commits must be present locally; use a full clone or fetch that history.

`wheel-check` builds the core and consumer wheels, installs each consumer alongside
the core wheel in a separate temporary environment, and exercises model fixtures and
CLI entry points outside the source trees. It also checks packaged browser/review assets
and jcol's installed process workflow. Dependency installation may use the package index;
inference checks stay offline.

## Shared boundary

| Module | Responsibility |
|---|---|
| `backends.py` | Provider catalog, credential/configuration lookup, capabilities, selection |
| `transport.py` | HTTP requests, total deadlines, retries, JSON/error handling |
| `client.py` | Client lifecycle, shared in-flight requests, transport delegation, common answer validation |
| `cache.py` | SQLite answer storage and atomic provenance writes, key serialization |
| `usage.py` | Usage validation, reported/estimated costs, meter and report accumulation |
| `provenance.py` | Versioned answer origins with literal or unknown responder identity |
| `errors.py` | Shared failure types and structured request exhaustion |

`DecisionClient` is an adapter base, not a complete standalone scoring SDK. Product
adapters supply their own `ask` and `_record` contracts. jselect calls the shared
transport directly while retaining its relevance batching, score cache, and statistics.
Tool-specific meter fields remain in small subclasses.

`backend_catalog` selects provider definitions in each tool's priority order and
accepts model overrides. The four decision clients retain moving model aliases;
jselect keeps its pinned defaults. The catalog also defines two opt-in local providers; tools choose whether to expose them. Adding a catalog entry does not automatically enable it in every tool.

`DecisionClient.share_request` returns a task and an ownership flag. It invokes a
lazy request factory only for the owner, allowing jlink's cache-only callers to
join existing requests and jsort to charge only the initiating caller. It removes
finished, failed, and cancelled tasks from the registry. Awaiting, cancellation,
cache identity, and jcol's hedging policy remain with the adapters; sharing does
not introduce cancellation shielding or change how a hedge is charged.

`Meter.record` and `record_usage` accumulate the same call/token/cost fields for
the four clients and jselect's dictionary reports. Meter callbacks run after
totals change and before latency/model updates. Cost-source labels, jsort's
maximum call cost, jlink's provenance summaries, and model fallback remain local.
`answer_provenance` constructs the shared version-1 metadata for jsort and jlink;
missing responder identity stays unknown rather than inheriting a requested alias.

No product imports another product, and the core imports none of them. NumPy,
pandas, SciPy, Polars, tokenizers, and browser dependencies stay in their owning
tools. HTTP/2 is an optional core extra used by jcol.

## Compatibility

- Existing prompts, CLI flags, supported backend choices, cache-key bytes, and
  saved project/scale/index formats are retained by the adapters.
- Existing `~/.config/jev` and cache locations continue to work. No user cache or
  credential files were opened or migrated during development.
- Legacy answer keys remain explicitly tool-owned. The shared `answer_key` function
  offers an opt-in, versioned provider/endpoint identity for future consumers; it
  never falls back to an ambiguous legacy entry. In particular, jlink's existing
  cross-backend cache behavior has not silently changed.
- jcol checkpoints and jselect's score cache remain separate from shared answer storage.
- Budget meanings remain local: zero is unlimited for jgrep/jsort, cache-only for
  jlink, and rejected by jselect's semantic scorer. jcol retains its scheduler policy.
- The separate experimental jgrep branch retains joint-read caching and keyless
  local backends. The main-branch migration exposes its original three providers;
  unrelated experiments and benchmark graphics are not part of that migration.
- All clients now use a true total HTTP deadline. jlink and jcol previously passed
  the remaining time only to httpx's individual network waits.
- All five use validated usage parsing. Malformed, negative, nonfinite, or boolean
  usage values fail rather than corrupting accounting; jselect retains its
  fractional-token and missing-token estimate policy.

These last two points are deliberate hardening accompanying the extraction.
The adapter tests remain the source of truth for each tool's external behavior.

## Cross-repository validation

```bash
python3 scripts/dev.py check
```

For a before/after request, result, and metering comparison, use a consumer's
environment with `scripts/probe_consumer.py TOOL --baseline-repo PATH --baseline-ref COMMIT`.
The probe covers cold/warm caches, repeated requests, in-flight sharing, typed
jcol answers, and provider definitions/defaults. It verifies shared accounting
for all five, request sharing for the four decision clients, and provenance
construction for jsort/jlink. It is a focused compatibility fixture, not an
exhaustive equivalence proof.

The core CI tests Python 3.10 and 3.13. The downstream workflow tests the exact
core revision against all five consumer main branches in separate jobs, including
wheel installs. Pull-request and main-push runs are enabled with the repository
variable `JEVKIT_CONSUMERS_READY=true`. Manual dispatch can select
a common consumer branch/tag before then. All five consumer repositories are public.

## Release sequence

The five packages retain independent releases. Their wheels contain a dependency
on `jevkit-runtime>=0.1.0,<0.2.0`, never a local source path; jcol requests the `http2` extra.

Before publishing any migrated consumer to PyPI:

1. Merge and tag the verified core as `v0.1.0`. Consumer CI checks out this tag
   beside its own source, so tests do not depend on PyPI publication timing.
2. Configure a PyPI Trusted Publisher for `jevkit-runtime`, repository
   `keltokhy/jevkit-core`, workflow `publish.yml`, environment `pypi`. Dispatch the
   publish workflow with the verified release tag. Publishing is explicit; creating
   a Git tag alone does not upload a package.
3. Release each consumer through its existing versioning/publishing process. Update
   its supported core range, lockfile, and CI core reference together on upgrades.

Source development uses the sibling checkout; standalone installs resolve the
runtime from PyPI. The cross-repository wheel checks install a freshly built core
wheel explicitly alongside each consumer. Existing published tool versions remain
independent of this migration. Standalone source clones can use `uv sync --no-sources`; published wheels use
ordinary dependencies.
