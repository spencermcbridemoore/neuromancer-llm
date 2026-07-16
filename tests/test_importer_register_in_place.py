"""Importer rank 6 — register-in-place probes (importer/register_in_place.py).

register_artifact_in_place records an existing local binary file as a standalone artifacts POINTER row against a
dedicated non-cloud 'local-corpus' backend — no copy, no cloud .put. The headline is HONEST ACCOUNTING: the registered
bytes never enter any R3 cloud budget SUM. All probes run on the head-built shared `repo` fixture (expected_lane='test',
so open_import_batch skips the canonical durability consult — no canonical fixture needed); files come from tmp_path.
"""

from __future__ import annotations

import hashlib
from typing import cast

import pytest
from sqlalchemy import text

from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.importer.ingress import (
    Confidentiality,
    ImportBatchHandle,
    ImporterIngressError,
    SourceSystem,
    open_import_batch,
)
from neuromancer_llm.importer.register_in_place import (
    LOCAL_CORPUS_BACKEND_KEY,
    RegisteredArtifact,
    register_artifact_in_place,
)
from neuromancer_llm.storage.quota import QuotaDeniedError, budget_group_for_prefix, usage_bytes

pytestmark = pytest.mark.pg


def _open(repo) -> ImportBatchHandle:
    return open_import_batch(repo, source_system=SourceSystem.LOCAL_FS, confidentiality=Confidentiality.OPEN)


def _write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _artifact_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM neuro.artifacts")).scalar_one()


def _backend_count(engine, key: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM neuro.storage_backends WHERE backend_key = :k"), {"k": key}
        ).scalar_one()


# --- the pointer row + the content-addressed integrity record -------------------------------------


def test_registers_pointer(repo, tmp_path):
    handle = _open(repo)
    data = b'{"logprobs": [-0.59, -1.2], "run": 1}'
    f = _write(tmp_path, "lp.parquet", data)
    r = register_artifact_in_place(repo, handle, source_path=f, uri="mcq-logprobs/run=1/lp.parquet")
    assert isinstance(r, RegisteredArtifact)
    assert r.registered is True
    assert r.sha256_hex == hashlib.sha256(data).hexdigest()
    assert r.size_bytes == len(data)
    with repo.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT a.kind, a.uri, a.sha256, a.size_bytes, a.bundle_id, a.retention, b.backend_key "
                "FROM neuro.artifacts a JOIN neuro.storage_backends b ON b.backend_id = a.backend_id "
                "WHERE a.artifact_id = :i"
            ),
            {"i": r.artifact_id},
        ).one()
    assert row.kind == "external_record"  # the default pre-cut seam
    assert row.uri == "mcq-logprobs/run=1/lp.parquet"
    assert bytes(row.sha256) == hashlib.sha256(data).digest()  # bytea compare (not hex)
    assert row.size_bytes == len(data)
    assert row.bundle_id is None  # standalone pointer, no bundle
    assert row.retention == "keep_forever"  # the owner-ruled default (NOT the DDL 'ttl' default)
    assert row.backend_key == LOCAL_CORPUS_BACKEND_KEY


def test_backend_is_non_cloud_and_distinct(repo, tmp_path):
    handle = _open(repo)
    register_artifact_in_place(repo, handle, source_path=_write(tmp_path, "e.arr", b"vec"), uri="emb/e.arr")
    with repo.engine.connect() as conn:
        row = conn.execute(
            text("SELECT driver, is_cloud, backend_key FROM neuro.storage_backends WHERE backend_key = :k"),
            {"k": LOCAL_CORPUS_BACKEND_KEY},
        ).one()
    assert row.driver == "local_fs"
    assert row.is_cloud is False
    assert row.backend_key == LOCAL_CORPUS_BACKEND_KEY
    assert LOCAL_CORPUS_BACKEND_KEY != "local-lake"  # a DISTINCT key from the capture lake (not conflated)


