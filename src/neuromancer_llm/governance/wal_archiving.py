"""GO-D-wal (pre-cliff item 3): the WAL-archiving durability arm — the pg_stat_archiver PRODUCER + contract.

The second arm of the ADR-0020 durability gate (the first is backup AGE, freshness.py/probes.py/health.py).
`run_wal_archiver_probe` reads the cluster's WAL-archiving health and records it into the shared signal:
system_health['wal_lag'] (the machine snapshot health.assert_wal_archiving_ok reads) + a probe_reports audit
row. The row itself is provisioned by governance/durability.py (WAL_LAG_ROW — bound-less, born fail-closed);
the gate branch is the CONSUMER (health.py). notify() belongs to the gate side, not here — and the interim arm
deliberately has NO gate-side notify: until A2-16 schedules this producer, the arm's only effect is the
fail-closed born-'blocked' row (stated, not silent).

The interim policy (owner-ruled GO-inputs, 2026-07-11; NO numeric lag threshold — that calibration plus the
signal-staleness check that comes with it is a named deferred follow-on):
  (A) archiving must be CONFIGURED — `archive_mode != 'off'` ('on' and 'always' both count) AND a mechanism
      set (`archive_command` or `archive_library` non-empty). A fresh PG reports failed_count=0 with archiving
      fully off, so any failure-count rule alone reads "no archiving at all" as healthy — this check is the
      arm's strongest fail-closed guarantee.
  (B) the MOST RECENT archive attempt must not have failed — `last_failed_time` set AND (never archived OR
      `last_failed_time >= last_archived_time`) blocks; ties fail closed. SELF-HEALING (ratified deviation
      from the ruling's literal cumulative `failed_count > 0`): a later successful segment flips the next
      probe back to ok, so recovery never requires pg_stat_reset_shared('archiver') — a reset that would
      erase the very failure history the monitor keys on. failed_count is still recorded verbatim in `detail`
      for audit.

⚠ The "(disabled)" show-hook trap: when archiving is INACTIVE, PostgreSQL substitutes the literal NON-empty
string "(disabled)" for `archive_command` on every display path (SHOW / current_setting / pg_settings —
`show_archive_command`, xlog.c; verified REL_18_STABLE). So `archiving_enabled` is computed from
`archive_mode` FIRST, and the literal "(disabled)" is treated as NO mechanism (belt) — a mechanism-only check
would read an archiving-off cluster as configured (fail-open). None of the three GUCs is superuser-only in
PG18 (guc_tables.c — no GUC_SUPERUSER_ONLY flag), so the producer needs no pg_read_all_settings.

Named deferred obligations (Deferred-Obligation Register; recorded 2026-07-11): the queue-drop/WAL-continuity
gap (`archive-push-queue-max` overflow drops WAL and reports SUCCESS — invisible to any archiver-error rule;
needs an archived-segment-continuity check; revisit before capture at volume) and the signal-staleness /
dead-producer gap (a stopped A2-16 timer leaves a stale 'ok' the interim gate trusts; owned by the deferred
numeric-threshold follow-on, which adds the gate-side flip+notify).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from .probes import _record_probe_report

if TYPE_CHECKING:
    from sqlalchemy import Engine

# The shared health_key (one constant imported by the durability registry, the producer here, and the A2-11a
# gate branch — the BACKUP_FRESHNESS_KEY idiom). NOTE a deviation from the backup arm's layout (GO-D-wal
# fold 7): the key lives in this producer module, not a neutral contract module — a bound-less arm has no
# pin/resolver to house separately. When the numeric-threshold repo-constant lands (the deferred follow-on),
# carve its freshness.py-style contract home and move the key there.
WAL_LAG_KEY = "wal_lag"

# The literal string PostgreSQL's show_archive_command hook substitutes for archive_command on EVERY display
# path while archiving is inactive (xlog.c, REL_18_STABLE) — non-empty, so never a configured mechanism.
_DISABLED_SENTINEL = "(disabled)"


class WalArchiverProbeError(RuntimeError):
    """A WAL-archiver probe found archiving unconfigured/failing, could not read the archiver stats, or could
    not be recorded — fail LOUD (never a silent 'archiver ok'). Raised AFTER the failure is persisted to
    system_health + probe_reports where a row exists to record it (persist-before-raise)."""


@dataclass(frozen=True)
class ArchiverSnapshot:
    """One raw read of the cluster's archiving state (GUCs + pg_stat_archiver). RAW facts only — the policy
    verdict lives in classify_archiver (one policy home), never in the reader."""

    archive_mode: str
    archive_command: str
    archive_library: str
    failed_count: int
    last_failed_time: _dt.datetime | None
    last_archived_time: _dt.datetime | None
    last_archived_wal: str | None


# The injectable stats seam (the BackupDriver idiom): (engine) -> ArchiverSnapshot. The default is the real
# SQL read; tests script snapshots, and the healthy test fixtures MUST use a synthetic reader (the test PG
# runs archiving-off, so the real reader can only ever classify blocked there).
ArchiverStatsReader = Callable[["Engine"], ArchiverSnapshot]


def read_pg_stat_archiver(engine: Engine) -> ArchiverSnapshot:
    """The REAL reader: the three archiving GUCs + the pg_stat_archiver stats, one transaction. All readable
    without special privileges (verified PG18: no GUC_SUPERUSER_ONLY on any of the three)."""
    with engine.begin() as conn:
        mode = conn.execute(text("SHOW archive_mode")).scalar_one()
        command = conn.execute(text("SHOW archive_command")).scalar_one()
        library = conn.execute(text("SHOW archive_library")).scalar_one()
        stats = (
            conn.execute(
                text(
                    "SELECT failed_count, last_failed_time, last_archived_time, last_archived_wal "
                    "FROM pg_stat_archiver"
                )
            )
            .mappings()
            .one()
        )
    return ArchiverSnapshot(
        archive_mode=str(mode),
        archive_command=str(command),
        archive_library=str(library),
        failed_count=int(stats["failed_count"]),
        last_failed_time=stats["last_failed_time"],
        last_archived_time=stats["last_archived_time"],
        last_archived_wal=stats["last_archived_wal"],
    )


def classify_archiver(snap: ArchiverSnapshot) -> tuple[bool, str]:
    """The interim policy core (pure; the module docstring's (A)+(B), owner-ruled 2026-07-11). Returns
    (ok, detail); the detail carries the raw stats verbatim so the cumulative failure history stays auditable
    even under the self-healing rule."""
    stats = (
        f"archive_mode={snap.archive_mode}; failed_count={snap.failed_count}; "
        f"last_failed_time={snap.last_failed_time}; last_archived_time={snap.last_archived_time}; "
        f"last_archived_wal={snap.last_archived_wal}"
    )
    command = "" if snap.archive_command == _DISABLED_SENTINEL else snap.archive_command
    if snap.archive_mode == "off" or not (command.strip() or snap.archive_library.strip()):
        return (False, f"WAL archiving not configured (fail closed): {stats}")
    if snap.last_failed_time is not None and (
        snap.last_archived_time is None or snap.last_failed_time >= snap.last_archived_time
    ):
        return (False, f"WAL archiver CURRENT failure (most recent attempt failed): {stats}")
    return (True, f"WAL archiver healthy: {stats}")


def _wal_lag_present(engine: Engine) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM neuro.system_health WHERE health_key = :k"), {"k": WAL_LAG_KEY}
            ).first()
            is not None
        )


def _bump_wal_lag(engine: Engine, *, ok: bool, detail: str) -> None:
    """Bump the signal as neuro_writer (grants.sql:48 UPDATE(status,detail,measured_at); never stale_after —
    the bound-less row's sentinel stays NULL for life). On a healthy read advance measured_at=now() (feedstock
    for the deferred signal-staleness check); on a blocked read leave it. Raises on rowcount 0 — a bump on an
    unseeded/vanished row is fail-loud, never a silent no-op (the probes._bump_freshness doctrine)."""
    if ok:
        sql = "UPDATE neuro.system_health SET status='ok', measured_at=now(), detail=:d WHERE health_key=:k"
    else:
        sql = "UPDATE neuro.system_health SET status='blocked', detail=:d WHERE health_key=:k"
    with engine.begin() as conn:
        result = conn.execute(text(sql), {"d": detail, "k": WAL_LAG_KEY})
    if result.rowcount == 0:
        raise WalArchiverProbeError(
            "wal_lag row absent at bump time (rowcount 0) — refusing a silent no-op. Seed it "
            "(`neuro db durability seed`, registrar/admin) before running the probe."
        )


def run_wal_archiver_probe(
    engine: Engine,
    *,
    stats_reader: ArchiverStatsReader = read_pg_stat_archiver,
    actor_id: int | None = None,
) -> ArchiverSnapshot:
    """Read the archiver's health once and record it into the wal_lag signal. `engine` must be a VERIFIED
    engine (repo.engine — the identity guard ran at construction). Order (the run_backup_probe mirror):

      1. the wal_lag row MUST already be seeded — abort loudly BEFORE the stats read, so a probe result is
         never orphaned with nowhere to record it (the writer cannot self-seed).
      2. read the stats — WRAPPED: a raising reader persists status='blocked' + a probe_reports row THEN
         re-raises (§10 MUST-FIX 3), so a read failure can never leave a prior 'ok' standing while archiver
         health is unknown.
      3. classify (the interim policy) and record the verdict (bump + probe_reports), THEN raise on a blocked
         classification (persist-before-raise, fail-loud).

    Returns the ArchiverSnapshot on a healthy read; raises WalArchiverProbeError otherwise.
    """
    if not _wal_lag_present(engine):
        raise WalArchiverProbeError(
            "system_health['wal_lag'] is not seeded — run `neuro db durability seed` (registrar/admin) at "
            "provisioning before the archiver probe. Refusing to run a probe that cannot be recorded."
        )
    try:
        snap = stats_reader(engine)
    except Exception as exc:  # noqa: BLE001 — record the failure, then re-raise loud (never a stale 'ok')
        _bump_wal_lag(engine, ok=False, detail=f"archiver read raised: {exc!r}")
        _record_probe_report(
            engine,
            probe_key=WAL_LAG_KEY,
            status="blocked",
            report_text=f"archiver read raised: {exc!r}",
            actor_id=actor_id,
        )
        raise WalArchiverProbeError("WAL-archiver probe failed: the archiver stats read raised") from exc

    ok, detail = classify_archiver(snap)
    _bump_wal_lag(engine, ok=ok, detail=detail)
    _record_probe_report(
        engine,
        probe_key=WAL_LAG_KEY,
        status="ok" if ok else "blocked",
        report_text=detail,
        actor_id=actor_id,
    )
    if not ok:
        raise WalArchiverProbeError(f"WAL-archiver probe recorded BLOCKED: {detail}")
    return snap
