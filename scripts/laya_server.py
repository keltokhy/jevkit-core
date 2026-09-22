"""Local System One adapter for a separately installed, pinned laya-mlx runtime.

python scripts/laya_server.py --checkpoint /path/to/snapshot --audit /path/to/audit.jsonl
State truncation fails explicitly unless --allow-truncation is supplied.
Dependencies belong in the server environment: laya-mlx, fastapi, uvicorn.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path


def make_app(agent, *, audit_path, served_model="laya-421m", allow_truncation=False):
    from fastapi import FastAPI, HTTPException
    from laya_mlx.common import build_prefix, serialize_state

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {
            "model": served_model,
            "max_tokens": agent.cfg["max_len"],
            "allow_truncation": allow_truncation,
            "checkpoint": str(agent.model_dir),
        }

    @app.post("/v1/systemone")
    async def decide(body: dict):
        if body.get("model") != served_model:
            raise HTTPException(400, f"This server only serves {served_model!r}")
        if "state" not in body or not isinstance(body.get("questions"), dict) or not body["questions"]:
            raise HTTPException(400, "state and a nonempty questions object are required")
        started = time.perf_counter()
        state, questions = body["state"], body["questions"]
        entry = {
            "state_sha256": hashlib.sha256(
                json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "questions_sha256": hashlib.sha256(
                json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "status": "started",
            "questions": {},
        }
        try:
            tokens = agent.tok(
                serialize_state(state).replace(agent.tok.mask_token, " "), add_special_tokens=False
            )["input_ids"]
            for qid, definition in questions.items():
                q = agent._to_internal(definition)
                prefix, _ = build_prefix(agent.tok, q, agent.cfg.get("head_max_len", 192))
                room = max(0, agent.cfg["max_len"] - len(prefix) - 1)
                entry["questions"][qid] = {
                    "state_tokens": len(tokens),
                    "available_state_tokens": room,
                    "dropped_state_tokens": max(0, len(tokens) - room),
                }
            if not allow_truncation and any(q["dropped_state_tokens"] for q in entry["questions"].values()):
                entry["status"] = "context_rejected"
                raise HTTPException(
                    422,
                    "State exceeds the Laya context budget; use shorter records "
                    "or explicitly enable audited truncation",
                )
            # An async route intentionally runs MLX on one event-loop thread, serially.
            result = agent.predict(state, questions)
            result["model"] = served_model
            result["usage"]["cost"] = 0.0
            result["context_audit"] = entry["questions"]
            entry["status"] = "ok"
            entry["usage"] = result["usage"]
            return result
        except (ValueError, TypeError, KeyError) as error:
            entry["status"] = "invalid_request"
            raise HTTPException(400, str(error)) from error
        finally:
            entry["seconds"] = time.perf_counter() - started
            with audit_path.open("a") as stream:
                stream.write(json.dumps(entry, allow_nan=False) + "\n")

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--audit", type=Path, required=True)
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--served-model", default="laya-421m")
    ap.add_argument("--allow-truncation", action="store_true")
    options = ap.parse_args()
    import laya_mlx
    import uvicorn

    agent = laya_mlx.load(str(options.checkpoint), dtype="float16", device="gpu")
    warmup = {"warmup": {"type": "noul", "instructions": "The text is a greeting."}}
    agent.predict("Hello.", warmup)
    options.audit.parent.mkdir(parents=True, exist_ok=True)
    # Do not accidentally mix distinct server sessions in one audit file.
    options.audit.touch(exist_ok=False)
    app = make_app(
        agent,
        audit_path=options.audit,
        served_model=options.served_model,
        allow_truncation=options.allow_truncation,
    )
    uvicorn.run(app, host="127.0.0.1", port=options.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
