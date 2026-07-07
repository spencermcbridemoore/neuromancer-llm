"""GO-D-restore (A2-9) probes — the A1-15+A2-9 HARD GATE.

A2-9 closes the residual A1-15 alone cannot: a VERBATIM canonical clone (the same `instance_uuid` copied
in on restore) verifies as canonical, because the repo pin (A1-15) only rejects a clone with a *fresh* uuid.
The restore-drill SCRUBS the restored clone's identity (fresh `instance_uuid` + `cloned_from`) so a verbatim
backup verifies as NON-canonical; a target/DSN guard (`--confirm-scratch` + scratch != canonical DSN) refuses
to run against the live canonical. Synthetic-dump-first (ruled): the scrub LOGIC + guard are exercised on an
isolated scratch DB (the `scratch_db` fixture); the real pgbackrest / desktop-NVMe restore is a deferred
A2-9-real step (gated on A2-7 + A2-10). Runs as the connection's admin role.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from neuromancer_llm.db.lanes import ConfigurationError, LaneAssertionError, assert_lane, read_identity
from neuromancer_llm.db.restore import restore_drill, same_target

pytestmark = pytest.mark.pg

PIN = "c7a1b953-485a-4127-a2e6-a18f7423742a"  # the canonical pin (db/canonical_instance.py)
# a distinct, unreachable DSN standing in for the canonical connection (only string-compared, never dialed)
FAKE_CANONICAL = "postgresql+psycopg://canon@127.0.0.1:1/canonical_not_scratch"


def _seed_verbatim_canonical_clone(scratch_url: str) -> None:
    """Simulate a restored VERBATIM canonical backup: the identity row with lane=canonical AND the pinned uuid."""
    eng = create_engine(scratch_url, future=True)
    with eng.begin() as conn:
        conn.execute(
            text("INSERT INTO neuro.database_identity (lane, instance_uuid) VALUES ('canonical', :pin)"),
            {"pin": PIN},
        )
    eng.dispose()


def test_unscrubbed_verbatim_clone_verifies_canonical(scratch_db: str) -> None:
    """(b) THE RESIDUAL A2-9 closes: an UNSCRUBBED verbatim clone verifies as canonical — A1-15's pin alone
    cannot reject it (the same uuid was copied in). This is why the scrub is load-bearing."""
    _seed_verbatim_canonical_clone(scratch_db)
    eng = create_engine(scratch_db, future=True)
    with eng.connect() as conn:
        identity = assert_lane(
            conn, expected_lane="canonical", expected_uuid=uuid.UUID(PIN)
        )  # PASSES — the hole
        assert str(identity["instance_uuid"]) == PIN
    eng.dispose()


def test_restore_drill_scrubbed_clone_is_non_canonical(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) after the drill scrubs, the clone verifies NON-canonical (fails the pin) and `cloned_from` records
    the original uuid."""
    _seed_verbatim_canonical_clone(scratch_db)
    monkeypatch.setenv("NEURO_DATABASE_URL", FAKE_CANONICAL)  # canonical DSN set + DISTINCT from scratch
    result = restore_drill(scratch_db, confirm_scratch=True)
    assert result["old_uuid"] == PIN
    assert result["new_uuid"] != PIN
    eng = create_engine(scratch_db, future=True)
    with eng.connect() as conn:
        with pytest.raises(LaneAssertionError):  # NON-canonical now — fails the pin
            assert_lane(conn, expected_lane="canonical", expected_uuid=uuid.UUID(PIN))
        row = read_identity(conn)
        assert row is not None
        assert str(row["cloned_from"]) == PIN  # provenance recorded
        assert row["lane"] == "canonical"  # still a canonical-LANE clone, just not THE pinned instance
    eng.dispose()


