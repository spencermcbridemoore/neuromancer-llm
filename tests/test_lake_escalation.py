"""B-7 lake-mirror persistent-block escalation (2026-07-20) — the NOTIFY-ONLY consumer of
system_health['lake_mirror_freshness']. Mirrors tests/test_escalation.py (the backup arm). Branches: row
missing / not blocked / blocked-within-onset / blocked-past-onset -> the actionable consequence+action message.
READ-ONLY (a plain SELECT). The escalate_after override is the §E·16 induced-test knob.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import text

from neuromancer_llm.governance.durability import LAKE_MIRROR_ROW, seed_row
from neuromancer_llm.governance.lake_escalation import evaluate_lake_mirror_block_escalation
from neuromancer_llm.governance.lake_freshness import LAKE_MIRROR_FRESHNESS_KEY

pytestmark = pytest.mark.pg


def _seed_blocked_days_ago(engine, days_ago: float) -> None:
    with engine.connect() as conn:
        seed_row(conn, LAKE_MIRROR_ROW)  # born 'blocked'
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE neuro.system_health SET status='blocked', "
                "measured_at = now() - (:d * interval '1 day') WHERE health_key = :k"
            ),
            {"d": days_ago, "k": LAKE_MIRROR_FRESHNESS_KEY},
        )


def test_row_missing_returns_none(repo):
    assert (
        evaluate_lake_mirror_block_escalation(repo.engine) is None
    )  # never seeded -> not the escalator's job


def test_not_blocked_returns_none(repo):
    with repo.engine.connect() as conn:
        seed_row(conn, LAKE_MIRROR_ROW)
    with repo.engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.system_health SET status='ok', measured_at=now() WHERE health_key = :k"),
            {"k": LAKE_MIRROR_FRESHNESS_KEY},
        )
    assert (
        evaluate_lake_mirror_block_escalation(repo.engine) is None
    )  # a fresh/ok mirror -> nothing to escalate


def test_blocked_within_onset_returns_none(repo):
    _seed_blocked_days_ago(repo.engine, 1)  # < the 3d pinned onset -> not yet persistent
    assert evaluate_lake_mirror_block_escalation(repo.engine) is None


def test_blocked_past_onset_returns_actionable_message(repo):
    _seed_blocked_days_ago(repo.engine, 5)  # > the 3d pinned onset
    msg = evaluate_lake_mirror_block_escalation(repo.engine)
    assert msg is not None
    assert "BLOB-LAKE MIRROR BLOCKED" in msg and "ACTION" in msg  # consequence + action copy


def test_override_onset_fires_on_any_current_block(repo):
    # the §E·16 induced-test knob: escalate_after=0 alerts on ANY current block (a just-recorded one) — the path
    # the deploy runbook uses to prove the real ping lands without waiting 3 days.
    _seed_blocked_days_ago(repo.engine, 0)
    assert evaluate_lake_mirror_block_escalation(repo.engine) is None  # within the 3d pin -> silent
    msg = evaluate_lake_mirror_block_escalation(repo.engine, escalate_after=_dt.timedelta(0))
    assert msg is not None and "BLOB-LAKE MIRROR BLOCKED" in msg
