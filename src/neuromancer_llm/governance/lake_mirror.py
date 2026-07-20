"""B-7 blob-lake mirror (2026-07-20): the catalog-driven Azure->desktop-NVMe mirror driver + its freshness
PRODUCER. The forward-looking HARD GATE before the first NON-recomputable capture (Stage-A local captures are
recomputable, ADR-0034; ephemeral-substrate Stage-B captures are not) — built now so it never blocks a real
experiment.

Shape: the driver mirrors backup_driver.py (chroot-scoped sftp BATCH push over the SHARED transport
governance/sftp_transport.py — one transport, not a second); the producer mirrors wal_archiving.py's second
durability arm (its OWN `_lake_mirror_present` + `_bump_lake_mirror`, the shared `_record_probe_report`, the
seed-check -> run -> persist-before-raise order), leaving probes.py untouched.

★ NOTIFY-ONLY (owner fork ruling, 2026-07-20): the freshness signal is a system_health DATA row that is NOT in
health.GATE_CONSULTED_KEYS — a stale lake mirror ALARMS (the daily lake escalation + the per-run OnFailure
ping) but NEVER blocks a canonical write (assert_durability_ok is unchanged). See lake_freshness.py.

Verify (ADR-0012): every cloud-bound shard is sha256-hashed AT REGISTRATION, so artifacts.sha256 is the
authoritative registered hash. The per-run verify fetches each PUSHED blob BACK over sftp and compares its
sha256+size to that registered hash — a torn/truncated transfer fails the mirror CLOSED. The exhaustive
re-verify of the WHOLE standing mirror against bit-rot (crawl.py::reconcile_artifacts pointed at the mirror
copy) is the ADR-0014 monthly-audit cadence, registered as a follow-on (the bit-rot window is not live until
non-recomputable data exists, Stage B).

Off-cloud independence (ADR-0014): the destination is the desktop-NVMe path, guarded by
assert_offcloud_destination (deny Azure shapes AND require a local-path shape) BEFORE the driver runs — an
Azure compromise must not be able to destroy the lake AND its only independent copy at once. EXAM_RESTRICTED
posture is inherited from the db-backups mirror (same chroot-scoped desktop under ADR-0007 control).

Coexistence with the db-backups mirror (both push to the desktop): the lake writes a DISTINCT manifest
(`.neuro-lake-manifest.json`) under a DISTINCT subdir (`lake/`), so neither arm's manifest-diff can ever see —
and therefore never `-rm` — the other's files, even in a shared chroot (the incident-hardened DB-backup mirror
must never be clobbered, log:214).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from sqlalchemy import text

from ..db.identity import sha256_bytes
from ..db.lanes import ConfigurationError
from .lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
from .probes import _record_probe_report, assert_offcloud_destination
from .sftp_transport import CommandRunner, run_sftp_batch, run_subprocess

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from ..storage.backends import StorageBackend

# Pinned constants (committed, not knobs — a change is an auditable commit; the backup_driver *_TIMEOUT_S idiom).
# The Azure per-read socket bound: an unbounded readall() on a network partition would hang the Type=oneshot
# unit forever with ZERO alerts (the unit never reaches 'failed', so OnFailure never fires) — a failure mode the
# backup arm structurally cannot have. The mirror source backend is built WITH this read_timeout (via
# make_backend), so a hang becomes a fail-closed ok=False -> status 'blocked' -> the daily escalation fires.
# The systemd TimeoutStartSec on neuro-lake-mirror.service is the outer belt above this suspender.
LAKE_MIRROR_AZURE_READ_TIMEOUT_S = 300.0
LAKE_SFTP_TIMEOUT_S = 3600.0

# A DISTINCT manifest name + subdir from the backup mirror's `.neuro-mirror-manifest.json` at the chroot root —
# the coexistence-safety invariant: neither arm's diff can see the other's files (see the module docstring).
LAKE_MANIFEST_NAME = ".neuro-lake-manifest.json"
LAKE_SUBDIR = "lake"


class LakeMirrorProbeError(RuntimeError):
    """A lake-mirror probe failed or could not be recorded — fail LOUD (never a silent 'mirror ok'). Raised
    after the failure is persisted to system_health + probe_reports (persist-before-raise)."""


@dataclass(frozen=True)
class LakeMirrorOutcome:
    """The driver's verdict. `ok` iff the push AND its full delta-verify AND the prune AND the manifest write
    all succeeded (or the lane is registered-but-empty). `checked` = live prod-lane artifacts examined;
    `pushed`/`deleted` = mirror mutations this run; `verified` = pushed blobs fetched-back-and-hash-checked
    against the ADR-0012 registered sha256. `detail` is the human summary recorded into the signal + report."""

    ok: bool
    detail: str
    checked: int
    pushed: int
    deleted: int
    verified: int


# The source-backend seam (driver, base_uri) -> a read backend for the lane, or None (the reconcile None-seam
# lesson: a seam that returns None must not crash the mirror — it fails the mirror CLOSED, not the process).
# The CLI builds it via registry.backends.make_backend so AzureBlobBackend is never constructed here (the C5
# factory pin), and passes the LAKE_MIRROR_AZURE_READ_TIMEOUT_S so the Azure read is bounded.
LakeSourceBackendFor = Callable[[str, str], "StorageBackend | None"]

# (engine, destination) -> outcome. The engine is used read-only (the catalog SELECT); the destination is the
# off-cloud-guarded desktop path (cosmetic in the sftp scheme — the remote paths are chroot-relative under
# LAKE_SUBDIR — but recorded for the audit + guarded by assert_offcloud_destination in the producer).
LakeMirrorDriver = Callable[["Engine", str], LakeMirrorOutcome]


_LAKE_BACKEND_SQL = (
    "SELECT backend_id, backend_key, driver, base_uri FROM neuro.storage_backends WHERE backend_key = :k"
)
# LIVE (non-deleted) artifacts on the mirrored lane only — a soft-deleted artifact's blob is intentionally gone
# (deletion RECORDED, phase0 Q6). backend_key=:lane is the SOLE enforcer of the artifacts-prod-ONLY scope
# ruling — mutation-verified + de-blinded by a cross-lane fixture.
_LIVE_LAKE_ARTIFACTS_SQL = (
    "SELECT a.artifact_id, a.uri, a.sha256, a.size_bytes "
    "FROM neuro.artifacts a JOIN neuro.storage_backends sb ON sb.backend_id = a.backend_id "
    "WHERE sb.backend_key = :k AND a.deleted_at IS NULL "
    "ORDER BY a.artifact_id"
)


def make_lake_mirror_driver(
    *,
    source_backend_for: LakeSourceBackendFor,
    ssh_alias: str,
    lane_backend_key: str = "artifacts-prod",
    subdir: str = LAKE_SUBDIR,
    manifest_name: str = LAKE_MANIFEST_NAME,
    runner: CommandRunner = run_subprocess,
) -> LakeMirrorDriver:
    """Build the lake-mirror driver. `source_backend_for` builds the lane's read backend (the None-seam);
    `ssh_alias` is the ssh_config Host alias carrying the desktop host/user/key (the credential never rides
    argv); `lane_backend_key` selects the mirrored lane (default the prod lake); `runner` is the injectable
    seam (tests script it)."""
    if not ssh_alias or any(c.isspace() for c in ssh_alias):
        raise ConfigurationError(f"ssh alias {ssh_alias!r} is empty/whitespace-bearing (fail closed).")

    def driver(engine: Engine, destination: str) -> LakeMirrorOutcome:
        with engine.connect() as conn:
            backend_row = (
                conn.execute(text(_LAKE_BACKEND_SQL), {"k": lane_backend_key}).mappings().one_or_none()
            )
        if backend_row is None:
            # A typo'd / unregistered lane fails CLOSED (distinguished from a registered-but-empty lane) — the
            # backup-arm "no repositories" fail-closed analog; else a wrong key greens the signal on zero rows.
            raise ConfigurationError(
                f"lake mirror: no storage_backends row for lane_backend_key {lane_backend_key!r} "
                "(unregistered/typo) — refusing to certify a mirror of a lane that does not exist (fail closed)."
            )
        with engine.connect() as conn:
            rows = conn.execute(text(_LIVE_LAKE_ARTIFACTS_SQL), {"k": lane_backend_key}).mappings().all()
        checked = len(rows)
        catalog = {str(r["uri"]): (bytes(r["sha256"]), int(r["size_bytes"])) for r in rows}
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)

            def sftp(lines: list[str]):
                return run_sftp_batch(runner, ssh_alias, lines, tmp_dir=tmp, timeout_s=LAKE_SFTP_TIMEOUT_S)

            manifest_rel = f"{subdir}/{manifest_name}"
            # 1. fetch the remote manifest (uri -> sha256 hex). A failed fetch = first run -> EMPTY manifest
            #    (over-push is the safe direction; the delete set derives from the REMOTE manifest so empty
            #    deletes nothing).
            remote_manifest_local = tmp / "remote-lake-manifest.json"
            got = sftp([f"get {manifest_rel} {remote_manifest_local.as_posix()}"])
            remote: dict[str, str] = {}
            if got.returncode == 0 and remote_manifest_local.exists():
                try:
                    parsed = json.loads(remote_manifest_local.read_text(encoding="utf-8"))
                    # isinstance guard: a valid-but-NON-dict manifest (a JSON array / scalar / null via
                    # corruption or tamper) would raise AttributeError on .items() and defeat self-heal — so
                    # a non-dict rebuilds by full push exactly like malformed JSON does.
                    if isinstance(parsed, dict):
                        remote = {str(k): str(v) for k, v in parsed.items()}
                except (ValueError, TypeError):
                    remote = {}  # unreadable manifest -> rebuild by full push (over-push, never over-delete)

            # 2. diff: content-addressed immutable blobs -> to_push = uris whose remote sha != the catalog sha
            #    (a new blob, or a torn remote at a known uri -> re-pushed); to_delete = remote uris absent from
            #    the live catalog (soft-deleted / pruned). A REGISTERED-but-empty lane (checked==0) flows
            #    through here normally: nothing to push, any orphans pruned, an (empty) manifest written -> it
            #    greens with checked=0 recorded. (The typo'd/unregistered lane already failed CLOSED above; an
            #    EMPTIED lane — every artifact soft-deleted — must still have its orphaned blobs pruned, which a
            #    checked==0 early-return would skip.)
            to_push = sorted(u for u, (sha, _sz) in catalog.items() if remote.get(u) != sha.hex())
            to_delete = sorted(u for u in remote if u not in catalog)

            # 3. push new/changed. The source backend is built ONLY when there is something to READ — a
            #    prune-only or no-op run touches no source (and never triggers AzureBlobBackend's
            #    create-container-on-construct). None-seam: a source_backend_for that returns None OR raises
            #    fails the mirror CLOSED, never crashes the probe (the reconcile lesson).
            pushed = 0
            if to_push:
                try:
                    source = source_backend_for(str(backend_row["driver"]), str(backend_row["base_uri"]))
                except Exception as exc:  # noqa: BLE001 — collect into a fail-closed outcome, never crash
                    return LakeMirrorOutcome(
                        ok=False,
                        detail=f"source backend unresolved ({type(exc).__name__}): {exc}",
                        checked=checked,
                        pushed=0,
                        deleted=0,
                        verified=0,
                    )
                if source is None:
                    return LakeMirrorOutcome(
                        ok=False,
                        detail="source_backend_for returned None (source unresolved) — fail closed",
                        checked=checked,
                        pushed=0,
                        deleted=0,
                        verified=0,
                    )
                for uri in to_push:
                    try:
                        blob = source.get(uri)
                    except Exception as exc:  # noqa: BLE001 — a source read failure fails the mirror CLOSED
                        return LakeMirrorOutcome(
                            ok=False,
                            detail=f"source get {uri!r} failed ({type(exc).__name__}): {exc}",
                            checked=checked,
                            pushed=pushed,
                            deleted=0,
                            verified=0,
                        )
                    staged = tmp / "stage"
                    staged.write_bytes(blob)
                    remote_path = f"{subdir}/{uri}"
                    parts = PurePosixPath(remote_path).parent.parts
                    lines = [f"-mkdir {'/'.join(parts[:i])}" for i in range(1, len(parts) + 1)]
                    lines.append(f"put {staged.as_posix()} {remote_path}")
                    put = sftp(lines)
                    if put.returncode != 0:
                        return LakeMirrorOutcome(
                            ok=False,
                            detail=f"sftp put {uri!r} failed (rc={put.returncode}): {put.stderr.strip()[:200]}",
                            checked=checked,
                            pushed=pushed,
                            deleted=0,
                            verified=0,
                        )
                    pushed += 1

            # 4. FULL delta-verify: fetch each pushed blob BACK and assert sha256+size vs the REGISTERED
            #    catalog hash (ADR-0012). Any mismatch -> ok=False BEFORE the manifest write (a torn transfer
            #    must never be manifest-recorded, else the next run empty-delta-bumps FRESH over a corrupt copy).
            verified = 0
            for i, uri in enumerate(to_push):
                want_sha, want_size = catalog[uri]
                back = tmp / f"verify-{i}"  # unique per iteration (the backup spot-{i} idiom) — a reused name
                # could read STALE bytes from a prior fetch if a get failed without writing; unique + the
                # rc!=0 short-circuit below both close that edge.
                fetched = sftp([f"get {subdir}/{uri} {back.as_posix()}"])
                if fetched.returncode != 0 or not back.exists():
                    return LakeMirrorOutcome(
                        ok=False,
                        detail=f"delta-verify fetch of {uri!r} failed (fail closed)",
                        checked=checked,
                        pushed=pushed,
                        deleted=0,
                        verified=verified,
                    )
                blob = back.read_bytes()
                if len(blob) != want_size or sha256_bytes(blob) != want_sha:
                    return LakeMirrorOutcome(
                        ok=False,
                        detail=f"delta-verify sha256/size MISMATCH on {uri!r} — torn transfer (fail closed)",
                        checked=checked,
                        pushed=pushed,
                        deleted=0,
                        verified=verified,
                    )
                verified += 1

            # 5. prune soft-deleted/expired (only under subdir/; a doubly-pruned file is not an error).
            deleted = 0
            if to_delete:
                pruned = sftp([f"-rm {subdir}/{u}" for u in to_delete])
                if pruned.returncode != 0:
                    return LakeMirrorOutcome(
                        ok=False,
                        detail=f"sftp prune failed (rc={pruned.returncode}): {pruned.stderr.strip()[:200]}",
                        checked=checked,
                        pushed=pushed,
                        deleted=0,
                        verified=verified,
                    )
                deleted = len(to_delete)

            # 6. write the manifest LAST, ONLY on full success (push + FULL delta-verify + prune all ok). A
            #    crash before this leaves extra remote files (re-pushed next run — over-push, never a manifest
            #    that claims files the mirror lacks).
            manifest = {u: sha.hex() for u, (sha, _sz) in catalog.items()}
            manifest_local = tmp / "local-lake-manifest.json"
            manifest_local.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            wrote = sftp([f"-mkdir {subdir}", f"put {manifest_local.as_posix()} {manifest_rel}"])
            if wrote.returncode != 0:
                return LakeMirrorOutcome(
                    ok=False,
                    detail=f"manifest write failed (rc={wrote.returncode}) — mirror not certified (fail closed)",
                    checked=checked,
                    pushed=pushed,
                    deleted=deleted,
                    verified=verified,
                )

        return LakeMirrorOutcome(
            ok=True,
            detail=(
                f"lane {lane_backend_key!r}: {checked} live artifacts; mirrored +{pushed}/-{deleted} to "
                f"{destination}/{subdir} (alias {ssh_alias}); {verified} delta-verified vs registered sha256 "
                "(ADR-0012); standing-mirror bit-rot re-verify = the ADR-0014 monthly audit (deferred)"
            ),
            checked=checked,
            pushed=pushed,
            deleted=deleted,
            verified=verified,
        )

    return driver


def _lake_mirror_present(engine: Engine) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM neuro.system_health WHERE health_key = :k"),
                {"k": LAKE_MIRROR_FRESHNESS_KEY},
            ).first()
            is not None
        )


def _bump_lake_mirror(engine: Engine, *, ok: bool, detail: str) -> None:
    """Bump the signal as neuro_writer (grants.sql:48 UPDATE(status,detail,measured_at); never stale_after).
    On a verified success advance measured_at=now() (the freshness proof); on failure leave measured_at
    (staleness accrues from the last-good mirror). Raises on rowcount 0 — a bump on an unseeded/vanished row is
    fail-loud, never a silent no-op (the probes._bump_freshness / wal _bump_wal_lag doctrine)."""
    if ok:
        sql = "UPDATE neuro.system_health SET status='ok', measured_at=now(), detail=:d WHERE health_key=:k"
    else:
        sql = "UPDATE neuro.system_health SET status='blocked', detail=:d WHERE health_key=:k"
    with engine.begin() as conn:
        result = conn.execute(text(sql), {"d": detail, "k": LAKE_MIRROR_FRESHNESS_KEY})
    if result.rowcount == 0:
        raise LakeMirrorProbeError(
            "lake_mirror_freshness row absent at bump time (rowcount 0) — refusing a silent no-op. Seed it "
            "(`neuro db durability seed`, registrar/admin) before running the probe."
        )


def run_lake_mirror_probe(
    engine: Engine,
    *,
    lake_mirror_driver: LakeMirrorDriver,
    destination: str,
    actor_id: int | None = None,
) -> LakeMirrorOutcome:
    """Run + verify one blob-lake mirror and record it into the freshness signal. `engine` must be a VERIFIED
    engine (repo.engine — the identity guard ran at construction). Order (the run_backup_probe /
    run_wal_archiver_probe mirror):

      1. `assert_offcloud_destination` — fail closed BEFORE the driver runs (ADR-0014 independence).
      2. the freshness row MUST already be seeded — abort loudly BEFORE touching the driver, so a real mirror
         is never orphaned with nowhere to record it (the writer cannot self-seed).
      3. run the injected mirror+verify.
      4. record the outcome (bump + probe_reports), THEN raise on failure (persist-before-raise, fail-loud).

    Returns the LakeMirrorOutcome on success; raises LakeMirrorProbeError on a failed/raising mirror (after
    recording) or ConfigurationError on a bad destination.
    """
    assert_offcloud_destination(destination)
    if not _lake_mirror_present(engine):
        raise LakeMirrorProbeError(
            "system_health['lake_mirror_freshness'] is not seeded — run `neuro db durability seed` "
            "(registrar/admin) at provisioning before the lake-mirror probe. Refusing to run a mirror that "
            "cannot be recorded."
        )
    try:
        outcome = lake_mirror_driver(engine, destination)
    except Exception as exc:  # noqa: BLE001 — record the failure, then re-raise loud (never a silent pass)
        _bump_lake_mirror(engine, ok=False, detail=f"lake mirror driver raised: {exc!r}")
        _record_probe_report(
            engine,
            probe_key=LAKE_MIRROR_FRESHNESS_KEY,
            status="blocked",
            report_text=f"{destination}: driver raised: {exc!r}",
            actor_id=actor_id,
        )
        raise LakeMirrorProbeError(f"lake mirror probe failed: driver raised for {destination!r}") from exc

    status = "ok" if outcome.ok else "blocked"
    _bump_lake_mirror(engine, ok=outcome.ok, detail=outcome.detail)
    _record_probe_report(
        engine,
        probe_key=LAKE_MIRROR_FRESHNESS_KEY,
        status=status,
        report_text=f"{destination}: {outcome.detail}",
        actor_id=actor_id,
    )
    if not outcome.ok:
        raise LakeMirrorProbeError(
            f"lake mirror probe recorded a FAILED/unverified mirror to {destination!r}: {outcome.detail}"
        )
    return outcome
