"""`db/run_report.py` — the FIRST read module of the library. A tested producer that turns one run into a
plain, frozen `RunReport` the `neuro runs show` delegate renders (ADR-0038 one-implementation-per-concept).

The read-side idiom, set here to be cited (the way rank 4 set the write-side one):

* A read is a MODULE-LEVEL FUNCTION over a verified engine, NOT a method on `Repository`. `Repository` is the
  fail-closed WRITE choke point (verify-once at construction, ADR-0006); a read needs no write path, only an
  already-verified engine, so it does not belong on the write object. The CLI hands us the engine
  `make_verified_engine` already identity-checked (lane / repo-pinned uuid), so this function inherits the
  guarantee without re-verifying — and it opens a read-only `connect()`, never the write-path `begin()`.
* v1 renders DB-RESIDENT FACTS ONLY: run identity + model identity (NULL-honest for an unlabeled/ADR-0036 run),
  per-run COUNTS, the input/config rows, the registered per-run metrics (ADR-0017), and storage POINTERS. There
  is NO artifact/parquet read-back — that is ADR-0009 territory (unverifiable in-band) and lives in
  `neuro capture show`, which reads the LOCAL lake parquet back under the manifest's sha256+size binding
  (capture/reader.py); reading a CLOUD-resident artifact BY BACKEND KEY is registered, not built. A pointer
  printed honestly beats a payload rendered unverifiably.
* PAYLOAD DISCIPLINE (EXAM_RESTRICTED): the capture side carries wire payloads — `capture_events.request_text` /
  `response_text` — that may hold restricted stimulus content. This module NEVER selects a payload column: events
  surface as COUNTS, `run_metrics.value_json` surfaces as a byte-length POINTER (`octet_length`, never its
  content), identity digests (fingerprint / tokenizer hashes) surface as BOUNDED hex prefixes, and storage
  pointers carry a backend key + uri, never bytes. The SQL is the mechanism — read it and see that no
  payload/content column is in any select list.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import Engine, text


class RunNotFoundError(Exception):
    """No run matches the given run_id / run_key. The CLI maps this to a clean fail-loud exit 1."""


@dataclass(frozen=True)
class RunModelIdentity:
    """The run's model identity, resolved fingerprint -> model_identities -> tokenizer_identities. Present ONLY
    when the run carries a fingerprint; an adhoc/unlabeled run (ADR-0036) has fingerprint_id NULL and this is
    `None` on the report.

    `tokenizer_hash_hex` is the TOKENIZER's own durable identity (`tokenizer_identities.tokenizer_hash`), which
    is a DIFFERENT registry row from the model: that table carries its own nullable `hf_repo`/`hf_revision`, so
    this digest says nothing about the model's hf label rendered beside it."""

    fingerprint_id: int
    fingerprint_hash_hex: str  # short hex prefix of the fingerprint hash (a pointer, not the config)
    declared_mode: str
    model_id: int
    hf_repo: str | None  # NULL for locally-trained / API-opaque models (ADR-0032)
    hf_revision: str | None
    dtype_quant: str
    serving_stack: str
    serving_version: str
    arch_family: str
    tokenizer_hash_hex: (
        str  # short hex prefix of tokenizer_identities.tokenizer_hash (a pointer, not the file)
    )


@dataclass(frozen=True)
class RunInputRef:
    """One `run_inputs` row as a pointer: its role and the single referent FK it binds (exactly one per row,
    DB-enforced by run_inputs_one_ref)."""

    role: str  # 'stimulus' | 'intervention' | 'asset'
    referent_kind: str  # 'prompt_set' | 'intervention_spec' | 'asset'
    referent_id: int


@dataclass(frozen=True)
class RunMetricRow:
    """One registered per-run metric (ADR-0017: metric_key is a vocabulary FK; value bounded). A scalar carries
    `value_num`; a json-kind metric carries ONLY its byte length (`value_json_bytes`) — the content is never
    read (payload discipline)."""

    metric_key: str
    value_num: float | None
    value_json_bytes: int | None  # octet_length(value_json); None for a scalar metric


@dataclass(frozen=True)
class RunStoragePointer:
    """One `table_manifests` row: a POINTER at a lake parquet the run produced (backend key + uri + row count).
    No read-back — the bytes are not fetched or verified here (ADR-0009)."""

    dataset_name: str
    backend_key: str
    uri: str
    row_count: int | None


