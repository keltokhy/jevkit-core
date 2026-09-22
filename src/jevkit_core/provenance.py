"""Answer origins shared by caches and saved results, independent of current defaults."""

from __future__ import annotations

import time


def answer_provenance(*, provider: str, requested_model: str, resolved_model: object) -> dict:
    """Record the responder literally; an absent model must never become the requested alias."""
    return {
        "version": 1,
        "provider": provider,
        "requested_model": requested_model,
        "resolved_model": resolved_model
        if isinstance(resolved_model, str) and resolved_model.strip()
        else None,
        "answered_at": time.time(),
    }
