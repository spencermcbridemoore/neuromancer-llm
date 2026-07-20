"""The /pgdata disk-pressure alarm (A2-8 deferred follow-on — the automated `df` watch).

A READ-ONLY operational alert, DISTINCT from both the ADR-0020 durability GATE (governance/health.py, which
BLOCKs canonical writes) and a durability SIGNAL row (governance/durability.py, seeded/consulted): disk
pressure neither blocks writes nor touches the DB. It replaces the A2-7 §3.4 manual `df -h /pgdata` watch with
an automated check that notify()s when the filesystem holding /pgdata (the PG data dir AND the repo1 pgbackrest
backup tree) crosses a pinned usage threshold — a full /pgdata STOPS both PostgreSQL and pgbackrest.

Shape mirrors governance/escalation.py: a pure `evaluate_disk_pressure` returns an actionable message (or None);
the CLI (`neuro probe disk`) notify()s it; a daily OnCalendar+Persistent timer drives it (the mirror-arm
precedent, NOT the A2-16 monotonic-only never-fires shape). This module deliberately writes nothing — the alert
IS the record (the ntfy history), exactly as escalation.py.

Pin idiom (freshness.py / wal_freshness.py / provisioning_invariants.py): DISK_PRESSURE_THRESHOLD is a COMMITTED
REPO CONSTANT with a fail-closed resolver — `None` means "no pin recorded" and resolution RAISES, never a silent
default that could let a filling disk read as fine.
"""

from __future__ import annotations

import shutil

from ..db.lanes import ConfigurationError

# The /pgdata usage FRACTION (used/total) at or past which the alarm fires (owner-ruled 2026-07-19; adjustable
# DATA, but a change is an auditable commit). 0.85 leaves headroom before PG/pgbackrest I/O is threatened. `None`
# means "no threshold pinned" (a fresh fork / mid-edit state) — resolution FAILS CLOSED, never an unpinned pass.
DISK_PRESSURE_THRESHOLD: float | None = 0.85


def resolve_disk_pressure_threshold() -> float:
    """The single threshold-resolution point (resolve_backup_stale_after idiom): the pinned usage fraction, or
    ConfigurationError if it is absent (an optional threshold that silently defaulted would recreate the
    dead-parameter fail-open — a filling disk reading as fine)."""
    if DISK_PRESSURE_THRESHOLD is None:
        raise ConfigurationError(
            "disk-pressure threshold pin is absent (governance/disk_pressure.py DISK_PRESSURE_THRESHOLD is "
            "None) — refusing to evaluate disk pressure on an unpinned threshold (fail closed). Record the "
            "fraction; a change is an auditable commit."
        )
    return DISK_PRESSURE_THRESHOLD


def evaluate_disk_pressure(path: str = "/pgdata", *, threshold: float | None = None) -> str | None:
    """Return an ACTIONABLE alert message iff the filesystem containing `path` is at/over the usage threshold;
    else None. READ-ONLY: a single `shutil.disk_usage` statvfs — no DB, no write.

    `threshold` overrides the pinned fraction for THIS call only — an explicit operator/diagnostic knob (the
    daily timer passes none, so AUTOMATED alarming is pin-governed and fail-closed via
    resolve_disk_pressure_threshold; e.g. 0.0 forces the alarm — the induced-failure test). The usage fraction
    is `used / total` (raw occupancy). A path that cannot be stat'd RAISES ConfigurationError (fail loud — a
    missing/unreadable /pgdata is itself an alertable condition, never a silent skip)."""
    bound = threshold if threshold is not None else resolve_disk_pressure_threshold()
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        raise ConfigurationError(
            f"disk-pressure check cannot stat {path!r} ({type(exc).__name__}) — a missing/unreadable path is "
            "itself alertable (fail closed)."
        ) from exc
    used_frac = usage.used / usage.total
    if used_frac < bound:
        return None
    gib = 1024**3
    return (
        f"neuromancer /pgdata DISK PRESSURE — {path} is {used_frac * 100:.0f}% full "
        f"({usage.used / gib:.1f}/{usage.total / gib:.1f} GiB), at/past the {bound * 100:.0f}% alarm "
        "threshold. /pgdata holds the PostgreSQL data dir AND the repo1 pgbackrest backup tree; a full volume "
        "STOPS both PostgreSQL and pgbackrest. ACTION: prune retained fulls (`pgbackrest info` / retention) "
        f"or expand the Cinder volume (ADR-0042), then re-check `df -h {path}`."
    )
