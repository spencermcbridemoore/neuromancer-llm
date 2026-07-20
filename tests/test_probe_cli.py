"""GO-D-timer (A2-16): the `neuro probe` CLI — the systemd timers' entry points — + the runner registry.

RED@7d680fb = the two probe commands were Stage-2 stubs (exit-1 "Stage 2") and
governance/probe_registry.py did not exist. Probes: the registry keyset == DURABILITY_KEYS (§8 fold 7 — a
future arm can never ship seeded-but-unrunnable); `run --key wal_lag` drives the REAL archiver probe end to
end (archiving-off test PG -> records blocked + exits non-zero, which is exactly what OnFailure alerting
keys on); the backup path wires the driver factory + destination (driver scripted via monkeypatch — the
real subprocess path is the VM's); `report` renders both rows + recent reports; `verify-config` delegates
with the loud not-checked warning when --timer-file is omitted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from neuromancer_llm.cli.app import app
from neuromancer_llm.governance.durability import DURABILITY_KEYS, seed_all
from neuromancer_llm.governance.probe_registry import PROBE_RUNNERS
from neuromancer_llm.governance.probes import BackupOutcome

_runner = CliRunner()

# A minimal PASSING conf for the CLI-delegate tests (the VERBATIM runbook fixture — inline comments, the
# tee-appended key — lives in tests/test_provisioning_invariants.py, where the parser contract is pinned).
_MINIMAL_CONF = """[global]
archive-async=y
archive-push-queue-max=32GiB
repo1-path=/pgdata/pgbackrest
repo1-retention-full-type=time
repo1-retention-full=30

