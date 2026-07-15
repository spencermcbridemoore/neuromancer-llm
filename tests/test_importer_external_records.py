"""Importer rank 5 — Layer 1: the external_records writer + migration 0002 probes (importer/external_records.py).

The writer content-addresses the natural key (source_pk = sha256(source_bytes)), byte-preserves the text payload,
persists the D1 stamp (migration 0002 columns confidentiality + derived_by_predecessor), and is keep-first idempotent
with RAISE-ON-DRIFT on a divergent re-grade (the house ADR-0048 idiom). All probes run on the shared `repo` fixture:
it is head-built (alembic upgrade head → carries 0002) and expected_lane='test' (so open_import_batch skips the
canonical durability consult — no canonical fixture needed). TEXT-only refusals and the DB-enforced NOT-NULL/CHECK belts
are exercised directly.
"""

from __future__ import annotations

import hashlib
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.importer.external_records import ExternalRecordResult, write_external_record
from neuromancer_llm.importer.ingress import (
    Confidentiality,
    ImportBatchHandle,
    ImporterIngressError,
    SourceSystem,
    open_import_batch,
)

pytestmark = pytest.mark.pg


def _open(repo, source_system: SourceSystem, confidentiality: Confidentiality) -> ImportBatchHandle:
    return open_import_batch(repo, source_system=source_system, confidentiality=confidentiality)


def _count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM neuro.external_records")).scalar_one()


# --- (D2) content-addressed natural key + the persisted D1 stamp ---------------------------------


def test_write_mints_content_addressed_key(repo):
    handle = _open(repo, SourceSystem.STUDY_QUERY_LLM, Confidentiality.EXAM_RESTRICTED)
    body = b'{"q": "capital of France?", "a": "Paris"}'
    result = write_external_record(
        repo,
        handle,
        source_table="mcq_answer_positions",
        source_bytes=body,
        derived_by_predecessor=True,
        payload_jsonb={"kind": "mcq"},
    )
    assert isinstance(result, ExternalRecordResult)
    assert result.inserted is True
    assert result.source_pk == hashlib.sha256(body).hexdigest()  # MINTED from the bytes, not fakeable
    with repo.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT import_batch_id, source_system, source_table, source_pk, payload_text, confidentiality, "
                "derived_by_predecessor FROM neuro.external_records WHERE external_record_id = :i"
            ),
            {"i": result.external_record_id},
        ).one()
    assert row.import_batch_id == handle.import_batch_id
    assert row.source_system == "study-query-llm"
    assert row.source_table == "mcq_answer_positions"
    assert row.source_pk == result.source_pk
    assert row.payload_text == body.decode("utf-8")  # byte-exact
    assert row.confidentiality == "exam_restricted"  # from the handle, NOT defaulted-clean
    assert row.derived_by_predecessor is True


# --- keep-first idempotency + RAISE-ON-DRIFT (the fold-1 fail-open guard) --------------------------


def test_write_identical_rescan_is_idempotent_noop(repo):
    body = b"an idempotent record"
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    r1 = write_external_record(
        repo,
        handle,
        source_table="found",
        source_bytes=body,
        derived_by_predecessor=False,
        payload_jsonb={"kind": "x"},
    )
    r2 = write_external_record(
        repo,
        handle,
        source_table="found",
        source_bytes=body,
        derived_by_predecessor=False,
        payload_jsonb={"kind": "x"},
    )
    assert r1.inserted is True
    assert r2.inserted is False  # keep-first no-op (a DO UPDATE ... RETURNING would yield inserted=True)
    assert r2.external_record_id == r1.external_record_id
    assert _count(repo.engine) == 1


