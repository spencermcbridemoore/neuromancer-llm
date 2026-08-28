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
from .lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
from .lake_mirror import LakeMirrorDriver, run_lake_mirror_probe
from .probes import BackupDriver, run_backup_probe
from .repo3_freshness import REPO3_FRESHNESS_KEY
from .repo3_probe import Repo3Driver, make_repo3_recency_driver, run_repo3_probe
from .wal_archiving import run_wal_archiver_probe
from .wal_freshness import WAL_LAG_KEY

if TYPE_CHECKING:
    from sqlalchemy import Engine


@dataclass(frozen=True)
class ProbeContext:
    """What a probe run may need beyond the engine. `backup_driver`/`destination` are required by the
    backup probe only; `lake_mirror_driver`/`destination` by the lake-mirror probe only (the CLI builds the
    real drivers; tests inject scripted ones)."""

    backup_driver: BackupDriver | None = None
    destination: str | None = None
    actor_id: int | None = None
    lake_mirror_driver: LakeMirrorDriver | None = None
    # repo3 needs NOTHING from the operator (see _run_repo3): this field is the TEST seam only, and the
    # runner default-constructs the real driver when it is None.
    repo3_driver: Repo3Driver | None = None


def _run_backup(engine: Engine, ctx: ProbeContext) -> None:
    if ctx.backup_driver is None or not ctx.destination:
        raise ConfigurationError(
            "the backup_freshness probe requires a backup driver and a destination "
            "(NEURO_BACKUP_DEST / --dest) — refusing to run without both (fail closed)."
        )
    run_backup_probe(
        engine, backup_driver=ctx.backup_driver, destination=ctx.destination, actor_id=ctx.actor_id
    )


def _run_wal(engine: Engine, ctx: ProbeContext) -> None:
    run_wal_archiver_probe(engine, actor_id=ctx.actor_id)


def _run_lake_mirror(engine: Engine, ctx: ProbeContext) -> None:
    if ctx.lake_mirror_driver is None or not ctx.destination:
        raise ConfigurationError(
            "the lake_mirror_freshness probe requires a lake-mirror driver and a destination "
            "(NEURO_LAKE_MIRROR_DEST / --lake-dest) — refusing to run without both (fail closed)."
        )
    run_lake_mirror_probe(
        engine,
        lake_mirror_driver=ctx.lake_mirror_driver,
        destination=ctx.destination,
        actor_id=ctx.actor_id,
    )


def _run_repo3(engine: Engine, ctx: ProbeContext) -> None:
    """The repo3 recency read (§A·72). ⚠ UNLIKE THE OTHER TWO DRIVER-BEARING ARMS, THIS ONE
    DEFAULT-CONSTRUCTS ITS DRIVER, and that is honest rather than a shortcut: repo3 has NO operator-supplied
    coordinate at all. There is no destination (nothing leaves the VM), no ssh alias, no credential — every
    coordinate is already a committed default (the stanza of record, the pinned repo index, the real
    subprocess runner), so there is no flag whose absence an operator could be told to fix. That is exactly
    the shape `_run_wal` has, and it is why this arm is not destination-bearing: a fail-closed refusal here
    would name a flag that does not exist, which is the log:242 defect (a refusal that cannot be acted on).
    `ctx.repo3_driver` exists so tests can script the seam."""
    driver = ctx.repo3_driver if ctx.repo3_driver is not None else make_repo3_recency_driver()
    run_repo3_probe(engine, repo3_driver=driver, actor_id=ctx.actor_id)


# The registry (keyed by the SAME constants as DURABILITY_ROWS; the keyset test pins the equality).
PROBE_RUNNERS: dict[str, Callable[..., None]] = {
    BACKUP_FRESHNESS_KEY: _run_backup,
    WAL_LAG_KEY: _run_wal,
    LAKE_MIRROR_FRESHNESS_KEY: _run_lake_mirror,
    REPO3_FRESHNESS_KEY: _run_repo3,
}
