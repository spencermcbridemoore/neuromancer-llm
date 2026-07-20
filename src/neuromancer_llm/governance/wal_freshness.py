"""The WAL-archiving-freshness contract (GO-D-wal signal-staleness follow-on) — the wal_lag signal's home.

The WAL-archiving half of the ADR-0020 durability gate is a single row in `neuro.system_health`, keyed
`wal_lag`, PRODUCED by governance/wal_archiving.py (the pg_stat_archiver probe) and CONSUMED by
governance/health.py::assert_wal_archiving_ok. This module is that arm's neutral CONTRACT home — the key
constant, the pinned signal-staleness bound + its fail-closed resolver, and the archiver-probe cadence pin
the bound is derived from. It was carved out of the producer module (wal_archiving.py fold 7: "when the
numeric-threshold repo-constant lands, carve its freshness.py-style contract home and move the key there")
when the signal-staleness gate branch landed, so the CONSUMER (health.py) and the PRODUCER (wal_archiving.py)
both import the key from a neutral leaf rather than the consumer depending on the producer.

Doctrine (the freshness.py / canonical_instance.py / price_pin.py pin idiom): the staleness bound is a
COMMITTED REPO CONSTANT, never a DB column. The wal_lag row is deliberately BOUND-LESS (its `stale_after`
column stays NULL — owner ruling D2, 2026-07-13): row PRESENCE is its provisioning proof, and the gate's
staleness basis is WAL_LAG_STALE_AFTER below (resolved fail-closed), compared against the row's `measured_at`
(which the producer advances ONLY on a healthy read). Because the row carries no `stale_after` sentinel, the
gate-enforced bound does NOT surface in `neuro db durability status` (has_bound=False) — an ACCEPTED D2
observability asymmetry vs backup_freshness's visible 8-day sentinel, NOT missing enforcement.

The fault this arm defends against: a stopped A2-16 archiver-probe timer (the dead-producer gap). The last
healthy probe leaves status='ok' with a frozen measured_at; without this bound the gate would trust that
stale 'ok' forever. WAL_LAG_STALE_AFTER is the age past which the gate flips wal_lag 'ok'->'blocked' + alerts.
"""

from __future__ import annotations

import datetime as _dt

from ..db.lanes import ConfigurationError

# The shared health_key (one constant imported by the producer wal_archiving.py, the durability registry
# durability.py, the probe-runner registry probe_registry.py, and the A2-11a gate health.py — the
# BACKUP_FRESHNESS_KEY idiom; moved here from the producer per wal_archiving.py fold 7).
WAL_LAG_KEY = "wal_lag"

# The A2-16 archiver-probe cadence of record (the neuro-archiver-probe systemd timer's OnUnitActiveSec=15min,
# owner-ruled GO-D-timer 2026-07-11). The signal-staleness bound below is derived from it — a dead producer is
# only detectable after several missed cycles. GRADUATED from a pure REFERENCE constant to a GATING pin when
# the verify_pgbackrest_config archiver-timer-cadence machine-check (wal D4) landed 2026-07-19: that check
# asserts the INSTALLED archiver timer's OnUnitActiveSec == this pin (the assertion-5 analog), resolving it
# fail-closed via resolve_archiver_probe_interval below exactly as assertion 5 resolves BASE_BACKUP_INTERVAL.
# `None` therefore means "no cadence pinned" and resolution FAILS CLOSED (never a compare-against-None); a
# change is an auditable commit (the freshness.py / provisioning_invariants.py pin idiom).
ARCHIVER_PROBE_INTERVAL: _dt.timedelta | None = _dt.timedelta(minutes=15)

# The missed-cycle / slack budget (the PROVISIONING_MARGIN idiom: the bound must clear interval + margin so a
# merely-late probe does not false-flip). 15min interval + 30min margin = 45min <= the 1h bound (15min slack).
WAL_LAG_MARGIN: _dt.timedelta = _dt.timedelta(minutes=30)

# The signal-staleness bound of record (owner ruling D1, 2026-07-13: ~4 missed 15-min cycles). `None` means "no
# bound pinned" (a fresh fork / mid-edit state) — resolution FAILS CLOSED on it, never an unpinned pass (the
# canonical_instance.py / price_pin.py / freshness.py::BACKUP_STALE_AFTER idiom). A change is an auditable commit.
WAL_LAG_STALE_AFTER: _dt.timedelta | None = _dt.timedelta(hours=1)


def resolve_wal_lag_stale_after() -> _dt.timedelta:
    """The single wal_lag-bound-resolution point (resolve_backup_stale_after idiom): the pinned signal-staleness
    bound, or ConfigurationError if it is absent (an optional bound that silently defaulted to None would
    recreate the dead-parameter fail-open in one hop). This is the AUTHORITATIVE comparison basis — the wal_lag
    row carries no `stale_after` sentinel at all (bound-less, D2)."""
    if WAL_LAG_STALE_AFTER is None:
        raise ConfigurationError(
            "wal_lag signal-staleness bound pin is absent (governance/wal_freshness.py WAL_LAG_STALE_AFTER is "
            "None) — refusing to evaluate WAL-archiver freshness on an unpinned bound (fail closed). Record the "
            "bound; a change is an auditable commit."
        )
    return WAL_LAG_STALE_AFTER


def resolve_archiver_probe_interval() -> _dt.timedelta:
    """The single archiver-probe-cadence resolution point (resolve_wal_lag_stale_after / resolve_base_backup_interval
    idiom): the pinned archiver-probe timer interval, or ConfigurationError if it is absent. Load-bearing since the
    verify_pgbackrest_config archiver-timer-cadence machine-check (wal D4, 2026-07-19) — the check resolves it here
    fail-closed, exactly as assertion 5 resolves BASE_BACKUP_INTERVAL, so an unpinned interval refuses to certify
    the installed timer rather than comparing a cadence against None."""
    if ARCHIVER_PROBE_INTERVAL is None:
        raise ConfigurationError(
            "archiver-probe cadence pin is absent (governance/wal_freshness.py ARCHIVER_PROBE_INTERVAL is None) — "
            "refusing to certify the installed archiver-probe timer cadence on an unpinned interval (fail closed). "
            "A change is an auditable commit."
        )
    return ARCHIVER_PROBE_INTERVAL
