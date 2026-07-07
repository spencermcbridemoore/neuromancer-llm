"""GO-D-backup (A2-10): off-cloud (desktop-NVMe) DB-backup automation + the backup-freshness signal.

This is the PRODUCER half of the freshness contract (governance/freshness.py). `run_backup_probe` runs a
verified backup push to an independent, NON-Azure destination and records the outcome into the shared signal:
system_health['backup_freshness'] (the machine snapshot A2-11a reads) + a probe_reports audit row (the human
log). A2-11a is the CONSUMER (the fail-closed staleness read + flip + notify) — notify() belongs to the gate,
not here.

Off-cloud independence (ADR-0014, plan section 1.3 #8): the copy MUST land on a destination NOT controlled by
the Azure connection string, so an Azure compromise/deletion cannot destroy the lake AND its only backup at
once. `assert_offcloud_destination` enforces it with a belt-and-suspenders guard (restore.py::same_target
doctrine): deny Azure-shaped targets AND positively require a local filesystem path, so an unrecognized Azure
alias (raw IP / private-endpoint FQDN / CNAME) fails closed by not matching the allow-shape.

Synthetic-first (ruled): the backup driver (the real pgbackrest -> desktop-NVMe push + verify) is INJECTED;
this builds + tests the automation LOGIC. The CLI/systemd-timer wiring with the concrete driver and a
NEURO_BACKUP_DEST env is the A2-16 forward-reference; the real desktop-NVMe restore is the deferred A2-9-real
step (gated on A2-7 + A2-10).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from ..db.lanes import ConfigurationError
from .freshness import BACKUP_FRESHNESS_KEY

if TYPE_CHECKING:
    from sqlalchemy import Engine


class BackupProbeError(RuntimeError):
    """A backup probe failed or could not be recorded — fail LOUD (never a silent 'backup ok'). Raised after
    the failure is persisted to system_health + probe_reports (persist-before-raise)."""


@dataclass(frozen=True)
class BackupOutcome:
    """The injected driver's verdict: `ok` iff the push AND its integrity verify both succeeded; `detail` is
    the human summary (backup label / size / failure reason) recorded into detail + the probe report."""

    ok: bool
    detail: str


# A synthetic-first seam: (destination) -> BackupOutcome. The real pgbackrest->desktop-NVMe driver is A2-16.
BackupDriver = Callable[[str], BackupOutcome]


# --- off-cloud destination guard (FOLD-5: deny Azure shapes AND require a local-path allow-shape) ---------
_CLOUD_SCHEMES = frozenset({"az", "azure", "abfs", "abfss", "wasb", "wasbs", "http", "https", "s3", "gs"})
# The SHARED Azure Storage endpoint suffix — covers blob / file / dfs / queue / table. `blob` alone fails
# OPEN on a non-blob endpoint: Azure Files `\\acct.file.core.windows.net\share` is a directly mountable UNC
# path that satisfies the local-path allow-shape, so the marker must match the whole storage suffix.
# Residuals (a target-string guard cannot resolve these — `--confirm`/operator config is the backstop, per
# db/restore.py:52's DNS/socket residual): sovereign-cloud suffixes (core.usgovcloudapi.net /
# core.chinacloudapi.cn) and a raw-IP UNC (\\10.0.0.9\share) aliasing a storage endpoint.
_AZURE_HOST_MARKER = "core.windows.net"
# A URL scheme is `<alpha>[alnum+.-]*://` — the `://` requirement means a Windows drive path (`D:/…`, `D:\…`,
# single slash / backslash) is NOT read as a scheme, so the drive-letter trap can't misfire.
_URL_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://")
_WIN_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")


def _url_scheme(dest: str) -> str | None:
    m = _URL_SCHEME.match(dest)
    return m.group(1).lower() if m else None


def _looks_like_local_path(dest: str) -> bool:
    """A recognizable LOCAL filesystem path: absolute POSIX (`/…`), a Windows drive path (`D:\\…` / `D:/…`),
    or a UNC share (`\\\\host\\share`). A bare/relative token is deliberately NOT accepted (ambiguous ->
    fail closed)."""
    return dest.startswith("/") or dest.startswith("\\\\") or bool(_WIN_DRIVE.match(dest))


def _azure_account_from_env() -> str | None:
    """The AccountName from AZURE_STORAGE_CONNECTION_STRING when it is set (else None) — so the guard can
    refuse a destination naming the very account the app writes to. Read here, not behavior-by-flag."""
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        return None
    for part in conn.split(";"):
        if part.strip().lower().startswith("accountname="):
            return part.split("=", 1)[1].strip() or None
    return None


def assert_offcloud_destination(dest: str) -> None:
    """Refuse (ConfigurationError, fail closed) any backup destination that is NOT an independent off-cloud
    path (ADR-0014). Belt-and-suspenders: deny Azure-shaped targets (cloud URL scheme, the blob host marker,
    or the AZURE_STORAGE_CONNECTION_STRING account name) AND positively require a local filesystem path — an
    Azure alias that slips the deny checks still fails the allow-shape."""
    d = dest.strip()
    if not d:
        raise ConfigurationError(
            "backup destination is empty — refusing (fail closed; the off-cloud copy is a desktop-NVMe path)."
        )
    scheme = _url_scheme(d)
    if scheme is not None and scheme in _CLOUD_SCHEMES:
        raise ConfigurationError(
            f"backup destination {dest!r} uses cloud scheme {scheme!r} — the independent copy MUST be "
            "off-cloud (ADR-0014); an Azure compromise cannot be allowed to destroy the lake AND its only "
            "backup at once. Point it at the desktop-NVMe path."
        )
    low = d.lower()
    if _AZURE_HOST_MARKER in low:
        raise ConfigurationError(
            f"backup destination {dest!r} names an Azure Storage endpoint ({_AZURE_HOST_MARKER}) — the "
            "off-cloud copy must not be Azure-controlled (ADR-0014)."
        )
    account = _azure_account_from_env()
    if account and account.lower() in low:
        raise ConfigurationError(
            f"backup destination {dest!r} names the Azure storage account {account!r} from "
            "AZURE_STORAGE_CONNECTION_STRING — the off-cloud copy must be independent of the Azure "
            "credential (ADR-0014)."
        )
    if not _looks_like_local_path(d):
        raise ConfigurationError(
            f"backup destination {dest!r} is not a recognizable local filesystem path (an absolute POSIX "
            "path, a Windows drive path, or a UNC share) — refusing (fail closed)."
        )


# --- the freshness-signal writes (writer role; execute against the VERIFIED engine) ----------------------
def _freshness_present(engine: Engine) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM neuro.system_health WHERE health_key = :k"),
                {"k": BACKUP_FRESHNESS_KEY},
            ).first()
            is not None
        )


def _bump_freshness(engine: Engine, *, ok: bool, detail: str) -> None:
    """Bump the signal as neuro_writer (grants.sql:48 UPDATE(status,detail,measured_at); no stale_after). On
    success advance measured_at=now() (the freshness proof); on failure leave measured_at (staleness accrues
    from the last-good backup). Raises on rowcount 0 — a bump on an unseeded/vanished row is fail-loud, never
    a silent no-op."""
    if ok:
        sql = "UPDATE neuro.system_health SET status='ok', measured_at=now(), detail=:d WHERE health_key=:k"
    else:
        sql = "UPDATE neuro.system_health SET status='blocked', detail=:d WHERE health_key=:k"
    with engine.begin() as conn:
        result = conn.execute(text(sql), {"d": detail, "k": BACKUP_FRESHNESS_KEY})
    if result.rowcount == 0:
        raise BackupProbeError(
            "backup_freshness row absent at bump time (rowcount 0) — refusing a silent no-op. Seed it "
            "(registrar/admin) before running the probe."
        )


def _record_probe_report(engine: Engine, *, status: str, report_text: str, actor_id: int | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.probe_reports (probe_key, actor_id, status, report_text) "
                "VALUES (:pk, :a, :s, :rt)"
            ),
            {"pk": BACKUP_FRESHNESS_KEY, "a": actor_id, "s": status, "rt": report_text},
        )


def run_backup_probe(
    engine: Engine,
    *,
    backup_driver: BackupDriver,
    destination: str,
    actor_id: int | None = None,
) -> BackupOutcome:
    """Run + verify one off-cloud backup and record it into the freshness signal. `engine` must be a VERIFIED
    engine (repo.engine — the identity guard ran at construction). Order:

      1. `assert_offcloud_destination` — fail closed BEFORE the driver runs (FOLD-5).
      2. the freshness row MUST already be seeded — abort loudly BEFORE touching the driver (FOLD-4), so a
         real backup is never orphaned with nowhere to record it (the writer cannot self-seed).
      3. run the injected push+verify.
      4. record the outcome (bump + probe_reports), THEN raise on failure (persist-before-raise, fail-loud).

    Returns the BackupOutcome on success; raises BackupProbeError on a failed/raising backup (after recording)
    or ConfigurationError on a bad destination.
    """
    assert_offcloud_destination(destination)
    if not _freshness_present(engine):
        raise BackupProbeError(
            "system_health['backup_freshness'] is not seeded — run seed_backup_freshness (registrar/admin) "
            "at provisioning before the A2-10 probe. Refusing to run a backup that cannot be recorded."
        )
    try:
        outcome = backup_driver(destination)
    except Exception as exc:  # noqa: BLE001 — record the failure, then re-raise loud (never a silent pass)
        _bump_freshness(engine, ok=False, detail=f"backup driver raised: {exc!r}")
        _record_probe_report(
            engine, status="blocked", report_text=f"{destination}: driver raised: {exc!r}", actor_id=actor_id
        )
        raise BackupProbeError(f"backup probe failed: driver raised for {destination!r}") from exc

    status = "ok" if outcome.ok else "blocked"
    # The bump (machine signal) and the probe_reports row (human audit) are two txns. A crash BETWEEN them
    # leaves a bumped-but-unaudited signal — a fail-CLOSED robustness gap, not a gate fail-open: the gate
    # reads measured_at, which only advances on a genuine success, so the missing row costs only an audit
    # entry, never a stale-passes-fresh. Single-txn atomicity is DEFERRED to A2-11a integration (where the
    # consume side threads one connection through the read+write).
    _bump_freshness(engine, ok=outcome.ok, detail=outcome.detail)
    _record_probe_report(
        engine, status=status, report_text=f"{destination}: {outcome.detail}", actor_id=actor_id
    )
    if not outcome.ok:
        raise BackupProbeError(
            f"backup probe recorded a FAILED/unverified backup to {destination!r}: {outcome.detail}"
        )
    return outcome
