"""Red-team: migrations own the schema; from-zero == frozen DDL (L10).

  * downgrade round-trip — `upgrade head` then `downgrade base` cleanly drops the neuro schema (the
    disaster-recovery path the parity test never exercises), in a THROWAWAY database.
  * schema completeness — the migrated session schema carries the full table set + the in-schema
    assign-once trigger/function (migrations materialized the whole schema, not a subset).

The frozen-DDL parity (incl. COMMENTs) is covered by tests/test_migration_ddl_parity.py — the real backstop.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.pg


def _sibling_url(base: str, dbname: str) -> str:
    return make_url(base).set(database=dbname).render_as_string(hide_password=False)


def _recreate_db(base: str, dbname: str) -> None:
    admin = create_engine(_sibling_url(base, "postgres"), future=True, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{dbname}' AND pid<>pg_backend_pid()"
            )
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{dbname}"')
            conn.exec_driver_sql(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.dispose()


def test_rt_migration_downgrade_round_trips(pg_url):
    """L10: `upgrade head` -> `downgrade base` round-trips cleanly in a throwaway DB — the downgrade DO-block
    (the disaster-recovery path) drops the neuro schema entirely."""
    try:
        _recreate_db(pg_url, "rt_downgrade")
    except Exception as exc:  # noqa: BLE001 — needs CREATE DATABASE; skip cleanly where not permitted
        pytest.skip(f"cannot create a sibling database ({exc})")

    url = _sibling_url(pg_url, "rt_downgrade")
    prev = os.environ.get("NEURO_DATABASE_URL")
    os.environ["NEURO_DATABASE_URL"] = url
    try:
        from alembic import command
        from alembic.config import Config

        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        eng = create_engine(url, future=True)
        try:
            with eng.connect() as conn:
                assert conn.execute(text("SELECT to_regclass('neuro.runs')")).scalar() is not None
            command.downgrade(cfg, "base")
            with eng.connect() as conn:
                # every neuro TABLE is gone after a full downgrade (the disaster-recovery path); the empty
                # schema itself may remain, which is fine — the objects are what the downgrade reclaims.
                assert conn.execute(text("SELECT to_regclass('neuro.runs')")).scalar() is None
                tables_left = conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname='neuro' "
                        "AND tablename <> 'alembic_version'"  # alembic's own bookkeeping survives, empty
                    )
                ).scalar_one()
            assert tables_left == 0
        finally:
            eng.dispose()
    finally:
        if prev is not None:
            os.environ["NEURO_DATABASE_URL"] = prev
        else:
            os.environ.pop("NEURO_DATABASE_URL", None)


def test_rt_migrated_schema_is_complete(engine):
    """L10: migrations-from-zero materialized the FULL schema — the core table set, the 13 enums, and the
    in-schema assign-once function + trigger (the guard L11 relies on), not a subset."""
    with engine.connect() as conn:
        tables = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname='neuro' AND tablename<>'alembic_version'")
        ).scalar_one()
        enums = conn.execute(
            text(
                "SELECT count(DISTINCT t.typname) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                "WHERE n.nspname='neuro' AND t.typtype='e'"
            )
        ).scalar_one()
        has_fn = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='neuro' AND p.proname='assert_assign_once'"
            )
        ).scalar_one()
        has_trigger = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='neuro' AND NOT t.tgisinternal AND t.tgname LIKE '%assign_once%'"
            )
        ).scalar_one()
        core = (
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='neuro' AND tablename = ANY(:t)"),
                {
                    "t": [
                        "runs",
                        "jobs",
                        "work_leases",
                        "bundles",
                        "artifacts",
                        "capture_events",
                        "fingerprints",
                        "model_identities",
                        "tokenizer_identities",
                        "methods",
                        "method_versions",
                        "replicate_links",
                        "divergence_measurements",
                        "expected_reproducibility_rules",
                        "database_identity",
                        "storage_backends",
                    ],
                },
            )
            .scalars()
            .all()
        )
    assert tables == 45, f"expected 45 neuro tables, got {tables}"
    assert enums == 13, f"expected 13 neuro enums, got {enums}"
    assert has_fn == 1 and has_trigger == 1  # the in-schema assign-once guard is materialized
    assert len(core) == 16  # every core table is present
