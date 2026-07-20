"""GO-D-gate (A2-11a): the durability-staleness gate — the CONSUMER of the backup-freshness contract.

RED@5b9429d = ImportError (governance/health.py is a Stage-2 stub — no assert_backup_fresh yet).

Composes A2-10's producer (seed_backup_freshness + _bump_freshness) to set up each state, then asserts the
gate fails CLOSED per the pinned contract: row-missing / stale_after-NULL / drift / status!=ok / stale ->
DurabilityGateError; a drift/stale transition flips status 'ok'->'blocked' via a guarded CAS and notify()s
ONCE per transition (no storm); a fresh, provisioned, in-bound signal passes.

GO-D-wal + the signal-staleness follow-on: the WAL-arm branches (assert_wal_archiving_ok: row-missing /
status!=ok / SIGNAL STALE [now()-measured_at > the 1h repo-constant bound -> guarded-CAS flip + notify
once-per-transition — the dead-producer close] / healthy) and the composed assert_durability_ok cross-arm
invariant (a fresh backup never vouches for a missing wal_lag signal). Healthy fixtures heal the WAL arm
SYNTHETICALLY — the archiving-OFF test PG cannot produce a healthy real archiver read.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.durability import LAKE_MIRROR_ROW, WAL_LAG_ROW, seed_row
from neuromancer_llm.governance.freshness import BACKUP_FRESHNESS_KEY, seed_backup_freshness
from neuromancer_llm.governance.health import (
    DurabilityGateError,
    _flip_and_notify,
    assert_backup_fresh,
    assert_durability_ok,
    assert_wal_archiving_ok,
)
from neuromancer_llm.governance.lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
from neuromancer_llm.governance.notify import NotifyError
from neuromancer_llm.governance.probes import _bump_freshness
from neuromancer_llm.governance.wal_archiving import _bump_wal_lag
from neuromancer_llm.governance.wal_freshness import WAL_LAG_KEY


class _NotifyRecorder:
    """Stand in for governance.health.notify (the real ntfy POST is covered by tests/test_notify.py)."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str, *, topic: str | None = None) -> None:
        self.messages.append(message)


@pytest.fixture
def notify_rec(monkeypatch):
    rec = _NotifyRecorder()
    monkeypatch.setattr("neuromancer_llm.governance.health.notify", rec)
    return rec


def _seed_backup_fresh(engine) -> None:
    # produce a fresh, healthy backup_freshness row via the A2-10 producer (seed -> verified success bump)
    with engine.connect() as conn:
        seed_backup_freshness(conn)
    _bump_freshness(engine, ok=True, detail="verified backup 1.0GiB")


def _heal_wal(engine) -> None:
    # heal the WAL arm SYNTHETICALLY (GO-D-wal §10 MUST-FIX 1): the archiving-OFF test PG can never produce
    # a healthy real read, so healthy fixtures bump wal_lag 'ok' directly (the producer's own paths are
    # probed in tests/test_wal_archiving.py).
    with engine.connect() as conn:
        seed_row(conn, WAL_LAG_ROW)
    _bump_wal_lag(engine, ok=True, detail="test provisioning: healthy archiver")


def _seed_fresh(engine) -> None:
    # the WHOLE interlock healthy (both ADR-0020 arms) — the healthy-path fixture
    _seed_backup_fresh(engine)
    _heal_wal(engine)


def _exec(engine, sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _status(engine, key: str = BACKUP_FRESHNESS_KEY) -> str | None:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT status FROM neuro.system_health WHERE health_key = :k"),
            {"k": key},
        ).scalar_one_or_none()


# ---- pure: the pin resolves before any DB read (fail closed) ----------------------------------------


def test_gate_fails_closed_when_pin_absent(monkeypatch):
    monkeypatch.setattr("neuromancer_llm.governance.freshness.BACKUP_STALE_AFTER", None)

    class _NoEngine:
        def begin(self):
            raise AssertionError("the gate must resolve the pin BEFORE touching the DB")

    with pytest.raises(ConfigurationError):
        assert_backup_fresh(_NoEngine())


# ---- pg: the six branches ---------------------------------------------------------------------------


