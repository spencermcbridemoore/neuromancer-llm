"""R1: the fail-closed identity guard at the write choke point.

A Repository (and the BundleRegistrar, exercised in tests/seam/) verifies the connected DB's identity at
construction — so no write path exists on an unprovisioned or wrong-lane target, and the canonical-lane
caller can additionally pin the repo-known instance_uuid.
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import text

from neuromancer_llm.db.lanes import LaneAssertionError
from neuromancer_llm.db.repository import Repository

pytestmark = pytest.mark.pg


def test_wrong_lane_blocks_writes(engine):
    # the test DB is provisioned 'test'; demanding 'canonical' fails closed BEFORE any write path exists
    with pytest.raises(LaneAssertionError):
        Repository(engine, expected_lane="canonical")


def test_unprovisioned_blocks_writes(engine):
    # remove the identity row, confirm the guard refuses to construct, then restore it for the rest of
    # the suite (try/finally keeps the shared session DB consistent even if the assertion fails).
    with engine.begin() as conn:
        saved = conn.execute(text("SELECT lane, instance_uuid FROM neuro.database_identity")).one()
        conn.execute(text("DELETE FROM neuro.database_identity"))
    try:
        with pytest.raises(LaneAssertionError):
            Repository(engine, expected_lane="test")
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO neuro.database_identity (lane, instance_uuid) VALUES (:l, :u)"),
                {"l": saved.lane, "u": saved.instance_uuid},
            )


def test_expected_uuid_pins_instance(engine):
    # Canonical-lane callers pin the repo-known instance_uuid so a restored clone (fresh uuid) is rejected
    # — lane AND repo-pinned uuid. Exercised here on the 'test' lane (the mechanism is lane-agnostic).
    with engine.connect() as conn:
        instance_uuid = conn.execute(text("SELECT instance_uuid FROM neuro.database_identity")).scalar_one()

    # correct uuid -> constructs fine
    Repository(engine, expected_lane="test", expected_uuid=instance_uuid)

    # wrong uuid -> fail closed
    with pytest.raises(LaneAssertionError):
        Repository(engine, expected_lane="test", expected_uuid=_uuid.uuid4())
