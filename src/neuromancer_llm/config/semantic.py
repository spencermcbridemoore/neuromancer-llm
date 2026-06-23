"""Versioned SEMANTIC config — the fingerprint hashes this section wholesale (ADR-0005).

The semantic section carries EVERY numerics-affecting fact, so two runs that would compute different
numbers can never share a fingerprint (one-function-per-fingerprint). For the owned vLLM capture lane on
sm89 that means the batch-invariance flag, the serving version, and the engine runner travel INSIDE the
fingerprint alongside the decoding parameters (E6 banking 2026-06-20: batch_invariant=on,
serving_version='vllm-0.23.0', runner='V1') — so a BI-on and a BI-off capture are never silently pooled.
Scheduling / placement parameters live in config/scheduling.py and are deliberately NOT here (ADR-0005).
"""

from __future__ import annotations

import json
from typing import Any


def canonical_semantic_config(section: dict[str, Any]) -> str:
    """Deterministic serialization (sorted keys, no insignificant whitespace), matching db/identity.py so
    `fingerprint_hash()` is recomputable from the stored TEXT (ADR-0003 TEXT bodies / ADR-0005)."""
    return json.dumps(section, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_semantic_config(
    *,
    declared_mode: str,
    decoding: dict[str, Any],
    batch_invariant: bool,
    serving_version: str,
    runner: str,
    prompt_identity: str,
    model_identity: str,
    extra: dict[str, Any] | None = None,
) -> str:
    """Assemble + canonically serialize the semantic section the fingerprint hashes wholesale.

    `decoding` is the requested decoding params (temperature/max_tokens/seed/logprobs); `prompt_identity`
    is the content hash of the stimulus (the same decode over a different prompt is a different
    experiment); `model_identity` is the model's 7-component identity hash — the model lives INSIDE the
    hashed material (BLOCK 2) so two captures differing only by model never collide on the global-UNIQUE
    fingerprint_hash; `batch_invariant` / `serving_version` / `runner` are the numerics-affecting substrate
    facts the E6 gate proved matter — they live inside the fingerprint so substrates never silently pool.
    """
    section: dict[str, Any] = {
        "version": 1,
        "declared_mode": declared_mode,
        "model_identity": model_identity,  # the model is INSIDE the fingerprint hash (BLOCK 2)
        "decoding": decoding,
        "prompt_identity": prompt_identity,
        # numerics-affecting substrate facts — INSIDE the fingerprint (E6 banking 2026-06-20)
        "substrate": {
            "batch_invariant": batch_invariant,
            "serving_version": serving_version,
            "runner": runner,
        },
    }
    if extra:
        section["extra"] = extra
    return canonical_semantic_config(section)
