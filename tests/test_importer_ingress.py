"""Importer rank 4 — the ingress batch-open gate probes (importer/ingress.py).

The gate carries the three importer invariants: (a) the ADR-0020 durability consult (canonical only, BEFORE the
import_batches INSERT), (b) the D1/D2 fail-closed confidentiality + source_system stamp, (c) D3 no-cloud-PUT (pinned by
tests/redteam/test_rt_importer_no_cloud_put.py). It mints the import_batches row and returns the ImportBatchHandle token
rank 5's external_records writer requires. The durability probes need a CANONICAL-lane DB (the consult fires only on
the verified canonical lane); the stamp/scope probes run on the shared 'test' lane (validation precedes the consult).
"""

from __future__ import annotations

import inspect
import uuid as _uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from neuromancer_llm.db.canonical_instance import CANONICAL_INSTANCE_UUID
from neuromancer_llm.db.provision import provision
from neuromancer_llm.db.repository import Repository
from neuromancer_llm.governance.durability import WAL_LAG_ROW, seed_row
from neuromancer_llm.governance.freshness import seed_backup_freshness
from neuromancer_llm.governance.health import DurabilityGateError
from neuromancer_llm.governance.probes import _bump_freshness
from neuromancer_llm.governance.wal_archiving import _bump_wal_lag
from neuromancer_llm.importer.ingress import (
    Confidentiality,
    ImportBatchHandle,
    ImporterIngressError,
    SourceSystem,
    open_import_batch,
)

pytestmark = pytest.mark.pg


# --- canonical-lane harness (per-file, matching test_rt_durability_consult / test_a2_17_acceptance) ---
@pytest.fixture
def canonical_url(pg_url):
    """An isolated DB provisioned as the CANONICAL lane (identity uuid pinned to the repo constant) so a
    canonical-lane Repository verifies and the ADR-0020 consult FIRES. Created + dropped per test — the shared
    'test' DB is the wrong lane (the consult would skip)."""
    name = f"canon_ingress_{_uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_url, future=True, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')  # noqa: S608 — generated hex, not user input
    admin.dispose()
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    ddl = Path("tests/reference/phase3-ddl.sql").read_text(encoding="utf-8")
    eng = create_engine(url, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(ddl.replace("%", "%%"))  # frozen schema (exec_driver_sql -> escape literal %)
    with eng.connect() as c:
        provision(c, lane="canonical")
        c.execute(
            text("UPDATE neuro.database_identity SET instance_uuid = :u"),
            {"u": str(CANONICAL_INSTANCE_UUID)},  # pin to the repo constant so verify_engine accepts it
        )
        c.commit()
    eng.dispose()
    try:
        yield url
    finally:
        admin = create_engine(pg_url, future=True, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.exec_driver_sql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"  # noqa: S608 — generated name
            )
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')  # noqa: S608 — generated name
        admin.dispose()


def _seed_interlock(engine, *, healthy: bool) -> None:
    """Seed BOTH ADR-0020 arms (assert_durability_ok composes backup_freshness AND wal_lag). `blocked` keeps both
    born-'blocked'; `healthy` heals both (the WAL heal is synthetic — the test PG has no live archiver)."""
    with engine.connect() as conn:
        seed_backup_freshness(conn)  # fail-closed birth: status='blocked', measured_at=epoch
        seed_row(conn, WAL_LAG_ROW)
    if healthy:
        _bump_freshness(engine, ok=True, detail="test provisioning: healthy backup")
        _bump_wal_lag(engine, ok=True, detail="test provisioning: healthy archiver")


def _canonical_repo(url) -> Repository:
    return Repository(create_engine(url, future=True), expected_lane="canonical")


def _batch_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM neuro.import_batches")).scalar_one()


# --- (b) D1/D2 stamp: REQUIRED, non-defaulted, fail-closed (test lane; validation precedes the consult) ---


def test_gate_refuses_invalid_confidentiality(repo):
    with pytest.raises(ImporterIngressError):
        open_import_batch(
            repo, source_system=SourceSystem.LOCAL_FS, confidentiality="open"
        )  # raw str, not a member
    assert _batch_count(repo.engine) == 0  # refused BEFORE any import_batches write


def test_gate_refuses_none_confidentiality(repo):
    with pytest.raises(ImporterIngressError):
        open_import_batch(repo, source_system=SourceSystem.LOCAL_FS, confidentiality=None)
    assert _batch_count(repo.engine) == 0


def test_gate_refuses_invalid_source_system(repo):
    with pytest.raises(ImporterIngressError):
        open_import_batch(
            repo, source_system="study-query-llm", confidentiality=Confidentiality.OPEN
        )  # raw str
    assert _batch_count(repo.engine) == 0


def test_gate_stamp_is_non_defaulted():
    """Structural: both stamp fields are keyword-only and default-less, so a batch-open missing either is a
    TypeError — an unstamped batch-open is impossible to express (no default-clean escape)."""
    sig = inspect.signature(open_import_batch)
    for name in ("source_system", "confidentiality"):
        p = sig.parameters[name]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty


# --- (a) ADR-0020 durability consult: canonical-lane only, BEFORE the mint ---


def test_gate_blocks_on_flipped_durability_canonical(canonical_url):
    repo = _canonical_repo(canonical_url)
    _seed_interlock(repo.engine, healthy=False)  # interlock BLOCKED
    with pytest.raises(DurabilityGateError):
        open_import_batch(
            repo, source_system=SourceSystem.STUDY_QUERY_LLM, confidentiality=Confidentiality.EXAM_RESTRICTED
        )
    assert _batch_count(repo.engine) == 0  # blocked BEFORE the INSERT -> no orphan batch row


def test_gate_opens_batch_healthy_canonical(canonical_url):
    repo = _canonical_repo(canonical_url)
    _seed_interlock(repo.engine, healthy=True)
    batch = open_import_batch(
        repo,
        source_system=SourceSystem.STUDY_QUERY_LLM,
        confidentiality=Confidentiality.EXAM_RESTRICTED,
        note="a2 healthy",
    )
    assert isinstance(batch, ImportBatchHandle)
    assert batch.import_batch_id > 0
    assert batch.source_system is SourceSystem.STUDY_QUERY_LLM
    assert batch.confidentiality is Confidentiality.EXAM_RESTRICTED
    assert _batch_count(repo.engine) == 1  # the batch row was minted
    with repo.engine.connect() as conn:
        row = conn.execute(
            text("SELECT source_system, note FROM neuro.import_batches WHERE import_batch_id = :b"),
            {"b": batch.import_batch_id},
        ).one()
    assert row.source_system == "study-query-llm"
    assert row.note == "a2 healthy"


# --- (a) lane scope: a test-lane import is NOT gated (ADR-0020 governs canonical only) ---


def test_gate_skips_consult_on_test_lane(repo):
    _seed_interlock(
        repo.engine, healthy=False
    )  # BLOCKED — but this is the 'test' lane, so the consult is skipped
    batch = open_import_batch(repo, source_system=SourceSystem.LOCAL_FS, confidentiality=Confidentiality.OPEN)
    assert batch.import_batch_id > 0  # opened despite the blocked interlock (canonical-only scope)
    assert _batch_count(repo.engine) == 1
