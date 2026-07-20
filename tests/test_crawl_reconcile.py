"""The torn-blob reconcile (bundles/crawl.py; ops-hardening sweep 2026-07-19): a catalog-wide sweep that
re-verifies every LIVE artifact's committed sha256+size against the ACTUAL blob, COLLECTING (never raising on)
torn / missing / unresolved blobs — the offline analogue of reader.verify_artifact's per-read binding (#7
seam-clobber + #10 GC-orphan). Markers: pg. Seeds REAL artifacts through the W1-W8 registrar (the trusted
writer path), mirroring tests/test_reader.py.
"""

from __future__ import annotations

import pytest

from neuromancer_llm.bundles.crawl import BackendRef, reconcile_artifacts
from neuromancer_llm.bundles.registrar import BundleRegistrar, TableManifestSpec
from neuromancer_llm.capture.adapters.vllm import LogprobSample
from neuromancer_llm.capture.events import logprob_parquet
from neuromancer_llm.capture.reader import manifest_artifacts
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg


def _seed_artifact(repo, backend, backend_id, run_id, *, shard: str, generated: int) -> None:
    """Write ONE real logprob parquet shard + its table_manifests row through the registrar (trusted writer)."""
    pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(8))
    pq_bytes, n, _ = logprob_parquet(
        LogprobSample(prompt="p", generated_token_id=generated, top_logprobs=pairs)
    )
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    reg.register(
        run_id=run_id,
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=f"logprobs/run={run_id}/{shard}",
        shards={shard: pq_bytes},
        artifact_kinds={shard: "token_table"},
        table_manifests=[TableManifestSpec(shard, "logprobs", row_count=n)],
    )


def _setup(seeded, tmp_path):
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    _seed_artifact(repo, backend, backend_id, seeded["run_id"], shard="logprobs-0000.parquet", generated=1000)
    return repo, backend, backend_id


def test_reconcile_clean_when_blobs_match(seeded, tmp_path):
    repo, backend, _ = _setup(seeded, tmp_path)
    report = reconcile_artifacts(repo.engine, backend_for=lambda _ref: backend)
    assert report.clean and report.findings == ()
    assert report.checked == report.ok >= 1  # the invariant: every live artifact matched


def test_reconcile_flags_torn_blob(seeded, tmp_path):
    repo, backend, _ = _setup(seeded, tmp_path)
    # SAME-LENGTH, different-bytes overwrite so the sha256 clause (not the cheap size check) is what fires (C5).
    art = manifest_artifacts(repo.engine, run_id=seeded["run_id"])[0]
    original = backend.get(art.uri)
    backend.put(art.uri, bytes((b ^ 0xFF) for b in original))  # identical length, every byte flipped
    report = reconcile_artifacts(repo.engine, backend_for=lambda _ref: backend)
    torn = report.with_status("torn")
    assert not report.clean and len(torn) == 1 and torn[0].uri == art.uri
    assert report.ok == report.checked - 1


def test_reconcile_flags_missing_blob(seeded, tmp_path):
    repo, backend, _ = _setup(seeded, tmp_path)
    art = manifest_artifacts(repo.engine, run_id=seeded["run_id"])[0]
    backend.delete(art.uri)  # the blob is gone; the catalog row survives -> a MISSING finding
    report = reconcile_artifacts(repo.engine, backend_for=lambda _ref: backend)
    assert len(report.with_status("missing")) == 1 and report.ok == report.checked - 1


def test_reconcile_flags_unresolved_backend(seeded, tmp_path):
    repo, _, _ = _setup(seeded, tmp_path)

    def _boom(ref: BackendRef):
        raise RuntimeError(f"no credential for {ref.backend_key}")

    report = reconcile_artifacts(repo.engine, backend_for=_boom)
    # a backend that cannot be built makes ALL its artifacts 'unresolved' — collected, not a crash.
    assert len(report.with_status("unresolved")) == report.checked >= 1 and report.ok == 0


def test_reconcile_collects_none_return_backend_as_unresolved(seeded, tmp_path):
    # a backend_for that RETURNS None (rather than raising) for an unresolvable backend must ALSO be COLLECTED
    # as 'unresolved' — never a KeyError aborting the whole sweep (the dict.get-style seam; post-vet hardening).
    repo, _, _ = _setup(seeded, tmp_path)
    report = reconcile_artifacts(repo.engine, backend_for=lambda _ref: None)
    assert len(report.with_status("unresolved")) == report.checked >= 1 and report.ok == 0


def test_reconcile_survives_one_bad_blob_and_finds_the_rest(seeded, tmp_path):
    # the COLLECT-not-raise property: a torn blob does NOT abort the sweep; a second (matching) artifact still
    # verifies OK. Seed a SECOND artifact, tamper only the first.
    repo, backend, backend_id = _setup(seeded, tmp_path)
    _seed_artifact(repo, backend, backend_id, seeded["run_id"], shard="logprobs-0001.parquet", generated=1001)
    arts = manifest_artifacts(repo.engine, run_id=seeded["run_id"])
    original = backend.get(arts[0].uri)
    backend.put(arts[0].uri, bytes((b ^ 0xFF) for b in original))
    report = reconcile_artifacts(repo.engine, backend_for=lambda _ref: backend)
    assert report.checked == 2 and report.ok == 1 and len(report.with_status("torn")) == 1


def test_reconcile_excludes_deleted_artifacts(seeded, tmp_path):
    # a soft-deleted artifact's blob is intentionally gone (deletion RECORDED, phase0 Q6) -> the reconcile must
    # EXCLUDE it (its absence is expected, NOT a MISSING finding). Anchors the `deleted_at IS NULL` filter.
    from sqlalchemy import text

    repo, backend, _ = _setup(seeded, tmp_path)
    art = manifest_artifacts(repo.engine, run_id=seeded["run_id"])[0]
    backend.delete(art.uri)  # blob gone
    with repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.artifacts SET deleted_at = now() WHERE uri = :u"), {"u": art.uri})
    report = reconcile_artifacts(repo.engine, backend_for=lambda _ref: backend)
    # the only artifact is soft-deleted -> excluded -> nothing checked, nothing flagged (never a MISSING finding)
    assert report.checked == 0 and report.findings == ()
