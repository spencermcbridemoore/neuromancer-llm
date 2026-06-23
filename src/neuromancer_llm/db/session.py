"""Engine / session factory. Reads `NEURO_DATABASE_URL` only (the env surface is infrastructure-only).

Postgres-only (ADR-0039 Reconsidered 2026-06-17): the URL is expected to be a
`postgresql+psycopg://` DSN. Behavior never lives in env vars (NEVER-AGAIN: SQ_*-style shell flags).

R1 (fail-closed write choke point): `make_verified_engine` / `verify_engine` run the lanes-v2 identity
guard ONCE before exposing the engine, so no write path exists on an unverified target (ADR-0006). The
engine verifies the DB IDENTITY (lane/uuid), not the connected ROLE — Postgres grants enforce the role.
The repository and the bundle registrar are constructed FROM a verified engine — they cannot be built
against an unprovisioned or wrong-lane DB.
"""

from __future__ import annotations

import os
import uuid as _uuid

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .lanes import ConfigurationError, assert_lane

ENV_URL = "NEURO_DATABASE_URL"


def database_url(url: str | None = None) -> str:
    url = url or os.environ.get(ENV_URL)
    if not url:
        raise ConfigurationError(
            f"{ENV_URL} unset — refusing to guess a target (fail closed). "
            "Set it to a postgresql+psycopg:// DSN."
        )
    return url


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """A RAW engine with NO identity guard — only for pre-identity tooling (migrations, `db provision`).
    Application/worker writes must go through make_verified_engine / a verified Repository instead."""
    return create_engine(database_url(url), echo=echo, future=True, pool_pre_ping=True)


def verify_engine(
    engine: Engine, *, expected_lane: str, expected_uuid: _uuid.UUID | str | None = None
) -> Engine:
    """Positively verify the engine's target identity ONCE (ADR-0006), then return it. Fail closed.

    Canonical-lane callers SHOULD also pass expected_uuid (the repo-pinned instance) so a restored clone
    with a fresh instance_uuid is rejected — lane AND repo-pinned uuid is the canonical check.
    """
    with engine.connect() as conn:
        assert_lane(conn, expected_lane=expected_lane, expected_uuid=expected_uuid)
    return engine


def make_verified_engine(
    url: str | None = None,
    *,
    expected_lane: str,
    expected_uuid: _uuid.UUID | str | None = None,
    echo: bool = False,
) -> Engine:
    """An engine whose target DB IDENTITY (lane / repo-pinned uuid) is positively verified before it is
    exposed (R1). It connects as whatever NEURO_DATABASE_URL's ROLE is — it does NOT confer or verify the
    role. The capture/control-plane path needs a role holding BOTH neuro_registrar registry INSERT AND
    neuro_writer bundle-lifecycle UPDATE grants (i.e. an admin DSN; see capture.events.capture_logprob);
    the role boundary is enforced by Postgres grants, not by this function (BLOCK 4 — honest name)."""
    return verify_engine(
        make_engine(url, echo=echo), expected_lane=expected_lane, expected_uuid=expected_uuid
    )


# Back-compat alias: the old name overstated the role (it verifies identity, not write privilege).
make_writer_engine = make_verified_engine


def make_reader_engine(
    url: str | None = None, *, expected_lane: str | None = None, echo: bool = False
) -> Engine:
    """A SELECT-only consumer engine (every consumption surface uses the neuro_reader role; phase0 Q12).

    The SELECT-only boundary is the connected ROLE, not this engine — connect with neuro_reader creds. No
    write guard runs (reads need none, ADR-0006); the read-time binding is integrity verify (sha256+size,
    capture/reader.py). When expected_lane is given, the lane is positively confirmed first (a SELECT the
    reader role may run) so a consumer can refuse to read the wrong lane.
    """
    engine = make_engine(url, echo=echo)
    if expected_lane is not None:
        with engine.connect() as conn:
            assert_lane(conn, expected_lane=expected_lane)
    return engine


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or make_engine(), class_=Session, expire_on_commit=False)
