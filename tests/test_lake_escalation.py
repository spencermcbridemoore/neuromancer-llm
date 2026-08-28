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


def _measured_at(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT measured_at FROM neuro.system_health WHERE health_key = :k"),
            {"k": LAKE_MIRROR_FRESHNESS_KEY},
        ).scalar_one()


def _assert_lake_copy(msg: str | None, *, measured_at, days: int) -> None:
    """★ EVERY copy claim for this arm on ONE message — the mirror of `_assert_backup_copy`; see its
    docstring for why the negative containments must not be split out."""
    assert msg is not None
    assert len(msg) > 400, "the message lost its substance; the negative assertions below would go vacuous"

    assert "sshd" not in msg and "Get-Service" not in msg
    assert "CAUSE NOT ESTABLISHED" in msg
    assert "ACTION" in msg  # the label survives; what changed is that it now carries a PROCEDURE
    # the CONTRASTS as phrases, not as token pairs — see `_assert_backup_copy` for the measurement that
    # forced this: two tokens both survive a mutation that destroys the sentence binding them.
    assert "OpenSSH/Operational" in msg
    assert "'[preauth]' lines: WSL2 NAT, not the VM" in msg
    assert "tailscale status" in msg
    assert "a down peer is still LISTED, as 'offline'" in msg
    assert "/dev/tcp" in msg
    assert "'refused' would mean you reached the host" in msg
    # ⚠ The SAME phrases as the backup arm asserts, on purpose: both arms share OFF_CLOUD_MIRROR_TRIAGE by
    # identity, so a reword that satisfied one arm's pins and broke the other would mean the sharing had
    # quietly stopped. Asserted here too rather than trusted to the identity pin, which cannot see wording.
    assert "run `neuro probe report`" in msg
    # ⚠ NOT "Every durability row prints a `detail=` field" -- MEASURED: that phrase STRADDLES the constant's
    # implicit-concatenation boundary ("...prints a `detail=` " + "field, and THIS row's line..."), so a
    # mutation deleting the whole reason-for-reading-it clause leaves "field" behind and the assertion PASSES.
    # It is the log:274 lesson one level down: a phrase that spans a wrap point pins only the wrap point.
    # Assert the clause that carries the MEANING and sits wholly inside one fragment.
    assert "two states write NO probe_reports row at all and their reason exists ONLY there" in msg
    assert "`neuro probe report --key <the health_key at the start of that line>`" in msg
    assert "records no reason at all" not in msg
    assert f"~{days}d" in msg and str(measured_at) in msg
    assert "cloud-only" not in msg and "DEGRADED" not in msg
    # ⚠ The former copy called this "the HARD GATE", one clause from the truth that it does not gate — and a
    # reader in a prior session concluded from a blocked lake row that the ADR-0020 gate had closed. The claim
    # is scoped to the GATE, not to "blocking a write": the registered capture-path preflight would block
    # writes with every existing pin still green.
    assert "HARD GATE" not in msg
    assert "not consulted by the ADR-0020 durability gate" in msg
    # the arm's own coordinates; the `not in` half is what makes an arm SWAP red rather than green.
    assert msg.startswith("neuromancer BLOB-LAKE MIRROR BLOCKED")
    assert "no confirmed lake mirror" in msg
    assert "neuro-lake-mirror.service" in msg and "neuro-backup.service" not in msg


def test_blocked_past_onset_returns_actionable_message(repo):
    _seed_blocked_days_ago(repo.engine, 5)  # > the 3d pinned onset
    _assert_lake_copy(
        evaluate_lake_mirror_block_escalation(repo.engine),
        measured_at=_measured_at(repo.engine),
        days=5,
    )


def test_override_onset_fires_on_any_current_block(repo):
    # the §E·16 induced-test knob: escalate_after=0 alerts on ANY current block (a just-recorded one) — the path
    # the deploy runbook uses to prove the real ping lands without waiting 3 days.
    _seed_blocked_days_ago(repo.engine, 0)
    assert evaluate_lake_mirror_block_escalation(repo.engine) is None  # within the 3d pin -> silent
    msg = evaluate_lake_mirror_block_escalation(repo.engine, escalate_after=_dt.timedelta(0))
    assert msg is not None and "BLOB-LAKE MIRROR BLOCKED" in msg
