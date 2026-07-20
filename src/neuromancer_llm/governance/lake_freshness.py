"""The blob-lake-mirror freshness contract (B-7, 2026-07-20) — the machine-readable signal that an
independent desktop-NVMe copy of the artifact lake exists and is current.

A LEAF module (the freshness.py / wal_freshness.py idiom): the health_key, the pinned staleness bound, and its
fail-closed resolver, and NOTHING heavy — so governance/durability.py can import the key + bound for the
provisioning row without pulling in the sftp/Azure driver. The PRODUCER (governance/lake_mirror.py) bumps the
signal; the notify-only ESCALATION (governance/lake_escalation.py) consumes it.

★ NOTIFY-ONLY, NOT A GATE (owner fork ruling, 2026-07-20). Unlike backup_freshness / wal_lag, this key is
DELIBERATELY kept out of health.GATE_CONSULTED_KEYS — a stale lake mirror ALARMS (the daily escalation + the
per-run OnFailure ping) but NEVER blocks a canonical write. The write-time hard gate before the first
NON-recomputable capture is a Stage-B capture-path PREFLIGHT (registered), which will read this signal
status-THEN-staleness (BLOCK on status != 'ok' OR now()-measured_at > the bound — the assert_backup_fresh
branch order); a staleness-only read would be fail-open for a freshly-failed mirror.

Doctrine (the freshness.py / canonical_instance.py / price_pin.py pin idiom): the staleness bound is a
COMMITTED REPO CONSTANT, never the DB column. `None` means "no bound pinned" (a fresh fork / mid-edit state) —
resolution FAILS CLOSED, never an unpinned pass; a change is an auditable commit.
"""

from __future__ import annotations

import datetime as _dt

from ..db.lanes import ConfigurationError

# The shared health_key (one constant imported by the provisioning row, the producer, and the escalation).
LAKE_MIRROR_FRESHNESS_KEY = "lake_mirror_freshness"

# The staleness bound of record. The lake mirror runs DAILY (ops/neuro-lake-mirror.timer, OnCalendar=daily),
# so 3 days is one cadence + ~2 missed-cycle margin — the same shape as backup_freshness's cadence+margin
# headroom. Owner-adjustable DATA, but a change is an auditable commit. `None` fails closed (the pin idiom).
LAKE_MIRROR_STALE_AFTER: _dt.timedelta | None = _dt.timedelta(days=3)


def resolve_lake_mirror_stale_after() -> _dt.timedelta:
    """The single bound-resolution point (resolve_backup_stale_after idiom): the pinned staleness bound, or
    ConfigurationError if it is absent (an optional bound that silently defaulted to None would recreate the
    dead-parameter fail-open). This is the AUTHORITATIVE comparison basis — the system_health.stale_after
    COLUMN is a provisioning sentinel, never the basis."""
    if LAKE_MIRROR_STALE_AFTER is None:
        raise ConfigurationError(
            "lake-mirror staleness bound pin is absent (governance/lake_freshness.py LAKE_MIRROR_STALE_AFTER "
            "is None) — refusing to seed or evaluate lake-mirror freshness on an unpinned bound (fail "
            "closed). Record the bound; a change is an auditable commit."
        )
    return LAKE_MIRROR_STALE_AFTER


def resolve_lake_mirror_block_escalate_after() -> _dt.timedelta:
    """The persistent-block ESCALATION onset — how long `lake_mirror_freshness` may read 'blocked' before
    governance/lake_escalation.py re-alerts a human. Onset = the staleness bound BY REFERENCE (never a second
    pinned number — one-implementation-per-concept): a healthy signal advances `measured_at` every daily
    cycle, so a 'blocked' status older than the bound means the independent off-cloud copy has not updated
    across the whole window the freshness gate would call fresh. Fail-closed via resolve_lake_mirror_stale_after."""
    return resolve_lake_mirror_stale_after()
