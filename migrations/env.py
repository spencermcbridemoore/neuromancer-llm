"""Alembic environment — lane-guarded, Postgres-only (ADR-0039 Reconsidered 2026-06-17).

Offline mode is deliberately unsupported: we never emit SQL we did not lane-check against a live
connection. The schema lives in `neuro`; the alembic version table lives there too, so the schema
must exist before context.configure() on an empty DB (migrations-from-zero bootstrap, B1).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from neuromancer_llm.db.lanes import (  # ADR-0006 positive identity + the PG-18 parity guard
    ConfigurationError,
    assert_lane,
    assert_pg_major,
)
from neuromancer_llm.db.orm import Base  # the models in db/orm.py

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

target_metadata = Base.metadata

ENV_URL = "NEURO_DATABASE_URL"
ENV_EXPECTED_LANE = "NEURO_MIGRATION_EXPECTED_LANE"


def _url() -> str:
    # Env surface is exactly NEURO_DATABASE_URL (+ optional connections file). ADR-0006.
    url = os.environ.get(ENV_URL)
    if not url:
        raise ConfigurationError(f"{ENV_URL} unset — migrations refuse to guess a target (fail closed).")
    return url


def _identity_row_exists(conn) -> bool:
    # True once `db provision` has written the identity ROW. On migrations-from-zero the table does not
    # exist yet; in the post-upgrade / pre-provision window the table exists but is empty — in BOTH cases
    # there is nothing to assert, so the lane guard is skipped (alembic doc B1). Once provisioned (row
    # present), every subsequent migration lane-checks.
    if conn.execute(text("SELECT to_regclass('neuro.database_identity')")).scalar() is None:
        return False
    return (conn.execute(text("SELECT count(*) FROM neuro.database_identity")).scalar() or 0) > 0


def run_migrations_online() -> None:
    url = _url()
    cfg = context.config
    cfg.set_main_option("sqlalchemy.url", url)
    connectable = engine_from_config(
        cfg.get_section(cfg.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as conn:
        # PG-major guard BEFORE any DDL (audit correction 2026-07-02): the migration syntax runs green
        # on PG 15-17, but the migrations==frozen-DDL parity proof is anchored to PG 18 — refuse any
        # other major loudly rather than materialize an unproven schema.
        assert_pg_major(conn.execute(text("SELECT current_setting('server_version_num')::int")).scalar_one())
        # B1 bootstrap: the alembic version table lives in schema `neuro` (version_table_schema below),
        # so the schema MUST exist before context.configure() on an empty DB (migrations-from-zero).
        # Commit the bootstrap BEFORE handing the connection to alembic: an in-progress transaction here
        # would be inherited (and NOT owned/committed) by context.begin_transaction(), silently rolling
        # back the entire migration on connection close. Everything is schema-qualified, so no
        # SET search_path is needed (which would itself re-open an inherited transaction).
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS neuro"))
        # Lane guard BEFORE any DDL (ADR-0006) — SKIP on a not-yet-provisioned DB: on migrations-from-zero
        # database_identity does not exist yet, so there is nothing to assert. `db provision` writes the
        # identity row after the first upgrade; every subsequent migration lane-checks. `expected_lane` is
        # a mandatory kwarg, never baked into code.
        if _identity_row_exists(conn):
            expected = os.environ.get(ENV_EXPECTED_LANE)
            if not expected:  # R10: explicit fail-closed message, not a raw KeyError
                raise ConfigurationError(
                    f"{ENV_EXPECTED_LANE} unset but the DB is provisioned — refusing to guess the lane "
                    "(fail closed). Set it to the lane this migration must run against."
                )
            assert_lane(conn, expected_lane=expected)
        conn.commit()
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            version_table_schema="neuro",
            compare_type=True,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    # Offline mode intentionally unsupported: we always assert lane on a live connection (ADR-0006).
    raise ConfigurationError("offline migrations are unsupported — we never emit un-lane-checked SQL.")

run_migrations_online()