@pytest.mark.pg
def test_fresh_passes(repo, notify_rec):
    _seed_fresh(repo.engine)
    assert assert_backup_fresh(repo.engine) is None  # no raise
    assert _status(repo.engine) == "ok"  # not flipped
    assert notify_rec.messages == []  # not alerted


@pytest.mark.pg
def test_row_missing_blocks(repo, notify_rec):
    # repo truncates system_health -> the row is absent (never seeded)
    with pytest.raises(DurabilityGateError, match="missing"):
        assert_backup_fresh(repo.engine)
    assert notify_rec.messages == []


@pytest.mark.pg
def test_stale_after_null_blocks(repo, notify_rec):
    _seed_fresh(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET stale_after = NULL WHERE health_key = :k",
        k=BACKUP_FRESHNESS_KEY,
    )
    with pytest.raises(DurabilityGateError, match="stale_after"):
        assert_backup_fresh(repo.engine)
    assert notify_rec.messages == []


@pytest.mark.pg
def test_status_blocked_blocks_without_renotify(repo, notify_rec):
    # a fresh, in-bound row whose status was already flipped 'blocked' (by an A2-10 failure or a prior gate
    # flip) -> branch 4 BLOCKs SILENTLY (once-per-transition: the alert fired when it first flipped).
    _seed_fresh(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET status = 'blocked' WHERE health_key = :k",
        k=BACKUP_FRESHNESS_KEY,
    )
    with pytest.raises(DurabilityGateError, match="not 'ok'"):
        assert_backup_fresh(repo.engine)
    assert notify_rec.messages == []


@pytest.mark.pg
def test_drift_flips_notifies_blocks_once(repo, notify_rec):
    _seed_fresh(repo.engine)
    # mis-provision the sentinel to a value != the pinned bound (drift)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET stale_after = INTERVAL '3 days' WHERE health_key = :k",
        k=BACKUP_FRESHNESS_KEY,
    )
    with pytest.raises(DurabilityGateError, match="drift"):
        assert_backup_fresh(repo.engine)
    assert _status(repo.engine) == "blocked"  # flipped
    assert len(notify_rec.messages) == 1 and "drift" in notify_rec.messages[0].lower()
    # a second consult still BLOCKs but does NOT re-alert (status is now 'blocked' -> CAS misses)
    with pytest.raises(DurabilityGateError):
        assert_backup_fresh(repo.engine)
    assert len(notify_rec.messages) == 1


@pytest.mark.pg
def test_stale_flips_notifies_blocks_once(repo, notify_rec):
    _seed_fresh(repo.engine)
    # age the last-verified backup past the 8-day bound
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '9 days' WHERE health_key = :k",
        k=BACKUP_FRESHNESS_KEY,
    )
    with pytest.raises(DurabilityGateError, match="STALE"):
        assert_backup_fresh(repo.engine)
    assert _status(repo.engine) == "blocked"  # flipped
    assert len(notify_rec.messages) == 1 and "stale" in notify_rec.messages[0].lower()
    # second consult -> branch 4 (status='blocked') BLOCKs silently, no re-alert
    with pytest.raises(DurabilityGateError):
        assert_backup_fresh(repo.engine)
    assert len(notify_rec.messages) == 1


@pytest.mark.pg
def test_stale_recovers_after_a_fresh_backup(repo, notify_rec):
    # a flipped-blocked signal RECOVERS when A2-10 records a new verified backup (status ok + fresh measured_at)
    _seed_fresh(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '9 days' WHERE health_key = :k",
        k=BACKUP_FRESHNESS_KEY,
    )
    with pytest.raises(DurabilityGateError):
        assert_backup_fresh(repo.engine)
    _bump_freshness(repo.engine, ok=True, detail="new verified backup")  # A2-10 success -> ok + now()
    assert assert_backup_fresh(repo.engine) is None
    assert _status(repo.engine) == "ok"


# ---- pg: the CAS guard prevents clobbering a concurrent fresh success --------------------------------