[neuro]
pg1-path=/pgdata/18/main
"""
_TIMER_OK = "[Timer]\nOnBootSec=15min\nOnUnitActiveSec=2d\n"


def _seed(engine) -> None:
    with engine.connect() as conn:
        seed_all(conn)


# ---- the registry (fold 7): one keyed surface, pinned to the durability registry ----------------------


def test_probe_runner_registry_covers_every_durability_row():
    # the GATE_CONSULTED_KEYS idiom, extended: every provisioned row has a RUNNER (and no phantom runners) —
    # a future arm is a one-surface append (DurabilityRow + gate branch + runner) or this reddens.
    assert frozenset(PROBE_RUNNERS) == DURABILITY_KEYS


# ---- run ------------------------------------------------------------------------------------------------


@pytest.mark.pg
def test_run_unknown_key_fails_closed(repo):
    r = _runner.invoke(app, ["probe", "run", "--key", "nope", "--lane", "test"])
    assert r.exit_code == 1 and "unknown probe key" in r.output


@pytest.mark.pg
def test_run_wal_lag_end_to_end_records_blocked_on_archiving_off_pg(repo):
    _seed(repo.engine)
    r = _runner.invoke(app, ["probe", "run", "--key", "wal_lag", "--lane", "test"])
    assert r.exit_code == 1  # archiving is OFF on the test PG -> blocked -> non-zero (OnFailure keys on this)
    with repo.engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, detail FROM neuro.system_health WHERE health_key='wal_lag'")
        ).one()
    assert row.status == "blocked" and "not configured" in row.detail


@pytest.mark.pg
def test_run_backup_requires_destination(repo):
    _seed(repo.engine)
    r = _runner.invoke(app, ["probe", "run", "--key", "backup_freshness", "--lane", "test"])
    assert r.exit_code == 1 and "destination" in r.output


@pytest.mark.pg
def test_run_backup_wires_driver_and_destination(repo, monkeypatch):
    _seed(repo.engine)
    seen: dict[str, str] = {}

    def _fake_factory(**kw):
        def driver(destination: str) -> BackupOutcome:
            seen["destination"] = destination
            seen["ssh_alias"] = kw["ssh_alias"]
            return BackupOutcome(ok=True, detail="scripted mirror ok")

        return driver

    monkeypatch.setattr(
        "neuromancer_llm.governance.backup_driver.make_pgbackrest_mirror_driver", _fake_factory
    )
    r = _runner.invoke(
        app,
        ["probe", "run", "--key", "backup_freshness", "--lane", "test", "--dest", "D:/neuro-backups"],
    )
    assert r.exit_code == 0, r.output
    assert seen == {"destination": "D:/neuro-backups", "ssh_alias": "neuro-desktop"}
    with repo.engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM neuro.system_health WHERE health_key='backup_freshness'")
        ).scalar_one()
    assert status == "ok"


@pytest.mark.pg
def test_run_lake_mirror_requires_destination(repo):
    _seed(repo.engine)
    r = _runner.invoke(app, ["probe", "run", "--key", "lake_mirror_freshness", "--lane", "test"])
    assert r.exit_code == 1 and "destination" in r.output


@pytest.mark.pg
def test_run_lake_mirror_wires_driver_and_destination(repo, monkeypatch):
    _seed(repo.engine)
    from neuromancer_llm.governance.lake_mirror import LakeMirrorOutcome

    seen: dict[str, str] = {}

    def _fake_factory(**kw):
        seen["ssh_alias"] = kw["ssh_alias"]
        seen["lane_backend_key"] = kw["lane_backend_key"]

        def driver(engine, destination: str) -> LakeMirrorOutcome:
            seen["destination"] = destination
            return LakeMirrorOutcome(
                ok=True, detail="scripted mirror ok", checked=1, pushed=1, deleted=0, verified=1
            )

        return driver

    monkeypatch.setattr("neuromancer_llm.governance.lake_mirror.make_lake_mirror_driver", _fake_factory)
    r = _runner.invoke(
        app,
        [
            "probe",
            "run",
            "--key",
            "lake_mirror_freshness",
            "--lane",
            "test",
            "--lake-dest",
            "D:/neuro-lake",
        ],
    )
    assert r.exit_code == 0, r.output
    assert seen == {
        "ssh_alias": "neuro-desktop",
        "lane_backend_key": "artifacts-prod",
        "destination": "D:/neuro-lake",
    }
    with repo.engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM neuro.system_health WHERE health_key='lake_mirror_freshness'")
        ).scalar_one()
    assert status == "ok"


@pytest.mark.pg
def test_lake_escalate_cli_override_alerts_on_current_block(repo, monkeypatch):
    # the induced-test knob through the CLI: seed the lake row blocked, --escalate-after-hours 0 fires the ping.
    from sqlalchemy import text as _text

    from neuromancer_llm.governance.durability import LAKE_MIRROR_ROW, seed_row

    with repo.engine.connect() as conn:
        seed_row(conn, LAKE_MIRROR_ROW)
    with repo.engine.begin() as conn:
        conn.execute(
            _text(
                "UPDATE neuro.system_health SET status='blocked', measured_at=now() "
                "WHERE health_key='lake_mirror_freshness'"
            )
        )
    sent: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m, **kw: sent.append(m))
    r = _runner.invoke(app, ["probe", "lake-escalate", "--lane", "test", "--escalate-after-hours", "0"])
    assert r.exit_code == 0, r.output
    assert "ESCALATED" in r.output and sent and "BLOB-LAKE MIRROR BLOCKED" in sent[0]


# ---- report ----------------------------------------------------------------------------------------------


@pytest.mark.pg
def test_report_renders_rows_and_recent_reports(repo):
    _seed(repo.engine)
    r = _runner.invoke(app, ["probe", "report", "--lane", "test"])
    assert r.exit_code == 0
    assert "backup_freshness" in r.output and "wal_lag" in r.output
    assert "probe report(s)" in r.output


@pytest.mark.pg
def test_report_flags_missing_rows(repo):
    r = _runner.invoke(app, ["probe", "report", "--lane", "test"])  # unseeded (repo truncated)
    assert r.exit_code == 0 and "MISSING" in r.output


# ---- verify-config (the CLI delegate; the assertion logic is tests/test_provisioning_invariants.py) ------


def test_verify_config_cli_ok_and_warns_without_timer(tmp_path):
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(_MINIMAL_CONF, encoding="utf-8")
    r = _runner.invoke(
        app,
        ["probe", "verify-config", "--conf", str(conf), "--legacy-conf", str(tmp_path / "legacy.conf")],
    )
    assert r.exit_code == 0 and "verify-config OK" in r.output
    assert "NOT checked" in r.output  # pre-install runs must be loud about the unchecked cadence


def test_verify_config_cli_checks_timer(tmp_path):
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(_MINIMAL_CONF, encoding="utf-8")
    timer = tmp_path / "neuro-backup.timer"
    timer.write_text(_TIMER_OK, encoding="utf-8")
    r = _runner.invoke(
        app,
        [
            "probe",
            "verify-config",
            "--conf",
            str(conf),
            "--timer-file",
            str(timer),
            "--legacy-conf",
            str(tmp_path / "legacy.conf"),
        ],
    )
    assert r.exit_code == 0 and "timer cadence checked" in r.output


def test_verify_config_cli_fails_loud_on_violation(tmp_path):
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(_MINIMAL_CONF.replace("repo1-retention-full-type=time\n", ""), encoding="utf-8")
    r = _runner.invoke(
        app,
        ["probe", "verify-config", "--conf", str(conf), "--legacy-conf", str(tmp_path / "legacy.conf")],
    )
    assert r.exit_code == 1 and "FAILED" in r.output


def test_verify_config_cli_checks_archiver_timer(tmp_path):
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(_MINIMAL_CONF, encoding="utf-8")
    archiver = tmp_path / "neuro-archiver-probe.timer"
    archiver.write_text("[Timer]\nOnBootSec=5min\nOnUnitActiveSec=15min\n", encoding="utf-8")
    r = _runner.invoke(
        app,
        [
            "probe",
            "verify-config",
            "--conf",
            str(conf),
            "--archiver-timer-file",
            str(archiver),
            "--legacy-conf",
            str(tmp_path / "legacy.conf"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "archiver-probe timer cadence checked" in r.output


def test_verify_config_cli_fails_on_archiver_drift(tmp_path):
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(_MINIMAL_CONF, encoding="utf-8")
    archiver = tmp_path / "neuro-archiver-probe.timer"
    archiver.write_text("[Timer]\nOnUnitActiveSec=30min\n", encoding="utf-8")  # != the pinned 15min
    r = _runner.invoke(
        app,
        [
            "probe",
            "verify-config",
            "--conf",
            str(conf),
            "--archiver-timer-file",
            str(archiver),
            "--legacy-conf",
            str(tmp_path / "legacy.conf"),
        ],
    )
    assert r.exit_code == 1 and "FAILED" in r.output and "archiver-probe" in r.output


# ---- disk (the /pgdata disk-pressure alarm; the assertion logic is tests/test_disk_pressure.py) --------


def _script_disk_usage(monkeypatch, used_frac: float) -> None:
    import shutil
    from types import SimpleNamespace

    total = 100 * 1024**3
    used = int(total * used_frac)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: SimpleNamespace(total=total, used=used, free=total - used)
    )


def test_disk_cli_no_alert_when_under_threshold(monkeypatch):
    _script_disk_usage(monkeypatch, 0.40)
    r = _runner.invoke(app, ["probe", "disk", "--path", "/pgdata"])
    assert r.exit_code == 0 and "within the usage threshold" in r.output


def test_disk_cli_alarms_and_notifies_when_over(monkeypatch):
    _script_disk_usage(monkeypatch, 0.95)
    sent: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m, **kw: sent.append(m))
    r = _runner.invoke(app, ["probe", "disk", "--path", "/pgdata"])
    assert r.exit_code == 0, r.output
    assert "ALARM" in r.output
    assert sent and "DISK PRESSURE" in sent[0]  # the alert actually reached notify()


def test_disk_cli_threshold_override_forces_alarm(monkeypatch):
    _script_disk_usage(monkeypatch, 0.10)  # well under the pin -> only the 0.0 override fires it
    sent: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m, **kw: sent.append(m))
    r = _runner.invoke(app, ["probe", "disk", "--path", "/pgdata", "--threshold", "0.0"])
    assert r.exit_code == 0 and "ALARM" in r.output and sent


def test_disk_cli_fails_closed_on_absent_pin(monkeypatch):
    _script_disk_usage(monkeypatch, 0.95)
    monkeypatch.setattr("neuromancer_llm.governance.disk_pressure.DISK_PRESSURE_THRESHOLD", None)
    r = _runner.invoke(app, ["probe", "disk", "--path", "/pgdata"])
    assert r.exit_code == 1 and "failed" in r.output
