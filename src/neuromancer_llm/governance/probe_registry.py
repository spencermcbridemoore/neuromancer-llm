"""GO-D-timer (A2-16): the probe-runner registry — the ONE key->producer mapping the timers drive.

§8 fold 7: the `neuro probe run --key` dispatch must live beside the durability registry, keyed by the SAME
constants, with a keyset test `frozenset(PROBE_RUNNERS) == DURABILITY_KEYS` — never an ad-hoc dict in the
CLI. A future durability arm is then a one-surface append here too (DurabilityRow + gate branch + runner),
and can never ship seeded-but-unrunnable (born-blocked forever with the timer unable to drive it, and
nothing red).

Runners run WRITER-grade (positively verified: grants.sql:30 probe_reports INSERT + :48 system_health
UPDATE(status,detail,measured_at) — every surface both probes touch). The backup runner needs the injected
driver + destination (the CLI builds them; `ProbeContext` carries them); the WAL runner needs neither.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db.lanes import ConfigurationError
from .freshness import BACKUP_FRESHNESS_KEY
from .probes import BackupDriver, run_backup_probe
from .wal_archiving import WAL_LAG_KEY, run_wal_archiver_probe

if TYPE_CHECKING:
    from sqlalchemy import Engine


@dataclass(frozen=True)
class ProbeContext:
    """What a probe run may need beyond the engine. `backup_driver`/`destination` are required by the
    backup probe only (the CLI builds the real driver; tests inject scripted ones)."""

    backup_driver: BackupDriver | None = None
    destination: str | None = None
    actor_id: int | None = None


def _run_backup(engine: Engine, ctx: ProbeContext) -> None:
    if ctx.backup_driver is None or not ctx.destination:
        raise ConfigurationError(
            "the backup_freshness probe requires a backup driver and a destination "
            "(NEURO_BACKUP_DEST / --dest) — refusing a driverless run (fail closed)."
        )
    run_backup_probe(
        engine, backup_driver=ctx.backup_driver, destination=ctx.destination, actor_id=ctx.actor_id
    )


def _run_wal(engine: Engine, ctx: ProbeContext) -> None:
    run_wal_archiver_probe(engine, actor_id=ctx.actor_id)


# The registry (keyed by the SAME constants as DURABILITY_ROWS; the keyset test pins the equality).
PROBE_RUNNERS: dict[str, Callable[..., None]] = {
    BACKUP_FRESHNESS_KEY: _run_backup,
    WAL_LAG_KEY: _run_wal,
}
