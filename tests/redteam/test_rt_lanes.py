"""Red-team: fail-closed lane identity (L1) + no-behavior-by-env-flag / one-implementation (L13).

  * L1 — the SOLE software defense (lanes.py) refuses an invalid expected_lane, a stored 'unknown' lane (the
    enum ADMITS it), a lane mismatch, a uuid mismatch, an unprovisioned DB, and a non-singleton identity;
    provision() refuses re-provision / an unknown lane.
  * L13 — the identity / queue / determinism core reads NO os.environ to switch BEHAVIOR, and there is
    exactly ONE claim path + ONE registrar (no parallel implementation).

Identity-mutating probes run inside a transaction that is ROLLED BACK so the shared session identity row
(the provisioned 'test' lane) is restored.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest
from sqlalchemy import text

from neuromancer_llm.db.lanes import ConfigurationError, LaneAssertionError, assert_lane
from neuromancer_llm.db.provision import provision

pytestmark = pytest.mark.pg

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "neuromancer_llm"


# --- L1: the lane guard fails closed --------------------------------------------------------------
@pytest.mark.parametrize("bad_lane", ["unknown", "prod", "Canonical", ""])
def test_rt_invalid_expected_lane_arg_fails_closed(engine, bad_lane):
    """L1: an expected_lane argument that is not a usable lane raises BEFORE any identity read (the
    expected_lane is a mandatory, validated arg — never an env default)."""
    with engine.connect() as conn, pytest.raises(LaneAssertionError):
        assert_lane(conn, expected_lane=bad_lane)


def test_rt_lane_mismatch_and_uuid_pin_fail_closed(engine):
    """L1: the connected DB is the provisioned 'test' lane — requiring a DIFFERENT lane, or pinning a wrong
    instance_uuid, both fail closed."""
    with engine.connect() as conn:
        with pytest.raises(LaneAssertionError):
            assert_lane(conn, expected_lane="canonical")  # lane mismatch
        with pytest.raises(LaneAssertionError):
            assert_lane(conn, expected_lane="test", expected_uuid=uuid.uuid4())  # uuid pin mismatch


def test_rt_stored_unknown_lane_fails_closed(engine):
    """L1 (the headline inversion case): the lane_kind enum ADMITS 'unknown', so the schema does NOT reject a
    stored lane='unknown' — the software branch is the sole defense. A stored 'unknown' fails closed at the
    stored-not-in-VALID_LANES branch (NOT the lane!=expected branch)."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("UPDATE neuro.database_identity SET lane = 'unknown'"))
            with pytest.raises(LaneAssertionError, match="UNKNOWN/invalid"):
                assert_lane(conn, expected_lane="test")
        finally:
            trans.rollback()  # restore the shared 'test' identity


def test_rt_unprovisioned_and_non_singleton_fail_closed(engine):
    """L1: no identity row (unprovisioned) fails closed; and a SECOND identity row is rejected by the
    singleton PK/CHECK (so read_identity can never go nondeterministic)."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("DELETE FROM neuro.database_identity"))
            with pytest.raises(LaneAssertionError, match="not provisioned"):
                assert_lane(conn, expected_lane="test")
        finally:
            trans.rollback()
    # a second identity row violates the singleton (only_row PK + CHECK)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(Exception):  # noqa: B017 — IntegrityError from the singleton constraint
                conn.execute(text("INSERT INTO neuro.database_identity (lane) VALUES ('staging')"))
        finally:
            trans.rollback()


def test_rt_provision_refuses_reprovision_and_unknown(engine):
    """L1: provision() refuses to re-provision a live DB (already 'test') and refuses an unknown lane —
    UNKNOWN is never written."""
    with engine.connect() as conn:
        with pytest.raises(ConfigurationError, match="already provisioned"):
            provision(conn, lane="canonical")
        with pytest.raises(ConfigurationError):
            provision(conn, lane="unknown")


# --- L13: no behavior-by-env-flag; one implementation per concept ---------------------------------
def test_rt_core_modules_read_no_env():
    """L13: the identity / queue / determinism / capture CORE reads NO os.environ / getenv — behavior never
    lives in an env var (env carries infrastructure config only: the DSN + lane, read in session.py /
    migrations/env.py). A behavior-switching env read in these modules would be the SQ_*-style sin."""
    for rel in (
        "db/lanes.py",
        "db/repository.py",
        "capture/events.py",
        "capture/determinism.py",
        "bundles/registrar.py",
    ):
        src = (_SRC / rel).read_text(encoding="utf-8")
        assert "os.environ" not in src and "getenv" not in src, f"{rel} reads an env var (behavior-by-flag?)"


def test_rt_one_queue_one_registrar():
    """L13: exactly ONE claim path (the worker delegate is a pass-through to Repository.claim, not a second
    queue) and ONE registrar concept (a single BundleRegistrar class). A parallel implementation would bite."""
    claim_src = (_SRC / "workers" / "claim.py").read_text(encoding="utf-8")
    assert "repo.claim(" in claim_src  # the delegate forwards to the one Repository.claim
    repo_src = (_SRC / "db" / "repository.py").read_text(encoding="utf-8")
    assert repo_src.count("def claim(") == 1  # exactly one claim method (no second queue)
    assert "SKIP LOCKED" in repo_src and "SKIP LOCKED" not in claim_src  # the only SKIP-LOCKED is here
    registrar_src = (_SRC / "bundles" / "registrar.py").read_text(encoding="utf-8")
    assert registrar_src.count("class BundleRegistrar") == 1  # one registrar concept
