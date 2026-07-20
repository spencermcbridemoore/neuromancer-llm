"""Persistent-block escalation for the blob-lake mirror (B-7, 2026-07-20).

A CONSUMER of system_health['lake_mirror_freshness'] (produced by governance/lake_mirror.py) — the parallel of
governance/escalation.py (the backup arm) and governance/disk_pressure.py (both notify-only). It RE-ALERTS: it
detects a lake_mirror_freshness signal that has read 'blocked' for LONGER than the pinned onset and returns an
actionable message for the CLI to notify(). It does NOT gate (the fork ruling: the lake row is not in
health.GATE_CONSULTED_KEYS) and it writes NOTHING (the alert IS the record — the ntfy history + the 'blocked'
probe_reports rows the mirror probe already writes).

WHY it exists: the per-run OnFailure ping (neuro-alert@) fires only on a mirror-run FAILURE and names the failed
unit, not the consequence. A DAILY re-alert whose message states the consequence + action closes the cadence +
copy gap the backup arm's incident (log:214) taught — an alert that is delivered-and-missed is not hardened
(§E·16); the induced test proves the real ping lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from .lake_freshness import LAKE_MIRROR_FRESHNESS_KEY, resolve_lake_mirror_block_escalate_after

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy import Engine


def evaluate_lake_mirror_block_escalation(
    engine: Engine, *, escalate_after: _dt.timedelta | None = None
) -> str | None:
    """Return an ACTIONABLE alert message iff `lake_mirror_freshness` has read 'blocked' longer than the onset
    bound; else None. READ-ONLY (a plain SELECT — no flip, no write).

    `escalate_after` overrides the pinned onset for THIS call only — an explicit operator/diagnostic knob (the
    daily timer passes none, so AUTOMATED escalation is pin-governed and fail-closed via
    resolve_lake_mirror_block_escalate_after; e.g. 0 to alert on any current block — the induced-failure test).

    The staleness comparison runs IN SQL (`now() - measured_at > :bound`) over the NOT-NULL `measured_at` — the
    governance/escalation.py idiom. Branches (each returns None = no alert):
      - row missing         -> None (a never-seeded signal is `neuro db durability seed`'s concern);
      - status != 'blocked' -> None (nothing to escalate — a fresh/ok mirror);
      - blocked but within the onset -> None (a single just-recorded block is not yet persistent);
      - blocked AND older than the onset -> the message.
    """
    bound = escalate_after if escalate_after is not None else resolve_lake_mirror_block_escalate_after()
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status, measured_at, (now() - measured_at) AS blocked_for, "
                    "(now() - measured_at) > :bound AS blocked_too_long "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"bound": bound, "k": LAKE_MIRROR_FRESHNESS_KEY},
            )
            .mappings()
            .one_or_none()
        )
    if row is None or row["status"] != "blocked" or not row["blocked_too_long"]:
        return None
    days = row["blocked_for"].days
    return (
        f"neuromancer BLOB-LAKE MIRROR BLOCKED ~{days}d — the artifact lake (artifacts-prod) is DEGRADED to "
        "cloud-only; the independent desktop-NVMe copy (ADR-0014) — the HARD GATE before the first "
        "non-recomputable capture — is NOT updating. ACTION: check the desktop sshd endpoint (Get-Service "
        f"sshd) and re-run neuro-lake-mirror.service. Last good lake mirror: {row['measured_at']}."
    )
