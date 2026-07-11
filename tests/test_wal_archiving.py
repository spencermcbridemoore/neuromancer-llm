"""GO-D-wal (pre-cliff item 3): the WAL-archiving arm — the PRODUCER + the interim policy core.

RED@1d198b2 = ModuleNotFoundError (neuromancer_llm.governance.wal_archiving absent).

Probes the owner-ruled interim (GO-D-wal GO-input 2, 2026-07-11: archiving-enabled check + self-healing
current-failure rule; NO numeric threshold):
  - classify_archiver (pure policy core): mode='off' blocks; mechanism-less blocks; the literal
    "(disabled)" show-hook sentinel is NOT a mechanism (fold 11); mode='always' is enabled (fold 5);
    a CURRENT failure blocks (never-archived / newer-than-success / tie fails closed); a RECOVERED archiver
    with failed_count>0 is OK (the self-healing discriminator vs cumulative failed_count>0 — encodes the
    owner's ratified GO-input-2 choice); enabled + both timestamps NULL is OK (fold 9, documented);
  - the producer mirrors run_backup_probe: healthy bump advances measured_at + audits; a blocked
    classification is RECORDED then RAISES (persist-before-raise); an unseeded row aborts BEFORE the reader
    (FOLD-4 analog); a RAISING reader persists 'blocked' then raises (§10 MUST-FIX 3 — never a stale 'ok');
    a bump on an absent row is rowcount-0 fail-loud;
  - the REAL reader against the live archiving-OFF test PG: pins archive_mode='off', the "(disabled)"
    sentinel empirically, and the fail-closed end-to-end probe (non-synthetic).
The gate branch (assert_wal_archiving_ok) is probed in tests/test_health_gate.py; the registry append in
tests/test_durability_seed.py; the grant legality in tests/redteam/test_rt_roles.py.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import text

from neuromancer_llm.governance.durability import WAL_LAG_ROW, seed_row
from neuromancer_llm.governance.wal_archiving import (
    WAL_LAG_KEY,
    ArchiverSnapshot,
    WalArchiverProbeError,
    _bump_wal_lag,
    classify_archiver,
    read_pg_stat_archiver,
    run_wal_archiver_probe,
)

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
_T1 = _dt.datetime(2026, 7, 10, 12, 0, 0, tzinfo=_dt.UTC)
_T2 = _dt.datetime(2026, 7, 10, 13, 0, 0, tzinfo=_dt.UTC)


def _snap(**over) -> ArchiverSnapshot:
    """A HEALTHY archiver snapshot by default (mode=on, a real command, a past success, no failure)."""
    base: dict = {
        "archive_mode": "on",
        "archive_command": "pgbackrest --stanza=neuro archive-push %p",
        "archive_library": "",
        "failed_count": 0,
        "last_failed_time": None,
        "last_archived_time": _T2,
        "last_archived_wal": "000000010000000000000042",
    }
    base.update(over)
    return ArchiverSnapshot(**base)


class _ScriptedReader:
    """A synthetic ArchiverStatsReader (the injectable seam). Records call count so a probe can assert it
    was NOT invoked; returns/raises a scripted result."""

    def __init__(self, *, snap: ArchiverSnapshot | None = None, raises: Exception | None = None) -> None:
        self.calls = 0
        self._snap = snap
        self._raises = raises

    def __call__(self, engine) -> ArchiverSnapshot:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._snap is not None
        return self._snap


def _seed_wal(engine) -> bool:
    with engine.connect() as conn:
        return seed_row(conn, WAL_LAG_ROW)


def _read(engine):
    with engine.begin() as conn:
        return (
            conn.execute(
                text(
                    "SELECT status, measured_at, stale_after, detail "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"k": WAL_LAG_KEY},
            )
            .mappings()
            .one_or_none()
        )


def _probe_reports(engine, *, status: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM neuro.probe_reports WHERE probe_key = :k AND status = :s"),
            {"k": WAL_LAG_KEY, "s": status},
        ).scalar_one()


# ---- pure: classify_archiver (the interim policy core) ----------------------------------------------


def test_classify_healthy_is_ok():
    ok, detail = classify_archiver(_snap())
    assert ok is True
    assert "failed_count=0" in detail  # the raw stats stay auditable in the detail string


def test_classify_mode_off_blocks():
    ok, detail = classify_archiver(_snap(archive_mode="off"))
    assert ok is False
    assert "not configured" in detail


def test_classify_no_mechanism_blocks():
    # archive_mode=on but neither archive_command nor archive_library set: PG retains WAL and archives
    # NOTHING with failed_count staying 0 — the fail-open a failed_count-only rule would miss.
    ok, _ = classify_archiver(_snap(archive_command="", archive_library=""))
    assert ok is False


def test_classify_disabled_sentinel_is_not_a_mechanism():
    # fold 11: every display path substitutes the literal "(disabled)" for archive_command when archiving
    # is inactive (PG show_archive_command hook) — a NON-empty string that must never read as configured.
    ok, _ = classify_archiver(_snap(archive_command="(disabled)", archive_library=""))
    assert ok is False


def test_classify_always_mode_is_enabled():
    # fold 5: archive_mode has three values (off/on/always); 'always' is a legitimate enabled mode and must
    # not be over-blocked by an `== 'on'` check.
    ok, _ = classify_archiver(_snap(archive_mode="always"))
    assert ok is True


@pytest.mark.parametrize(
    ("last_failed", "last_archived"),
    [
        (_T1, None),  # failed and NEVER archived
        (_T2, _T1),  # most recent attempt failed (failure newer than the last success)
        (_T1, _T1),  # tie -> fail closed
    ],
)
def test_classify_current_failure_blocks(last_failed, last_archived):
    ok, detail = classify_archiver(
        _snap(failed_count=3, last_failed_time=last_failed, last_archived_time=last_archived)
    )
    assert ok is False
    assert "failed_count=3" in detail


def test_classify_recovered_failure_is_ok_self_healing():
    # THE GO-input-2 discriminator (owner-ratified 2026-07-11): cumulative failed_count>0 with a LATER
    # successful archive is healthy — the rule self-heals on real recovery instead of wedging until a
    # signal-hiding pg_stat_reset_shared('archiver'). Reddens if the build implements cumulative-blocking.
    ok, detail = classify_archiver(_snap(failed_count=5, last_failed_time=_T1, last_archived_time=_T2))
    assert ok is True
    assert "failed_count=5" in detail  # the cumulative history stays visible for audit


def test_classify_enabled_but_never_archived_is_ok_documented():
    # fold 9 (documented decision): archiving enabled + both timestamps NULL (just-enabled, or post
    # pg_stat_reset_shared) -> OK. Low blast radius; the signal-staleness deferral (§7.2) owns the "has
    # archiving proven itself" invariant.
    ok, _ = classify_archiver(_snap(last_failed_time=None, last_archived_time=None, last_archived_wal=None))
    assert ok is True


# ---- pg: the producer (mirrors run_backup_probe) -----------------------------------------------------


@pytest.mark.pg
def test_probe_healthy_bumps_and_audits(repo):
    _seed_wal(repo.engine)
    reader = _ScriptedReader(snap=_snap())
    snap = run_wal_archiver_probe(repo.engine, stats_reader=reader)
    assert snap.archive_mode == "on" and reader.calls == 1
    row = _read(repo.engine)
    assert row is not None
    assert row["status"] == "ok"
    assert row["measured_at"] > _EPOCH  # advanced forward on a healthy read
    assert row["stale_after"] is None  # bound-less row: the sentinel stays NULL (never touched by a bump)
    assert _probe_reports(repo.engine, status="ok") == 1


@pytest.mark.pg
def test_probe_blocked_records_then_raises(repo):
    _seed_wal(repo.engine)
    reader = _ScriptedReader(snap=_snap(archive_mode="off"))
    with pytest.raises(WalArchiverProbeError):
        run_wal_archiver_probe(repo.engine, stats_reader=reader)
    row = _read(repo.engine)
    assert row is not None
    assert row["status"] == "blocked"
    assert row["measured_at"] == _EPOCH  # NOT advanced on a blocked read
    assert _probe_reports(repo.engine, status="blocked") == 1  # recorded (persist-before-raise)


@pytest.mark.pg
def test_probe_aborts_before_reader_when_unseeded(repo):
    # repo truncates system_health -> the wal_lag row is absent. FOLD-4 analog: abort loudly BEFORE the
    # stats read, so a probe result is never orphaned with nowhere to record it.
    reader = _ScriptedReader(snap=_snap())
    with pytest.raises(WalArchiverProbeError):
        run_wal_archiver_probe(repo.engine, stats_reader=reader)
    assert reader.calls == 0  # reader NOT invoked
    assert _read(repo.engine) is None  # nothing seeded/written
    assert _probe_reports(repo.engine, status="ok") == 0


@pytest.mark.pg
def test_probe_reader_raise_records_blocked_then_raises(repo):
    # §10 MUST-FIX 3: a RAISING reader after a prior healthy probe must never leave a stale 'ok' the gate
    # would trust — persist 'blocked' + audit, THEN raise (the WAL analog of the backup arm's
    # test_driver_exception_records_then_raises).
    _seed_wal(repo.engine)
    run_wal_archiver_probe(repo.engine, stats_reader=_ScriptedReader(snap=_snap()))  # a prior healthy probe
    assert _read(repo.engine)["status"] == "ok"
    boom = RuntimeError("connection dropped mid-read")
    with pytest.raises(WalArchiverProbeError) as ei:
        run_wal_archiver_probe(repo.engine, stats_reader=_ScriptedReader(raises=boom))
    assert ei.value.__cause__ is boom  # chained, fail-loud
    row = _read(repo.engine)
    assert row["status"] == "blocked"  # the stale 'ok' was closed, not trusted
    assert "raised" in row["detail"]
    assert _probe_reports(repo.engine, status="blocked") == 1


@pytest.mark.pg
def test_bump_on_unseeded_row_raises(repo):
    with pytest.raises(WalArchiverProbeError, match="rowcount 0"):
        _bump_wal_lag(repo.engine, ok=True, detail="x")


# ---- pg: the REAL reader on the live (archiving-OFF) test PG -----------------------------------------


@pytest.mark.pg
def test_real_reader_on_archiving_off_pg_fails_closed(repo):
    # Non-synthetic proof: the real SQL read works, and an archiving-OFF cluster (the testcontainers
    # default) classifies BLOCKED. Also pins the "(disabled)" show-hook sentinel empirically (fold 11).
    snap = read_pg_stat_archiver(repo.engine)
    assert snap.archive_mode == "off"
    assert snap.archive_command == "(disabled)"  # the show hook substitutes it while archiving is inactive
    ok, _ = classify_archiver(snap)
    assert ok is False
    _seed_wal(repo.engine)
    with pytest.raises(WalArchiverProbeError):
        run_wal_archiver_probe(repo.engine)  # the DEFAULT reader, end to end
    row = _read(repo.engine)
    assert row["status"] == "blocked" and "not configured" in row["detail"]
