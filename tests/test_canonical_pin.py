"""A1-15 (A1 runbook §10.4): the repo-pinned canonical instance_uuid resolves at engine construction.

Before A1-15, expected_uuid was plumbed through the whole verified-engine stack but NO live caller
passed it — a same-lane restored clone with a FRESH instance_uuid verified as canonical (the
dead-parameter fail-open; plan §1.3 #2). These probes pin the closure at the shared choke point
(session.verify_engine — Repository/BundleRegistrar call it directly, bypassing make_verified_engine):

  * (a) canonical lane + matching pin verifies with NO caller-passed uuid (the pin resolves);
  * (b) HEADLINE — a canonical-lane DB whose instance_uuid differs from the pin is REFUSED at engine
    construction without the caller passing a uuid (proves the wiring, not just assert_lane);
  * (c) canonical lane + ABSENT pin -> ConfigurationError (fail closed, never a silent unpinned pass);
  * the §10.3 operator gate: `neuro db verify --lane canonical` auto-resolves the pin; a wrong
    --expected-uuid is the fail-closed negative.

The shared session DB is provisioned 'test'; each probe temporarily rewrites the singleton identity
row (COMMITTED — verify_engine opens its own connection, so a rolled-back transaction would be
invisible to it) and restores it in finally. canonical_instance imports stay function-local outside
probe (b) so the headline probe demonstrates a BEHAVIORAL red on pre-A1-15 code (no import error
masking the fail-open).
"""

from __future__ import annotations

import contextlib
import uuid as _uuid

import pytest
from sqlalchemy import create_engine, text

from neuromancer_llm.db.lanes import ConfigurationError, LaneAssertionError
from neuromancer_llm.db.session import verify_engine

pytestmark = pytest.mark.pg

# Deliberately NOT the pin (the recorded pin is a random v4; this fixed nil-adjacent uuid cannot
# collide with a gen_random_uuid() output in practice and keeps the probes deterministic).
FRESH_CLONE_UUID = "00000000-0000-4000-8000-000000000000"


@contextlib.contextmanager
def _identity_rewritten(engine, *, lane: str, instance_uuid: str):
    """Temporarily rewrite the singleton identity row (committed), restoring the provisioned 'test'
    row in finally so the shared session DB stays consistent for the rest of the suite."""
    with engine.begin() as conn:
        saved = conn.execute(text("SELECT lane, instance_uuid FROM neuro.database_identity")).one()
        conn.execute(
            text("UPDATE neuro.database_identity SET lane = :l, instance_uuid = :u"),
            {"l": lane, "u": _uuid.UUID(instance_uuid)},
        )
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE neuro.database_identity SET lane = :l, instance_uuid = :u"),
                {"l": saved.lane, "u": saved.instance_uuid},
            )


def test_canonical_pin_matching_verifies(engine):
    """§10.4(a): canonical lane + matching pin — verify_engine succeeds with NO caller-passed uuid;
    the repo pin (db/canonical_instance.py) resolves at the choke point."""
    from neuromancer_llm.db.canonical_instance import CANONICAL_INSTANCE_UUID

    assert CANONICAL_INSTANCE_UUID is not None, "the committed pin must hold the recorded uuid"
    with _identity_rewritten(engine, lane="canonical", instance_uuid=str(CANONICAL_INSTANCE_UUID)):
        assert verify_engine(engine, expected_lane="canonical") is engine


def test_canonical_fresh_clone_uuid_refused_without_caller_uuid(engine):
    """§10.4(b) HEADLINE: a canonical-lane DB whose instance_uuid DIFFERS from the pin (the
    restored-clone shape) is refused AT ENGINE CONSTRUCTION with the caller passing nothing — the
    wiring is live, not just assert_lane's dormant comparison. Both direct verify_engine and the
    Repository constructor (which calls verify_engine directly, bypassing make_verified_engine —
    the Stage-B worker path) refuse."""
    from neuromancer_llm.db.repository import Repository

    with _identity_rewritten(engine, lane="canonical", instance_uuid=FRESH_CLONE_UUID):
        with pytest.raises(LaneAssertionError, match="instance_uuid mismatch"):
            verify_engine(engine, expected_lane="canonical")
        with pytest.raises(LaneAssertionError, match="instance_uuid mismatch"):
            Repository(engine, expected_lane="canonical")


def test_canonical_absent_pin_fails_closed(engine, monkeypatch):
    """§10.4(c): canonical lane + ABSENT pin -> ConfigurationError (fail closed, not an unpinned pass)
    — an optional pin silently defaulting to None would recreate the dead-parameter fail-open in one
    hop. staging/test resolve no pin and are unchanged (the rest of the suite, which runs the 'test'
    lane through this same choke point, is the standing proof)."""
    from neuromancer_llm.db import canonical_instance

    monkeypatch.setattr(canonical_instance, "CANONICAL_INSTANCE_UUID", None)
    with (
        _identity_rewritten(engine, lane="canonical", instance_uuid=FRESH_CLONE_UUID),
        pytest.raises(ConfigurationError, match="unpinned"),
    ):
        verify_engine(engine, expected_lane="canonical")
    # resolution precedes the verifying connection: an engine pointing NOWHERE still fails on the
    # absent pin (ConfigurationError), not on connect — the guard never touches an unpinned target.
    dead = create_engine("postgresql+psycopg://nobody:x@127.0.0.1:9/void", future=True)
    with pytest.raises(ConfigurationError, match="unpinned"):
        verify_engine(dead, expected_lane="canonical")


def test_cli_verify_expected_uuid_pin(engine):
    """§10.3: the operator acceptance gate itself exercises the pin — `neuro db verify --lane
    canonical` auto-resolves the repo pin (exit 0, prints the identity); a wrong --expected-uuid is
    the fail-closed negative (exit 1 via _clean_fail translating the lanes.py instance_uuid
    mismatch); the test lane stays pin-free (unchanged)."""
    from typer.testing import CliRunner

    from neuromancer_llm.cli.app import app
    from neuromancer_llm.db.canonical_instance import CANONICAL_INSTANCE_UUID

    runner = CliRunner()
    # the CLI connects via NEURO_DATABASE_URL, seated to the session test DB by conftest._build_schema
    with _identity_rewritten(engine, lane="canonical", instance_uuid=str(CANONICAL_INSTANCE_UUID)):
        ok = runner.invoke(app, ["db", "verify", "--lane", "canonical"])
        assert ok.exit_code == 0, ok.output
        assert f"instance_uuid={CANONICAL_INSTANCE_UUID}" in ok.output
        bad = runner.invoke(app, ["db", "verify", "--lane", "canonical", "--expected-uuid", FRESH_CLONE_UUID])
        assert bad.exit_code == 1
        # the §10.3 VERBATIM negative: the lanes.py mismatch translated by _clean_fail — exit 1 alone
        # would also match an arbitrary crash, so pin the operator-facing diagnostic itself
        assert "error (fail closed): instance_uuid mismatch" in bad.stderr
    ok = runner.invoke(app, ["db", "verify", "--lane", "test"])  # no pin resolved off-canonical
    assert ok.exit_code == 0, ok.output
