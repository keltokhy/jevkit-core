# GLiNER2.5-Decide (local, experimental)

Any JevKit tool can send its decisions to a GLiNER2.5-Decide server on your own machine instead
of a hosted provider. The server is chosen only by name, never automatically, and its calls are
metered at zero API fees:

```bash
jgrep --api gliner "a complaint about noise" complaints.txt
jcol --api gliner run table.csv codebook.json
```

[GLiNER2.5-Decide](https://huggingface.co/fastino/GLiNER2.5-Decide) is Fastino's 340M-parameter
classifier (a DeBERTa-v3-large encoder, Apache 2.0) for intent, routing, sentiment, priority and
policy decisions. It picks labels; it does not reason or write text. No JevKit package ships the
model; it runs in its own environment and process through the `gliner2` library.

## Setup

Keep the model dependencies in a separate environment. From a directory outside this repository:

```bash
uv venv --python 3.12 gliner-env
uv pip install --python gliner-env/bin/python 'gliner2==2.0.0' fastapi uvicorn
```

Then start the adapter from this repository with that environment's Python:

```bash
gliner-env/bin/python scripts/gliner_server.py --audit /path/to/gliner-audit.jsonl
```

It downloads `fastino/GLiNER2.5-Decide` on first start (pass `--checkpoint` for a local snapshot),
warms the model, binds to loopback port 8082, and serves `/v1/systemone`. Then, in another
terminal:

```bash
printf '%s\n' 'The music next door keeps me awake.' 'The elevator is broken.' |
  jgrep --api gliner --stats -o 'a complaint about noise'
```

## How questions are read

Each question is one classification task over the record, with the question's instructions as
the task prompt, and is read in its own forward pass:

| Question | Labels | Answer |
|---|---|---|
| `noul` | `yes` and `no`, described by the question's `true` and `false` criteria if it has them | `noul` is the probability of `yes` |
| `choice` | the options, with their descriptions | the most probable option, and every option's probability |
| `score` | the scale's levels, bottom first | the expected position on the scale, from 0 |

A record with several fields is read as one `field: value` line per field. Other question types
are refused with HTTP 400. The library reads long texts whole; the model's accuracy on long
inputs has not been measured here.

## Configuration

| Setting | Meaning |
|---|---|
| `--api gliner` or `JEV_API=gliner` | Select it; it is never picked by discovery |
| `JEV_GLINER_URL` or `~/.config/jev/gliner.url` | Full endpoint, default `http://127.0.0.1:8082/v1/systemone` |
| `JEV_GLINER_API_KEY` or `~/.config/jev/gliner.key` | Optional bearer token for a server that authenticates; the supplied adapter does not |
| `--model` or `JEV_MODEL` | `gliner2.5-decide`; the supplied adapter rejects other IDs |
| `JEV_PRICE_PER_MTOK` | Overrides the zero price for `--stats`, `--estimate`, and dollar budgets |

`JEV_URL` overrides every endpoint. Zero API fees exclude hardware and electricity.

Answers share the ordinary per-question cache with the hosted providers, keyed by provider,
endpoint, model, state, and question. After changing the weights behind the same URL and model ID,
use `--no-cache` or a separate `XDG_CACHE_HOME`.

## What to expect

Not yet benchmarked with the JevKit tools. Fastino reports 60.2% average accuracy on its own
17-domain held-out suite. Evaluate thresholds on your own labeled inputs before relying on the
probabilities.
