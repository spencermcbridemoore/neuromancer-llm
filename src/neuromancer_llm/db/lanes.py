"""Lanes v2 — positive in-band database identity; UNKNOWN fails closed (ADR-0006).

A singleton `neuro.database_identity` row is written at provisioning (`neuro db provision`) and
positively verified ONCE per engine construction at every writer entry point (R1: session.py
verify_engine / make_verified_engine) BEFORE any write path exists, via a *mandatory* `expected_lane`
argument (never an env var of its own). The canonical check is lane AND repo-pinned uuid match.
UNKNOWN fails closed for every intent. This closes the predecessor's confirmed bidirectional lane
inversion. (Caveat, ADR-0006 Amended 2026-07-02: pool_pre_ping recycles a dead pooled connection
without re-verification; a per-connect listener is deferred to the ADR-0046/role-split bundle.)

The same `assert_lane` guards migrations (migrations/env.py) once a DB is provisioned, and is skipped
only on migrations-from-zero (empty DB with no identity row yet).
"""

from __future__ import annotations

import uuid as _uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

VALID_LANES = frozenset({"canonical", "staging", "test"})  # 'unknown' is never a usable lane

# The migrations-from-zero == frozen-DDL parity proof (tests/test_migration_ddl_parity.py) is anchored
# to PostgreSQL 18; any other major is an UNPROVEN schema even where the migration syntax runs green.
PG_PARITY_MAJOR = 18


class LaneAssertionError(RuntimeError):
    """Raised when the connected DB's identity does not positively match the expected lane.

    Always fail closed: no identity row, an 'unknown' lane, a lane mismatch, or a uuid mismatch all
    raise. A write must never proceed past this guard on an unverified target.
    """


class ConfigurationError(RuntimeError):
    """Raised when required infrastructure config is missing/ambiguous (fail closed, ADR-0006).

    Distinct from LaneAssertionError (which is about a connected DB's identity): this is about the
    environment surface (e.g. NEURO_DATABASE_URL / NEURO_MIGRATION_EXPECTED_LANE unset). CLI delegates
    translate it to a clean typer.Exit; library code raises it instead of a raw SystemExit/KeyError (R8).
    """


def assert_pg_major(server_version_num: int) -> None:
    """Fail loud unless the connected server's major version is the PG-18 parity baseline.

    Migration 0001's SYNTAX runs green on PG 15-17 (`NULLS NOT DISTINCT` needs >= 15;
    `gen_random_uuid()` is core since 13), so nothing else would refuse a wrong major — but the
    migrations==frozen-DDL parity proof is anchored to PG 18, so a quietly-green PG 16 build would be
    an UNPROVEN schema. The migrate path calls this BEFORE any DDL (audit correction 2026-07-02;
    closes the X8 runbook's false "a non-18 PG breaks migrate itself" safety net).
    """
    if not (180000 <= server_version_num < 190000):
        raise ConfigurationError(
            f"connected PostgreSQL is major {server_version_num // 10000} "
            f"(server_version_num={server_version_num}) but the schema parity baseline is "
            f"PG {PG_PARITY_MAJOR} — migrations-from-zero == frozen-DDL is proven on PG 18 only; "
            "refusing to migrate (fail closed). Install the PGDG PostgreSQL 18 package."
        )


def read_identity(conn: Connection) -> dict | None:
    """Return the singleton identity row as a dict, or None if the table/row does not exist yet."""
    if conn.execute(text("SELECT to_regclass('neuro.database_identity')")).scalar() is None:
        return None
    row = (
        conn.execute(
            text("SELECT lane, instance_uuid, cloned_from, schema_major FROM neuro.database_identity")
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def assert_lane(
    conn: Connection,
    *,
    expected_lane: str,
    expected_uuid: _uuid.UUID | str | None = None,
) -> dict:
    """Positively verify the connected DB before any write. Fail closed on anything but a match.

    expected_lane is mandatory (ADR-0006). expected_uuid pins the instance for the canonical lane
    (lane AND repo-pinned uuid) when the caller knows which instance it must be talking to.
    """
    if expected_lane not in VALID_LANES:
        raise LaneAssertionError(
            f"expected_lane={expected_lane!r} is not a usable lane (one of {sorted(VALID_LANES)}); "
            "'unknown' fails closed."
        )
    identity = read_identity(conn)
    if identity is None:
        raise LaneAssertionError(
            "no neuro.database_identity row — the DB is not provisioned; refusing to write (fail closed). "
            "Run `neuro db provision` on a provably-empty DB to establish identity."
        )
    lane = identity["lane"]
    if lane not in VALID_LANES:
        raise LaneAssertionError(f"database_identity.lane={lane!r} is UNKNOWN/invalid — fail closed.")
    if lane != expected_lane:
        raise LaneAssertionError(
            f"lane mismatch: connected DB is {lane!r} but {expected_lane!r} was required (fail closed)."
        )
    if expected_uuid is not None:
        got = str(identity["instance_uuid"])
        want = str(expected_uuid)
        if got != want:
            raise LaneAssertionError(
                f"instance_uuid mismatch: connected DB is {got} but {want} was pinned (fail closed)."
            )
    return identity