def test_restore_drill_refuses_canonical_dsn(scratch_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) the guard FIRES: pointing --scratch-url at the canonical DSN (NEURO_DATABASE_URL) is REFUSED,
    and the identity is left untouched (no scrub on the canonical target)."""
    _seed_verbatim_canonical_clone(scratch_db)
    monkeypatch.setenv("NEURO_DATABASE_URL", scratch_db)  # scratch == canonical -> must refuse
    with pytest.raises(ConfigurationError):
        restore_drill(scratch_db, confirm_scratch=True)
    eng = create_engine(scratch_db, future=True)
    with eng.connect() as conn:  # untouched — still the verbatim canonical clone
        assert_lane(conn, expected_lane="canonical", expected_uuid=uuid.UUID(PIN))
    eng.dispose()


def test_restore_drill_requires_confirm_scratch(scratch_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """(c') the guard FIRES: without --confirm-scratch the destructive drill refuses BEFORE any connection,
    identity untouched."""
    _seed_verbatim_canonical_clone(scratch_db)
    monkeypatch.setenv("NEURO_DATABASE_URL", FAKE_CANONICAL)
    with pytest.raises(ConfigurationError):
        restore_drill(scratch_db, confirm_scratch=False)
    eng = create_engine(scratch_db, future=True)
    with eng.connect() as conn:
        assert_lane(conn, expected_lane="canonical", expected_uuid=uuid.UUID(PIN))  # untouched
    eng.dispose()


def test_partial_scrub_still_canonical(scratch_db: str) -> None:
    """(d) a PARTIAL scrub (cloned_from set but instance_uuid UNCHANGED) STILL verifies canonical — pins that
    the `instance_uuid` regeneration is the load-bearing closer, not `cloned_from`."""
    _seed_verbatim_canonical_clone(scratch_db)
    eng = create_engine(scratch_db, future=True)
    with eng.begin() as conn:
        conn.execute(text("UPDATE neuro.database_identity SET cloned_from = instance_uuid"))  # uuid UNCHANGED
    with eng.connect() as conn:
        assert_lane(
            conn, expected_lane="canonical", expected_uuid=uuid.UUID(PIN)
        )  # STILL canonical — not closed
    eng.dispose()


def test_same_target_normalizes_loopback_and_port() -> None:
    """(F1, pure) same_target() treats loopback ALIASES + the default port as the SAME box — else the guard
    is defeated by a 127.0.0.1-vs-localhost-vs-::1 (or omitted-port) spelling of the live canonical."""
    base = "postgresql+psycopg://u:p@127.0.0.1:5432/neuro"
    assert same_target(base, "postgresql+psycopg://u:p@localhost:5432/neuro")  # localhost == 127.0.0.1
    assert same_target(base, "postgresql+psycopg://u:p@[::1]:5432/neuro")  # ::1 == 127.0.0.1
    assert same_target(base, "postgresql+psycopg://u:p@127.0.0.1/neuro")  # omitted port == 5432
    assert not same_target(
        base, "postgresql+psycopg://u:p@127.0.0.1:5432/other"
    )  # distinct db stays distinct
    assert not same_target(base, "postgresql+psycopg://u:p@db.example.com:5432/neuro")  # distinct host
    assert not same_target(base, "postgresql+psycopg://u:p@127.0.0.1:5433/neuro")  # distinct port


def test_restore_drill_refuses_canonical_loopback_alias(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(F1, integration) the guard REFUSES when the canonical DSN is the SAME server+db as --scratch-url but
    spelled with a DIFFERENT loopback alias — the drill must not scrub the live canonical via an alias."""
    _seed_verbatim_canonical_clone(scratch_db)
    scratch_host = (make_url(scratch_db).host or "").lower()
    alias = "127.0.0.1" if scratch_host in ("localhost", "") else "localhost"  # a different loopback spelling
    canonical_alias = make_url(scratch_db).set(host=alias).render_as_string(hide_password=False)
    monkeypatch.setenv("NEURO_DATABASE_URL", canonical_alias)
    with pytest.raises(ConfigurationError):
        restore_drill(scratch_db, confirm_scratch=True)
    eng = create_engine(scratch_db, future=True)
    with eng.connect() as conn:  # untouched — the guard fired before any scrub
        assert_lane(conn, expected_lane="canonical", expected_uuid=uuid.UUID(PIN))
    eng.dispose()
