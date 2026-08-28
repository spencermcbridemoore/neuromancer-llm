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

from .alert_triage import ESCALATION_ARMS
from .block_escalation import evaluate_block_escalation
from .freshness import BACKUP_FRESHNESS_KEY, resolve_backup_block_escalate_after

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy import Engine


def evaluate_backup_block_escalation(
    engine: Engine, *, escalate_after: _dt.timedelta | None = None
) -> str | None:
    """Return an ACTIONABLE alert message iff `backup_freshness` has read 'blocked' longer than the onset
    bound; else None. READ-ONLY (a plain SELECT — no flip, no write).

    ⚠ THE BODY MOVED, THE BEHAVIOUR DID NOT (2026-08-28, the repo3 unit). The four-branch chain, its SQL and
    the `days` extraction now live ONCE in `governance/block_escalation.py`; this module supplies the three
    things that are actually this arm's own — the health_key, the pinned onset resolver, and the registry
    entry carrying its copy. The extraction was behaviour-preserving and this file's existing tests were the
    net. See that module for why each arm keeps its own delegate rather than collapsing further.

    `escalate_after` overrides the pinned onset for THIS call only — an explicit operator/diagnostic knob
    (the daily timer passes none, so AUTOMATED escalation is pin-governed and fail-closed via
    resolve_backup_block_escalate_after). Default resolves the pin (ConfigurationError if absent).
    """
    return evaluate_block_escalation(
        engine,
        health_key=BACKUP_FRESHNESS_KEY,
        arm=ESCALATION_ARMS[BACKUP_FRESHNESS_KEY],
        resolve_onset=resolve_backup_block_escalate_after,
        escalate_after=escalate_after,
    )