@pytest.mark.pg
def test_flip_cas_guard_no_clobber_on_stale_read(repo, notify_rec):
    # the flip is pinned to the measured_at the gate READ; a fresh backup that landed since (advancing
    # measured_at) makes the CAS miss -> status stays 'ok', no false alert (the race the guard closes).
    _seed_fresh(repo.engine)
    import datetime as _dt

    stale_read = _dt.datetime(2000, 1, 1, tzinfo=_dt.UTC)  # a measured_at the current row does NOT have
    _flip_and_notify(
        repo.engine,
        key=BACKUP_FRESHNESS_KEY,
        reason="would-be stale alert",
        detail="x",
        guard_measured_at=stale_read,
    )
    assert _status(repo.engine) == "ok"  # NOT clobbered
    assert notify_rec.messages == []  # NOT alerted


# ---- pg: the WAL-archiving arm branches (GO-D-wal) ----------------------------------------------------


@pytest.mark.pg
def test_wal_arm_row_missing_blocks(repo, notify_rec):
    # repo truncates system_health -> the wal_lag row is absent (never seeded)
    with pytest.raises(DurabilityGateError, match="wal_lag.*missing"):
        assert_wal_archiving_ok(repo.engine)
    assert notify_rec.messages == []  # a missing row can't be flipped -> no notify


@pytest.mark.pg
def test_wal_arm_born_blocked_blocks(repo, notify_rec):
    with repo.engine.connect() as conn:
        seed_row(conn, WAL_LAG_ROW)  # born 'blocked' -> the gate BLOCKs until the first healthy probe
    with pytest.raises(DurabilityGateError, match="not 'ok'"):
        assert_wal_archiving_ok(repo.engine)
    assert notify_rec.messages == []


@pytest.mark.pg
def test_wal_arm_healthy_passes(repo):
    _heal_wal(repo.engine)
    assert assert_wal_archiving_ok(repo.engine) is None


# ---- pg: the WAL-arm signal-staleness branch (the dead-producer close, GO-D-wal follow-on) -----------
# NOTE test_wal_arm_born_blocked_blocks (above) doubles as the ORDERING guard for this branch: a born row
# (status='blocked', measured_at='epoch') is caught at the status!='ok' branch BEFORE is_stale, so its
# `notify_rec.messages == []` assertion reddens against any mutant that orders is_stale before the status check.


def test_wal_arm_pin_absent_fails_closed(monkeypatch):
    # the gate resolves the staleness bound BEFORE any DB read; an absent pin fails closed (never an
    # unpinned pass) — mirrors test_gate_fails_closed_when_pin_absent for the WAL arm.
    monkeypatch.setattr("neuromancer_llm.governance.wal_freshness.WAL_LAG_STALE_AFTER", None)

    class _NoEngine:
        def begin(self):
            raise AssertionError("the gate must resolve the wal_lag bound BEFORE touching the DB")

    with pytest.raises(ConfigurationError):
        assert_wal_archiving_ok(_NoEngine())


@pytest.mark.pg
def test_wal_stale_flips_notifies_blocks_once(repo, notify_rec):
    # THE dead-producer close: a stopped archiver probe leaves status='ok' with a frozen measured_at. Aging
    # measured_at past the 1h bound must flip 'ok'->'blocked' + alert ONCE, then BLOCK silently thereafter.
    _heal_wal(repo.engine)  # status='ok', measured_at=now()
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '2 hours' WHERE health_key = :k",
        k=WAL_LAG_KEY,
    )
    with pytest.raises(DurabilityGateError, match="STALE"):
        assert_wal_archiving_ok(repo.engine)
    assert _status(repo.engine, WAL_LAG_KEY) == "blocked"  # flipped
    assert len(notify_rec.messages) == 1 and "stale" in notify_rec.messages[0].lower()
    # a second consult -> branch 2 (status='blocked') BLOCKs silently, no re-alert (once-per-transition)
    with pytest.raises(DurabilityGateError):
        assert_wal_archiving_ok(repo.engine)
    assert len(notify_rec.messages) == 1


@pytest.mark.pg
def test_wal_stale_recovers_after_a_fresh_probe(repo, notify_rec):
    # a flipped-blocked wal_lag RECOVERS when the archiver probe records a fresh healthy read (ok + now())
    _heal_wal(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '2 hours' WHERE health_key = :k",
        k=WAL_LAG_KEY,
    )
    with pytest.raises(DurabilityGateError):
        assert_wal_archiving_ok(repo.engine)
    _bump_wal_lag(repo.engine, ok=True, detail="archiver recovered")  # a healthy probe -> ok + now()
    assert assert_wal_archiving_ok(repo.engine) is None
    assert _status(repo.engine, WAL_LAG_KEY) == "ok"


