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

⚠ THE COPY LIVES IN `governance/alert_triage.py`, NOT HERE (alert-copy repair, 2026-08-28), shared with the
backup arm by construction rather than by two hand-written copies that had already diverged. Two claims this
module's message used to make are corrected there: it no longer calls this signal "the HARD GATE" (the
write-time preflight that would gate is registered-and-unbuilt, and no capture-path consumer of this row
exists), and it no longer names the desktop sshd endpoint as the remedy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .alert_triage import ESCALATION_ARMS
from .block_escalation import evaluate_block_escalation
from .lake_freshness import LAKE_MIRROR_FRESHNESS_KEY, resolve_lake_mirror_block_escalate_after

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy import Engine


def evaluate_lake_mirror_block_escalation(
    engine: Engine, *, escalate_after: _dt.timedelta | None = None
) -> str | None:
    """Return an ACTIONABLE alert message iff `lake_mirror_freshness` has read 'blocked' longer than the onset
    bound; else None. READ-ONLY (a plain SELECT — no flip, no write).

    ⚠ THE BODY MOVED, THE BEHAVIOUR DID NOT (2026-08-28, the repo3 unit) — the four-branch chain and its SQL
    now live ONCE in `governance/block_escalation.py`; this module supplies only what is this arm's own.

    `escalate_after` overrides the pinned onset for THIS call only — an explicit operator/diagnostic knob (the
    daily timer passes none, so AUTOMATED escalation is pin-governed and fail-closed via
    resolve_lake_mirror_block_escalate_after; e.g. 0 to alert on any current block — the induced-failure test).
    """
    return evaluate_block_escalation(
        engine,
        health_key=LAKE_MIRROR_FRESHNESS_KEY,
        arm=ESCALATION_ARMS[LAKE_MIRROR_FRESHNESS_KEY],
        resolve_onset=resolve_lake_mirror_block_escalate_after,
        escalate_after=escalate_after,
    )
