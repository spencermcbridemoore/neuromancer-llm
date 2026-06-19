"""The three distinct hash roles (ADR-0005), never conflated:

  * fingerprint  = experiment identity  (hash of the SEMANTIC config section wholesale)
  * sha256       = storage integrity    (hash of artifact bytes)
  * model identity = the 7-component canonical tuple hash

(The fourth, replay divergence, is a measurement, not a hash — see divergence_measurements.)
All canonical serializations are deterministic (sorted keys, no insignificant whitespace) so a hash is
recomputable from the recorded inputs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_bytes(data: bytes) -> bytes:
    """Storage-integrity hash of raw bytes (artifact sha256)."""
    return hashlib.sha256(data).digest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_hash(obj: Any) -> bytes:
    """Content-hash identity for prompt sets / derived sets (phase0 Q1/Q3)."""
    return sha256_bytes(_canonical(obj))


def model_identity_hash(
    *,
    hf_repo: str | None,
    hf_revision: str | None,
    dtype_quant: str,
    tokenizer_hash: bytes,
    serving_stack: str,
    serving_version: str,
    arch_family: str,
) -> bytes:
    """sha256 over the 7-component model identity (ADR-0005), canonical serialization."""
    return content_hash(
        {
            "hf_repo": hf_repo,
            "hf_revision": hf_revision,
            "dtype_quant": dtype_quant,
            "tokenizer_hash": tokenizer_hash.hex(),
            "serving_stack": serving_stack,
            "serving_version": serving_version,
            "arch_family": arch_family,
        }
    )


def fingerprint_hash(semantic_config_text: str) -> bytes:
    """Experiment-identity hash: the semantic config section hashed wholesale (ADR-0005)."""
    return sha256_bytes(semantic_config_text.encode("utf-8"))
