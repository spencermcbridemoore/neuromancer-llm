"""Provisioning: write the lanes-v2 identity row, create the four roles, apply phase3-grants.sql.

Grants are a SEPARATE provisioning step (Finding 4): they are NOT part of the Alembic migration target
(so phase3-ddl.sql / the initial migration is standalone-executable on an empty DB). `neuro db roles`
creates the roles then applies `grants.sql`, AFTER the roles exist (ADR-0007). `neuro db provision`
writes the singleton database_identity row on a provably-empty (just-migrated) DB (ADR-0006).
"""

from __future__ import annotations

import importlib.resources
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .lanes import VALID_LANES, ConfigurationError

# Role CLASSES (ADR-0007). Per-human / per-worker logins are GRANTed one of these in real provisioning;
# the security boundary (C3) is that GPU/remote workers connect as neuro_writer, never neuro_registrar.
ROLES = ("neuro_admin", "neuro_writer", "neuro_reader", "neuro_registrar")


def grants_sql() -> str:
    """The security-contract companion to the schema (ADR-0007), shipped as package data."""
    return (
        importlib.resources.files("neuromancer_llm.db.sql").joinpath("grants.sql").read_text(encoding="utf-8")
    )


def schema_built(conn: Connection) -> bool:
    return conn.execute(text("SELECT to_regclass('neuro.database_identity')")).scalar() is not None


def provision(conn: Connection, *, lane: str, note: str | None = None) -> None:
    """Write the singleton identity row on a provably-empty DB (ADR-0006). UNKNOWN is never written."""
    if lane not in VALID_LANES:
        raise ConfigurationError(
            f"lane={lane!r} is not a usable lane (one of {sorted(VALID_LANES)}); UNKNOWN fails closed."
        )
    if not schema_built(conn):
        raise ConfigurationError(
            "schema not built — run `neuro db migrate` first (migrations own the schema)."
        )
    existing = conn.execute(text("SELECT lane FROM neuro.database_identity")).scalar()
    if existing is not None:
        raise ConfigurationError(
            f"already provisioned (lane={existing!r}); refusing to re-provision (fail closed)."
        )
    conn.execute(
        text("INSERT INTO neuro.database_identity (lane, note) VALUES (:lane, :note)"),
        {"lane": lane, "note": note},
    )
    conn.commit()


def create_roles(conn: Connection, *, password: str | None = None) -> None:
    """Idempotently create the four role classes. With a password they are LOGIN roles (dev/CI); in
    production they are typically NOLOGIN classes that per-human/per-worker logins inherit.

    R3: the password is a Postgres-quoted LITERAL via psycopg `sql.Literal`, never an f-string. CREATE
    ROLE is a utility statement that cannot take a bound parameter, so a password containing a quote /
    backslash / `$$` must be safely escaped — sql.Literal does it. The existence check is Python-side (no
    DO block, whose `$$` quoting an operator password could itself break), and the composed statement is
    executed via a raw psycopg cursor so its literal bytes are never re-parsed for %-placeholders.
    """
    from psycopg import sql

    raw: Any = conn.connection.dbapi_connection  # raw psycopg connection (untyped DBAPI escape hatch)
    for role in ROLES:
        if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar() is not None:
            continue
        if password is not None:
            stmt = sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        else:
            stmt = sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role))
        with raw.cursor() as cur:
            cur.execute(stmt)
    conn.commit()


def apply_grants(conn: Connection) -> None:
    """Apply phase3-grants.sql — AFTER the roles exist. The file is multi-statement, no params."""
    conn.exec_driver_sql(grants_sql())
    conn.commit()


def provision_roles(conn: Connection, *, password: str | None = None) -> None:
    """`neuro db roles`: create the four roles, then apply the grants contract."""
    create_roles(conn, password=password)
    apply_grants(conn)