# --- THE ACCOUNTING PROBE: registered bytes never enter any cloud budget SUM ----------------------


def test_registered_bytes_never_enter_a_budget_group(repo, tmp_path):
    handle = _open(repo)
    data = b"x" * 4096
    r = register_artifact_in_place(
        repo, handle, source_path=_write(tmp_path, "c.parquet", data), uri="c/c.parquet"
    )
    # Resolve the backend the register ACTUALLY used, then assert BOTH: the bytes landed on it, AND it is structurally
    # un-budgeted (in NO R3 cloud budget group) — so those bytes can never be summed against a cloud $ ceiling. (Keyed
    # on the register's real backend, not a red-herring sibling: a mutation that targets a budgeted backend reddens the
    # budget_group_for_prefix assertion below.)
    with repo.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT b.backend_key, b.backend_id FROM neuro.artifacts a "
                "JOIN neuro.storage_backends b ON b.backend_id = a.backend_id WHERE a.artifact_id = :i"
            ),
            {"i": r.artifact_id},
        ).one()
        assert usage_bytes(conn, [row.backend_id]) == len(
            data
        )  # the bytes DID land on this (non-cloud) backend
    with pytest.raises(QuotaDeniedError):
        budget_group_for_prefix(
            row.backend_key
        )  # ...which is in NO cloud budget group (structurally un-consultable)


# --- idempotency (keep-first) + backend idempotency + raise-on-drift -------------------------------


def test_register_is_idempotent_keep_first(repo, tmp_path):
    handle = _open(repo)
    f = _write(tmp_path, "f.parquet", b"same bytes")
    r1 = register_artifact_in_place(repo, handle, source_path=f, uri="f/f.parquet")
    r2 = register_artifact_in_place(repo, handle, source_path=f, uri="f/f.parquet")
    assert r1.registered is True
    assert r2.registered is False  # keep-first no-op
    assert r2.artifact_id == r1.artifact_id
    assert _artifact_count(repo.engine) == 1


def test_backend_idempotent_single_row(repo, tmp_path):
    handle = _open(repo)
    r1 = register_artifact_in_place(repo, handle, source_path=_write(tmp_path, "a", b"aaa"), uri="a")
    r2 = register_artifact_in_place(repo, handle, source_path=_write(tmp_path, "b", b"bbb"), uri="b")
    # two DIFFERENT files share ONE local-corpus backend row (never re-minted)
    with repo.engine.connect() as conn:
        be1 = conn.execute(
            text("SELECT backend_id FROM neuro.artifacts WHERE artifact_id = :i"), {"i": r1.artifact_id}
        ).scalar_one()
        be2 = conn.execute(
            text("SELECT backend_id FROM neuro.artifacts WHERE artifact_id = :i"), {"i": r2.artifact_id}
        ).scalar_one()
    assert be1 == be2
    assert _backend_count(repo.engine, LOCAL_CORPUS_BACKEND_KEY) == 1


def test_divergent_bytes_same_uri_raises(repo, tmp_path):
    handle = _open(repo)
    r1 = register_artifact_in_place(
        repo, handle, source_path=_write(tmp_path, "v1", b"first bytes"), uri="v/v"
    )
    # SAME uri, DIFFERENT bytes → a divergent re-register must fail loud, never silently keep-first
    with pytest.raises(IdentityMismatchError):
        register_artifact_in_place(
            repo, handle, source_path=_write(tmp_path, "v2", b"different bytes!!"), uri="v/v"
        )
    with repo.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT sha256, size_bytes FROM neuro.artifacts WHERE artifact_id = :i"),
            {"i": r1.artifact_id},
        ).one()
    assert (
        bytes(stored.sha256) == hashlib.sha256(b"first bytes").digest()
    )  # the FIRST bytes stand (DO NOTHING)
    assert stored.size_bytes == len(b"first bytes")
    assert _artifact_count(repo.engine) == 1


