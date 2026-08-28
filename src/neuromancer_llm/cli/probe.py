"""`neuro probe` — run | report | verify-config | escalate | lake-escalate | repo3-escalate | disk. The timers' entry points (ADR-0015/0020).

`run` drives ONE registered durability producer (the registry lives in governance/probe_registry.py, keyed
by the same constants as DURABILITY_ROWS — a future arm is a one-surface append); `report` is the
SELECT-only operational rendering (system_health + the last N probe_reports); `verify-config` is the
ruling-§3.5 provisioning-invariant assertion over the REAL pgbackrest config (+ the installed backup and
archiver-probe timer cadences when --timer-file / --archiver-timer-file are passed — the latter is wal D4);
`escalate` re-alerts on a PERSISTENT backup_freshness block (mirror-arm hardening, 2026-07-17);
`repo3-escalate` does the same for the notify-only repo3 third copy (§A·72, 2026-08-28); `disk` alarms
on /pgdata disk pressure (the automated `df` watch, A2-8 follow-on). All thin delegates (GO-D-timer, owner GO
2026-07-11; escalate + disk are the mirror-arm-precedent follow-ons).
"""

from __future__ import annotations

import typer

app = typer.Typer(
    no_args_is_help=True,
    help="Operator probes: run | report | verify-config | escalate | lake-escalate | repo3-escalate | disk.",
)