@dataclass(frozen=True)
class RunReport:
    """One run's DB-resident provenance, ready to render. `model is None` and `is_unlabeled is True` are FACTS an
    adhoc run displays honestly (ADR-0036), never a crash."""

    # --- identity ---
    run_id: int
    run_key: str
    campaign_key: str
    actor_key: str
    actor_display_name: str
    actor_kind: str
    work_slug: str
    variant_digest: str
    run_kind: str
    origin: str
    lane: str  # the verified lane this report was read against (the run lives in this lane's DB by construction)
    is_unlabeled: bool
    invocation_id: (
        uuid.UUID | None
    )  # NULL = the canonical run; non-NULL = an explicit re-invocation (ADR-0005)
    created_at: dt.datetime
    finalized_at: dt.datetime | None
    status: str  # derived: 'finalized' when finalized_at is set, else 'in-progress'
    # --- model identity (None for an unlabeled/adhoc run) ---
    model: RunModelIdentity | None
    # --- per-run counts ---
    event_count: int
    spilled_event_count: (
        int  # capture_events whose payload spilled to an artifact (count only, never content)
    )
    input_count: int
    metric_count: int
    manifest_count: int
    # --- detail rows ---
    inputs: tuple[RunInputRef, ...]
    metrics: tuple[RunMetricRow, ...]
    storage_pointers: tuple[RunStoragePointer, ...]


# The head query: run identity + campaign + actor, LEFT JOIN fingerprint -> model -> tokenizer (all NULL for an
# unlabeled run). NO payload column is selected; `tokenizer_hash` is an identity DIGEST, not tokenizer content,
# and is bounded to a hex prefix before it is rendered. The WHERE clause is appended from one of two FIXED
# literals below (the value is always a bound param; the selector column is never interpolated).
_HEAD_SELECT = """
    SELECT r.run_id, r.run_key, r.work_slug, r.variant_digest, r.run_kind, r.origin,
           r.is_unlabeled, r.fingerprint_id, r.invocation_id, r.created_at, r.finalized_at,
           c.campaign_key,
           a.actor_key, a.display_name AS actor_display_name, a.kind AS actor_kind,
           f.fingerprint_hash, f.declared_mode,
           m.model_id, m.hf_repo, m.hf_revision, m.dtype_quant,
           m.serving_stack, m.serving_version, m.arch_family,
           t.tokenizer_hash
    FROM neuro.runs r
    JOIN neuro.campaigns c ON c.campaign_id = r.campaign_id
    JOIN neuro.actors a ON a.actor_id = r.actor_id
    LEFT JOIN neuro.fingerprints f ON f.fingerprint_id = r.fingerprint_id
    LEFT JOIN neuro.model_identities m ON m.model_id = f.model_id
    LEFT JOIN neuro.tokenizer_identities t ON t.tokenizer_id = m.tokenizer_id
"""
_HEAD_BY_ID = _HEAD_SELECT + " WHERE r.run_id = :sel"
_HEAD_BY_KEY = _HEAD_SELECT + " WHERE r.run_key = :sel"

# Five scalar counts in one round trip. `spilled_event_count` counts events with a spill FK set — a pointer count,
# NEVER the spilled bytes. octet_length/count are the only functions touching payload-bearing tables.
_COUNTS_SQL = """
    SELECT
      (SELECT count(*) FROM neuro.capture_events WHERE run_id = :id) AS event_count,
      (SELECT count(*) FROM neuro.capture_events
         WHERE run_id = :id
           AND (request_spill_artifact_id IS NOT NULL OR response_spill_artifact_id IS NOT NULL)
      ) AS spilled_event_count,
      (SELECT count(*) FROM neuro.run_inputs WHERE run_id = :id) AS input_count,
      (SELECT count(*) FROM neuro.run_metrics WHERE run_id = :id) AS metric_count,
      (SELECT count(*) FROM neuro.table_manifests WHERE run_id = :id) AS manifest_count
"""

_INPUTS_SQL = """
    SELECT role, prompt_set_id, asset_id, intervention_spec_id
    FROM neuro.run_inputs WHERE run_id = :id ORDER BY run_input_id
"""

# value_json surfaces ONLY as its byte length (octet_length) — the content is never selected (payload discipline).
_METRICS_SQL = """
    SELECT metric_key, value_num, octet_length(value_json) AS value_json_bytes
    FROM neuro.run_metrics WHERE run_id = :id ORDER BY metric_key
"""

_POINTERS_SQL = """
    SELECT tm.dataset_name, sb.backend_key, a.uri, tm.row_count
    FROM neuro.table_manifests tm
    JOIN neuro.artifacts a ON a.artifact_id = tm.artifact_id
    JOIN neuro.storage_backends sb ON sb.backend_id = a.backend_id
    WHERE tm.run_id = :id ORDER BY tm.table_manifest_id
"""


def _referent(row) -> tuple[str, int]:
    """Map a run_inputs row to (referent_kind, referent_id). Exactly one referent FK is set (run_inputs_one_ref
    is DB-enforced), so this is a total function over any valid row; an all-NULL row would be a schema breach and
    is surfaced loudly rather than silently defaulted."""
    if row["prompt_set_id"] is not None:
        return "prompt_set", int(row["prompt_set_id"])
    if row["intervention_spec_id"] is not None:
        return "intervention_spec", int(row["intervention_spec_id"])
    if row["asset_id"] is not None:
        return "asset", int(row["asset_id"])
    raise RuntimeError(  # unreachable given run_inputs_one_ref; loud, never a silent 'unknown:0'
        f"run_inputs row for role={row['role']!r} has no referent FK — schema invariant run_inputs_one_ref broken"
    )


