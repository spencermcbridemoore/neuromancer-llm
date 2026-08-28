"""Persistent-block escalation for the repo3 (Backblaze B2) third copy — NOTIFY-ONLY (§A·72, 2026-08-28).

A CONSUMER of system_health['repo3_freshness'] (produced by governance/repo3_probe.py) — the third instance
of the governance/escalation.py + governance/lake_escalation.py pattern, and the first one that is a pure
delegate: the evaluator body it would otherwise have copied lives in governance/block_escalation.py, and its
operator copy lives in governance/alert_triage.py. This module supplies only what is genuinely this arm's:
the health_key, the pinned onset resolver, and the registry entry.

It does NOT gate (the key is deliberately absent from health.GATE_CONSULTED_KEYS) and it writes NOTHING —
the alert IS the record, alongside the 'blocked' probe_reports rows the repo3 probe already wrote.

⚠ WHY THIS ARM NEEDS ITS OWN TRIAGE, AND WHY `require_triage` COULD NOT HAVE FORCED IT. The two off-cloud
arms share OFF_CLOUD_MIRROR_TRIAGE because they share one failing leg — the desktop sftp push. repo3's leg is
an S3-compatible HTTPS call to a cloud bucket: nothing in that procedure applies, and handing this arm the
desktop procedure would be the 2026-08-28 alert-copy defect reproduced one arm over. `require_triage` makes
that choice EXPLICIT but cannot make it CORRECT — its own docstring says so — so the correctness is here, in
a triage written against the four failure families a B2 repository actually has.

⚠ AND WHY THE ALERT IS SLOW, STATED RATHER THAN DISCOVERED IN AN INCIDENT: worst case is roughly the 2-day
backup cadence plus the 4-day onset before the daily escalation fires, and repo3 has no per-cycle
`OnFailure=` ping of its own because its systemd lines carry the ignore-failure `-` prefix that keeps it from
blocking the gating freshness bump. See governance/repo3_freshness.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .alert_triage import ESCALATION_ARMS
from .block_escalation import evaluate_block_escalation
from .repo3_freshness import REPO3_FRESHNESS_KEY, resolve_repo3_block_escalate_after

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy import Engine


def evaluate_repo3_block_escalation(
    engine: Engine, *, escalate_after: _dt.timedelta | None = None
) -> str | None:
    """Return an ACTIONABLE alert message iff `repo3_freshness` has read 'blocked' longer than the onset
    bound; else None. READ-ONLY.

    `escalate_after` overrides the pinned onset for THIS call only — the operator/diagnostic knob (the daily
    timer passes none, so automated escalation stays pin-governed and fail-closed via
    resolve_repo3_block_escalate_after; 0 alerts on any current block, which is how the deploy runbook lands
    the E·16 proof without waiting four days).
    """
    return evaluate_block_escalation(
        engine,
        health_key=REPO3_FRESHNESS_KEY,
        arm=ESCALATION_ARMS[REPO3_FRESHNESS_KEY],
        resolve_onset=resolve_repo3_block_escalate_after,
        escalate_after=escalate_after,
    )
