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

A September 2026 jgrep experiment on an M3 Ultra found fast, strong short-text classification
(92% top-1 on a news set) but weak precision on spam at the default 0.5 cutoff (53%, rising to
79% at 0.9) and weak code judgments. Treat it as a fast classifier for short texts, and evaluate
thresholds on your own labeled inputs before relying on the scores.
