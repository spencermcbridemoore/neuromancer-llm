"""GO-D-backup (A2-10): the off-cloud (desktop-NVMe) backup automation + the backup-freshness signal.

RED@d3d2a90 = ModuleNotFoundError (neuromancer_llm.governance.freshness absent; probes.py is a Stage-2 stub).

Probes the pinned freshness contract (system_health['backup_freshness']; owner-nodded 2026-07-07):
  - the pin resolves / fails closed when absent (repo-constant-authoritative);
  - the seed is fail-CLOSED (status='blocked', measured_at=epoch) + idempotent;
  - a verified success bumps measured_at forward + status='ok' + a probe_reports audit row;
  - a FAILED/raising backup is RECORDED (status='blocked', measured_at NOT advanced) then RAISES (fail-loud);
  - the probe ABORTS before touching the driver if the row is unseeded (FOLD-4 — no orphaned backup);
  - the off-cloud destination guard (allow-shape + deny) precedes the driver (FOLD-5);
  - the grant asymmetry the contract leans on (writer bumps but cannot seed nor forge stale_after).
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import text

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.freshness import (
    BACKUP_FRESHNESS_KEY,
    resolve_backup_stale_after,
    seed_backup_freshness,
)
from neuromancer_llm.governance.probes import (
    BackupOutcome,
    BackupProbeError,
    _bump_freshness,
    assert_offcloud_destination,
    run_backup_probe,
)

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
_EIGHT_DAYS = _dt.timedelta(days=8)


class _RecordingDriver:
    """A synthetic backup driver (the real pgbackrest->desktop-NVMe push is A2-16). Records call count so a
    probe can assert it was NOT invoked, and returns/raises a scripted result."""

    def __init__(self, *, outcome: BackupOutcome | None = None, raises: Exception | None = None) -> None:
        self.calls = 0
        self._outcome = outcome
        self._raises = raises

    def __call__(self, destination: str) -> BackupOutcome:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._outcome is not None
        return self._outcome


def _seed(engine) -> bool:
    with engine.connect() as conn:
        return seed_backup_freshness(conn)


def _read(engine):
    with engine.begin() as conn:
        return (
            conn.execute(
                text(
                    "SELECT status, measured_at, stale_after, detail "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"k": BACKUP_FRESHNESS_KEY},
            )
            .mappings()
            .one_or_none()
        )


def _probe_reports(engine, *, status: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM neuro.probe_reports WHERE probe_key = :k AND status = :s"),
            {"k": BACKUP_FRESHNESS_KEY, "s": status},
        ).scalar_one()


# ---- pure: the pin (repo-constant-authoritative) ----------------------------------------------------


def test_resolve_bound_is_eight_days():
    assert resolve_backup_stale_after() == _EIGHT_DAYS


def test_resolve_bound_fails_closed_when_pin_absent(monkeypatch):
    monkeypatch.setattr("neuromancer_llm.governance.freshness.BACKUP_STALE_AFTER", None)
    with pytest.raises(ConfigurationError):
        resolve_backup_stale_after()


def test_seed_fails_closed_before_any_write_when_pin_absent(monkeypatch):
    # the seed resolves the pin FIRST -> an absent pin raises BEFORE any DB statement (fail closed).
    monkeypatch.setattr("neuromancer_llm.governance.freshness.BACKUP_STALE_AFTER", None)

    class _NoExec:
        def execute(self, *a, **k):
            raise AssertionError("seed must not touch the DB when the pin is absent")

        def commit(self):
            raise AssertionError("seed must not commit when the pin is absent")

    with pytest.raises(ConfigurationError):
        seed_backup_freshness(_NoExec())


# ---- pure: the off-cloud destination guard (FOLD-5, allow-shape + deny) ------------------------------


@pytest.mark.parametrize(
    "dest",
    [
        "https://neuromancerllm.blob.core.windows.net/db-backups",  # https blob REST + host marker
        "az://acct/container/x",
        "azure://acct/container/x",
        "abfss://c@acct.dfs.core.windows.net/x",
        "wasbs://c@acct.blob.core.windows.net/x",
        "https://10.0.0.5/db-backups",  # raw-IP Azure alias -> https scheme still fails closed
        "myacct.blob.core.windows.net/db-backups",  # no scheme, host marker
        "\\\\acct.file.core.windows.net\\share\\db",  # UNC to Azure Files (mountable) -> core.windows.net
        "backups.example.com/container",  # CNAME, no scheme -> not a local path (belt)
        "backups/relative",  # ambiguous relative token
        "",  # empty
    ],
)
def test_offcloud_guard_rejects_non_local(dest):
    with pytest.raises(ConfigurationError):
        assert_offcloud_destination(dest)


def test_offcloud_guard_rejects_conn_string_account(monkeypatch):
    monkeypatch.setenv(
        "AZURE_STORAGE_CONNECTION_STRING",
        "DefaultEndpointsProtocol=https;AccountName=neuromancerllm;AccountKey=k;EndpointSuffix=core.windows.net",
    )
    # a LOCAL-shaped path that nonetheless names the Azure account -> refused (independence, ADR-0014)
    with pytest.raises(ConfigurationError):
        assert_offcloud_destination("/mnt/backups/neuromancerllm/db")


@pytest.mark.parametrize(
    "dest",
    [
        "/mnt/desktop/neuro-backups",
        "D:\\neuro-backups",
        "D:/neuro-backups",
        "\\\\desktop\\share\\neuro-backups",
    ],
)
def test_offcloud_guard_accepts_local_paths(dest):
    assert assert_offcloud_destination(dest) is None


# ---- pg: the seed (fail-closed + idempotent) --------------------------------------------------------


@pytest.mark.pg
def test_seed_is_fail_closed_and_idempotent(repo):
    assert _seed(repo.engine) is True
    row = _read(repo.engine)
    assert row is not None
    assert row["status"] == "blocked"  # born fail-closed: the gate BLOCKs until the first verified backup
    assert row["measured_at"] == _EPOCH  # epoch, not now() -- the time-based check also fails closed
    assert row["stale_after"] == _EIGHT_DAYS  # the provisioning sentinel == the pin
    # idempotent: a second seed inserts nothing and does not disturb the row
    assert _seed(repo.engine) is False
    assert _read(repo.engine)["measured_at"] == _EPOCH


@pytest.mark.pg
def test_bump_on_unseeded_row_raises(repo):
    # defense-in-depth beyond FOLD-4's pre-check: a bump against an absent/vanished row is fail-loud
    # (never a silent no-op). repo truncates system_health, so the backup_freshness row is absent.
    with pytest.raises(BackupProbeError, match="rowcount 0"):
        _bump_freshness(repo.engine, ok=True, detail="x")


# ---- pg: run_backup_probe (produce the signal) ------------------------------------------------------


@pytest.mark.pg
def test_success_bumps_and_audits(repo):
    _seed(repo.engine)
    driver = _RecordingDriver(outcome=BackupOutcome(ok=True, detail="base+wal 1.2GiB verified"))
    out = run_backup_probe(
        repo.engine, backup_driver=driver, destination="/mnt/desktop/neuro-backups", actor_id=None
    )
    assert out.ok is True
    assert driver.calls == 1
    row = _read(repo.engine)
    assert row["status"] == "ok"
    assert row["measured_at"] > _EPOCH  # advanced forward on success (the freshness proof)
    assert row["stale_after"] == _EIGHT_DAYS  # a bump leaves the sentinel untouched (app-SQL layer belt)
    assert "1.2GiB" in row["detail"]
    assert _probe_reports(repo.engine, status="ok") == 1


@pytest.mark.pg
def test_failed_backup_records_then_raises(repo):
    _seed(repo.engine)
    driver = _RecordingDriver(outcome=BackupOutcome(ok=False, detail="verify checksum mismatch"))
    with pytest.raises(BackupProbeError):
        run_backup_probe(repo.engine, backup_driver=driver, destination="/mnt/desktop/neuro-backups")
    row = _read(repo.engine)
    assert row["status"] == "blocked"
    assert row["measured_at"] == _EPOCH  # NOT advanced on failure -- staleness accrues from last-good
    assert _probe_reports(repo.engine, status="blocked") == 1  # recorded (fail-loud, not a silent pass)


@pytest.mark.pg
def test_driver_exception_records_then_raises(repo):
    _seed(repo.engine)
    boom = RuntimeError("pgbackrest exited 45")
    driver = _RecordingDriver(raises=boom)
    with pytest.raises(BackupProbeError) as ei:
        run_backup_probe(repo.engine, backup_driver=driver, destination="/mnt/desktop/neuro-backups")
    assert ei.value.__cause__ is boom  # chained, fail-loud
    row = _read(repo.engine)
    assert row["status"] == "blocked"
    assert row["measured_at"] == _EPOCH
    assert _probe_reports(repo.engine, status="blocked") == 1


@pytest.mark.pg
def test_aborts_before_driver_when_unseeded(repo):
    # repo truncates system_health -> the freshness row is absent (unseeded). FOLD-4: abort loudly BEFORE
    # touching the backup driver, so a real backup is never orphaned with nowhere to record it.
    driver = _RecordingDriver(outcome=BackupOutcome(ok=True, detail="should never run"))
    with pytest.raises(BackupProbeError):
        run_backup_probe(repo.engine, backup_driver=driver, destination="/mnt/desktop/neuro-backups")
    assert driver.calls == 0  # driver NOT invoked
    assert _read(repo.engine) is None  # nothing seeded/written
    assert _probe_reports(repo.engine, status="ok") == 0


@pytest.mark.pg
def test_offcloud_guard_precedes_driver(repo):
    _seed(repo.engine)
    driver = _RecordingDriver(outcome=BackupOutcome(ok=True, detail="should never run"))
    with pytest.raises(ConfigurationError):
        run_backup_probe(
            repo.engine, backup_driver=driver, destination="https://neuromancerllm.blob.core.windows.net/x"
        )
    assert driver.calls == 0  # guarded BEFORE the push
    assert _read(repo.engine)["status"] == "blocked"  # untouched (still the seed's fail-closed state)


# The grant-legality probe (Rank-3 fold — writer bumps but cannot seed nor forge stale_after) is an L7
# role-boundary probe; it lives with its siblings + the shared `rt` helper in
# tests/redteam/test_rt_roles.py::test_rt_backup_freshness_grant_boundary (L13, one helper per concept).
