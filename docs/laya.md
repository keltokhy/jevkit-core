# Laya (local, experimental)

Any JevKit tool can send its decisions to a Laya server on your own machine instead of a hosted
provider. The server is chosen only by name, never automatically, and its calls are metered at
zero API fees:

```bash
jgrep --api laya "a complaint about noise" complaints.txt
jsort --api laya "more urgent" tickets.txt
JEV_API=laya jlink link left.csv right.csv --on name
```

This guide runs the general English 421M-parameter [Laya](https://github.com/NandhaKishorM/laya)
checkpoint through the independent [laya-mlx port](https://github.com/mizorewww/laya-mlx) on
Apple silicon. No JevKit package ships the model; it runs in its own environment and process.
The multilingual and newer typed-decision checkpoints have not been tried.

## Setup (Apple silicon)

Keep the model dependencies in a separate environment. From a directory outside this repository:

```bash
git clone https://github.com/mizorewww/laya-mlx.git
cd laya-mlx
git checkout fc1df62828a3fedf4d8229fdac1cbd85f1cdf337
uv sync --python 3.12
uv pip install --python .venv/bin/python fastapi==0.141.1 uvicorn==0.53.0
.venv/bin/python -c 'from huggingface_hub import snapshot_download; print(snapshot_download("aac6fef/laya-mlx", revision="047678560251f28113ee8f5df4be82102c7bf336"))'
```

Use the printed snapshot path as `--checkpoint`, then start the adapter from this repository
with that environment's Python:

```bash
/path/to/laya-mlx/.venv/bin/python scripts/laya_server.py \
  --checkpoint /path/to/downloaded/snapshot \
  --audit /path/to/laya-audit.jsonl
```

It binds to loopback port 8081, loads FP16 weights, warms the model, and serves `/v1/systemone`.
`/health` reports the checkpoint, context budget, and truncation policy. Then, in another terminal:

```bash
printf '%s\n' 'The music next door keeps me awake.' 'The elevator is broken.' |
  jgrep --api laya -j 4 --timeout 120 --stats -o 'a complaint about noise'
```

## Context limit

The model's 512-token window includes the question's instructions and options, leaving less
room for the record. The adapter checks the remaining budget separately for every question and
answers HTTP 422 if any state would be cropped; the tool reports an error rather than a negative
answer. `--allow-truncation` enables the native runtime's prefix retention instead, and every
request then records the available and dropped state tokens per question in the audit log.
Ordinary use should keep rejection on. Long question instructions can also be shortened by the
runtime's own question budget; the adapter audits state truncation, not question truncation.

## Configuration

| Setting | Meaning |
|---|---|
| `--api laya` or `JEV_API=laya` | Select it; it is never picked by discovery |
| `JEV_LAYA_URL` or `~/.config/jev/laya.url` | Full endpoint, default `http://127.0.0.1:8081/v1/systemone` |
| `JEV_LAYA_API_KEY` or `~/.config/jev/laya.key` | Optional bearer token for a server that authenticates; the supplied adapter does not |
| `--model` or `JEV_MODEL` | `laya-421m`; the supplied adapter rejects other IDs |
| `JEV_PRICE_PER_MTOK` | Overrides the zero price for `--stats`, `--estimate`, and dollar budgets |

`JEV_URL` overrides every endpoint. Zero API fees exclude hardware and electricity.

Laya reads each question on its own, so its answers share the ordinary per-question cache with
the hosted providers, keyed by provider, endpoint, model, state, and question. Pin the server
and the weights; after changing the model behind the same URL and model ID, use `--no-cache`
or a separate `XDG_CACHE_HOME`.

## What to expect

On September 22, 2026, Laya (`laya-421m`) ran each tool's benchmarks on an Apple M3 Ultra with
96 GiB of unified memory, beside Jev 1.13's recorded runs. It is a fast classifier of short texts
into broad topics, and not a substitute for Jev elsewhere. In
[jgrep](https://github.com/keltokhy/jgrep/blob/main/docs/benchmarks/local-models-2026-09-22.md)
it had the best news accuracy of the three models, 92% against Jev's 87%, and finished 2,000
messages in 46 s; at the default cutoff it also flagged 235 messages that were not spam, against
Jev's 41. Its 512-token window, question included, decides what it can read: the adapter refuses
longer requests with HTTP 422, which the tools report as failed decisions, so in
[jcol](https://github.com/keltokhy/jcol/blob/main/benchmarks/README.md#local-models-2026-09-22)
it refused 27 of 100 complaint narratives and in
[jsort](https://github.com/keltokhy/jsort/blob/main/docs/benchmarks/local-models-2026-09-22.md)
every whole FOMC statement. On what it could read, its pairwise comparisons in jsort did not
follow Jev's order, and in
[jlink](https://github.com/keltokhy/jlink/blob/main/docs/benchmarks/local-models-2026-09-22.md)
it scored most candidate pairs as matches, for an F1 of 0.16 to 0.22 on firms, products and
software where Jev's was 0.67 to 0.94. Evaluate thresholds on your own labeled inputs before
relying on the scores.
