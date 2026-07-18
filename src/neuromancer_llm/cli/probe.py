"""`neuro probe` — run | report | verify-config | escalate. The A2-16 timers' entry points (ADR-0015/0020).

`run` drives ONE registered durability producer (the registry lives in governance/probe_registry.py, keyed
by the same constants as DURABILITY_ROWS — a future arm is a one-surface append); `report` is the
SELECT-only operational rendering (system_health + the last N probe_reports); `verify-config` is the
ruling-§3.5 provisioning-invariant assertion over the REAL pgbackrest config (+ the installed timer when
--timer-file is passed); `escalate` re-alerts on a PERSISTENT backup_freshness block (mirror-arm hardening,
2026-07-17). All thin delegates (GO-D-timer, owner GO 2026-07-11; escalate = the mirror-arm follow-on).
"""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, help="Operator probes: run | report | verify-config | escalate.")


@app.command()
def run(
    key: str = typer.Option(..., help="the durability row to produce: backup_freshness | wal_lag"),
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before any write (fail closed)",
    ),
    dest: str | None = typer.Option(
        None,
        envvar="NEURO_BACKUP_DEST",
        help="off-cloud mirror destination PATH (backup_freshness only; the desktop-side path, e.g. "
        "D:/neuro-backups — validated by the off-cloud guard before the driver runs)",
    ),
    ssh_alias: str = typer.Option(
        "neuro-desktop",
        help="ssh_config Host alias carrying the desktop host/user/key (the credential never rides argv)",
    ),
    repo_path: str = typer.Option(
        "/pgdata/pgbackrest", help="the LOCAL pgbackrest repo tree the mirror pushes from"
    ),
    stanza: str = typer.Option("neuro", help="pgbackrest stanza"),
) -> None:
    """Run ONE durability producer and record its signal (writer-grade; the systemd timers' ExecStart).
    Fails loud (non-zero) when the probe records a blocked signal — OnFailure= alerting keys on this."""
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_verified_engine
    from ..governance.backup_driver import make_pgbackrest_mirror_driver
    from ..governance.freshness import BACKUP_FRESHNESS_KEY
    from ..governance.probe_registry import PROBE_RUNNERS, ProbeContext
    from ..governance.probes import BackupProbeError
    from ..governance.wal_archiving import WalArchiverProbeError

    try:
        runner = PROBE_RUNNERS.get(key)
        if runner is None:
            raise ConfigurationError(
                f"unknown probe key {key!r} (fail closed; known: {sorted(PROBE_RUNNERS)})."
            )
        ctx = ProbeContext()
        if key == BACKUP_FRESHNESS_KEY:
            if not dest:
                raise ConfigurationError(
                    "backup_freshness requires a destination (--dest / NEURO_BACKUP_DEST) — fail closed."
                )
            ctx = ProbeContext(
                backup_driver=make_pgbackrest_mirror_driver(
                    repo_path=repo_path, ssh_alias=ssh_alias, stanza=stanza
                ),
                destination=dest,
            )
        engine = make_verified_engine(expected_lane=lane)
        runner(engine, ctx)
    except (ConfigurationError, LaneAssertionError, BackupProbeError, WalArchiverProbeError) as exc:
        typer.secho(f"probe {key} failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"probe {key} (lane={lane}): recorded ok")


@app.command()
def report(
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before the read (fail closed)",
    ),
    limit: int = typer.Option(10, help="how many recent probe_reports rows to show"),
) -> None:
    """Render the operational durability state: every system_health durability row + recent probe reports
    (SELECT-only; `neuro db durability status` is the PROVISIONING check — this is the operational one)."""
    from sqlalchemy import text

    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_verified_engine
    from ..governance.durability import status_all

    try:
        engine = make_verified_engine(expected_lane=lane)
        with engine.connect() as conn:
            rows = status_all(conn)
            reports = conn.execute(
                text(
                    "SELECT probe_key, status, reported_at, report_text FROM neuro.probe_reports "
                    "ORDER BY probe_report_id DESC LIMIT :n"
                ),
                {"n": limit},
            ).all()
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"probe report failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for r in rows:
        state = f"status={r.status}" if r.present else "MISSING (run `neuro db durability seed`)"
        typer.echo(f"{r.health_key}: {state} measured_at={r.measured_at}")
    typer.echo(f"--- last {len(reports)} probe report(s) ---")
    for rep in reports:
        typer.echo(f"[{rep.reported_at}] {rep.probe_key}: {rep.status} — {(rep.report_text or '')[:120]}")


