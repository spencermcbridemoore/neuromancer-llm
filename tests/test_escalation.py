"""Mirror-arm hardening (2026-07-17, log:214/§D): the persistent-block ESCALATION path.

RED before this unit: governance/escalation.py, freshness.resolve_backup_block_escalate_after, and
`neuro probe escalate` did not exist. The root cause (observed first-hand): the once-per-backup-cycle
OnFailure ping fired BOTH times the mirror was down but was delivered-and-missed — cadence (only every
BASE_BACKUP_INTERVAL) + copy (names the failed UNIT, not the consequence). This re-alerts DAILY with a
consequence-and-action message while backup_freshness stays 'blocked' past one BASE_BACKUP_INTERVAL.
READ-ONLY against system_health; the alert is the notify(), the record is the ntfy history + the existing
'blocked' probe_reports rows.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from neuromancer_llm.cli.app import app
from neuromancer_llm.governance.durability import seed_all
from neuromancer_llm.governance.escalation import evaluate_backup_block_escalation
from neuromancer_llm.governance.freshness import resolve_backup_block_escalate_after

_runner = CliRunner()


def _seed(engine) -> None:
    with engine.connect() as conn:
        seed_all(conn)


def _set_backup_freshness(engine, *, status: str, age_days: int) -> None:
    """Force backup_freshness into a chosen (status, age) state — the escalation reads status + measured_at."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE neuro.system_health SET status=:s, measured_at=now() - (:d * interval '1 day') "
                "WHERE health_key='backup_freshness'"
            ),
            {"s": status, "d": age_days},
        )


# ---- the pinned onset (DB-free) --------------------------------------------------------------------------


def test_escalate_onset_is_the_base_backup_interval():
    # bound = BASE_BACKUP_INTERVAL by reference (one-implementation-per-concept; no second magic number)
    from neuromancer_llm.governance.provisioning_invariants import resolve_base_backup_interval

    assert resolve_backup_block_escalate_after() == resolve_base_backup_interval()


def test_escalate_onset_fails_closed_when_interval_pin_absent(monkeypatch):
    from neuromancer_llm.db.lanes import ConfigurationError
    from neuromancer_llm.governance import provisioning_invariants

    monkeypatch.setattr(provisioning_invariants, "BASE_BACKUP_INTERVAL", None)
    with pytest.raises(ConfigurationError):
        resolve_backup_block_escalate_after()


# ---- evaluate (read-only) --------------------------------------------------------------------------------


@pytest.mark.pg
def test_evaluate_returns_message_on_persistent_block(repo):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="blocked", age_days=10)
    msg = evaluate_backup_block_escalation(repo.engine)
    assert msg is not None
    assert "cloud-only" in msg and "sshd" in msg and "~10d" in msg


@pytest.mark.pg
def test_evaluate_none_when_block_within_onset(repo):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="blocked", age_days=1)  # < the 2d BASE_BACKUP_INTERVAL onset
    assert evaluate_backup_block_escalation(repo.engine) is None


@pytest.mark.pg
def test_evaluate_none_when_status_ok(repo):
    _seed(repo.engine)
    _set_backup_freshness(
        repo.engine, status="ok", age_days=10
    )  # old but ok -> not a block, nothing to escalate
    assert evaluate_backup_block_escalation(repo.engine) is None


@pytest.mark.pg
def test_evaluate_none_when_row_missing(repo):
    # unseeded (the fixture truncates): no backup_freshness row -> None (a never-seeded signal is the gate's /
    # `neuro db durability seed`'s concern, not the escalator's)
    assert evaluate_backup_block_escalation(repo.engine) is None


@pytest.mark.pg
def test_evaluate_override_fires_on_recent_block(repo):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="blocked", age_days=0)  # a just-recorded block
    assert evaluate_backup_block_escalation(repo.engine) is None  # within the pinned onset -> silent
    # the operator override (the induced-failure-test knob): onset 0 -> any CURRENT block escalates now
    assert evaluate_backup_block_escalation(repo.engine, escalate_after=_dt.timedelta(0)) is not None


# ---- the CLI verb (notify monkeypatched; the real delivery channel is proven at the live induced test) ----


@pytest.mark.pg
def test_escalate_cli_no_alert_when_ok(repo, monkeypatch):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="ok", age_days=0)
    calls: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m: calls.append(m))
    r = _runner.invoke(app, ["probe", "escalate", "--lane", "test"])
    assert r.exit_code == 0, r.output
    assert "no alert" in r.output and calls == []


@pytest.mark.pg
def test_escalate_cli_alerts_and_notifies_on_persistent_block(repo, monkeypatch):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="blocked", age_days=10)
    calls: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m: calls.append(m))
    r = _runner.invoke(app, ["probe", "escalate", "--lane", "test"])
    assert r.exit_code == 0, r.output
    assert "ESCALATED" in r.output
    assert len(calls) == 1 and "cloud-only" in calls[0] and "sshd" in calls[0]


@pytest.mark.pg
def test_escalate_cli_override_fires_immediately(repo, monkeypatch):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="blocked", age_days=0)  # recent block
    calls: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m: calls.append(m))
    r = _runner.invoke(app, ["probe", "escalate", "--lane", "test", "--escalate-after-hours", "0"])
    assert r.exit_code == 0, r.output
    assert "ESCALATED" in r.output and len(calls) == 1
