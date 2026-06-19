"""Zero-dependency bundle spec primitives — NO db / storage / heavy imports (the no-db gate).

A worker (possibly no-egress, ARCC) builds a self-describing bundle with only the stdlib so it can run
where no Postgres driver exists; the DB registers it afterward (b finding 36). This module therefore
imports only hashlib / json / dataclasses / uuid.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

# The 14 manifest blocks (capture contract §5). A manifest is registrable with zero ambient state.
MANIFEST_BLOCKS: tuple[str, ...] = (
    "producer",  # 1  producer identity
    "run_model_identity",  # 2  run + model identity
    "capture_config",  # 3  capture config
    "hook_point_map",  # 4  site_kind enum + namespace, canonical names (ADR-0016)
    "tokenization",  # 5  tokenization + token-table ref
    "payloads",  # 6  tensor keys / shapes / dtypes
    "chunk_map",  # 7  chunk map
    "integrity",  # 8  per-file sha256
    "retention",  # 9  {ttl_class, pinned}
    "recompute_recipe",  # 10 recompute recipe + estimated cost
    "lineage",  # 11 lineage
    "per_tensor_stats",  # 12 per-tensor stats
    "completeness",  # 13 completeness
    "footer",  # 14 footer self-description KV
)

# Stable namespace so a resumed worker derives the SAME bundle_uuid and re-registers idempotently (W6/W7).
_BUNDLE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "neuromancer-llm/bundle")


def sha256_bytes(data: bytes) -> bytes:
    """Storage-integrity digest (for the bytea columns)."""
    return hashlib.sha256(data).digest()


def sha256_hex(data: bytes) -> str:
    """Hex digest (for the manifest integrity block)."""
    return hashlib.sha256(data).hexdigest()


def bundle_uuid_for(*parts: object) -> uuid.UUID:
    """Deterministic bundle identity from stable parts (run/dataset/shard-set) — survives resume."""
    return uuid.uuid5(_BUNDLE_NS, "/".join(str(p) for p in parts))


@dataclass(frozen=True)
class Shard:
    """One payload file in a bundle."""

    name: str
    data: bytes

    @property
    def sha256_hex(self) -> str:
        return sha256_hex(self.data)

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def canonical_json(obj: object) -> bytes:
    """Deterministic serialization (sorted keys, no insignificant whitespace) — a manifest hashes stably."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
