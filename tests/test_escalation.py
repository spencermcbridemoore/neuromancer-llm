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


def _assert_backup_copy(msg: str | None, *, measured_at) -> None:
    """★ EVERY copy claim for this arm, asserted ON ONE MESSAGE — deliberately not split across probes.

    AC1 ("sshd" absent) and AC9 ("repo1/repo2" absent) are NEGATIVE containments, and a negative containment
    passes on `""`, on `None`, and on a message that lost its entire action clause. Co-locating them with the
    positive claims and a substance floor is what stops them passing vacuously; split across separate tests
    they would be the happy-precondition family.
    """
    assert msg is not None
    assert len(msg) > 400, "the message lost its substance; the negative assertions below would go vacuous"

    # (1) the defect itself: the guessed remedy is GONE, in both its forms.
    assert "sshd" not in msg and "Get-Service" not in msg
    # (2) and the copy says, in terms, that it does not know the cause.
    assert "CAUSE NOT ESTABLISHED" in msg
    # (3) the three discriminating steps: the COMMAND, then the CONTRAST that makes it discriminating.
    #     ⚠ THE CONTRAST IS ASSERTED AS A PHRASE, NOT AS TWO TOKENS. The mutation matrix MEASURED that
    #     `"ABSENT" in msg and "offline" in msg` SURVIVES a mutation that destroys the contrast between
    #     them — both words remain while the sentence binding them is gone. Two tokens are not a contrast.
    assert "OpenSSH/Operational" in msg
    assert "'[preauth]' lines: WSL2 NAT, not the VM" in msg
    assert "tailscale status" in msg
    assert "a down peer is still LISTED, as 'offline'" in msg
    assert "/dev/tcp" in msg
    assert "'refused' would mean you reached the host" in msg
    # (4) step 0 — the recorded reason, which is what scopes steps 1-3 to one of ~13 disjuncts.
    # ⚠ REWORDED 2026-08-28 (the repo3 unit's amend), and PINNED AS PHRASES rather than as the bare token
    # `neuro probe report`, which survived every wording change this step has ever had. Step 0 names TWO
    # reads because the closure that made it followable has two halves: `detail=` is the ONLY trace of the
    # states that write no probe_reports row, and `--key` is what makes the probe_reports half reachable past
    # the 15-minute WAL-archiver probe. The old copy asserted the opposite of the first half.
    assert "run `neuro probe report`" in msg
    assert "Every durability row prints a `detail=` field" in msg
    assert "two states write NO probe_reports row at all and their reason exists ONLY there" in msg
    assert "`neuro probe report --key <the health_key at the start of that line>`" in msg
    assert "The flag is not optional in practice" in msg
    # the NEGATIVE half: the superseded claim must be gone, and it is asserted alongside the positives above
    # so it cannot pass vacuously on a gutted string (the co-location rule this file already follows).
    assert "records no reason at all" not in msg
    # (5) the disjunction is disclosed rather than narrowed to the transport. ⚠ ASSERT THE DISCLOSURE, NOT
    #     THE WORD `pgbackrest`: the mutation matrix MEASURED that a bare `"pgbackrest" in msg` SURVIVES
    #     gutting this clause, because step 0's reason list ("recency: / pgbackrest verify / ...") also
    #     contains the token. A probe satisfied by an unrelated part of the same string pins nothing.
    assert "folds SEVERAL checks into one boolean" in msg
    # ⚠ CHANGED 2026-08-28 (the repo3 unit), and the change was FORCED, not cosmetic. This used to assert
    # "EVERY configured pgbackrest repo". That was true while the driver derived its repo set from the info
    # JSON; the GATE_BASIS_REPOS pin made it FALSE, because a configured-but-unpinned repo3 is reported
    # without setting this row. Leaving the old assertion would have pinned a checkable falsehood in place.
    # Both halves are asserted as PHRASES: the basis claim, AND the sentence that tells an operator a
    # non-basis repo has its own row — the second is what stops someone reading "gate basis" as "all repos".
    assert "every repo in the pinned GATE BASIS" in msg
    assert "OUTSIDE the basis is reported in this row's detail but does NOT set it" in msg
    assert "EVERY configured pgbackrest repo" not in msg
    assert "cloud-repo cadence stall lands here too" in msg
    # (6) what worked is preserved: the age and the last-good timestamp, the timestamp READ BACK FROM THE DB
    #     rather than from the message that produced it (written-and-never-read-back).
    assert "~10d" in msg and str(measured_at) in msg
    # (7) the superseded consequence clause is gone in both its wordings.
    assert "cloud-only" not in msg and "DEGRADED" not in msg
    # (8) NOTIFY-ONLY belongs to the LAKE arm only — asserting it here would be a checkable falsehood, since
    #     backup_freshness IS inside health.GATE_CONSULTED_KEYS. A shared composer makes this leak easy.
    assert "NOTIFY-ONLY" not in msg
    # (9) the arm's own coordinates — an arm SWAP is the likeliest refactor error, and the `not in` half is
    #     what makes a swap RED rather than green.
    assert msg.startswith("neuromancer OFF-CLOUD BACKUP MIRROR BLOCKED")
    assert "no confirmed off-cloud backup" in msg
    assert "neuro-backup.service" in msg and "neuro-lake-mirror.service" not in msg


def _measured_at(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT measured_at FROM neuro.system_health WHERE health_key='backup_freshness'")
        ).scalar_one()


@pytest.mark.pg
def test_evaluate_returns_message_on_persistent_block(repo):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="blocked", age_days=10)
    _assert_backup_copy(evaluate_backup_block_escalation(repo.engine), measured_at=_measured_at(repo.engine))


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
    expected = evaluate_backup_block_escalation(repo.engine)
    r = _runner.invoke(app, ["probe", "escalate", "--lane", "test"])
    assert r.exit_code == 0, r.output
    assert "ESCALATED" in r.output
    # ★ EQUALITY on the notify() argument, not containment. Containment is MONOTONE: a CLI that APPENDED its
    # own remedy ("Also check Get-Service sshd.") would still contain the runner's message and pass. Equality
    # is what forbids a second copy at the CLI layer — the log:242 two-layer defect, pinned rather than
    # re-checked by hand. `expected` comes from an INDEPENDENT evaluate call, not from the captured output.
    assert calls == [expected]
    _assert_backup_copy(calls[0], measured_at=_measured_at(repo.engine))
    assert calls[0] in r.output  # and the echo carries it verbatim


@pytest.mark.pg
def test_escalate_cli_override_fires_immediately(repo, monkeypatch):
    _seed(repo.engine)
    _set_backup_freshness(repo.engine, status="blocked", age_days=0)  # recent block
    calls: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m: calls.append(m))
    r = _runner.invoke(app, ["probe", "escalate", "--lane", "test", "--escalate-after-hours", "0"])
    assert r.exit_code == 0, r.output
    assert "ESCALATED" in r.output and len(calls) == 1
