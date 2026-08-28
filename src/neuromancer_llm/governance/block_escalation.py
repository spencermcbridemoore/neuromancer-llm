"""The shared persistent-block EVALUATOR — one implementation of "has this durability row been blocked too
long?", consumed by every escalation arm (extracted 2026-08-28, the repo3 unit).

WHY IT EXISTS NOW AND NOT EARLIER. `governance/escalation.py` and `governance/lake_escalation.py` shipped as
two hand-written copies whose bodies were 15 of 17 lines byte-identical — the same SQL, the same four-branch
chain, the same `days` extraction — differing ONLY in a health_key constant and an onset resolver.
`alert_triage.py` registered that duplication as a follow-on rather than closing it, because closing it was
not that unit's charter. repo3 is the THIRD arm, and a third copy is where the house precedent says extract:
`sftp_transport.py` was pulled out of `backup_driver.py` at B-7 for exactly this reason — "so a second
transport is not invented" (§A·65).

⚠ THE DUPLICATION FIGURE, MEASURED RATHER THAN CARRIED. `alert_triage.py`'s prose gave it as "15 of 17 lines
byte-identical". Counted at HEAD~1, the two function bodies were **20 lines each with exactly 3 differing** —
the key constant, the onset resolver, and the `ESCALATION_ARMS[...]` lookup. The conclusion is unchanged and
the number was never load-bearing, but a module has no business restating another module's uncounted figure
as its own justification.

★ WHAT THE EXTRACTION DELIBERATELY DOES **NOT** COLLAPSE, AND WHY THE ARM MODULES STILL EXIST.
Each arm keeps its own three-line module and its own CLI verb. That is not oversight:

  * Each arm's module is the one place its (key, onset resolver, registry entry) triple is named, and each
    passes its OWN `arm=` from `alert_triage.ESCALATION_ARMS`. That keeps every arm module an IMPORTER of
    `alert_triage`, which is what preserves `test_only_the_two_escalation_arms_consume_the_shared_copy`'s two
    stated purposes — making `disk_pressure.py`'s exclusion falsifiable, and making a new arm's arrival LOUD.
    ⚠ An earlier draft of this extraction took `health_key=` and looked up the arm HERE. That would have left
    this module the sole importer of `alert_triage` and collapsed that probe to `f(x)`-vs-`f(x)`: a fourth
    arm would have imported this module instead and the probe would have stayed green. The signature is
    `arm=` on purpose, and the probe's expected set is now the three arms PLUS this module.
  * Collapsing the three `neuro probe *-escalate` CLI verbs into one parameterised verb would change the
    shipped CLI surface. That is the registered `neuro probe triage` follow-on — a different surface, and
    out of scope here.

So what a fourth arm still copies is a three-line delegate, a CLI verb and a systemd pair; what it no longer
copies is the evaluator or the message.

READ-ONLY against system_health, like both bodies it replaces: no flip, no write. The record of an escalation
is the ntfy history plus the 'blocked' probe_reports rows the arm's own producer already wrote.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy import text

from .alert_triage import BlockAlertArm, compose_block_alert

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy import Engine


def evaluate_block_escalation(
    engine: Engine,
    *,
    health_key: str,
    arm: BlockAlertArm,
    resolve_onset: Callable[[], _dt.timedelta],
    escalate_after: _dt.timedelta | None = None,
) -> str | None:
    """Return an ACTIONABLE alert message iff `health_key` has read 'blocked' longer than its onset bound;
    else None. READ-ONLY (a plain SELECT).

    `escalate_after` overrides the pinned onset for THIS call only — an explicit operator/diagnostic knob
    (every daily timer passes none, so AUTOMATED escalation stays pin-governed and fail-closed through the
    arm's own `resolve_onset`; e.g. 0 to alert on any current block, which is the induced-failure test).

    The staleness comparison runs IN SQL (`now() - measured_at > :bound`) so Postgres does the tz-aware
    timestamptz math over the NOT-NULL `measured_at` — the governance/health.py idiom. Branches (each
    returning None = no alert):
      - row missing         -> None (a never-seeded signal is `neuro db durability seed`'s concern, not the
                              escalator's — escalation speaks only to a PERSISTENT block of a provisioned row);
      - status != 'blocked' -> None (nothing to escalate);
      - blocked but within the onset -> None (a single just-recorded block is not yet persistent);
      - blocked AND older than the onset -> the composed message.

    ⚠ `resolve_onset` is resolved ONLY when no override is supplied, preserving each arm's fail-closed
    behaviour on an absent pin exactly as the two hand-written bodies had it.
    """
    bound = escalate_after if escalate_after is not None else resolve_onset()
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status, measured_at, (now() - measured_at) AS blocked_for, "
                    "(now() - measured_at) > :bound AS blocked_too_long "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"bound": bound, "k": health_key},
            )
            .mappings()
            .one_or_none()
        )
    if row is None or row["status"] != "blocked" or not row["blocked_too_long"]:
        return None
    return compose_block_alert(arm=arm, days=row["blocked_for"].days, measured_at=row["measured_at"])