def test_multichunk_streamed_hash(repo, tmp_path):
    handle = _open(repo)
    data = b"neuromancer" * (150 * 1024)  # ~1.6 MiB → ≥2 read chunks, exercises the accumulation loop
    assert len(data) > 1024 * 1024
    r = register_artifact_in_place(
        repo, handle, source_path=_write(tmp_path, "big.parquet", data), uri="big/big.parquet"
    )
    assert r.sha256_hex == hashlib.sha256(data).hexdigest()
    assert r.size_bytes == len(data)
    with repo.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT sha256, size_bytes FROM neuro.artifacts WHERE artifact_id = :i"),
            {"i": r.artifact_id},
        ).one()
    assert bytes(stored.sha256) == hashlib.sha256(data).digest()
    assert stored.size_bytes == len(data)


# --- fail-closed refusals: no artifacts row AND no orphan local-corpus backend row ----------------


def test_refuses_raw_id_handle(repo, tmp_path):
    f = _write(tmp_path, "x", b"x")
    for bad in (1, None, "not-a-handle"):
        with pytest.raises(ImporterIngressError):
            register_artifact_in_place(repo, cast(ImportBatchHandle, bad), source_path=f, uri="x")
    assert _artifact_count(repo.engine) == 0
    assert _backend_count(repo.engine, LOCAL_CORPUS_BACKEND_KEY) == 0  # refused BEFORE the backend is minted


def test_refuses_unknown_kind(repo, tmp_path):
    handle = _open(repo)
    with pytest.raises(ImporterIngressError):
        register_artifact_in_place(
            repo, handle, source_path=_write(tmp_path, "x", b"x"), uri="x", kind="bogus"
        )
    assert _artifact_count(repo.engine) == 0
    assert _backend_count(repo.engine, LOCAL_CORPUS_BACKEND_KEY) == 0


def test_refuses_bad_retention(repo, tmp_path):
    handle = _open(repo)
    with pytest.raises(ImporterIngressError):
        register_artifact_in_place(
            repo, handle, source_path=_write(tmp_path, "x", b"x"), uri="x", retention="bogus"
        )
    assert _artifact_count(repo.engine) == 0
    assert _backend_count(repo.engine, LOCAL_CORPUS_BACKEND_KEY) == 0


def test_tensor_kind_requires_shape_and_dtype(repo, tmp_path):
    handle = _open(repo)
    f = _write(tmp_path, "t", b"tensorbytes")
    # a tensor kind without shape/dtype is refused before any DB write
    with pytest.raises(ImporterIngressError):
        register_artifact_in_place(repo, handle, source_path=f, uri="t/no", kind="dense_tensor")
    assert _artifact_count(repo.engine) == 0
    assert _backend_count(repo.engine, LOCAL_CORPUS_BACKEND_KEY) == 0
    # WITH shape+dtype it registers, and the int4[] shape round-trips (proves the typed bindparam)
    r = register_artifact_in_place(
        repo, handle, source_path=f, uri="t/ok", kind="dense_tensor", shape=[4, 8, 16], dtype="float32"
    )
    assert r.registered is True
    with repo.engine.connect() as conn:
        row = conn.execute(
            text("SELECT shape, dtype FROM neuro.artifacts WHERE artifact_id = :i"), {"i": r.artifact_id}
        ).one()
    assert list(row.shape) == [4, 8, 16]
    assert row.dtype == "float32"


def test_refuses_missing_source_path(repo, tmp_path):
    handle = _open(repo)
    missing = str(tmp_path / "does-not-exist.parquet")
    with pytest.raises(ImporterIngressError):
        register_artifact_in_place(repo, handle, source_path=missing, uri="missing")
    assert _artifact_count(repo.engine) == 0
    assert _backend_count(repo.engine, LOCAL_CORPUS_BACKEND_KEY) == 0  # refused BEFORE the backend is minted