def build_run_report(
    engine: Engine,
    *,
    run_id: int | None = None,
    run_key: str | None = None,
    lane: str = "canonical",
) -> RunReport:
    """Read one run into a `RunReport`. Pass EXACTLY ONE of `run_id` / `run_key` (the CLI enforces this first for
    a clean usage error; this guard keeps the library safe standalone). `lane` is the already-verified lane the
    caller's engine is on — recorded on the report as identity context, not re-verified here. Raises
    `RunNotFoundError` (mapped to exit 1 by the CLI) when no run matches. Read-only: opens `connect()`, selects no
    payload column."""
    if (run_id is None) == (run_key is None):
        raise ValueError("build_run_report requires exactly one of run_id / run_key")

    with engine.connect() as conn:
        if run_id is not None:
            head = conn.execute(text(_HEAD_BY_ID), {"sel": run_id}).mappings().one_or_none()
        else:
            head = conn.execute(text(_HEAD_BY_KEY), {"sel": run_key}).mappings().one_or_none()
        if head is None:
            sel = f"run_id={run_id}" if run_id is not None else f"run_key={run_key!r}"
            raise RunNotFoundError(f"no run matches {sel} on lane {lane!r}")

        rid = int(head["run_id"])
        counts = conn.execute(text(_COUNTS_SQL), {"id": rid}).mappings().one()
        input_rows = conn.execute(text(_INPUTS_SQL), {"id": rid}).mappings().all()
        metric_rows = conn.execute(text(_METRICS_SQL), {"id": rid}).mappings().all()
        pointer_rows = conn.execute(text(_POINTERS_SQL), {"id": rid}).mappings().all()

    model: RunModelIdentity | None = None
    if head["fingerprint_id"] is not None:
        model = RunModelIdentity(
            fingerprint_id=int(head["fingerprint_id"]),
            fingerprint_hash_hex=bytes(head["fingerprint_hash"]).hex()[:16],
            declared_mode=str(head["declared_mode"]),
            model_id=int(head["model_id"]),
            hf_repo=head["hf_repo"],
            hf_revision=head["hf_revision"],
            dtype_quant=str(head["dtype_quant"]),
            serving_stack=str(head["serving_stack"]),
            serving_version=str(head["serving_version"]),
            arch_family=str(head["arch_family"]),
            # Unconditional, exactly like model_id above: fingerprints.model_id and model_identities.tokenizer_id
            # are BOTH `NOT NULL REFERENCES`, so a non-NULL fingerprint_id guarantees this join resolved. A
            # None-arm here would be an unreachable branch no fixture could redden (precedent 14).
            tokenizer_hash_hex=bytes(head["tokenizer_hash"]).hex()[:16],
        )

    inputs = tuple(
        RunInputRef(role=str(r["role"]), referent_kind=k, referent_id=i)
        for r in input_rows
        for (k, i) in (_referent(r),)
    )
    metrics = tuple(
        RunMetricRow(
            metric_key=str(r["metric_key"]),
            value_num=None if r["value_num"] is None else float(r["value_num"]),
            value_json_bytes=None if r["value_json_bytes"] is None else int(r["value_json_bytes"]),
        )
        for r in metric_rows
    )
    pointers = tuple(
        RunStoragePointer(
            dataset_name=str(r["dataset_name"]),
            backend_key=str(r["backend_key"]),
            uri=str(r["uri"]),
            row_count=None if r["row_count"] is None else int(r["row_count"]),
        )
        for r in pointer_rows
    )

    finalized_at = head["finalized_at"]
    return RunReport(
        run_id=rid,
        run_key=str(head["run_key"]),
        campaign_key=str(head["campaign_key"]),
        actor_key=str(head["actor_key"]),
        actor_display_name=str(head["actor_display_name"]),
        actor_kind=str(head["actor_kind"]),
        work_slug=str(head["work_slug"]),
        variant_digest=str(head["variant_digest"]),
        run_kind=str(head["run_kind"]),
        origin=str(head["origin"]),
        lane=lane,
        is_unlabeled=bool(head["is_unlabeled"]),
        invocation_id=head["invocation_id"],
        created_at=head["created_at"],
        finalized_at=finalized_at,
        status="finalized" if finalized_at is not None else "in-progress",
        model=model,
        event_count=int(counts["event_count"]),
        spilled_event_count=int(counts["spilled_event_count"]),
        input_count=int(counts["input_count"]),
        metric_count=int(counts["metric_count"]),
        manifest_count=int(counts["manifest_count"]),
        inputs=inputs,
        metrics=metrics,
        storage_pointers=pointers,
    )
