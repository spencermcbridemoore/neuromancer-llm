"""GO-D-seed (pre-cliff item 1): the durability-row provisioning surface — seed / reconcile / status.

The ADR-0020 durability gate (governance/health.py) fail-closed BLOCKs a canonical write unless the
`neuro.system_health` durability rows prove the DB is durable. Those rows must be SEEDED once at
provisioning (registrar/admin — the writer holds no INSERT, grants.sql:48), and their `stale_after`
SENTINEL kept aligned to the pinned bound. This module is the ONE surface that seeds/reconciles/reports
EVERY durability row, so a new arm is a PURE APPEND to DURABILITY_ROWS — never a per-arm CLI that forgets a
row (a provisioning trap; owner's top constraint, 2026-07-07). The WAL-archiving arm (GO-D-wal, 2026-07-11)
landed exactly that way: WAL_LAG_ROW below, zero CLI change. `neuro db durability {seed,reconcile,status}`
is the thin CLI over it.

Doctrine (the freshness.py / canonical_instance.py / price_pin.py pin idiom, carried):
  - Each row is seeded born-FAIL-CLOSED (status='blocked', measured_at=epoch) so the gate BLOCKs until a
    real signal advances it (a verified backup for backup_freshness; the arm's own producer for others).
  - A row's `stale_after` BOUND is OPTIONAL. backup_freshness pins the >8-day age bound as its sentinel; a
    bound-less row (e.g. item-3 wal_lag, whose interim policy is the boolean "any archiver error blocks")
    seeds `stale_after` NULL — its ROW PRESENCE is the provisioning proof and its own gate branch supplies
    liveness. The authoritative bound is always the repo constant (resolve_*), never the DB column.
  - seed = INSERT (registrar|admin). reconcile = `stale_after` UPDATE — **ADMIN-ONLY** (neuro_registrar
    holds no system_health UPDATE, grants.sql). reconcile RE-ALIGNS an already-provisioned (NON-NULL)
    drifted bound only; it NEVER fills a NULL (that would defeat the gate's `stale_after IS NULL -> BLOCK`
    sentinel — provisioning a NULL is seed's job, co-established with the born blocked/epoch), and it never
    touches status/measured_at (a genuine 'blocked' self-heals only via a real signal bump).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .freshness import BACKUP_FRESHNESS_KEY, resolve_backup_stale_after
from .lake_freshness import LAKE_MIRROR_FRESHNESS_KEY, resolve_lake_mirror_stale_after
from .wal_freshness import WAL_LAG_KEY


@dataclass(frozen=True)
class DurabilityRow:
    """A `system_health` durability row's provisioning spec.

    `stale_after_bound` is a LATE-BOUND resolver (called at each seed/reconcile, so a bound-constant change
    and the pin-absent fail-closed path are honored at call time, never baked in at import), or None for a
    bound-less row (seeds `stale_after` NULL; never reconciled; never reported as 'drift')."""

    health_key: str
    born_detail: str
    stale_after_bound: Callable[[], _dt.timedelta] | None = None
    born_status: str = "blocked"


@dataclass(frozen=True)
class RowStatus:
    """A read-only durability-row report (governance-visibility; the CLI `status` renders it)."""

    health_key: str
    present: bool
    status: str | None
    stale_after: _dt.timedelta | None
    measured_at: _dt.datetime | None
    has_bound: bool
    drift: bool  # only meaningful for bound-carrying rows; always False for bound-less / absent rows


# The registry: the ONE place every durability row is declared. backup_freshness reuses freshness.py's key +
# bound so there is exactly one seeding implementation (seed_row); seed_backup_freshness delegates to it.
# wal_lag (GO-D-wal, the item-3 pure append) is BOUND-LESS: its interim policy is the boolean archiver check
# (wal_archiving.classify_archiver), so stale_after seeds NULL — row PRESENCE is its provisioning proof and
# health.assert_wal_archiving_ok supplies its liveness.
BACKUP_FRESHNESS_ROW = DurabilityRow(
    health_key=BACKUP_FRESHNESS_KEY,
    born_detail="seeded; awaiting first verified backup",
    stale_after_bound=resolve_backup_stale_after,
)
WAL_LAG_ROW = DurabilityRow(
    health_key=WAL_LAG_KEY,
    born_detail="seeded; awaiting first WAL-archiver probe",
    stale_after_bound=None,
)
# lake_mirror_freshness (B-7, 2026-07-20) is a durability SIGNAL row that is DELIBERATELY NON-GATING (owner
# fork ruling): it is provisioned + produced through these ONE surfaces (seed_all / the probe registry) so it
# can never ship seeded-but-unrunnable, but its key is NOT in health.GATE_CONSULTED_KEYS — a stale lake mirror
# ALARMS (the daily lake escalation), it never BLOCKs a canonical write. It carries the stale_after bound so its
# freshness is measurable (the future Stage-B capture-path preflight reads it status-then-staleness).
LAKE_MIRROR_ROW = DurabilityRow(
    health_key=LAKE_MIRROR_FRESHNESS_KEY,
    born_detail="seeded; awaiting first verified lake mirror",
    stale_after_bound=resolve_lake_mirror_stale_after,
)
DURABILITY_ROWS: tuple[DurabilityRow, ...] = (BACKUP_FRESHNESS_ROW, WAL_LAG_ROW, LAKE_MIRROR_ROW)

# The set of health_keys this surface provisions — cross-checked against the keys the gate consults
# (governance/health.py::GATE_CONSULTED_KEYS; a test asserts GATE_CONSULTED_KEYS <= DURABILITY_KEYS), so a new
# gate branch can never ship without its provisioning row. The relation is a PROPER subset since B-7: every
# GATE_CONSULTED key is provisioned here, but not every provisioned key gates — lake_mirror_freshness is a
# provisioned, produced, NON-GATING signal (notify-only, the fork ruling).
DURABILITY_KEYS: frozenset[str] = frozenset(r.health_key for r in DURABILITY_ROWS)


def seed_row(conn: Connection, row: DurabilityRow) -> bool:
    """Seed ONE durability row born fail-closed (status=<born_status>, measured_at=epoch,
    stale_after=<resolved pin or NULL>), idempotent (ON CONFLICT DO NOTHING). Resolves the bound FIRST so an
    absent pin raises ConfigurationError BEFORE any DB statement (fail closed). Commits (mirrors
    freshness.seed_backup_freshness / db.restore.scrub_identity — callers pass a bare Connection). Returns
    True iff it inserted."""
    stale = row.stale_after_bound() if row.stale_after_bound is not None else None
    result = conn.execute(
        text(
            "INSERT INTO neuro.system_health (health_key, status, detail, measured_at, stale_after) "
            "VALUES (:k, :st, :d, 'epoch'::timestamptz, :stale) "
            "ON CONFLICT (health_key) DO NOTHING"
        ),
        {"k": row.health_key, "st": row.born_status, "d": row.born_detail, "stale": stale},
    )
    conn.commit()
    return bool(result.rowcount)


def seed_all(conn: Connection, rows: Sequence[DurabilityRow] = DURABILITY_ROWS) -> dict[str, bool]:
    """Seed every registry row (idempotent). Returns {health_key: inserted?}."""
    return {row.health_key: seed_row(conn, row) for row in rows}


def reconcile_row(conn: Connection, row: DurabilityRow) -> tuple[bool, int]:
    """Re-align an EXISTING, provisioned row's `stale_after` sentinel to the current pin. Returns
    (present, reconciled_rowcount).

    A bound-less row has no sentinel to reconcile -> (present, 0). NEVER fills a NULL `stale_after` (the
    `... IS NOT NULL ...` guard) — provisioning a NULL is seed's job, and filling it here would flip the
    gate's fail-closed 'stale_after IS NULL -> BLOCK' sentinel to a FRESH read with no backup ever recorded.
    Only re-aligns a NON-NULL drifted bound. ADMIN-ONLY at the DB (stale_after UPDATE; grants.sql)."""
    present = (
        conn.execute(
            text("SELECT 1 FROM neuro.system_health WHERE health_key = :k"), {"k": row.health_key}
        ).scalar()
        is not None
    )
    if row.stale_after_bound is None:
        return (present, 0)
    pin = row.stale_after_bound()  # resolve first (fail closed if absent), before any write
    result = conn.execute(
        text(
            "UPDATE neuro.system_health SET stale_after = :pin "
            "WHERE health_key = :k AND stale_after IS NOT NULL AND stale_after IS DISTINCT FROM :pin"
        ),
        {"pin": pin, "k": row.health_key},
    )
    conn.commit()
    return (present, result.rowcount)


def reconcile_all(
    conn: Connection, rows: Sequence[DurabilityRow] = DURABILITY_ROWS
) -> dict[str, tuple[bool, int]]:
    """Reconcile every registry row. Returns {health_key: (present, reconciled_rowcount)}. A MISSING row is
    reported (present=False) so the caller fails loud + directs to `seed` — a missing row still BLOCKs the
    gate (branch 1), so it is a provisioning gap, not a fail-open."""
    return {row.health_key: reconcile_row(conn, row) for row in rows}


def status_all(conn: Connection, rows: Sequence[DurabilityRow] = DURABILITY_ROWS) -> list[RowStatus]:
    """Read-only report of every registry row: presence, status, `stale_after` drift vs the pin, measured_at.
    Drift is computed ONLY for bound-carrying rows (a bound-less row is present+status, never 'drifted'; an
    absent row is present=False, drift=False)."""
    out: list[RowStatus] = []
    for row in rows:
        db = (
            conn.execute(
                text(
                    "SELECT status, stale_after, measured_at FROM neuro.system_health WHERE health_key = :k"
                ),
                {"k": row.health_key},
            )
            .mappings()
            .one_or_none()
        )
        has_bound = row.stale_after_bound is not None
        if db is None:
            out.append(RowStatus(row.health_key, False, None, None, None, has_bound, False))
            continue
        drift = has_bound and db["stale_after"] != row.stale_after_bound()  # type: ignore[misc]
        out.append(
            RowStatus(
                row.health_key,
                True,
                db["status"],
                db["stale_after"],
                db["measured_at"],
                has_bound,
                bool(drift),
            )
        )
    return out
