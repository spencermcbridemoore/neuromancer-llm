"""GO-D-timer (A2-16): the REAL off-cloud BackupDriver — pgbackrest-stream mirror to the desktop NVMe.

The concrete implementation `run_backup_probe` injects (governance/probes.py:52-53 left this as the A2-16
forward-reference). Owner-ruled transport (GO-input 1(b)-as-repaired, 2026-07-11): a VM-side post-backup
verify-then-mirror push over sftp BATCH mode (works against stock Windows OpenSSH, which ships NO rsync) to
a chroot-scoped desktop account; `NEURO_BACKUP_DEST` carries only the desktop-side PATH (it must pass
`assert_offcloud_destination`'s local-path allow-shape — the guard is unchanged); the SSH host/user/key
travel as an ssh_config Host ALIAS consumed only through the CommandRunner seam.

Driver order (every step through the seam, every step with a PINNED timeout — §8 fold 5: a transfer to a
desktop that sleeps mid-stream must become a loud failure, never a wedged-forever unit):

  0. RECENCY (§8 fold 3 — the headline): `pgbackrest info --output=json`; the newest FULL backup on EACH
     configured repo must be younger than BASE_BACKUP_INTERVAL + PROVISIONING_MARGIN, else blocked. A
     mirror of an old-but-intact repo must NEVER bump freshness (that would hold the 8-day gate open
     forever while base backups have silently stopped) — and per-repo means a stopped repo2 cadence
     (A2-7 §4.4) goes loud here even though the mirror reads repo1.
  1. VERIFY: `pgbackrest --repo=1 verify` — integrity BEFORE transfer. Honest limit: the mirror is
     TRANSFER-verified, never restore-verified (that is A2-9-real), and it copies a live tree.
  2. MIRROR (manifest-based, so pgbackrest expire's retention pruning propagates): diff the local repo tree
     against the REMOTE manifest (a JSON file this driver maintains at the mirror root) -> push new/changed,
     delete expired, then sha256 SPOT-VERIFY a sample of pushed files by fetching them back, and write the
     updated manifest LAST (info/manifest files last — a torn mirror is detectable, not adopted).

`BackupOutcome.ok` is True ONLY when recency AND verify AND mirror AND spot-verify all succeeded — the
probes.py contract ("ok iff the push AND its integrity verify both succeeded") extended with recency.
Fail-closed shape: any nonzero/timed-out step returns ok=False with a step-labeled detail;
`run_backup_probe` records it (persist-before-raise) and raises BackupProbeError.

Env-free by design (the L13 scan pins it): the destination/alias/paths arrive as PARAMETERS — the ONE env
read (`NEURO_BACKUP_DEST`) lives in the CLI layer (`neuro probe run`, a typer envvar option).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import tempfile
from pathlib import Path, PurePosixPath

from ..db.lanes import ConfigurationError
from .probes import BackupOutcome
from .provisioning_invariants import resolve_base_backup_interval, resolve_provisioning_margin
from .sftp_transport import (
    CommandResult as CommandResult,
)
from .sftp_transport import (
    CommandRunner,
    run_sftp_batch,
    run_subprocess,
)

# Pinned per-step timeouts (§8 fold 5). Committed constants, not knobs: a change is an auditable commit.
INFO_TIMEOUT_S = 120.0
VERIFY_TIMEOUT_S = 3600.0
SFTP_TIMEOUT_S = 3600.0

# The mirror's manifest file, at the mirror root (chroot-relative on the remote side).
MANIFEST_NAME = ".neuro-mirror-manifest.json"
_SPOT_CHECKS = 3  # pushed files re-fetched + sha256-compared per run (plus backup.info when present)


# CommandResult / CommandRunner / run_subprocess now live in governance/sftp_transport.py (the shared
# transport primitive extracted for B-7 so a second transport is not invented) and are re-exported above so
# this module's by-name test imports (test_backup_driver.py) stay green.


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_manifest(repo_path: Path) -> dict[str, int]:
    """relpath (posix) -> size for every file under the repo tree. Size-diff drives the push set; the
    sha256 spot-verify is the integrity sample (a full per-file hash of a multi-GB repo per run is the
    at-volume follow-on, not the greenfield default)."""
    out: dict[str, int] = {}
    for p in sorted(repo_path.rglob("*")):
        # skip symlinks: the real repo carries backup/<stanza>/latest -> the newest backup dir; following
        # it would duplicate that whole backup into the mirror (and rglob's symlink traversal is
        # Python-version-dependent — an explicit policy beats an accidental one).
        if p.is_file() and not p.is_symlink():
            out[PurePosixPath(p.relative_to(repo_path)).as_posix()] = p.stat().st_size
    return out


def _repo_freshness_from_info(
    info_json: str, *, stanza: str, bound: _dt.timedelta, now: _dt.datetime
) -> tuple[bool, str]:
    """Parse `pgbackrest info --output=json`: the newest FULL backup per repo must be younger than `bound`.
    Any parse surprise fails CLOSED (a recency check that cannot read the evidence must not pass)."""
    try:
        stanzas = json.loads(info_json)
        target = next(s for s in stanzas if s.get("name") == stanza)
        repo_keys = [int(r["key"]) for r in target["repo"]]
        newest_full: dict[int, tuple[float, str]] = {}
        for b in target.get("backup", []):
            if b.get("type") != "full":
                continue
            key = int(b["database"]["repo-key"])
            stop = float(b["timestamp"]["stop"])
            if key not in newest_full or stop > newest_full[key][0]:
                newest_full[key] = (stop, str(b.get("label", "?")))
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        return (False, f"recency: could not read pgbackrest info json ({type(exc).__name__}) — fail closed")
    if not repo_keys:  # vet M2: an EMPTY repo array must never certify freshness against zero repos
        return (False, "recency: pgbackrest info lists NO repositories (fail closed)")
    labels: list[str] = []
    for key in sorted(repo_keys):
        if key not in newest_full:
            return (False, f"recency: repo{key} has NO full backup (fail closed)")
        stop, label = newest_full[key]
        age = now - _dt.datetime.fromtimestamp(stop, tz=_dt.UTC)
        if age > bound:
            return (
                False,
                f"recency: repo{key}'s newest full backup {label} is {age.days}d old > the pinned "
                f"{bound.days}d (BASE_BACKUP_INTERVAL + margin) — the base-backup cadence has stalled "
                "(fail closed; a mirror of an aging repo must not bump freshness)",
            )
        labels.append(f"repo{key}:{label}")
    return (True, ", ".join(labels))


def make_pgbackrest_mirror_driver(
    *,
    repo_path: str | Path,
    ssh_alias: str,
    stanza: str = "neuro",
    repo: int = 1,
    runner: CommandRunner = run_subprocess,
    workdir: str | Path | None = None,
    now: _dt.datetime | None = None,
):
    """Build the BackupDriver (destination -> BackupOutcome). `repo_path` = the LOCAL pgbackrest repo tree
    (runbook: /pgdata/pgbackrest); `ssh_alias` = the ssh_config Host alias carrying host/user/key (the
    credential NEVER rides argv); `runner` is the injectable seam (tests script it); `now` is threaded for
    deterministic recency tests (defaults to the real UTC clock at call time)."""
    repo_root = Path(repo_path)
    if not ssh_alias or any(c.isspace() for c in ssh_alias):
        raise ConfigurationError(f"ssh alias {ssh_alias!r} is empty/whitespace-bearing (fail closed).")

    def driver(destination: str) -> BackupOutcome:
        # `destination` is the desktop-side path — already validated by assert_offcloud_destination in
        # run_backup_probe (the guard runs FIRST); under the chroot the sftp session is mirror-root-relative,
        # so it is recorded in the detail for the audit row, not re-derived here.
        moment = now or _dt.datetime.now(_dt.UTC)
        bound = resolve_base_backup_interval() + resolve_provisioning_margin()

        # 0. RECENCY (fold 3)
        info = runner(["pgbackrest", f"--stanza={stanza}", "info", "--output=json"], timeout_s=INFO_TIMEOUT_S)
        if info.returncode != 0:
            return BackupOutcome(
                ok=False, detail=f"pgbackrest info failed (rc={info.returncode}): {info.stderr.strip()[:200]}"
            )
        fresh, recency_detail = _repo_freshness_from_info(info.stdout, stanza=stanza, bound=bound, now=moment)
        if not fresh:
            return BackupOutcome(ok=False, detail=recency_detail)

        # 1. VERIFY (integrity before transfer)
        verify = runner(
            ["pgbackrest", f"--stanza={stanza}", f"--repo={repo}", "verify"], timeout_s=VERIFY_TIMEOUT_S
        )
        if verify.returncode != 0:
            return BackupOutcome(
                ok=False,
                detail=f"pgbackrest verify --repo={repo} failed (rc={verify.returncode}): {verify.stderr.strip()[:200]}",
            )

        if not repo_root.is_dir():
            return BackupOutcome(ok=False, detail=f"local repo path {str(repo_root)!r} is not a directory")

        with tempfile.TemporaryDirectory(dir=workdir) as td:
            tmp = Path(td)

            def sftp(batch_lines: list[str]) -> CommandResult:
                return run_sftp_batch(runner, ssh_alias, batch_lines, tmp_dir=tmp, timeout_s=SFTP_TIMEOUT_S)

            # 2a. fetch the remote manifest (a failed fetch = first run -> EMPTY manifest: over-push is the
            # safe direction, and the delete set derives from the REMOTE manifest so empty deletes nothing)
            remote_manifest_local = tmp / "remote-manifest.json"
            got = sftp([f"get {MANIFEST_NAME} {remote_manifest_local.as_posix()}"])
            remote: dict[str, int] = {}
            if got.returncode == 0 and remote_manifest_local.exists():
                try:
                    remote = {
                        str(k): int(v)
                        for k, v in json.loads(remote_manifest_local.read_text(encoding="utf-8")).items()
                    }
                except (ValueError, TypeError):
                    remote = {}  # unreadable manifest -> rebuild by full push (over-push, never over-delete)

            local = _local_manifest(repo_root)
            to_push = sorted(p for p, size in local.items() if remote.get(p) != size)
            to_delete = sorted(p for p in remote if p not in local)

            # 2b. push new/changed + prune expired (retention tracking — the mirror must follow expire)
            lines: list[str] = []
            made: set[str] = set()
            for rel in to_push:
                parent = PurePosixPath(rel).parent.as_posix()
                if parent not in (".", "") and parent not in made:
                    parts = PurePosixPath(parent).parts
                    for i in range(1, len(parts) + 1):
                        d = "/".join(parts[:i])
                        if d not in made:
                            lines.append(f"-mkdir {d}")  # '-' prefix: mkdir-exists is not an error
                            made.add(d)
                lines.append(f"put {(repo_root / rel).as_posix()} {rel}")
            for rel in to_delete:
                lines.append(f"-rm {rel}")  # '-' prefix: a doubly-pruned file is not an error
            if lines:
                pushed = sftp(lines)
                if pushed.returncode != 0:
                    return BackupOutcome(
                        ok=False,
                        detail=f"sftp mirror push failed (rc={pushed.returncode}): {pushed.stderr.strip()[:200]}",
                    )

            # 2c. sha256 SPOT-VERIFY a sample of what we just pushed (fetch back, compare) + backup.info
            sample = to_push[:_SPOT_CHECKS]
            info_rel = f"backup/{stanza}/backup.info"
            if info_rel in local and info_rel not in sample:
                sample.append(info_rel)
            for i, rel in enumerate(sample):
                back = tmp / f"spot-{i}"
                fetched = sftp([f"get {rel} {back.as_posix()}"])
                if fetched.returncode != 0 or not back.exists():
                    return BackupOutcome(
                        ok=False, detail=f"spot-verify fetch of {rel!r} failed (fail closed)"
                    )
                if _sha256_file(back) != _sha256_file(repo_root / rel):
                    return BackupOutcome(
                        ok=False,
                        detail=f"spot-verify sha256 MISMATCH on {rel!r} — torn/corrupt transfer (fail closed)",
                    )

            # 2d. the manifest goes LAST (a crash before this line leaves extra remote files, re-pushed next
            # run — over-push, never a manifest that claims files the mirror does not have)
            local_manifest_file = tmp / "local-manifest.json"
            local_manifest_file.write_text(json.dumps(local, sort_keys=True), encoding="utf-8")
            wrote = sftp([f"put {local_manifest_file.as_posix()} {MANIFEST_NAME}"])
            if wrote.returncode != 0:
                return BackupOutcome(
                    ok=False,
                    detail=f"manifest write failed (rc={wrote.returncode}) — mirror not certified (fail closed)",
                )

        return BackupOutcome(
            ok=True,
            detail=(
                f"repos fresh [{recency_detail}]; verify --repo={repo} ok; mirrored +{len(to_push)}/"
                f"-{len(to_delete)} files to {destination} (alias {ssh_alias}); {len(sample)} spot-checks ok; "
                "transfer-verified only (restore-verification = A2-9-real)"
            ),
        )

    return driver
