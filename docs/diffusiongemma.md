# DiffusionGemma (local, experimental)

Any JevKit tool can send its decisions to a DiffusionGemma decision server on your own machine
instead of a hosted provider. The server is chosen only by name, never automatically, and its
calls are metered at zero API fees:

```bash
jgrep --api diffusiongemma -j 1 "a complaint about noise" complaints.txt
jcol --api diffusiongemma run table.csv codebook.json
```

No JevKit package ships the model; it runs in its own environment and process. Model quality
and useful probability thresholds need evaluation on your own inputs.

## Apple silicon

[OpenJev](https://github.com/razorback16/openjev) supplies an MLX implementation of the
structured-read approach from [vLLM PR #57250](https://github.com/vllm-project/vllm/pull/57250).
Its 4-bit checkpoint is roughly 16.6 GB to download and needs about 16 GB of model memory plus
working memory. Install the server in its own directory and environment:

```bash
git clone https://github.com/razorback16/openjev.git
cd openjev
git checkout e04794ab36e4f7e6040c2547baecdb2737ce2e79
uv sync --python 3.12 --extra mlx
OPENJEV_BACKEND=mlx uv run --extra mlx python -m openjev
```

The server downloads `mlx-community/diffusiongemma-26B-A4B-it-4bit` on first start. Wait for
`Application startup complete` before querying it. `OPENJEV_MLX_MODEL` can point at a downloaded
snapshot to pin the weights; the revision used in the original experiment was
`a7a81407613811e8ba63af92ac0d852b809e191f`. Then, in another terminal:

```bash
printf '%s\n' 'The music next door keeps me awake.' 'The elevator is broken.' |
  jgrep --api diffusiongemma --model openjev-0.1 -j 1 --timeout 120 --stats -o 'a complaint about noise'
```

Start with one request at a time: the MLX backend executes model work serially, and a deep
queue can exceed a tool's per-request deadline. The longer timeout covers the first inference.
Raise concurrency from measured throughput once the model is warm.

## NVIDIA / vLLM

Run OpenJev's documented vLLM deployment, or the prototype `structured_server.py` from
[PR #57250](https://github.com/vllm-project/vllm/pull/57250). That PR was unmerged at the time of
writing, so a released vLLM is not enough on its own; follow the server's pinned build
instructions. Its `/v1/systemone` adapter sits in front of vLLM. For the PR example server's
default port:

```bash
JEV_DIFFUSIONGEMMA_URL=http://127.0.0.1:8011/v1/systemone \
  jgrep --api diffusiongemma --model jev-latest 'a stack trace' build.log
```

## Configuration

| Setting | Meaning |
|---|---|
| `--api diffusiongemma` or `JEV_API=diffusiongemma` | Select it; it is never picked by discovery |
| `JEV_DIFFUSIONGEMMA_URL` or `~/.config/jev/diffusiongemma.url` | Full endpoint, default `http://127.0.0.1:8080/v1/systemone` |
| `JEV_DIFFUSIONGEMMA_API_KEY` or `~/.config/jev/diffusiongemma.key` | Optional bearer token; no `Authorization` header when absent |
| `--model` or `JEV_MODEL` | Default `openjev-latest`; `openjev-0.1` pins the server's decision model |
| `JEV_PRICE_PER_MTOK` | Overrides the zero price for `--stats`, `--estimate`, and dollar budgets |

`JEV_URL` overrides every endpoint. A cost the server reports always wins over the price.
Zero means no API fee, not zero compute. Configure a price before using a dollar budget against
a metered remote server; an offline estimate is a byte-based approximation and cannot predict
the server's adaptive re-reads.

## Joint reads and the cache

A diffusion read answers every question in a batch in the light of the others, so the runtime
keys each of this provider's answers on the provider, endpoint, model, state, and the entire
ordered batch of questions, IDs included. A batch is reused only whole: if any slot is missing
from the cache, the whole batch is sent again. Hosted providers and Laya keep per-question
caching. Pin both server and weights; after changing weights or inference settings behind the
same URL and model, use `--no-cache` or a separate `XDG_CACHE_HOME`.

## What to expect

On September 22, 2026, DiffusionGemma (`openjev-0.1`) ran each tool's benchmarks on an Apple M3
Ultra with 96 GiB of unified memory, beside Jev 1.13's recorded runs. It is a reasonable
substitute for Jev when the text must stay on your machine and each decision is about one record,
or about long ones. In
[jgrep](https://github.com/keltokhy/jgrep/blob/main/docs/benchmarks/local-models-2026-09-22.md)
its spam F1 was 0.87 against Jev's 0.91, with the same news accuracy; in
[jlink](https://github.com/keltokhy/jlink/blob/main/docs/benchmarks/local-models-2026-09-22.md)
it came within 0.03 of Jev's F1 on four of five benchmarks; in
[jcol](https://github.com/keltokhy/jcol/blob/main/benchmarks/README.md#local-models-2026-09-22)
it agreed with the product label on 78 of 100 complaints against Jev's 75; and in
[jsort](https://github.com/keltokhy/jsort/blob/main/docs/benchmarks/local-models-2026-09-22.md)
its scores for whole FOMC statements correlated 0.89 with Jev's. It is not a substitute for
sorting single short lines, where its comparisons had a reliability of 0.62 and 0.39 against
Jev's 0.97 and 0.96, or for judging isolated diff lines. The server runs one call at a time on
the GPU: about a third of a second per short record, and 14 to 18 minutes per 3,000 record pairs.
Keep it experimental and evaluate its cutoff on your own data.
