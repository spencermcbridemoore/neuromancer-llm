"""GO-D-gate (A2-11a) + GO-D-wal: the durability gate (ADR-0020) — the CONSUMER of the durability signals.

`assert_backup_fresh(engine)` reads system_health['backup_freshness'] (produced by governance/probes.py,
A2-10) and RAISES `DurabilityGateError` (fail closed) unless the DB backup is provably fresh.
`assert_wal_archiving_ok(engine)` reads system_health['wal_lag'] (produced by governance/wal_archiving.py,
GO-D-wal 2026-07-11) and RAISES unless WAL archiving is provably healthy AND fresh (the signal-staleness /
dead-producer branch landed 2026-07-13). `assert_durability_ok(engine)` is
the STABLE single entry point A2-11b's consult targets — it composes BOTH arms (the callsite never changed
when the WAL arm landed, exactly as designed), so a caller can refuse a canonical write before it happens.

Doctrine:
  - The staleness bound is a REPO CONSTANT per arm (freshness.py::BACKUP_STALE_AFTER for backup;
    wal_freshness.py::WAL_LAG_STALE_AFTER for wal_lag), never the DB column (the guard cannot trust the very DB
    it protects to hold its own threshold). For backup, the system_health.stale_after COLUMN is a provisioning
    sentinel + drift-check; the wal_lag row is bound-less (no sentinel, staleness-only — no drift branch).
  - The staleness comparison runs IN SQL (`now() - measured_at > :bound`) so Postgres does tz-aware
    timestamptz math and a NULL delta is impossible (measured_at is NOT NULL).
  - A drift/stale transition flips status 'ok'->'blocked' via a GUARDED CAS and notify()s ONCE per transition:
    once the flip persists, a later consult short-circuits SILENTLY (a stale row at branch 4 status!='ok'; a
    drifted row re-enters branch 3 but its CAS misses on status!='ok'), so a persistent fault does not re-fire
    the alert on every consult. The staleness flip is additionally pinned to the measured_at the gate READ so
    a fresh backup that landed since (advancing measured_at) is not clobbered.
  - The gate takes the ENGINE, not a caller connection: the flip commits in its OWN transaction so it cannot
    roll back with a caller's aborted write (which would let the same transition re-notify next call).
  - notify() is fail-LOUD (ADR-0019): a broken alert channel raises, which still BLOCKs the write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from .freshness import BACKUP_FRESHNESS_KEY, resolve_backup_stale_after
from .notify import notify
from .wal_freshness import WAL_LAG_KEY, resolve_wal_lag_stale_after

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy import Engine


# The health_keys assert_durability_ok consults — enforced against governance/durability.py::DURABILITY_KEYS
# (a test asserts GATE_CONSULTED_KEYS <= DURABILITY_KEYS) so a NEW gate branch cannot ship without its
# provisioning row. backup_freshness (A2-11a) + wal_lag (GO-D-wal, the item-3 second edit of the two-edit
# change).
GATE_CONSULTED_KEYS: frozenset[str] = frozenset({BACKUP_FRESHNESS_KEY, WAL_LAG_KEY})


class DurabilityGateError(RuntimeError):
    """The durability interlock is BLOCKING a write (ADR-0020): the DB backup is missing, unprovisioned,
    mis-provisioned (drift), already-failed, or stale. Fail LOUD — a caller must not proceed to a canonical
    write while this raises."""


def _flip_and_notify(
    engine: Engine, *, key: str, reason: str, detail: str, guard_measured_at: _dt.datetime | None = None
) -> None:
    """Flip the given `key`'s status 'ok'->'blocked' via a guarded CAS in an AUTONOMOUS txn, then
    notify() ONCE per transition (only when the CAS actually flipped a row). `guard_measured_at`, when given,
    pins the flip to the measured_at the gate READ, so a fresh signal that landed between the read and the flip
    (its bump advanced measured_at) is NOT clobbered into 'blocked' with a false alert — the CAS simply misses.
    `key` selects the arm (backup_freshness or wal_lag) — REQUIRED, no default: a defaulted key would let a new
    caller silently flip the wrong arm (a fail-open smell)."""
    sql = "UPDATE neuro.system_health SET status='blocked', detail=:d WHERE health_key=:k AND status='ok'"
    params: dict[str, object] = {"d": detail, "k": key}
    if guard_measured_at is not None:
        sql += " AND measured_at=:m"
        params["m"] = guard_measured_at
    with engine.begin() as conn:
        flipped = conn.execute(text(sql), params).rowcount
    if flipped:
        notify(reason)  # fail-LOUD (ADR-0019): a broken alert channel raises -> the write still BLOCKs


def assert_backup_fresh(engine: Engine) -> None:
    """Fail-closed gate on the DB-backup-age half of ADR-0020. Raises DurabilityGateError unless the backup is
    provably fresh. Branch order (fail closed at each step):

      1. row missing        -> BLOCK (never seeded)
      2. stale_after IS NULL -> BLOCK (unprovisioned sentinel)
      3. stale_after != pin  -> flip + notify + BLOCK (mis-provisioned / drift)
      4. status != 'ok'      -> BLOCK (already recorded blocked; no re-flip/notify -> once-per-transition)
      5. now()-measured_at > pin (IN SQL) -> guarded-CAS flip + notify-if-transition + BLOCK (stale)
      6. else                -> return (fresh).
    """
    bound = resolve_backup_stale_after()  # repo-constant bound; ConfigurationError if the pin is absent
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status, stale_after, measured_at, (now() - measured_at) > :bound AS is_stale "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"bound": bound, "k": BACKUP_FRESHNESS_KEY},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise DurabilityGateError(
            "durability BLOCK: system_health['backup_freshness'] row is missing (never seeded). Seed it "
            "(registrar/admin) at provisioning before a canonical write."
        )
    if row["stale_after"] is None:
        raise DurabilityGateError(
            "durability BLOCK: backup_freshness.stale_after IS NULL (unprovisioned sentinel)."
        )
    if row["stale_after"] != bound:
        _flip_and_notify(
            engine,
            key=BACKUP_FRESHNESS_KEY,
            reason=(
                f"neuromancer durability: backup_freshness stale_after {row['stale_after']} != pinned bound "
                f"{bound} (drift) — BLOCKING canonical writes"
            ),
            detail=f"stale_after drift: {row['stale_after']} != pinned {bound}",
        )
        raise DurabilityGateError(
            f"durability BLOCK: backup_freshness.stale_after ({row['stale_after']}) != the pinned bound "
            f"({bound}) — mis-provisioned (drift)."
        )
    if row["status"] != "ok":
        raise DurabilityGateError(
            f"durability BLOCK: backup_freshness.status is {row['status']!r} (not 'ok') — the last backup "
            "failed or a prior gate flipped it."
        )
    if row["is_stale"]:
        _flip_and_notify(
            engine,
            key=BACKUP_FRESHNESS_KEY,
            reason=(
                f"neuromancer durability: DB backup is STALE (age > pinned bound {bound}) — BLOCKING "
                "canonical writes"
            ),
            detail=f"backup stale: now() - measured_at > {bound}",
            guard_measured_at=row["measured_at"],
        )
        raise DurabilityGateError(
            f"durability BLOCK: DB backup is STALE (now() - measured_at > the pinned bound {bound})."
        )
    return None  # fresh: provisioned, no drift, status ok, within the bound


def assert_wal_archiving_ok(engine: Engine) -> None:
    """Fail-closed gate on the WAL-archiving arm of ADR-0020 (GO-D-wal + the signal-staleness follow-on). Raises
    DurabilityGateError unless the wal_lag signal is present, healthy, AND fresh. Branch order (fail closed at
    each step):

      1. row missing     -> BLOCK (never seeded)
      2. status != 'ok'  -> BLOCK (the producer recorded unconfigured/failing archiving, or the born-blocked
                            state; NO re-flip/notify -> once-per-transition. A born row [status='blocked',
                            measured_at='epoch'] is caught HERE, BEFORE the staleness branch, so it never
                            spuriously alerts.)
      3. now()-measured_at > bound (IN SQL) -> guarded-CAS flip 'ok'->'blocked' + notify-if-transition + BLOCK
                            (the DEAD-PRODUCER close: a stopped A2-16 archiver-probe timer leaves a stale 'ok'
                            the gate would otherwise trust forever; the producer advances measured_at only on a
                            healthy read, so a frozen measured_at with status still 'ok' means the probe stopped)
      4. else            -> return (healthy: provisioned, status ok, within the bound).

    The staleness bound is the REPO CONSTANT wal_freshness.py::WAL_LAG_STALE_AFTER (never a DB column — the
    wal_lag row is bound-less, D2). The comparison runs IN SQL over a NOT-NULL measured_at (born 'epoch'), and
    the flip is pinned to the measured_at the gate READ so a concurrent healthy probe advancing measured_at is
    not clobbered — the same guarded-CAS/autonomous-txn/once-per-transition mechanism assert_backup_fresh uses
    (shared via _flip_and_notify). This arm carries no sentinel-drift branch (bound-less), only staleness."""
    bound = resolve_wal_lag_stale_after()  # repo-constant bound; ConfigurationError if the pin is absent
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status, measured_at, (now() - measured_at) > :bound AS is_stale "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"bound": bound, "k": WAL_LAG_KEY},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise DurabilityGateError(
            "durability BLOCK: system_health['wal_lag'] row is missing (never seeded). Seed it "
            "(`neuro db durability seed`, registrar/admin) at provisioning before a canonical write."
        )
    if row["status"] != "ok":
        raise DurabilityGateError(
            f"durability BLOCK: wal_lag.status is {row['status']!r} (not 'ok') — WAL archiving is unconfigured, "
            "failing, or the first archiver probe has not run (governance/wal_archiving.py)."
        )
    if row["is_stale"]:
        _flip_and_notify(
            engine,
            key=WAL_LAG_KEY,
            reason=(
                f"neuromancer durability: WAL-archiver signal STALE (age > pinned bound {bound}) — the archiver "
                "probe has stopped; BLOCKING canonical writes"
            ),
            detail=f"wal_lag stale: now() - measured_at > {bound}",
            guard_measured_at=row["measured_at"],
        )
        raise DurabilityGateError(
            f"durability BLOCK: wal_lag signal is STALE (now() - measured_at > the pinned bound {bound}) — the "
            "archiver probe has stopped (dead producer)."
        )
    return None  # healthy: provisioned, the last archiver probe classified ok, and the signal is fresh


def assert_durability_ok(engine: Engine) -> None:
    """The STABLE single entry point A2-11b's consult targets (ADR-0020): EVERY durability arm is consulted
    here — backup freshness (A2-11a) AND WAL archiving (GO-D-wal) — so one-arm-healthy can never read the
    other-arm-missing as healthy. Both must pass; either raises DurabilityGateError (fail closed).

    PRECONDITION: pass a WRITER-grade engine — a drift/stale transition flips system_health.status, an UPDATE
    only neuro_writer/neuro_admin hold (grants.sql:48); a reader/registrar engine would raise an untyped
    InsufficientPrivilege on the flip. A2-11b (the consult) chooses the engine and must treat ANY exception
    from this call as a BLOCK (fail closed)."""
    assert_backup_fresh(engine)
    assert_wal_archiving_ok(engine)