def test_write_divergent_regrade_raises(repo):
    body = b"the same source record bytes"
    h1 = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    r1 = write_external_record(
        repo, h1, source_table="found", source_bytes=body, derived_by_predecessor=False
    )
    assert r1.inserted is True
    # SAME bytes + source_table (⇒ same natural key, which excludes the stamp) under a STRICTER grade must fail LOUD,
    # never silently keep the first 'open' (the D1 gap-reads-clean fail-open).
    h2 = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.EXAM_RESTRICTED)
    with pytest.raises(IdentityMismatchError):
        write_external_record(repo, h2, source_table="found", source_bytes=body, derived_by_predecessor=False)
    with repo.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT confidentiality FROM neuro.external_records WHERE source_pk = :pk"),
            {"pk": r1.source_pk},
        ).scalar_one()
    assert stored == "open"  # the FIRST grade stands; the divergent re-grade did not overwrite it
    assert _count(repo.engine) == 1


def test_write_divergent_derived_flag_raises(repo):
    body = b"same bytes, different derived flag"
    h = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    write_external_record(repo, h, source_table="found", source_bytes=body, derived_by_predecessor=False)
    with pytest.raises(IdentityMismatchError):  # derived_by_predecessor drift also fails loud
        write_external_record(repo, h, source_table="found", source_bytes=body, derived_by_predecessor=True)
    assert _count(repo.engine) == 1


def test_write_changed_bytes_is_a_new_row(repo):
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    r1 = write_external_record(
        repo, handle, source_table="found", source_bytes=b"first", derived_by_predecessor=False
    )
    r2 = write_external_record(
        repo, handle, source_table="found", source_bytes=b"second", derived_by_predecessor=False
    )
    assert r1.inserted is True and r2.inserted is True
    assert r1.source_pk != r2.source_pk  # a changed payload gets a NEW content-addressed key
    assert r1.external_record_id != r2.external_record_id
    assert _count(repo.engine) == 2  # ...and a NEW row; the old row stays (a re-scan cannot rewrite a mirror)


# --- TEXT-only, fail-closed input validation (register-in-place is rank 6) -------------------------


def test_write_refuses_non_utf8_binary(repo):
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    with pytest.raises(ImporterIngressError):
        write_external_record(
            repo, handle, source_table="found", source_bytes=b"\xff\xfe\x01\x02", derived_by_predecessor=False
        )
    assert _count(repo.engine) == 0  # refused BEFORE the txn opens


def test_write_refuses_nul_in_text(repo):
    # a NUL (0x00) is VALID UTF-8 but Postgres text cannot hold it — the writer must refuse cleanly (uniform
    # ImporterIngressError), not let a raw driver DataError leak out.
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    with pytest.raises(ImporterIngressError):
        write_external_record(
            repo,
            handle,
            source_table="found",
            source_bytes=b"valid text\x00more",
            derived_by_predecessor=False,
        )
    assert _count(repo.engine) == 0


def test_write_refuses_raw_id_handle(repo):
    _open(
        repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN
    )  # a real batch exists; but we pass a forged/raw handle
    for bad in (1, None, "not-a-handle"):
        with pytest.raises(ImporterIngressError):
            write_external_record(
                repo,
                cast(ImportBatchHandle, bad),
                source_table="found",
                source_bytes=b"x",
                derived_by_predecessor=False,
            )
    assert _count(repo.engine) == 0


def test_write_refuses_non_bool_derived(repo):
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    with pytest.raises(ImporterIngressError):
        write_external_record(
            repo, handle, source_table="found", source_bytes=b"x", derived_by_predecessor=cast(bool, "no")
        )
    assert _count(repo.engine) == 0


# --- the payload_jsonb sidecar (the repo's first jsonb write) --------------------------------------


