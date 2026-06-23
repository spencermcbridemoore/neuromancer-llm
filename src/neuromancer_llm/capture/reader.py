"""capture/reader.py — the READ + VERIFY half of the logprob slice (export discipline; phase0 Q12).

A SELECT-only consumer (neuro_reader) reconstructs the captured logprobs from the lake WITHOUT holding any
write grant: it locates the parquet via table_manifests + artifacts for a run, integrity-verifies each blob
against the manifest's recorded sha256 + size (a binding — phase0 Q6 / ADR-0008), and DuckDB-reads it from
the lake by uri. The DB holds pointers + integrity; the bytes live in the lake. This is the consumer side
of capture/events.py: nothing here writes, and the reader role cannot (tests/test_security.py pins that).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from sqlalchemy import Engine, text

from ..db.identity import sha256_bytes
from ..db.lanes import ConfigurationError
from ..storage.backends import StorageBackend


class IntegrityError(RuntimeError):
    """Lake bytes diverged from the manifest's recorded sha256/size — fail loud (the read-time binding,
    phase0 Q6). The artifact row's sha256 + size_bytes are authoritative; the blob MUST match them."""


@dataclass(frozen=True)
class ManifestArtifact:
    """One table_manifests row joined to its parquet artifact (the read path's unit)."""

    table_manifest_id: int
    dataset_name: str
    partition_path: str
    uri: str
    sha256: bytes
    size_bytes: int
    row_count: int | None
    kind: str


@dataclass(frozen=True)
class LogprobReadResult:
    """What a SELECT-only consumer reconstructed: the integrity-verified manifest artifacts + the generated
    token's logprob read back from the lake parquet via DuckDB."""

    run_id: int
    dataset_name: str
    artifacts: tuple[ManifestArtifact, ...]
    integrity_verified: int
    queried_uri: str
    generated_token_id: int
    generated_logprob: float
    total_rows: int


def manifest_artifacts(
    engine: Engine, *, run_id: int, dataset_name: str | None = None
) -> list[ManifestArtifact]:
    """SELECT the table_manifests rows for a run (optionally one dataset) joined to their parquet artifacts.
    A pure SELECT — runnable by the neuro_reader role (SELECT-only); uri/sha256/size are the read bindings."""
    sql = (
        "SELECT tm.table_manifest_id, tm.dataset_name, tm.partition_path, tm.row_count, "
        "a.uri, a.sha256, a.size_bytes, a.kind "
        "FROM neuro.table_manifests tm JOIN neuro.artifacts a ON a.artifact_id = tm.artifact_id "
        "WHERE tm.run_id = :r"
    )
    params: dict[str, object] = {"r": run_id}
    if dataset_name is not None:
        sql += " AND tm.dataset_name = :d"
        params["d"] = dataset_name
    sql += " ORDER BY a.uri"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [
        ManifestArtifact(
            table_manifest_id=row["table_manifest_id"],
            dataset_name=row["dataset_name"],
            partition_path=row["partition_path"],
            uri=row["uri"],
            sha256=bytes(row["sha256"]),
            size_bytes=row["size_bytes"],
            row_count=row["row_count"],
            kind=row["kind"],
        )
        for row in rows
    ]


def verify_artifact(backend: StorageBackend, artifact: ManifestArtifact) -> bytes:
    """Fetch the blob and verify it against the manifest's recorded sha256 + size (the binding). Raise
    IntegrityError on any mismatch — a consumer never trusts lake bytes that disagree with the catalog."""
    blob = backend.get(artifact.uri)
    if len(blob) != artifact.size_bytes or sha256_bytes(blob) != artifact.sha256:
        raise IntegrityError(
            f"lake artifact {artifact.uri} failed integrity: got {len(blob)}B "
            f"sha256={sha256_bytes(blob).hex()[:12]}, manifest says {artifact.size_bytes}B "
            f"sha256={artifact.sha256.hex()[:12]} (phase0 Q6 binding)."
        )
    return blob


def _duckdb_generated_row(blob: bytes) -> tuple[int, float, int]:
    """DuckDB-query the ALREADY-VERIFIED in-memory parquet bytes — NOT a re-read from disk (C6: closes the
    TOCTOU, the bytes queried are provably the bytes whose sha256+size were verified). Backend-agnostic:
    the blob came from backend.get(uri), so any lake (local_fs or Azure) works. Returns the generated
    token's (token_id, logprob) + the candidate row count."""
    import duckdb
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(blob))
    con = duckdb.connect()
    try:
        con.register("shard", table)
        row = con.execute(
            "SELECT token_id, logprob FROM shard WHERE is_generated ORDER BY rank LIMIT 1"
        ).fetchone()
        total = con.execute("SELECT count(*) FROM shard").fetchone()
    finally:
        con.close()
    if row is None:
        raise IntegrityError("parquet shard has no is_generated row (expected exactly one).")
    return int(row[0]), float(row[1]), int(total[0] if total is not None else 0)


def read_run_logprobs(
    engine: Engine, backend: StorageBackend, *, run_id: int, dataset_name: str = "logprobs"
) -> LogprobReadResult:
    """Reconstruct a run's captured logprobs as a SELECT-only consumer: locate the parquet via
    table_manifests/artifacts, integrity-verify EVERY artifact against the manifest (sha256+size binding),
    then DuckDB-read the token_table shard from the lake by uri and return the generated token's logprob.

    `engine` should be a SELECT-only (neuro_reader) connection — the read path holds no write grant; the
    integrity verify is the in-code binding. Raises if no manifest exists or integrity fails (fail loud).
    """
    artifacts = manifest_artifacts(engine, run_id=run_id, dataset_name=dataset_name)
    if not artifacts:
        raise ConfigurationError(
            f"no table_manifests row for run {run_id} dataset {dataset_name!r} — nothing to read."
        )
    token_tables = [a for a in artifacts if a.kind == "token_table"]
    target = token_tables[0] if token_tables else artifacts[0]
    target_blob = b""
    for artifact in artifacts:
        blob = verify_artifact(backend, artifact)  # the binding on EVERY artifact, fail loud
        if artifact is target:
            target_blob = blob  # query the VERIFIED bytes (C6), not a second disk read
    tid, lp, total = _duckdb_generated_row(target_blob)
    return LogprobReadResult(
        run_id=run_id,
        dataset_name=dataset_name,
        artifacts=tuple(artifacts),
        integrity_verified=len(artifacts),
        queried_uri=target.uri,
        generated_token_id=tid,
        generated_logprob=lp,
        total_rows=total,
    )
