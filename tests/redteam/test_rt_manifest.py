"""§5(a) manifest-population probes (the pre-A2 BLOCKING gate; owner GO + mapping confirmation 2026-07-03).

Three permanent regressions over the populated manifest (capture contract §5, blocks 2/5/13/14):
  (i)  identity blocks — block 2 (run -> fingerprint -> model identity) and block 5 (tokenization +
       token-table ref) carry the SAME natural-key values the registered identity rows hold, compared
       against the DB rows (not merely the capture inputs), with DB surrogate ids deliberately absent;
  (ii) footer mirror — table_manifests.footer_kv_sha256 is WRITTEN and equals sha256(canonical_json(
       block-14 KV)), and the KV round-trips byte-identically from the parquet file's own footer
       metadata: file <-> manifest <-> DB cross-checked on ONE source dict;
  (iii) honest completeness — block 13 is COMPUTED: the logprob lane declares its structurally
       inapplicable blocks (4/6/7/12) N/A and is complete; a generic identity-less bundle honestly
       reports complete: false with absent: ["run_model_identity", "tokenization"].

Each probe was demonstrated RED at 6083a9a (block 2 = {"run_id": ...} only, block 5 = {},
footer_kv_sha256 had ZERO writers, completeness hardcoded complete: True) -> GREEN with this pass.
Markers: pg (real Postgres; LocalFs lake).
"""

from __future__ import annotations

import io
import json

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.bundlespec import canonical_json, sha256_bytes
from neuromancer_llm.bundles.registrar import BundleRegistrar
from neuromancer_llm.capture.adapters.vllm import LogprobSample
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg


def _sample(n: int = 20, generated: int = 1000) -> LogprobSample:
    pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(n))  # descending; argmax = token 1000
    return LogprobSample(prompt="p", generated_token_id=generated, top_logprobs=pairs)


def _manifest(tmp_path, partition_path: str) -> dict:
    return json.loads(LocalFsBackend(tmp_path).get(f"{partition_path}/manifest.json"))


def test_rt_manifest_identity_blocks_populated(repo, tmp_path, rt_capture):
    """(i) Blocks 2/5 carry the registered rows' NATURAL KEYS (hashes + components) — never {}/run_id-only.
    The compare is against the DB rows the capture registered, so a manifest that drifts from the registry
    (either direction) turns this probe red."""
    res = rt_capture(repo, tmp_path, _sample(20))
    m = _manifest(tmp_path, res.partition_path)

    with repo.engine.connect() as conn:
        fp = conn.execute(
            text(
                "SELECT fingerprint_hash, declared_mode, semantic_config FROM neuro.fingerprints "
                "WHERE fingerprint_id = :f"
            ),
            {"f": res.fingerprint_id},
        ).one()
        mi = conn.execute(
            text(
                "SELECT identity_hash, hf_repo, hf_revision, dtype_quant, serving_stack, "
                "serving_version, arch_family, tokenizer_id FROM neuro.model_identities "
                "WHERE model_id = :m"
            ),
            {"m": res.model_id},
        ).one()
        tok = conn.execute(
            text(
                "SELECT tokenizer_hash, hf_repo, hf_revision FROM neuro.tokenizer_identities "
                "WHERE tokenizer_id = :t"
            ),
            {"t": mi.tokenizer_id},
        ).one()

    b2 = m["run_model_identity"]
    assert b2["run_id"] == res.run_id and b2["run_key"] == res.run_key
    assert b2["fingerprint"]["fingerprint_hash"] == bytes(fp.fingerprint_hash).hex()
    assert b2["fingerprint"]["declared_mode"] == fp.declared_mode == res.declared_mode
    # the fingerprint-hash PREIMAGE rides in the manifest (owner-ruled IN 2026-07-03): the fingerprints
    # row is rebuildable from the manifest alone, and FIX #4's recompute keeps any rebuild loud.
    assert b2["fingerprint"]["semantic_config"] == fp.semantic_config == res.semantic_config
    assert b2["model"]["identity_hash"] == bytes(mi.identity_hash).hex()
    assert (
        b2["model"]["hf_repo"],
        b2["model"]["hf_revision"],
        b2["model"]["dtype_quant"],
        b2["model"]["serving_stack"],
        b2["model"]["serving_version"],
        b2["model"]["arch_family"],
    ) == (mi.hf_repo, mi.hf_revision, mi.dtype_quant, mi.serving_stack, mi.serving_version, mi.arch_family)
    assert b2["model"]["tokenizer_hash"] == bytes(tok.tokenizer_hash).hex()
    # DB surrogate ids stay OUT of the manifest (§8: a crawl-rebuild mints fresh ids — embedding them
    # would manufacture false manifest<->DB disagreement; the hashes are what identity reconcile compares).
    assert "model_id" not in b2["model"] and "fingerprint_id" not in b2["fingerprint"]

    b5 = m["tokenization"]
    assert b5["tokenizer_hash"] == bytes(tok.tokenizer_hash).hex()
    assert b5["hf_repo"] == tok.hf_repo and b5["hf_revision"] == tok.hf_revision
    assert b5["token_table_ref"] == {"shard": "logprobs-0000.parquet", "artifact_kind": "token_table"}
    assert b5["token_table_ref"]["shard"] in m["integrity"]  # resolvable INSIDE the manifest (§5)


