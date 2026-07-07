"""The backup-freshness contract (A2-10 <-> A2-11a) — the shared machine-readable durability signal.

The DB-backup-age half of the ADR-0020 durability gate is a single row in `neuro.system_health`, keyed
`backup_freshness` (the DDL comment at phase3-ddl.sql:583 enumerates this key). This module is its ONE home:
the key constant, the pinned staleness bound, its fail-closed resolver, and the once-only seed. A2-10
(governance/probes.py) PRODUCES the signal (bump + audit); A2-11a (governance/health.py) CONSUMES it (the
fail-closed read + flip + notify). Both import the constants HERE so a typo can never split them.

Doctrine (the canonical_instance.py / price_pin.py idiom): the staleness bound is a COMMITTED REPO CONSTANT,
never the DB column. The same DB the durability guard protects is exactly the thing that goes stale, rolls
back, or gets restored — so trusting its own `stale_after` column as the comparison basis would reintroduce
the very fail-open the pin idiom exists to kill. The `system_health.stale_after` COLUMN is therefore a
provisioning SENTINEL + drift-check only: seeded ONCE (registrar/admin — the writer holds no INSERT on
system_health, grants.sql:48), non-NULL proves an operator deliberately established the contract, and NULL
(the server default) means unprovisioned -> the gate BLOCKs. The authoritative bound is BACKUP_STALE_AFTER
below (owner-nodded repo-constant-authoritative, 2026-07-07).
"""

from __future__ import annotations

import datetime as _dt

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db.lanes import ConfigurationError

# The shared health_key (one constant imported by BOTH the seed here and the A2-11a gate).
BACKUP_FRESHNESS_KEY = "backup_freshness"

# The staleness bound of record (ADR-0020: a DB backup >8 days old flips the gate). `None` means "no bound
# pinned" (a fresh fork / mid-edit state) — resolution FAILS CLOSED on it, never an unpinned pass (the
# canonical_instance.py CANONICAL_INSTANCE_UUID / price_pin.py STORAGE_PRICE_PIN idiom). A change is an
# auditable commit, exactly the event that should be loud.
BACKUP_STALE_AFTER: _dt.timedelta | None = _dt.timedelta(days=8)


def resolve_backup_stale_after() -> _dt.timedelta:
    """The single bound-resolution point (canonical_instance.py::expected_uuid_for_lane /
    price_pin.py::resolve_price idiom): the pinned staleness bound, or ConfigurationError if it is absent (an
    optional bound that silently defaulted to None would recreate the dead-parameter fail-open in one hop).
    This is the AUTHORITATIVE comparison basis — the system_health.stale_after COLUMN is a sentinel, never the
    basis."""
    if BACKUP_STALE_AFTER is None:
        raise ConfigurationError(
            "backup staleness bound pin is absent (governance/freshness.py BACKUP_STALE_AFTER is None) — "
            "refusing to seed or evaluate backup freshness on an unpinned bound (fail closed). Record the "
            "ADR-0020 >8d bound; a change is an auditable commit."
        )
    return BACKUP_STALE_AFTER


def seed_backup_freshness(conn: Connection) -> bool:
    """Seed the `backup_freshness` row ONCE, registrar/admin at provisioning (the writer holds no INSERT on
    system_health — grants.sql:48). Idempotent (ON CONFLICT DO NOTHING) and FAIL-CLOSED at birth so the gate
    BLOCKs until A2-10 records the first verified backup:

      - status='blocked'  — the born-healthy server default ('ok') would read the gate OPEN before any backup;
      - measured_at=epoch — the server default now() would read FRESH for the first 8 days with no backup, so
        seed it far in the past and let a real success (A2-10) advance it;
      - stale_after=the pin — the provisioning sentinel (non-NULL == deliberately provisioned; the gate BLOCKs
        on NULL). The authoritative comparison basis is still the repo constant, never this column.

    Returns True iff it inserted (False == already seeded). Commits (mirrors db/restore.py::scrub_identity).
    """
    stale = resolve_backup_stale_after()  # fail closed if the pin is absent, before any write
    result = conn.execute(
        text(
            "INSERT INTO neuro.system_health (health_key, status, detail, measured_at, stale_after) "
            "VALUES (:k, 'blocked', :d, 'epoch'::timestamptz, :stale) "
            "ON CONFLICT (health_key) DO NOTHING"
        ),
        {"k": BACKUP_FRESHNESS_KEY, "d": "seeded; awaiting first verified backup", "stale": stale},
    )
    conn.commit()
    return bool(result.rowcount)
