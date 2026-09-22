# Local extraction validation — 2026-09-22

The five source baselines are recorded in `consumer-baselines.json`. No remote
branches were fetched and no model inference was performed. Tests used mock
transports or local HTTP fixtures with outbound sockets blocked and model
credentials removed. Dependency installation and public tokenizer preparation
are separate from those inference-free checks.

| Package | Interpreter | Result |
|---|---|---|
| jevkit-core | Python 3.13.15 | 27 passed |
| jevkit-core | Python 3.10 | 27 passed |
| jgrep | Python 3.12.14 | 299 passed |
| jsort | Python 3.13.15 | 159 passed |
| jlink | Python 3.12.14 | 720 passed |
| jselect | Python 3.13.15 | 164 passed |
| jcol | Python 3.12.14 | 138 passed, 3 existing warnings |

The consumer total is 1,480. All existing consumer test files were retained
without modifying their assertions. All five baselines were also tested before
the extraction; jselect's first attempt had an incorrect tokenizer-cache path,
then passed all 164 tests after pointing to the existing local cache. The first
migrated jselect run exposed a timeout error-message regression, fixed in the
adapter without changing its test.

Additional checks:

- All five import `/Volumes/K3/GitHub/jevkit-core/src/jevkit_core` from their separate
  editable development environments. Instrumentation verifies that each actually
  calls the shared transport.
- All five match their original Git versions on the focused request/result/metering
  probe: raw request bodies, responses, warm-cache reuse, and common counters.
  The four client adapters also exercise in-flight request sharing; jcol includes
  binary, categorical, and scale answers. This is bounded fixture evidence, not
  exhaustive equivalence across every input.
- Fresh core and consumer wheels install in five independent temporary environments
  outside source checkouts. Every installed CLI reports its version and every
  installed consumer uses the wheel's core, not the editable checkout.
- jlink review HTML/JS/CSS and jcol's browser HTML are present in the installed wheels.
- The installed jcol process checks pass for pipes, partial completion/resume,
  offline export, Parquet stdin, and SIGINT recovery.
- Core and jselect lint/format checks pass. All modified GitHub workflow YAML parses;
  all repository diffs pass whitespace checks. Hosted CI has not been run.

Reproduce the suite from the core checkout:

```bash
python3 scripts/dev.py --suffix=-jevkit setup
python3 scripts/dev.py --suffix=-jevkit check
python3 scripts/dev.py --suffix=-jevkit wheel-check
```

Raw local logs are retained in `/Volumes/K3/agent-working-space/jevkit/`, including
`development-check.log`, `wheel-check-final.log`, `core-py310.log`, and the five
`*-contract.log` files. These logs are development artifacts, not package contents.

Release boundary: this validates the local development implementation and locally
built packages. It does not establish live model quality, hosted CI status, or
publication. The core remote/tag/package must be published before migrated consumer
CI and public releases can resolve their pinned core dependency.