def test_rt_manifest_footer_kv_mirror(repo, tmp_path, rt_capture):
    """(ii) The block-14 mirror: table_manifests.footer_kv_sha256 (ZERO writers at 6083a9a) is written and
    equals sha256(canonical_json(kv)); the parquet file's OWN footer metadata carries the identical
    neuromancer.* KV. One source dict, three holders — file, manifest, DB."""
    import pyarrow.parquet as pq_mod

    res = rt_capture(repo, tmp_path, _sample(20))
    m = _manifest(tmp_path, res.partition_path)

    kv = m["footer"]["shards"]["logprobs-0000.parquet"]
    assert set(kv) == {
        "neuromancer.format",
        "neuromancer.dataset_name",
        "neuromancer.schema_major",
        "neuromancer.columns",
        "neuromancer.row_count",
    }  # exactly the five ruled keys (owner-confirmed 2026-07-03)
    assert kv["neuromancer.dataset_name"] == "logprobs"
    assert kv["neuromancer.row_count"] == str(res.parquet_row_count)

    with repo.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT tm.footer_kv_sha256, a.uri FROM neuro.table_manifests tm "
                "JOIN neuro.artifacts a ON a.artifact_id = tm.artifact_id "
                "WHERE tm.run_id = :r AND tm.dataset_name = 'logprobs'"
            ),
            {"r": res.run_id},
        ).one()
    assert row.footer_kv_sha256 is not None
    assert bytes(row.footer_kv_sha256) == sha256_bytes(canonical_json(kv))

    # the shard file is SELF-describing: its embedded neuromancer.* footer subset == block 14 verbatim.
    # (Only the neuromancer.* subset is the contract — the raw footer's ARROW:schema blob is
    # pyarrow-version-dependent and deliberately outside the hash.)
    blob = LocalFsBackend(tmp_path).get(row.uri)
    meta = pq_mod.read_schema(io.BytesIO(blob)).metadata or {}
    embedded = {k.decode(): v.decode() for k, v in meta.items() if k.startswith(b"neuromancer.")}
    assert embedded == kv


def test_rt_manifest_honest_completeness(repo, seeded, tmp_path, rt_capture):
    """(iii) Block 13 is COMPUTED (at 6083a9a it hardcoded {"shard_count": N, "complete": True}). The
    logprob lane declares blocks 4/6/7/12 structurally N/A and is complete; a generic identity-less
    bundle honestly reports complete: false + the absent identity blocks (owner-confirmed 2026-07-03:
    N/A stays reserved for structural inapplicability — absence is reported as absence)."""
    res = rt_capture(repo, tmp_path, _sample(20))
    comp = _manifest(tmp_path, res.partition_path)["completeness"]
    assert comp["shard_count"] == 1
    assert comp["not_applicable"] == ["chunk_map", "hook_point_map", "payloads", "per_tensor_stats"]
    assert comp["absent"] == []
    assert comp["complete"] is True
    assert "run_model_identity" in comp["populated"] and "tokenization" in comp["populated"]

    # generic seam-style register (no identity payloads): honestly incomplete, never a hardcoded True.
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    part = f"generic/run={seeded['run_id']}/part-0000"
    reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="generic",
        partition_path=part,
        shards={"shard-0000.bin": b"payload-bytes"},
    )
    comp2 = _manifest(tmp_path, part)["completeness"]
    assert comp2["complete"] is False
    assert comp2["absent"] == ["run_model_identity", "tokenization"]
    assert comp2["not_applicable"] == []
