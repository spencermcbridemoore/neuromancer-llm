"""Red-team: migrations own the schema; from-zero == frozen DDL (L10).

  * downgrade round-trip — `upgrade head` then `downgrade base` drops every neuro OBJECT (the
    disaster-recovery path the parity test never exercises), in a THROWAWAY database. ⚠ The empty
    SCHEMA itself REMAINS (0001's downgrade drops tables/types/functions individually, never the
    schema) — and that is load-bearing, not incidental: the post-downgrade `pg_proc` assert is scoped
    to `nspname='neuro'`, so if the schema were ever dropped instead, that assert would read 0
    unconditionally and the orphan-function net would go permanently, silently green.
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
    (the disaster-recovery path) drops every neuro OBJECT; the empty schema itself remains (see the
    module header — the surviving schema is what makes the pg_proc assert below meaningful)."""
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
                # ...and every in-schema FUNCTION too. Tables-only was blind to an orphaned function: a
                # downgrade with a wrong argument signature is a SILENT no-op under `DROP ... IF EXISTS`,
                # so the triggers vanish with the tables while their functions survive and no assertion
                # here could tell. True at 0001 (its DO-block drops assert_assign_once explicitly) and at
                # 0003, so this is a regression net for the whole class, not a new obligation.
                fns_left = conn.execute(
                    text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='neuro'"
                    )
                ).scalar_one()
            assert tables_left == 0
            assert fns_left == 0, "a downgrade left an orphan neuro.* function behind"
        finally:
            eng.dispose()
    finally:
        if prev is not None:
            os.environ["NEURO_DATABASE_URL"] = prev
        else:
            os.environ.pop("NEURO_DATABASE_URL", None)


def test_rt_migrated_schema_is_complete(engine):
    """L10: migrations-from-zero materialized the FULL schema — the core table set, the 13 enums, the
    in-schema assign-once function + trigger (the guard L11 relies on), AND migration 0003's four
    state-CAS guard functions + their four triggers (ADR-0046 P-4) — not a subset."""
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
        # 0003 (ADR-0046 P-4): the jobs/bundles state-CAS guards + the capture_events.model_id bind.
        # NAME-SCOPED, mirroring the assign-once asserts above, so the two migrations' objects are pinned
        # independently and a dropped trigger cannot be masked by an unrelated one existing.
        c2_fns = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='neuro' AND p.proname IN ('assert_job_state_transition', "
                "'assert_bundle_state_transition', 'assert_bundle_insert_state', 'assert_capture_model_bind')"
            )
        ).scalar_one()
        c2_triggers = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='neuro' AND NOT t.tgisinternal AND t.tgname IN "
                "('jobs_state_transition', 'bundles_state_transition', 'bundles_state_insert', "
                "'capture_events_model_bind')"
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
    # 0003 adds NEITHER a table NOR an enum (its guards are functions + triggers), so these two do not
    # move — the `call_failures` satellite was declined from 0003 for exactly that reason (it adds a TABLE).
    assert tables == 45, f"expected 45 neuro tables, got {tables}"
    assert enums == 13, f"expected 13 neuro enums, got {enums}"
    assert has_fn == 1 and has_trigger == 1  # the in-schema assign-once guard is materialized
    assert c2_fns == 4, f"expected 4 migration-0003 guard functions, got {c2_fns}"
    assert c2_triggers == 4, f"expected 4 migration-0003 guard triggers, got {c2_triggers}"
    assert len(core) == 16  # every core table is present