@pytest.mark.pg
def test_wal_stale_guard_through_gate_no_clobber_on_concurrent_bump(repo, notify_rec, monkeypatch):
    # Drive the wal staleness branch THROUGH the gate while a concurrent healthy archiver probe advances
    # measured_at BETWEEN the gate's read and its flip: the measured_at-guarded CAS must MISS -> no false
    # 'blocked'/alert, yet the gate still BLOCKs on its own stale read. FAILS if the branch drops
    # guard_measured_at (then the CAS matches on status alone -> clobbers the fresh probe's success).
    _heal_wal(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '2 hours' WHERE health_key = :k",
        k=WAL_LAG_KEY,
    )

    def _racing_flip(engine, *, key, reason, detail, guard_measured_at=None):
        _bump_wal_lag(engine, ok=True, detail="concurrent healthy archiver")  # a fresh probe lands first
        _flip_and_notify(engine, key=key, reason=reason, detail=detail, guard_measured_at=guard_measured_at)

    monkeypatch.setattr("neuromancer_llm.governance.health._flip_and_notify", _racing_flip)
    with pytest.raises(DurabilityGateError):  # still fail-closed on the gate's own stale read
        assert_wal_archiving_ok(repo.engine)
    assert _status(repo.engine, WAL_LAG_KEY) == "ok"  # the concurrent fresh probe was NOT clobbered
    assert notify_rec.messages == []  # no false stale alert


@pytest.mark.pg
def test_wal_flip_touches_only_its_own_arm(repo, notify_rec):
    # the _flip_and_notify key parametrization: a wal_lag staleness flip must touch ONLY the wal_lag row,
    # never backup_freshness (guards the shared helper against a cross-arm clobber).
    _seed_fresh(repo.engine)  # BOTH arms fresh + healthy
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '2 hours' WHERE health_key = :k",
        k=WAL_LAG_KEY,
    )
    with pytest.raises(DurabilityGateError, match="STALE"):
        assert_wal_archiving_ok(repo.engine)
    assert _status(repo.engine, WAL_LAG_KEY) == "blocked"  # wal flipped
    assert _status(repo.engine, BACKUP_FRESHNESS_KEY) == "ok"  # backup UNTOUCHED


@pytest.mark.pg
def test_wal_flip_notify_failure_still_blocks(repo, monkeypatch):
    # ADR-0019: a broken alert channel must still BLOCK. If notify() raises on a wal staleness flip, the gate
    # still fails LOUD (the flip persisted BEFORE notify -> once-per-transition holds).
    _heal_wal(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '2 hours' WHERE health_key = :k",
        k=WAL_LAG_KEY,
    )

    def _boom(message, *, topic=None):
        raise NotifyError("ntfy channel down")

    monkeypatch.setattr("neuromancer_llm.governance.health.notify", _boom)
    with pytest.raises((DurabilityGateError, NotifyError)):
        assert_wal_archiving_ok(repo.engine)
    assert _status(repo.engine, WAL_LAG_KEY) == "blocked"  # the flip persisted before the alert failed


# ---- pg: assert_durability_ok composes BOTH arms (the stable A2-11b consult entry point) --------------


@pytest.mark.pg
def test_durability_ok_delegates(repo, notify_rec):
    _seed_fresh(repo.engine)
    assert assert_durability_ok(repo.engine) is None
    _exec(
        repo.engine,
        "TRUNCATE neuro.system_health",
    )
    with pytest.raises(DurabilityGateError):
        assert_durability_ok(repo.engine)


@pytest.mark.pg
def test_durability_ok_blocks_when_wal_missing_despite_fresh_backup(repo, notify_rec):
    # THE cross-arm invariant (GO-D-wal §4 test h / the merged-handoff constraint): one-arm-healthy must
    # never read the other-arm-missing as healthy — a fresh backup does NOT vouch for the WAL signal.
    _seed_backup_fresh(repo.engine)  # backup arm fresh + healthy; wal_lag row deliberately ABSENT
    assert assert_backup_fresh(repo.engine) is None  # arm 1 alone passes...
    with pytest.raises(DurabilityGateError, match="wal_lag"):
        assert_durability_ok(repo.engine)  # ...but the composed gate BLOCKs
    _heal_wal(repo.engine)
    assert assert_durability_ok(repo.engine) is None  # both arms healthy -> passes


