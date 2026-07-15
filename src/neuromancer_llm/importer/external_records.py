"""Importer rank 5 — Layer 1: the faithful `external_records` writer (phase3-importer-spec.md D1/D2; ADR-0003/0047/0048).

`write_external_record` mirrors ONE text record into `external_records`: content-addressed, keep-first idempotent, and
DRIFT-LOUD on the D1 stamp. It is the first code in the system to write an `external_records` row.

Contract:
  * Natural key (D2): `source_pk = sha256(source_bytes)` hex — MINTED here (the caller cannot fake it); `source_table` =
    the corpus kind; `source_system` = `handle.source_system` (the closed vocab). `payload_text` = the byte-exact text
    (`source_bytes.decode('utf-8')`); `confidentiality` + `derived_by_predecessor` persist the D1 stamp (migration 0002).
  * TEXT ONLY, fail closed: non-UTF-8 `source_bytes`, and a NUL (0x00) inside otherwise-valid UTF-8 (Postgres `text`
    cannot store NUL), are REFUSED — a binary/NUL payload is register-in-place (rank 6), never a Layer-1 mirror. A
    manifest/index row for a binary corpus (path, sha256, shape) is itself TEXT/JSON and IS written through this writer;
    only the raw binary payload is refused.
  * IDEMPOTENCY = keep-first with RAISE-ON-DRIFT (the house ADR-0048 idiom, db/repository.py:185-233). The D2 key
    EXCLUDES the stamp, so the SAME bytes re-imported under a divergent `confidentiality`/`derived_by_predecessor`
    collide on the key: this raises `IdentityMismatchError` — a divergent re-grade must NOT silently keep-first (that
    reads CLEAN when it should read DIRTY, the D1 fail-open). An IDENTICAL re-scan is a no-op (`inserted=False`, same
    id). A re-scan whose BYTES changed gets a NEW `source_pk` (a new row); the old row stays. (`payload_jsonb` is a
    derived sidecar and is deliberately NOT in the drift check; `import_batch_id`/`imported_at` differ every scan and are
    never compared.)

Durability is NOT re-consulted here — the rank-4 gate `open_import_batch` consulted `assert_durability_ok` ONCE per
batch (readiness §4·3); a per-row consult is forbidden and structurally pinned (tests/redteam/test_rt_importer_no_cloud_put.py).
PRECONDITION: part of the admin-DSN importer flow. Keep-first is enforced by the ON CONFLICT DO NOTHING clause + ADR-0047
+ this drift guard; the `neuro_registrar` UPDATE-denial on this table is only a backstop that would bite if the writer
were ever run as `neuro_registrar` rather than on the admin DSN the importer actually uses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from ..db.repository import IdentityMismatchError
from .ingress import ImportBatchHandle, ImporterIngressError

if TYPE_CHECKING:
    from ..db.repository import Repository


@dataclass(frozen=True)
class ExternalRecordResult:
    """The outcome of one Layer-1 mirror write."""

    external_record_id: int
    source_pk: str  # sha256(source_bytes) hex — the minted content-addressed natural key (D2)
    inserted: bool  # True = new row; False = idempotent keep-first no-op (an identical re-scan)


_INSERT = text(
    "INSERT INTO neuro.external_records "
    "(import_batch_id, source_system, source_table, source_pk, payload_text, payload_jsonb, "
    " confidentiality, derived_by_predecessor) "
    "VALUES (:b, :ss, :st, :pk, :pt, :pj, :cf, :dp) "
    "ON CONFLICT (source_system, source_table, source_pk) DO NOTHING RETURNING external_record_id"
).bindparams(
    # none_as_null=True: a None sidecar → a SQL NULL (so `WHERE payload_jsonb IS NULL` finds no-sidecar rows),
    # NOT the jsonb scalar 'null' that JSONB()'s default (none_as_null=False) would store. dict → jsonb.
    bindparam("pj", type_=JSONB(none_as_null=True))
)

_RESELECT = text(
    "SELECT external_record_id, confidentiality, derived_by_predecessor FROM neuro.external_records "
    "WHERE source_system = :ss AND source_table = :st AND source_pk = :pk"
)


def write_external_record(
    repo: Repository,
    handle: ImportBatchHandle,
    *,
    source_table: str,
    source_bytes: bytes,
    derived_by_predecessor: bool,
    payload_jsonb: dict | None = None,
) -> ExternalRecordResult:
    """Mirror ONE text record into external_records through the Layer-1 writer (see the module docstring for the full
    contract). Requires an `ImportBatchHandle` from `open_import_batch` (not a raw id) so routing through the rank-4 gate
    stays structural. Returns an `ExternalRecordResult`. Raises `ImporterIngressError` on a non-text/NUL payload or an
    invalid `handle`/`derived_by_predecessor`, or `IdentityMismatchError` on a divergent-stamp re-import."""
    # Fail-closed input validation — a parameter annotation does NOT type-check at runtime (ingress.py:91-93), so these
    # isinstance guards are load-bearing: a raw id / a coercible string would otherwise be silently accepted or coerced.
    if not isinstance(handle, ImportBatchHandle):
        raise ImporterIngressError(
            "handle must be an ImportBatchHandle from open_import_batch — a raw id is refused (fail closed; "
            "coverage stays structural)."
        )
    if not isinstance(derived_by_predecessor, bool):
        raise ImporterIngressError(
            f"derived_by_predecessor must be a bool, got {derived_by_predecessor!r} — refusing (fail closed; a "
            "coercible string would silently persist an unintended per-record D1 marking)."
        )
    # TEXT-only boundary, fail closed BEFORE any DB work: non-UTF-8 OR a NUL (which Postgres text cannot hold) is a
    # register-in-place payload (rank 6), not a Layer-1 mirror. Uniform ImporterIngressError.
    try:
        payload_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImporterIngressError(
            "source_bytes are not valid UTF-8 — a binary payload is register-in-place (rank 6), not a Layer-1 text "
            "mirror (fail closed)."
        ) from exc
    if "\x00" in payload_text:
        raise ImporterIngressError(
            "source_bytes contain a NUL (0x00) — Postgres text cannot store it; a NUL-bearing payload is "
            "register-in-place (rank 6), not a Layer-1 mirror (fail closed)."
        )
    source_pk = hashlib.sha256(source_bytes).hexdigest()
    params = {
        "b": handle.import_batch_id,
        "ss": handle.source_system.value,
        "st": source_table,
        "pk": source_pk,
        "pt": payload_text,
        "pj": payload_jsonb,
        "cf": handle.confidentiality.value,
        "dp": derived_by_predecessor,
    }
    with repo.engine.begin() as conn:
        new_id = conn.execute(_INSERT, params).scalar_one_or_none()
        if new_id is not None:
            return ExternalRecordResult(int(new_id), source_pk, inserted=True)
        # keep-first: the natural key already exists. Re-SELECT and RAISE-ON-DRIFT on the D1 stamp — never silently
        # keep-first a divergent re-grade (ADR-0048/D1); an identical re-scan is an idempotent no-op.
        row = conn.execute(
            _RESELECT, {"ss": handle.source_system.value, "st": source_table, "pk": source_pk}
        ).one()
    if (
        row.confidentiality != handle.confidentiality.value
        or row.derived_by_predecessor != derived_by_predecessor
    ):
        raise IdentityMismatchError(
            f"external_records {source_pk[:12]}… already exists with a DIFFERENT stamp (stored "
            f"confidentiality={row.confidentiality!r}/derived_by_predecessor={row.derived_by_predecessor}; incoming "
            f"{handle.confidentiality.value!r}/{derived_by_predecessor}) — refusing to silently keep-first a divergent "
            "re-grade (fail loud; ADR-0048/D1). Reconcile deliberately, do not re-scan."
        )
    return ExternalRecordResult(int(row.external_record_id), source_pk, inserted=False)