@app.command()
def run(
    key: str = typer.Option(
        ...,
        help="the durability row to produce: backup_freshness | wal_lag | lake_mirror_freshness | repo3_freshness",
    ),
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
    lake_dest: str | None = typer.Option(
        None,
        envvar="NEURO_LAKE_MIRROR_DEST",
        help="off-cloud lake-mirror destination PATH (lake_mirror_freshness only; the desktop-side path, "
        "e.g. D:/neuro-lake — validated by the off-cloud guard before the driver runs)",
    ),
    ssh_alias: str = typer.Option(
        "neuro-desktop",
        help="ssh_config Host alias carrying the desktop host/user/key (the credential never rides argv)",
    ),
    repo_path: str = typer.Option(
        "/pgdata/pgbackrest", help="the LOCAL pgbackrest repo tree the mirror pushes from"
    ),
    stanza: str = typer.Option("neuro", help="pgbackrest stanza"),
    lane_backend_key: str = typer.Option(
        "artifacts-prod",
        help="the storage_backends lane the lake mirror pushes (lake_mirror_freshness only; prod-only ruling)",
    ),
) -> None:
    """Run ONE durability producer and record its signal (writer-grade; the systemd timers' ExecStart).
    Fails loud (non-zero) when the probe records a blocked signal — OnFailure= alerting keys on this."""
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_verified_engine
    from ..governance.backup_driver import make_pgbackrest_mirror_driver
    from ..governance.freshness import BACKUP_FRESHNESS_KEY
    from ..governance.lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
    from ..governance.lake_mirror import (
        LAKE_MIRROR_AZURE_READ_TIMEOUT_S,
        LakeMirrorProbeError,
        make_lake_mirror_driver,
    )
    from ..governance.probe_registry import PROBE_RUNNERS, ProbeContext
    from ..governance.probes import BackupProbeError
    from ..governance.repo3_probe import Repo3ProbeError
    from ..governance.wal_archiving import WalArchiverProbeError
    from ..registry.backends import make_backend

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
        elif key == LAKE_MIRROR_FRESHNESS_KEY:
            if not lake_dest:
                raise ConfigurationError(
                    "lake_mirror_freshness requires a destination (--lake-dest / NEURO_LAKE_MIRROR_DEST) — "
                    "fail closed."
                )

            def _source_backend_for(driver: str, base_uri: str):
                # the read-only Azure source, built through the C5 factory (never a raw AzureBlobBackend);
                # connection_string=None -> make_backend reads AZURE_STORAGE_CONNECTION_STRING (infra config).
                # The read_timeout bounds each Azure socket read so a hang fails the mirror closed.
                return make_backend(
                    driver,
                    base_uri=base_uri,
                    connection_string=None,
                    read_timeout=LAKE_MIRROR_AZURE_READ_TIMEOUT_S,
                )

            ctx = ProbeContext(
                lake_mirror_driver=make_lake_mirror_driver(
                    source_backend_for=_source_backend_for,
                    ssh_alias=ssh_alias,
                    lane_backend_key=lane_backend_key,
                ),
                destination=lake_dest,
            )
        engine = make_verified_engine(expected_lane=lane)
        runner(engine, ctx)
    except (
        ConfigurationError,
        LaneAssertionError,
        BackupProbeError,
        WalArchiverProbeError,
        LakeMirrorProbeError,
        # repo3 (§A·72): without this arm a blocked repo3 read would escape as an unhandled traceback
        # instead of the clean exit-1 that `OnFailure=` alerting keys on — the same contract every other
        # producer already has here.
        Repo3ProbeError,
    ) as exc:
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
    key: str | None = typer.Option(
        None,
        help="restrict the probe_reports listing to ONE probe key (e.g. repo3_freshness). Without it the "
        "wal_lag archiver probe, which records a row every 15 minutes, crowds out every other arm's "
        "reason within a couple of hours — so the escalation alerts name this flag.",
    ),
) -> None:
    """Render the operational durability state: every system_health durability row + recent probe reports
    (SELECT-only; `neuro db durability status` is the PROVISIONING check — this is the operational one).

    ⚠ WHY `--key` EXISTS (added 2026-08-28 with the repo3 arm). Every escalation alert's triage opens with
    "read the recorded reason first" and points here. That step was UNRUNNABLE in practice for any arm but
    wal_lag: this listing had no per-key predicate, defaults to 10 rows, and `governance/wal_archiving.py`
    writes a probe_reports row on EVERY run of a 15-minute timer (~96/day) — so the default view covers
    roughly the last two hours and is entirely wal_lag, while a backup or repo3 reason is DAYS old by
    construction (their cadence is 2 days). The alerts named a command that could not surface what they
    told the operator to look for. One optional predicate fixes it for all four arms at once.
    ⚠ AND `system_health.detail` IS RENDERED HERE TOO (same unit), which CLOSES the residual
    `governance/alert_triage.py` had registered rather than fixed. Two states write NO probe_reports row at
    all and were therefore unreachable from step (0) by any flag: a GATE-origin flip (stale_after drift /
    staleness), whose reason `governance/health.py::_flip_and_notify` writes ONLY to `detail`, and a
    freshly-SEEDED row, born blocked carrying its provisioning detail. Both now appear on the `detail=`
    field of their system_health line, so every arm's "read the recorded reason first" is followable.
    ⚠ This paragraph previously said the opposite -- that `detail` is never rendered -- thirty lines above
    the code that renders it, in the same unit that added it. Typer prints this docstring as `--help`, so it
    was an operator-facing falsehood about the very function carrying it (precedent 15)."""
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
                    # ⚠ CAST(:k AS text), not a bare `:k IS NULL`: Postgres cannot infer a parameter's type
                    # from `$1 IS NULL` alone and raises AmbiguousParameter (measured here). And it is
                    # CAST(...) rather than `:k::text`, because the `:param::cast` form MIS-PARSES in
                    # SQLAlchemy's text() — a trap this repo has already hit twice and documented.
                    "SELECT probe_key, status, reported_at, report_text FROM neuro.probe_reports "
                    "WHERE (CAST(:k AS text) IS NULL OR probe_key = CAST(:k AS text)) "
                    "ORDER BY probe_report_id DESC LIMIT :n"
                ),
                {"n": limit, "k": key},
            ).all()
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"probe report failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for r in rows:
        state = f"status={r.status}" if r.present else "MISSING (run `neuro db durability seed`)"
        # ⚠ `detail` IS RENDERED (added 2026-08-28 with the repo3 arm), and it closes a residual
        # `governance/alert_triage.py` had registered rather than fixed. Two states are visible ONLY here:
        # a GATE-origin flip (stale_after drift / staleness) writes no probe_reports row at all, and a
        # freshly-SEEDED row is born blocked with its provisioning detail and no probe row either. Every
        # arm's triage opens with "read the recorded reason first" — without this line that instruction was
        # unfollowable for both states, and an operator would read a born-blocked row as an outage.
        detail = f" detail={r.detail!r}" if r.present and r.detail else ""
        typer.echo(f"{r.health_key}: {state} measured_at={r.measured_at}{detail}")
    scope = f" for {key}" if key else ""
    typer.echo(f"--- last {len(reports)} probe report(s){scope} ---")
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
    archiver_timer_file: str | None = typer.Option(
        None,
        help="the installed neuro-archiver-probe.timer unit file (wal D4) — when given, its OnUnitActiveSec "
        "must EQUAL the pinned ARCHIVER_PROBE_INTERVAL (the 15-min cadence WAL_LAG_STALE_AFTER is derived "
        "from); omit only pre-install; the daily verify-config timer always passes it",
    ),
    legacy_conf: str = typer.Option(
        "/etc/pgbackrest.conf", help="the legacy file-form path that must NOT shadow the real config"
    ),
) -> None:
    """Assert the ruling-§3.5 provisioning invariants (retention/staleness/cadence/queue-max consistency)
    against the REAL pgbackrest config, plus the installed backup + archiver-probe timer cadences (wal D4).
    Fail-loud, REDACTION-safe (the config holds the Azure key — no failure message ever echoes file content)."""
    from pathlib import Path

    from ..db.lanes import ConfigurationError
    from ..governance.provisioning_invariants import (
        resolve_gate_basis_repos,
        verify_pgbackrest_config,
    )

    try:
        timer_text = Path(timer_file).read_text(encoding="utf-8") if timer_file else None
        archiver_text = Path(archiver_timer_file).read_text(encoding="utf-8") if archiver_timer_file else None
        report = verify_pgbackrest_config(
            conf,
            timer_unit_text=timer_text,
            archiver_timer_unit_text=archiver_text,
            legacy_conf_path=legacy_conf,
        )
    except (ConfigurationError, OSError) as exc:
        typer.secho(f"verify-config FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    days = ", ".join(f"repo{k}={v}d" for k, v in sorted(report.retention_days.items()))
    typer.echo(f"verify-config OK: repos {list(report.repos)}; retention {days}")
    # Assertion 8's REPORTED half. Printing it is what makes "repo3 is deliberately non-gating" an
    # operational fact an operator sees on the daily run, rather than a claim in a comment — and it is why
    # provisioning_invariants.py is entitled to say the daily verify-config run prints it.
    if report.non_gating_repos:
        typer.echo(
            f"gate basis: {sorted(resolve_gate_basis_repos())}; NON-GATING (reported only): "
            f"{list(report.non_gating_repos)}"
        )
    else:
        typer.echo(f"gate basis: {sorted(resolve_gate_basis_repos())}; every configured repo gates")
    _echo_cadence(report.timer_checked, "backup timer", "BASE_BACKUP_INTERVAL", "--timer-file")
    _echo_cadence(
        report.archiver_timer_checked,
        "archiver-probe timer",
        "ARCHIVER_PROBE_INTERVAL",
        "--archiver-timer-file",
    )


def _echo_cadence(checked: bool, label: str, pin: str, flag: str) -> None:
    """Render one timer's cadence-check line: confirmed, or a LOUD not-checked warning (acceptable only
    pre-install — the daily verify-config timer passes every --*-timer-file)."""
    if checked:
        typer.echo(f"{label} cadence checked: OnUnitActiveSec == {pin}")
    else:
        typer.secho(
            f"{label} cadence NOT checked (no {flag}) — acceptable ONLY pre-install; the daily "
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


@app.command("lake-escalate")
def lake_escalate(
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before the read (fail closed)",
    ),
    escalate_after_hours: float | None = typer.Option(
        None,
        help="override the pinned escalation onset (LAKE_MIRROR_STALE_AFTER) for THIS call only — an explicit "
        "operator/diagnostic knob; the daily timer passes none (pin-governed). e.g. 0 to alert on any "
        "current block (the induced-failure test).",
    ),
) -> None:
    """Re-alert on a PERSISTENT lake_mirror_freshness block (the daily escalation the per-run OnFailure ping lacks).

    Fires an ntfy alert when lake_mirror_freshness has read 'blocked' longer than the pinned onset, with a
    message stating the CONSEQUENCE + ACTION rather than the failed unit name. READ-ONLY against system_health;
    a no-op (exit 0, prints its decision) when not blocked or still within the onset. The daily
    neuro-lake-mirror-escalate.timer's ExecStart. NOTIFY-ONLY: the lake mirror never gates a canonical write."""
    import datetime as _dt

    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_verified_engine
    from ..governance.lake_escalation import evaluate_lake_mirror_block_escalation
    from ..governance.notify import notify

    try:
        override = _dt.timedelta(hours=escalate_after_hours) if escalate_after_hours is not None else None
        engine = make_verified_engine(expected_lane=lane)
        message = evaluate_lake_mirror_block_escalation(engine, escalate_after=override)
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"probe lake-escalate failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if message is None:
        typer.echo(
            f"probe lake-escalate (lane={lane}): lake_mirror_freshness is not in a persistent-block state — "
            "no alert."
        )
        return
    # notify() is fail-LOUD (ADR-0019): a broken channel raises -> non-zero exit -> the unit's OnFailure=
    # neuro-alert@ fires as the fallback ping. The alert IS the record; escalation writes nothing to the DB.
    notify(message)
    typer.secho(f"probe lake-escalate (lane={lane}): ESCALATED — {message}", fg=typer.colors.YELLOW)


@app.command("repo3-escalate")
def repo3_escalate(
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before the read (fail closed)",
    ),
    escalate_after_hours: float | None = typer.Option(
        None,
        help="override the pinned escalation onset (BASE_BACKUP_INTERVAL + PROVISIONING_MARGIN) for THIS "
        "call only — an explicit operator/diagnostic knob; the daily timer passes none (pin-governed). "
        "e.g. 0 to alert on any current block (the induced-failure test).",
    ),
) -> None:
    """Re-alert on a PERSISTENT repo3 block (the daily escalation; repo3 has no per-cycle OnFailure ping).

    Fires an ntfy alert when repo3_freshness has read 'blocked' longer than the pinned onset, with a message
    stating the CONSEQUENCE + a DISCRIMINATING procedure for an object-store repository. READ-ONLY against
    system_health; a no-op (exit 0, prints its decision) when not blocked or still within the onset. The
    daily neuro-repo3-escalate.timer's ExecStart.

    NOTIFY-ONLY: the repo3_freshness row is not consulted by the ADR-0020 gate. ⚠ That is a statement about
    the ROW — a failing repo3 can still refuse canonical writes through `wal_lag`, because pgbackrest pushes
    WAL to every configured repository. The alert copy says so; see governance/repo3_freshness.py."""
    import datetime as _dt

    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_verified_engine
    from ..governance.notify import notify
    from ..governance.repo3_escalation import evaluate_repo3_block_escalation

    try:
        override = _dt.timedelta(hours=escalate_after_hours) if escalate_after_hours is not None else None
        engine = make_verified_engine(expected_lane=lane)
        message = evaluate_repo3_block_escalation(engine, escalate_after=override)
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"probe repo3-escalate failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if message is None:
        typer.echo(
            f"probe repo3-escalate (lane={lane}): repo3_freshness is not in a persistent-block state — "
            "no alert."
        )
        return
    # notify() is fail-LOUD (ADR-0019): a broken channel raises -> non-zero exit -> the unit's OnFailure=
    # neuro-alert@ fires as the fallback ping. The alert IS the record; escalation writes nothing to the DB.
    notify(message)
    typer.secho(f"probe repo3-escalate (lane={lane}): ESCALATED — {message}", fg=typer.colors.YELLOW)


