"""The 14-block self-describing manifest (capture contract §5). Imports only bundlespec (no db)."""

from __future__ import annotations

from collections.abc import Sequence

from .bundlespec import MANIFEST_BLOCKS, Shard, canonical_json


def build_manifest(
    *,
    producer: str,
    run_id: int,
    dataset_name: str,
    shards: Sequence[Shard],
    retention: str = "ttl",
    pinned: bool = False,
    recompute_recipe: str | None = None,
    lineage: list | None = None,
) -> dict:
    """Assemble all 14 manifest blocks. A bundle is registrable from this alone (zero ambient state)."""
    manifest = {
        "producer": {"producer": producer},
        "run_model_identity": {"run_id": run_id},
        "capture_config": {"dataset_name": dataset_name},
        "hook_point_map": {},  # populated by the capture lane (Stage 2)
        "tokenization": {},
        "payloads": [{"name": s.name, "size_bytes": s.size_bytes} for s in shards],
        "chunk_map": {},
        "integrity": {s.name: s.sha256_hex for s in shards},  # per-file sha256
        "retention": {"ttl_class": retention, "pinned": pinned},
        "recompute_recipe": {"recipe": recompute_recipe, "estimated_cost": None},
        "lineage": lineage or [],
        "per_tensor_stats": {},
        "completeness": {"shard_count": len(shards), "complete": True},
        "footer": {"format": "neuromancer-bundle/1", "self_describing": True},
    }
    missing = set(MANIFEST_BLOCKS) - set(manifest)
    assert not missing, f"manifest missing blocks: {sorted(missing)}"  # all 14 present, always
    return manifest


def manifest_bytes(manifest: dict) -> bytes:
    return canonical_json(manifest)
