"""Local System One adapter for GLiNER2.5-Decide, run through a separately installed gliner2.

python scripts/gliner_server.py --audit /path/to/audit.jsonl
Dependencies belong in the server environment: gliner2 (with torch), fastapi, uvicorn.

Each question becomes one GLiNER2 classification task, read in its own forward pass so every
answer depends only on its state and question, like Laya's. The full label distribution is read
with softmax over all labels, which the library returns when the task is marked multi-label with a
zero threshold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

CHECKPOINT = "fastino/GLiNER2.5-Decide"
SERVED_MODEL = "gliner2.5-decide"


def serialize_state(state) -> str:
    """The text the model reads: a string as is, a record as one `key: value` line per field."""
    if isinstance(state, str):
        return state
    if isinstance(state, dict):
        return "\n".join(
            f"{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}"
            for key, value in state.items()
        )
    return json.dumps(state, ensure_ascii=False)


def task(question: dict) -> tuple[dict, list[str]]:
    """One classification task for a System One question, and its labels in order."""
    kind, instructions = question.get("type"), question.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("every question needs instructions")
    criteria = question.get("criteria")
    if kind == "noul":
        criteria = criteria if isinstance(criteria, dict) else {}
        labels = {"yes": criteria.get("true", "yes"), "no": criteria.get("false", "no")}
    elif kind in ("choice", "score"):
        if isinstance(criteria, dict):
            labels = {str(k): str(v) for k, v in criteria.items()}
        elif isinstance(criteria, list):
            labels = [str(level) for level in criteria]
        else:
            raise ValueError(f"a {kind} question needs criteria listing its options")
        if len(labels) < 2:
            raise ValueError(f"a {kind} question needs at least two options")
    else:
        raise ValueError(f"unsupported question type {kind!r}; this server answers noul, choice and score")
    config = {
        "labels": labels,
        "prompt": instructions,
        "multi_label": True,
        "class_act": "softmax",
        "cls_threshold": 0.0,
    }
    return config, list(labels)


def answer(question: dict, order: list[str], found: list[dict]) -> dict:
    """The System One answer from the label distribution the model returned."""
    probabilities = {label: 0.0 for label in order}
    for item in found:
        probabilities[item["label"]] = float(item["confidence"])
    best = max(order, key=probabilities.__getitem__)
    if question["type"] == "noul":
        return {"noul": probabilities["yes"]}
    if question["type"] == "choice":
        return {"choice": best, "probabilities": probabilities, "confidence": probabilities[best]}
    # A scale's levels are ordered from the bottom, so the score is the expected position on it.
    score = sum(i * probabilities[label] for i, label in enumerate(order))
    return {"score": score, "confidence": probabilities[best]}


def predict(model, state, questions: dict[str, dict]) -> dict[str, dict]:
    text = serialize_state(state)
    tasks = {qid: task(question) for qid, question in questions.items()}
    answers = {}
    for qid, (config, order) in tasks.items():
        found = model.classify_text(text, {"q": config}, include_confidence=True)["q"]
        answers[qid] = answer(questions[qid], order, found)
    return answers


def make_app(model, *, audit_path, served_model=SERVED_MODEL, checkpoint=CHECKPOINT):
    from fastapi import FastAPI, HTTPException

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"model": served_model, "checkpoint": checkpoint}

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
        }
        try:
            # An async route intentionally runs the model on one event-loop thread, serially.
            answers = predict(model, state, questions)
            entry["status"] = "ok"
            return {"model": served_model, "answers": answers, "usage": {"cost": 0.0}}
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
    ap.add_argument("--checkpoint", default=CHECKPOINT, help="Hugging Face repo or local snapshot")
    ap.add_argument("--audit", type=Path, required=True)
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--served-model", default=SERVED_MODEL)
    options = ap.parse_args()
    import uvicorn
    from gliner2 import AutoExtractor

    model = AutoExtractor.from_pretrained(options.checkpoint)
    predict(model, "Hello.", {"warmup": {"type": "noul", "instructions": "The text is a greeting."}})
    options.audit.parent.mkdir(parents=True, exist_ok=True)
    # Do not accidentally mix distinct server sessions in one audit file.
    options.audit.touch(exist_ok=False)
    app = make_app(
        model, audit_path=options.audit, served_model=options.served_model, checkpoint=options.checkpoint
    )
    uvicorn.run(app, host="127.0.0.1", port=options.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