@app.command("verify-config")
def verify_config(
    conf: str = typer.Option(
        "/etc/pgbackrest/pgbackrest.conf", help="the REAL pgbackrest config to assert against"
    ),
    timer_file: str | None = typer.Option(
        None,
        help="the installed neuro-backup.timer unit file — when given, its OnUnitActiveSec must EQUAL the "
        "pinned BASE_BACKUP_INTERVAL (omit only for the pre-install provisioning run; the daily timer "
        "always passes it)",
    ),
    legacy_conf: str = typer.Option(
        "/etc/pgbackrest.conf", help="the legacy file-form path that must NOT shadow the real config"
    ),
) -> None:
    """Assert the ruling-§3.5 provisioning invariants (retention/staleness/cadence/queue-max consistency)
    against the REAL pgbackrest config. Fail-loud, REDACTION-safe (the config holds the Azure key — no
    failure message ever echoes file content)."""
    from pathlib import Path

    from ..db.lanes import ConfigurationError
    from ..governance.provisioning_invariants import verify_pgbackrest_config

    try:
        timer_text = Path(timer_file).read_text(encoding="utf-8") if timer_file else None
        report = verify_pgbackrest_config(conf, timer_unit_text=timer_text, legacy_conf_path=legacy_conf)
    except (ConfigurationError, OSError) as exc:
        typer.secho(f"verify-config FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    days = ", ".join(f"repo{k}={v}d" for k, v in sorted(report.retention_days.items()))
    typer.echo(f"verify-config OK: repos {list(report.repos)}; retention {days}")
    if report.timer_checked:
        typer.echo("timer cadence checked: OnUnitActiveSec == BASE_BACKUP_INTERVAL")
    else:
        typer.secho(
            "timer cadence NOT checked (no --timer-file) — acceptable ONLY pre-install; the daily "
            "verify-config timer must pass it",
            fg=typer.colors.YELLOW,
        )


@app.command()
def escalate(
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before the read (fail closed)",
    ),
    escalate_after_hours: float | None = typer.Option(
        None,
        help="override the pinned escalation onset (BASE_BACKUP_INTERVAL) for THIS call only — an explicit "
        "operator/diagnostic knob; the daily timer passes none (pin-governed). e.g. 0 to alert on any "
        "current block (the induced-failure test).",
    ),
) -> None:
    """Re-alert on a PERSISTENT backup_freshness block (the daily escalation the per-cycle OnFailure ping lacks).

    Fires an ntfy alert when backup_freshness has read 'blocked' longer than the pinned onset
    (BASE_BACKUP_INTERVAL), with a message stating the CONSEQUENCE + ACTION rather than the failed unit name.
    READ-ONLY against system_health; a no-op (exit 0, prints its decision) when not blocked or still within the
    onset. The daily timer's ExecStart."""
    import datetime as _dt

    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_verified_engine
    from ..governance.escalation import evaluate_backup_block_escalation
    from ..governance.notify import notify

    try:
        override = _dt.timedelta(hours=escalate_after_hours) if escalate_after_hours is not None else None
        engine = make_verified_engine(expected_lane=lane)
        message = evaluate_backup_block_escalation(engine, escalate_after=override)
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"probe escalate failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if message is None:
        typer.echo(
            f"probe escalate (lane={lane}): backup_freshness is not in a persistent-block state — no alert."
        )
        return
    # notify() is fail-LOUD (ADR-0019): a broken channel raises -> non-zero exit -> the unit's OnFailure=
    # neuro-alert@ fires as the fallback ping. The alert IS the record; escalation writes nothing to the DB.
    notify(message)
    typer.secho(f"probe escalate (lane={lane}): ESCALATED — {message}", fg=typer.colors.YELLOW)
