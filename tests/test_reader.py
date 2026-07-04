"""The READ + VERIFY half (export discipline): a SELECT-only consumer (neuro_reader) reconstructs captured
logprobs from the lake via table_manifests/artifacts, integrity-verifies (sha256+size) against the manifest,
and DuckDB-reads the parquet from the lake by uri. The integrity binding fails loud on a tampered blob.

Markers: pg (real Postgres). The reader-role tests also provision cluster roles (provisioned_roles).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from neuromancer_llm.bundles.registrar import BundleRegistrar, TableManifestSpec
from neuromancer_llm.capture.adapters.vllm import LogprobSample
from neuromancer_llm.capture.events import logprob_parquet
from neuromancer_llm.capture.reader import IntegrityError, manifest_artifacts, read_run_logprobs
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg


def _sample(n: int = 20, generated: int = 1000) -> LogprobSample:
    pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(n))  # descending; argmax = token 1000
    return LogprobSample(prompt="p", generated_token_id=generated, top_logprobs=pairs)


def _write_logprob_bundle(repo, backend, backend_id, run_id, sample) -> tuple[int, int]:
    """Write one real logprob parquet shard through the W1-W8 registrar with its table_manifests row
    (as the trusted writer path); returns (bundle_id, row_count). The read path then consumes it."""
    pq_bytes, n, _ = logprob_parquet(sample)
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    shard = "logprobs-0000.parquet"
    bundle_id = reg.register(
        run_id=run_id,
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=f"logprobs/run={run_id}/part-0000",
        shards={shard: pq_bytes},
        artifact_kinds={shard: "token_table"},
        table_manifests=[TableManifestSpec(shard, "logprobs", row_count=n)],
    )
    return bundle_id, n


def test_read_run_logprobs_as_reader_role(seeded, provisioned_roles, role_url, tmp_path):
    """A neuro_reader (SELECT-only) connection reconstructs the captured logprobs: locate via the manifest,
    integrity-verify, DuckDB-read the generated token. Proves a consumer with only SELECT on PG + read on the
    lake reconstructs the data — no write grant involved."""
    repo = seeded["repo"]
    run_id = seeded["run_id"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    _, n = _write_logprob_bundle(repo, backend, backend_id, run_id, _sample(20, generated=1000))

    # the consumer connects as neuro_reader (SELECT-only) — a DISTINCT engine, no write grant
    reader_engine = create_engine(role_url(provisioned_roles, "neuro_reader"), future=True)
    try:
        result = read_run_logprobs(reader_engine, backend, run_id=run_id, dataset_name="logprobs")
    finally:
        reader_engine.dispose()

    assert result.integrity_verified == 1 == len(result.artifacts)
    art = result.artifacts[0]
    assert art.kind == "token_table" and art.row_count == n == 20
    assert result.total_rows == 20
    # the generated token is the argmax (token 1000) with its exact logprob round-tripped through parquet
    assert result.generated_token_id == 1000
    assert result.generated_logprob == -0.01


def test_read_integrity_fails_loud_on_tampered_blob(seeded, tmp_path):
    """The sha256+size binding: if the lake bytes diverge from the manifest's recorded artifact, the read
    raises IntegrityError rather than returning corrupted data (phase0 Q6)."""
    repo = seeded["repo"]
    run_id = seeded["run_id"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    _write_logprob_bundle(repo, backend, backend_id, run_id, _sample(8))

    # tamper the lake file with a SAME-LENGTH, different-bytes overwrite so the size check passes and the
    # sha256 clause is the one that actually fires (Panel #1 C5: the old test changed size and never
    # exercised the sha256 half — a refactor dropping it would have passed).
    arts = manifest_artifacts(repo.engine, run_id=run_id)
    original = backend.get(arts[0].uri)
    tampered = bytes((b ^ 0xFF) for b in original)  # identical length, every byte flipped
    assert len(tampered) == len(original) and tampered != original
    backend.put(arts[0].uri, tampered)

    with pytest.raises(IntegrityError):
        read_run_logprobs(repo.engine, backend, run_id=run_id)


def test_read_missing_manifest_raises(seeded, tmp_path):
    from neuromancer_llm.db.lanes import ConfigurationError

    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    with pytest.raises(ConfigurationError):
        read_run_logprobs(repo.engine, backend, run_id=seeded["run_id"], dataset_name="logprobs")