@app.command()
def disk(
    path: str = typer.Option(
        "/pgdata", help="the filesystem path to check (holds the PG data dir + the repo1 backup tree)"
    ),
    threshold: float | None = typer.Option(
        None,
        help="override the pinned usage-fraction threshold for THIS call only (0-1; e.g. 0.0 to force the "
        "alarm — the induced-failure test). The daily timer passes none (pin-governed, fail-closed).",
    ),
) -> None:
    """Alarm on /pgdata disk pressure (the automated `df` watch) — a READ-ONLY, notify()-only check.

    Fires an actionable ntfy alert when the filesystem is at/over the pinned usage threshold; a no-op (exit 0,
    prints its decision) otherwise. No DB, no lane (a pure statvfs). The daily neuro-disk-pressure.timer's
    ExecStart."""
    from ..db.lanes import ConfigurationError
    from ..governance.disk_pressure import evaluate_disk_pressure
    from ..governance.notify import notify

    try:
        message = evaluate_disk_pressure(path, threshold=threshold)
    except ConfigurationError as exc:
        typer.secho(f"probe disk failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if message is None:
        typer.echo(f"probe disk: {path} is within the usage threshold — no alert.")
        return
    # notify() is fail-LOUD (ADR-0019): a broken channel raises -> non-zero exit -> the unit's OnFailure=
    # neuro-alert@ fires as the fallback ping. The alert IS the record; the disk check writes nothing.
    notify(message)
    typer.secho(f"probe disk: ALARM — {message}", fg=typer.colors.YELLOW)
