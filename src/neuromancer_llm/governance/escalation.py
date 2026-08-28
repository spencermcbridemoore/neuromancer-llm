"""Persistent-block escalation for the off-cloud backup mirror (mirror-arm hardening, 2026-07-17; log:214/§D).

A CONSUMER of system_health['backup_freshness'] (produced by governance/probes.py) — DISTINCT from the
ADR-0020 gate (governance/health.py), which BLOCKS canonical writes. This one only RE-ALERTS: it detects a
backup_freshness signal that has read 'blocked' for LONGER than the pinned onset and returns an actionable
message for the CLI to notify().

WHY it exists (the 2026-07-13..17 incident, root-caused first-hand): the once-per-backup-cycle OnFailure
ping (neuro-alert@) fired correctly BOTH times the mirror was down, but (a) it fires only every
BASE_BACKUP_INTERVAL, so a persistent block re-alerts at most twice over four days, and (b) its copy names
the failed UNIT ("neuromancer timer FAILED: neuro-backup.service"), not the CONSEQUENCE — both pings were
delivered and MISSED. This closes the cadence + copy gap: a DAILY re-alert (the timer) whose message states
the consequence and the action.

⚠ THE COPY LIVES IN `governance/alert_triage.py`, NOT HERE (alert-copy repair, 2026-08-28). This module once
hardcoded "ACTION: check the desktop sshd endpoint (Get-Service sshd)" — a remedy that was WRONG in two of the
three recorded blocks, and which the lake arm carried as a second, independent copy. The action is now a
DISCRIMINATING PROCEDURE the two arms share by construction; read that module for why each step is worded as
it is, and for what this row does and does NOT establish.

READ-ONLY against system_health. The record of an escalation is the ntfy history + the existing 'blocked'
probe_reports rows the backup probe already writes; this module deliberately writes nothing (a standalone
probe_reports audit row was considered and declined to keep the change minimal and read-only — a trivial
future add).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from .alert_triage import ESCALATION_ARMS, compose_block_alert
from .freshness import BACKUP_FRESHNESS_KEY, resolve_backup_block_escalate_after

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy import Engine


def evaluate_backup_block_escalation(
    engine: Engine, *, escalate_after: _dt.timedelta | None = None
) -> str | None:
    """Return an ACTIONABLE alert message iff `backup_freshness` has read 'blocked' longer than the onset
    bound; else None. READ-ONLY (a plain SELECT — no flip, no write).

    `escalate_after` overrides the pinned onset for THIS call only — an explicit operator/diagnostic knob
    (the daily timer passes none, so AUTOMATED escalation is pin-governed and fail-closed via
    resolve_backup_block_escalate_after). Default resolves the pin (ConfigurationError if absent).

    The staleness comparison runs IN SQL (`now() - measured_at > :bound`) so Postgres does tz-aware
    timestamptz math over the NOT-NULL `measured_at` — the governance/health.py idiom. Branches (each returns
    None = no alert):
      - row missing  -> None (a never-seeded signal is the durability gate's / `neuro db durability seed`'s
                        concern, not the escalator's — escalation speaks only to a PERSISTENT block of a
                        provisioned row);
      - status != 'blocked' -> None (nothing to escalate — a fresh/ok backup);
      - blocked but within the onset -> None (a single just-recorded block is not yet persistent);
      - blocked AND older than the onset -> the message.
    """
    bound = escalate_after if escalate_after is not None else resolve_backup_block_escalate_after()
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status, measured_at, (now() - measured_at) AS blocked_for, "
                    "(now() - measured_at) > :bound AS blocked_too_long "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"bound": bound, "k": BACKUP_FRESHNESS_KEY},
            )
            .mappings()
            .one_or_none()
        )
    if row is None or row["status"] != "blocked" or not row["blocked_too_long"]:
        return None
    days = row["blocked_for"].days
    return compose_block_alert(
        arm=ESCALATION_ARMS[BACKUP_FRESHNESS_KEY], days=days, measured_at=row["measured_at"]
    )