@pytest.mark.pg
def test_durability_ok_ignores_a_blocked_lake_mirror(repo, notify_rec):
    # B-7 fork ruling, pinned BEHAVIORALLY (not just declaratively via GATE_CONSULTED_KEYS): the blob-lake
    # mirror is NOTIFY-ONLY. With BOTH gating arms fresh + the lake_mirror_freshness row seeded 'blocked' (its
    # maximally-stale born state), assert_durability_ok STILL passes — a stale lake mirror alarms but never
    # blocks a canonical write. A future edit that inserts assert_lake_mirror_fresh into the gate body (the
    # over-scoped gating the owner ruled OUT) would redden this.
    _seed_fresh(repo.engine)  # backup + wal fresh + healthy
    with repo.engine.connect() as conn:
        seed_row(conn, LAKE_MIRROR_ROW)  # born 'blocked' + epoch — a maximally-stale lake mirror
    assert _status(repo.engine, LAKE_MIRROR_FRESHNESS_KEY) == "blocked"  # the lake arm IS blocked...
    assert assert_durability_ok(repo.engine) is None  # ...yet the durability gate does NOT block the write


# ---- pg: the stale-branch CAS guard THROUGH the gate (the race this unit exists to close) ------------


@pytest.mark.pg
def test_stale_guard_through_gate_no_clobber_on_concurrent_bump(repo, notify_rec, monkeypatch):
    # Drive branch 5 THROUGH assert_backup_fresh while a concurrent A2-10 success advances measured_at BETWEEN
    # the gate's read and its flip: the measured_at-guarded CAS must MISS -> no false 'blocked'/alert, yet the
    # gate still BLOCKs on its own stale read (fail closed, re-evaluated next call). This FAILS if health.py
    # drops the `guard_measured_at=row["measured_at"]` thread (then the CAS matches on status alone -> clobber).
    _seed_fresh(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '9 days' WHERE health_key = :k",
        k=BACKUP_FRESHNESS_KEY,
    )

    def _racing_flip(engine, *, key, reason, detail, guard_measured_at=None):
        _bump_freshness(engine, ok=True, detail="concurrent verified backup")  # a fresh success lands first
        _flip_and_notify(engine, key=key, reason=reason, detail=detail, guard_measured_at=guard_measured_at)

    monkeypatch.setattr("neuromancer_llm.governance.health._flip_and_notify", _racing_flip)
    with pytest.raises(DurabilityGateError):  # still fail-closed on the gate's own stale read
        assert_backup_fresh(repo.engine)
    assert _status(repo.engine) == "ok"  # the concurrent fresh success was NOT clobbered
    assert notify_rec.messages == []  # no false stale alert


# ---- pg: a broken alert channel must still BLOCK (ADR-0019) ------------------------------------------


@pytest.mark.pg
def test_flip_notify_failure_still_blocks(repo, monkeypatch):
    # ADR-0019: a broken alert channel must NOT let a write through. If notify() raises on a stale flip, the
    # gate still fails LOUD (the flip persisted BEFORE notify -> once-per-transition holds; a blocking
    # exception propagates so A2-11b's consult still refuses the write).
    _seed_fresh(repo.engine)
    _exec(
        repo.engine,
        "UPDATE neuro.system_health SET measured_at = now() - INTERVAL '9 days' WHERE health_key = :k",
        k=BACKUP_FRESHNESS_KEY,
    )

    def _boom(message, *, topic=None):
        raise NotifyError("ntfy channel down")

    monkeypatch.setattr("neuromancer_llm.governance.health.notify", _boom)
    with pytest.raises((DurabilityGateError, NotifyError)):  # any blocking exception refuses the write
        assert_backup_fresh(repo.engine)
    assert _status(repo.engine) == "blocked"  # the flip persisted before the alert failed (fail closed)
