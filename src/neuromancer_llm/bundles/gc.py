"""Bundle GC — the reaper collects ONLY unsealed (unregistered) bundles; sealed are EXEMPT (ADR-0010).

This exemption is exactly what makes W6/W7 recoverable instead of data loss: the reaper can never eat a
sealed bundle, so a resumed worker re-registers it. Deletion of registered data is the mirror — tombstone
row BEFORE blob removal (capture contract §4); blob-orphan sweep is the crawl drill (Stage 2).
"""

from __future__ import annotations

import datetime as _dt

from sqlalchemy import Engine, text

# Sealed/registered/staged/verified bundles are NEVER collected. Only never-registered ones are.
COLLECTIBLE_STATES = ("unsealed",)
# R-GC: an unsealed bundle younger than this may be a worker mid-registration; deleting it would
# FK-violate when its register txn inserts artifacts. Only age it out after the grace window.
DEFAULT_GRACE = _dt.timedelta(hours=1)


def collect_unsealed(engine: Engine, *, grace: _dt.timedelta = DEFAULT_GRACE) -> int:
    """Delete unsealed (never-registered) bundles OLDER than `grace`. Sealed/registered are exempt.

    Unsealed bundles have no registered artifacts (registration is W6-W7, last), so deleting the row is
    safe — but only once past the grace window, so an in-flight registration is never reaped out from
    under itself (R-GC). Returns the number of bundles collected.
    """
    with engine.begin() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT bundle_id FROM neuro.bundles "
                    "WHERE state = ANY(:states) AND created_at < now() - :grace"
                ),
                {"states": list(COLLECTIBLE_STATES), "grace": grace},
            ).all()
        ]
        if not ids:
            return 0
        conn.execute(text("DELETE FROM neuro.bundles WHERE bundle_id = ANY(:ids)"), {"ids": ids})
        return len(ids)
