# Validation record — 2026-09-22

The five source baselines are recorded in `consumer-baselines.json`. Bootstrap
fetches verified current remote main branches. No model inference was performed. Tests used mock
transports or local HTTP fixtures with outbound sockets blocked and model
credentials removed. Dependency installation and public tokenizer preparation
are separate from those inference-free checks.

| Package | Interpreter | Result |
|---|---|---|
| jevkit-core | Python 3.13.15 | 44 passed |
| jevkit-core | Python 3.10.21 | 44 passed |
| jgrep | Python 3.12.14 | 282 passed |
| jsort | Python 3.13.15 | 159 passed |
| jlink | Python 3.12.14 | 720 passed |
| jselect | Python 3.13.15 | 164 passed |
| jcol | Python 3.12.14 | 138 passed, 3 existing warnings |

The consumer total for the main-branch migrations is 1,463. All existing consumer test files were retained
without modifying their assertions. The core now includes 17 additional tests
for provider selection/model overrides, accounting callback order, unknown-model
provenance, single-owner charges, cache-only sharing, task isolation, and cleanup
after success, failure, or cancellation. All five baselines were also tested before
the extraction; jselect's first attempt had an incorrect tokenizer-cache path,
then passed all 164 tests after pointing to the existing local cache. The first
migrated jselect run exposed a timeout error-message regression, fixed in the
adapter without changing its test.

Additional checks:

- All five import `/Volumes/K3/GitHub/jevkit-core/src/jevkit_core` from their separate
  editable development environments. Instrumentation verifies that each actually
  calls the shared transport.
- All five match their original Git versions on the focused request/result/metering
  probe: raw request bodies, responses, warm-cache reuse, common counters, and
  effective provider definitions/defaults. The provider comparison normalizes
  capability fields that were implicit in older adapters.
  The four client adapters also exercise in-flight request sharing; jcol includes
  binary, categorical, and scale answers. This is bounded fixture evidence, not
  exhaustive equivalence across every input.
- Instrumentation verifies that all five use shared usage accumulation, the four
  decision clients use shared in-flight request handling, and jsort/jlink construct
  answer provenance through the core. jselect keeps its relevance batching and
  separate score-cache behavior. Existing tests retain coverage for jsort's charge
  attribution, jlink's cache-only sharing, and jcol's hedging.
- Fresh core and consumer wheels install in five independent temporary environments
  outside source checkouts. Every installed CLI reports its version and every
  installed consumer uses the wheel's core, not the editable checkout.
- jlink review HTML/JS/CSS and jcol's browser HTML are present in the installed wheels.
- The installed jcol process checks pass for pipes, partial completion/resume,
  offline export, Parquet stdin, and SIGINT recovery.
- Core and jselect lint/format checks pass. All modified GitHub workflow YAML parses;
  all repository diffs pass whitespace checks. The initial six PRs and the
  individual main-branch suites also passed hosted CI on Python 3.10 and 3.13.

Reproduce the suite from the core checkout:

```bash
python3 scripts/dev.py setup
python3 scripts/dev.py check
python3 scripts/dev.py wheel-check
```

Current logs are retained in `/Volumes/K3/agent-working-space/jevkit/`:
`core-followup-check.log`, `core-followup-wheels.log`, and
`core-followup-py310.log`. The initial extraction logs and two preliminary
provider-comparison runs are preserved separately. Those preliminary comparisons
needed normalization of implicit legacy capability fields; consumer behavior did
not require a correction. These logs are development artifacts, not package contents.

Release verification: `jevkit-runtime 0.1.0` is published on PyPI and the
`v0.1.0` GitHub release. Freshly built wheels of all five consumers installed
in separate temporary environments with the runtime resolved from PyPI. Their
CLI and instrumented offline probes passed (`pypi-install-check.log`). These
checks do not establish live model quality. Consumer PyPI releases remain
independently versioned and were not published as part of this migration.


## GitHub bootstrap

The distribution is named `jevkit-runtime`: the PyPI project `jevkit-core` belongs
to another developer. The repository remains `keltokhy/jevkit-core`, and Python
imports remain `jevkit_core`. The five source overrides and lockfiles use the new
distribution name. Wheel checks install the freshly built runtime explicitly.

The jgrep PR is based on main (`7b2c867`), excluding the separate experimental
backend and benchmark commits used in the first local exploration. Its 282-test main-branch suite passes; the earlier local 299-test run covered
that experimental branch. The other four baselines are unchanged.

All six repositories are public, so the hosted downstream matrix can check out
all five consumers without extra repository credentials. The matrix is gated by
`JEVKIT_CONSUMERS_READY` during the initial six-PR bootstrap, then enabled for
future core pull requests and pushes. PyPI publishing is manually dispatched after
Trusted Publishing is configured; Git tags alone do not publish a distribution.

Bootstrap logs: `publish-setup.log`, `publish-check.log`, and `publish-wheels.log`
in the same local report directory. The source-only jgrep migration, distribution
rename, and installed wheel paths were verified again before merging.


## Development runner PATH regression

The first hosted downstream run exposed a runner-only issue: Python used the
selected virtual environment, but subprocess CLI lookup inherited the system
PATH. jlink's installed-entrypoint checks therefore found Java's `jlink` and
could not find `jev-link`. The runner now prepends the selected environment's
executable directory and sets `VIRTUAL_ENV` for run, check, and wheel-check.

Three regression cases reproduce a conflicting command on PATH and verify that
each action selects its own environment. All three failed before the fix and
passed afterward, bringing the core suite to 47 tests. The published runtime
code and v0.1.0 tag are unchanged by this development-script correction.
