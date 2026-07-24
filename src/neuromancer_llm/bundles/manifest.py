"""The 14-block self-describing manifest (capture contract §5). Imports only bundlespec (no db)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .bundlespec import (
    MANIFEST_BLOCKS,
    RunModelIdentityBlock,
    Shard,
    TokenizationBlock,
    canonical_json,
)

# The blocks THIS lane populates-or-reports (completeness `absent` tracks exactly these; the remaining
# placeholder blocks — hook_point_map / chunk_map / per_tensor_stats — are later-stage content and are
# either declared not_applicable by the caller or simply not listed as populated).
_POPULATABLE_IDENTITY_BLOCKS: tuple[str, ...] = ("run_model_identity", "tokenization")


def _placeholder_blocks(manifest: dict, run_id: int) -> set[str]:
    """Blocks still carrying their known placeholder form (no real content). Structural, deterministic."""
    empty: set[str] = set()
    for name in MANIFEST_BLOCKS:
        value = manifest[name]
        if name == "run_model_identity":
            if value == {"run_id": run_id}:
                empty.add(name)
        elif name == "recompute_recipe":
            if value.get("recipe") is None:
                empty.add(name)
        elif name in ("completeness", "footer", "producer", "capture_config", "retention"):
            continue  # always self-populated
        elif not value:  # {} or []
            empty.add(name)
    return empty


def build_manifest(
    *,
    producer: str,
    run_id: int,
    dataset_name: str,
    shards: Sequence[Shard],
    retention: str = "ttl",
    pinned: bool = False,
    recompute_recipe: str | None = None,
    estimated_cost: object | None = None,
    lineage: list | None = None,
    run_model_identity: RunModelIdentityBlock | None = None,
    tokenization: TokenizationBlock | None = None,
    not_applicable: Sequence[str] = (),
    shard_footer_kv: Mapping[str, Mapping[str, str]] | None = None,
) -> dict:
    """Assemble all 14 manifest blocks. A bundle is registrable from this alone (zero ambient state).

    The identity payloads (blocks 2/5) are plain VALUES passed in by the caller — this module never
    queries a DB (the no-db worker gate). `not_applicable` declares blocks the LANE is structurally
    unable to carry (e.g. tensor blocks for a logprob table shard); an applicable-but-unsupplied
    identity block lands in completeness `absent` instead, and `complete` is COMPUTED — never hardcoded.
    `shard_footer_kv` records each shard's embedded footer self-description KV in block 14 (the same
    dict the registrar hashes into table_manifests.footer_kv_sha256 — one source object).
    """
    na = tuple(sorted(set(not_applicable)))
    unknown = set(na) - set(MANIFEST_BLOCKS)
    if unknown:  # fail loud on a typo'd declaration — a silent misspelling would fake completeness
        raise ValueError(f"not_applicable names unknown manifest blocks: {sorted(unknown)}")

    if run_model_identity is not None:
        rmi = run_model_identity
        block2: dict = {
            "run_id": run_id,
            "run_key": rmi.run_key,
            "fingerprint": {
                "fingerprint_hash": rmi.fingerprint_hash_hex,
                "declared_mode": rmi.declared_mode,
                "semantic_config": rmi.semantic_config,
            },
            "model": {
                "identity_hash": rmi.identity_hash_hex,
                "hf_repo": rmi.hf_repo,
                "hf_revision": rmi.hf_revision,
                "dtype_quant": rmi.dtype_quant,
                "tokenizer_hash": rmi.tokenizer_hash_hex,
                "serving_stack": rmi.serving_stack,
                "serving_version": rmi.serving_version,
                "arch_family": rmi.arch_family,
            },
        }
    else:
        block2 = {"run_id": run_id}

    if tokenization is not None:
        tok = tokenization
        block5: dict = {
            "tokenizer_hash": tok.tokenizer_hash_hex,
            "hf_repo": tok.hf_repo,
            "hf_revision": tok.hf_revision,
            "token_table_ref": (
                {"shard": tok.token_table_shard, "artifact_kind": "token_table"}
                if tok.token_table_shard is not None
                else None
            ),
        }
    else:
        block5 = {}

    footer: dict = {"format": "neuromancer-bundle/1", "self_describing": True}
    if shard_footer_kv:
        footer["shards"] = {name: dict(kv) for name, kv in sorted(shard_footer_kv.items())}

    manifest = {
        "producer": {"producer": producer},
        "run_model_identity": block2,
        "capture_config": {"dataset_name": dataset_name},
        "hook_point_map": {},  # populated by the capture lane (Stage 2)
        "tokenization": block5,
        "payloads": [{"name": s.name, "size_bytes": s.size_bytes} for s in shards],
        "chunk_map": {},
        "integrity": {s.name: s.sha256_hex for s in shards},  # per-file sha256
        "retention": {"ttl_class": retention, "pinned": pinned},
        "recompute_recipe": {"recipe": recompute_recipe, "estimated_cost": estimated_cost},
        "lineage": lineage or [],
        "per_tensor_stats": {},
        "completeness": {},  # computed below, once every other block is assembled
        "footer": footer,
    }

    empty = _placeholder_blocks(manifest, run_id)
    # N/A wins over "populated" for reporting: the logprob lane declares payloads N/A in the contract
    # sense (tensor keys/shapes/dtypes) even though the block keeps the file inventory.
    populated = [b for b in MANIFEST_BLOCKS if b not in empty and b not in na and b != "completeness"]
    absent = [b for b in _POPULATABLE_IDENTITY_BLOCKS if b in empty and b not in na]
    manifest["completeness"] = {
        "shard_count": len(shards),
        "populated": populated,
        "not_applicable": list(na),
        "absent": absent,
        "complete": not absent,  # computed, never hardcoded (audit 2026-07-02 / §5(a))
    }

    missing = set(MANIFEST_BLOCKS) - set(manifest)
    assert not missing, f"manifest missing blocks: {sorted(missing)}"  # all 14 present, always
    return manifest


def manifest_bytes(manifest: dict) -> bytes:
    return canonical_json(manifest)