def test_write_payload_jsonb_round_trips(repo):
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    sidecar = {"kind": "mcq", "nested": {"a": 1, "b": [2, 3]}}
    r = write_external_record(
        repo,
        handle,
        source_table="found",
        source_bytes=b"with sidecar",
        derived_by_predecessor=False,
        payload_jsonb=sidecar,
    )
    with repo.engine.connect() as conn:
        stored, kind = conn.execute(
            text(
                "SELECT payload_jsonb, payload_jsonb->>'kind' FROM neuro.external_records WHERE external_record_id = :i"
            ),
            {"i": r.external_record_id},
        ).one()
    assert stored == sidecar  # real jsonb round-trips to a dict (not a stringified blob)
    assert kind == "mcq"  # the functional-index expression external_records_kind_idx is queryable
    r2 = write_external_record(
        repo, handle, source_table="found", source_bytes=b"no sidecar", derived_by_predecessor=False
    )
    with repo.engine.connect() as conn:
        is_sql_null, typeof = conn.execute(
            text(
                "SELECT payload_jsonb IS NULL, jsonb_typeof(payload_jsonb) FROM neuro.external_records "
                "WHERE external_record_id = :i"
            ),
            {"i": r2.external_record_id},
        ).one()
    # None must be a SQL NULL, NOT a jsonb 'null' scalar: a consumer's `WHERE payload_jsonb IS NULL` must find the
    # no-sidecar row, and jsonb_typeof(SQL NULL) is NULL (a jsonb 'null' would report 'null'). NOTE: a plain
    # `assert val is None` is VACUOUS here — psycopg deserializes jsonb 'null' to Python None too — so this
    # SQL-level distinction is the load-bearing check (post-build vet, 2026-07-15).
    assert is_sql_null is True
    assert typeof is None


# --- migration 0002: the DB-enforced NOT-NULL + CHECK belt (option B, the "gap reads DIRTY" second belt) ---


def test_confidentiality_not_null_db_enforced(repo):
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    base = {"b": handle.import_batch_id, "ss": "local-fs", "st": "found", "pt": "payload", "dp": False}
    # a direct INSERT that omits ONLY confidentiality (every other NOT-NULL col valid) is refused by the DB
    with pytest.raises(IntegrityError), repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.external_records (import_batch_id, source_system, source_table, source_pk, "
                "payload_text, derived_by_predecessor) VALUES (:b, :ss, :st, :pk, :pt, :dp)"
            ),
            {**base, "pk": "pk_no_conf"},
        )
    # positive control: the SAME INSERT WITH a valid grade succeeds (proves the row is otherwise legal) — distinct pk
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.external_records (import_batch_id, source_system, source_table, source_pk, "
                "payload_text, confidentiality, derived_by_predecessor) VALUES (:b, :ss, :st, :pk, :pt, :cf, :dp)"
            ),
            {**base, "pk": "pk_with_conf", "cf": "open"},
        )
    assert _count(repo.engine) == 1


def test_confidentiality_check_rejects_out_of_vocab(repo):
    handle = _open(repo, SourceSystem.LOCAL_FS, Confidentiality.OPEN)
    base = {"b": handle.import_batch_id, "ss": "local-fs", "st": "found", "pt": "payload", "dp": False}
    ins = text(
        "INSERT INTO neuro.external_records (import_batch_id, source_system, source_table, source_pk, payload_text, "
        "confidentiality, derived_by_predecessor) VALUES (:b, :ss, :st, :pk, :pt, :cf, :dp)"
    )
    with pytest.raises(IntegrityError), repo.engine.begin() as conn:  # out-of-vocab -> CHECK violation
        conn.execute(ins, {**base, "pk": "pk_bogus", "cf": "bogus"})
    # every real vocab member is accepted (DISTINCT source_pk so the UNIQUE never masks the CHECK)
    members = list(Confidentiality)
    for i, member in enumerate(members):
        with repo.engine.begin() as conn:
            conn.execute(ins, {**base, "pk": f"pk_ok_{i}", "cf": member.value})
    assert _count(repo.engine) == len(members)


def test_0002_columns_present_and_typed(repo):
    with repo.engine.connect() as conn:
        cols = dict(
            conn.execute(
                text(
                    "SELECT column_name, data_type || '/' || is_nullable FROM information_schema.columns "
                    "WHERE table_schema='neuro' AND table_name='external_records' "
                    "AND column_name IN ('confidentiality', 'derived_by_predecessor')"
                )
            ).all()
        )
        check = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                "JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='neuro' AND t.relname='external_records' "
                "AND c.contype='c' AND c.conname='external_records_confidentiality_vocab'"
            )
        ).scalar_one()
    assert cols == {"confidentiality": "text/NO", "derived_by_predecessor": "boolean/NO"}
    assert check == 1  # the named closed-vocab CHECK exists
