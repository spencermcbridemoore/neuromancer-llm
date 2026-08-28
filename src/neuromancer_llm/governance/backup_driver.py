"""GO-D-timer (A2-16): the REAL off-cloud BackupDriver — pgbackrest-stream mirror to the desktop NVMe.

The concrete implementation `run_backup_probe` injects (governance/probes.py:52-53 left this as the A2-16
forward-reference). Owner-ruled transport (GO-input 1(b)-as-repaired, 2026-07-11): a VM-side post-backup
verify-then-mirror push over sftp BATCH mode (works against stock Windows OpenSSH, which ships NO rsync) to
a chroot-scoped desktop account; `NEURO_BACKUP_DEST` carries only the desktop-side PATH (it must pass
`assert_offcloud_destination`'s local-path allow-shape — the guard is unchanged); the SSH host/user/key
travel as an ssh_config Host ALIAS consumed only through the CommandRunner seam.

Driver order (every step through the seam, every step with a PINNED timeout — §8 fold 5: a transfer to a
desktop that sleeps mid-stream must become a loud failure, never a wedged-forever unit):

  0. RECENCY (§8 fold 3 — the headline): `pgbackrest info --output=json`; the newest FULL backup on each
     repo IN THE PINNED GATE BASIS (provisioning_invariants.GATE_BASIS_REPOS) must be younger than
     BASE_BACKUP_INTERVAL + PROVISIONING_MARGIN, else blocked. A mirror of an old-but-intact repo must
     NEVER bump freshness (that would hold the 8-day gate open forever while base backups have silently
     stopped) — and per-repo means a stopped repo2 cadence (A2-7 §4.4) goes loud here even though the
     mirror reads repo1. ⚠ CHANGED 2026-08-28 (repo3 unit, owner-nodded): the basis is the PIN, not
     "each CONFIGURED repo". A conf-added repo (repo3 on B2, §A·72) is READ and REPORTED in the detail but
     does NOT gate — that is what makes it NOTIFY-ONLY — while a repo in the basis that has vanished from
     the conf now fails CLOSED instead of silently leaving. Anything you say about this step's coverage
     must be keyed to the basis; `governance/alert_triage.py`'s backup arm is worded that way for the same
     reason.
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
from .provisioning_invariants import (
    resolve_base_backup_interval,
    resolve_gate_basis_repos,
    resolve_provisioning_margin,
)
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


class InfoUnreadableError(RuntimeError):
    """`pgbackrest info --output=json` could not be parsed into (repos, newest-full-per-repo).

    Carries only the EXCEPTION TYPE NAME, never the offending content: the info JSON is derived from a conf
    that holds account-wide cloud keys, and the redaction contract that governs provisioning_invariants.py
    applies to anything downstream of that file."""


def newest_full_per_repo(
    info_json: str, *, stanza: str
) -> tuple[tuple[int, ...], dict[int, tuple[float, str]]]:
    """Parse `pgbackrest info --output=json` into (configured repo keys, {repo-key: (stop_epoch, label)}).

    ONE implementation of "read the info JSON", TWO callers asking DIFFERENT questions: the gating recency
    over `GATE_BASIS_REPOS` (`_repo_freshness_from_info` below) and one non-gating repo's own recency
    (governance/repo3_probe.py). A second parser here would be the one-implementation-per-concept violation
    this repo keeps correcting, and the two would drift on exactly the edge cases that matter.

    Raises InfoUnreadableError on ANY parse surprise — both callers convert that to a fail-CLOSED blocked
    outcome, because a recency check that cannot read its evidence must never pass."""
    try:
        stanzas = json.loads(info_json)
        target = next(s for s in stanzas if s.get("name") == stanza)
        repo_keys = tuple(int(r["key"]) for r in target["repo"])
        newest_full: dict[int, tuple[float, str]] = {}
        for b in target.get("backup", []):
            if b.get("type") != "full":
                continue
            key = int(b["database"]["repo-key"])
            stop = float(b["timestamp"]["stop"])
            if key not in newest_full or stop > newest_full[key][0]:
                newest_full[key] = (stop, str(b.get("label", "?")))
    except (ValueError, KeyError, StopIteration, TypeError, AttributeError) as exc:
        # ⚠ AttributeError was ADDED 2026-08-28 and it was a REAL hole, found by the repo3 unit's own
        # redaction probe rather than reasoned about: `pgbackrest info` is expected to yield a LIST of
        # stanzas, but a top-level JSON OBJECT parses fine and then iterates as its KEYS — plain strings —
        # so `s.get(...)` raised AttributeError straight out of this function, past the fail-closed
        # conversion, as an untyped crash. The inherited tuple covered every shape anyone had thought of;
        # it did not cover that one.
        raise InfoUnreadableError(type(exc).__name__) from exc
    return (repo_keys, newest_full)


def _non_gating_note(
    non_gating: list[int],
    newest_full: dict[int, tuple[float, str]],
    *,
    bound: _dt.timedelta,
    now: _dt.datetime,
) -> str:
    """Render the REPORTED-but-not-gating repos as a suffix, or "" when there are none.

    ⚠ THE EMPTY-STRING CASE IS LOAD-BEARING AND IS PINNED. With today's `{1,2}` conf there are no non-gating
    repos, so this returns "" and the detail string is BYTE-IDENTICAL to what shipped before the pin. That
    equality is the unit's no-churn proof, and a test asserts the literal rather than a substring."""
    if not non_gating:
        return ""
    parts: list[str] = []
    for key in non_gating:
        if key not in newest_full:
            parts.append(f"repo{key}:NO FULL BACKUP")
            continue
        stop, label = newest_full[key]
        age = now - _dt.datetime.fromtimestamp(stop, tz=_dt.UTC)
        parts.append(f"repo{key}:{label}" + (f" STALE {age.days}d" if age > bound else ""))
    return " (non-gating, reported only: " + ", ".join(parts) + ")"


def _repo_freshness_from_info(
    info_json: str, *, stanza: str, bound: _dt.timedelta, now: _dt.datetime
) -> tuple[bool, str]:
    """The GATING recency check: every repo in `GATE_BASIS_REPOS` must be present in the info JSON AND carry
    a FULL backup younger than `bound`. Repos outside the basis are READ and REPORTED, never gating.

    ★ THE BASIS IS THE PIN, NOT THE CONF (repo3 unit, 2026-08-28; owner-nodded both directions). Deriving the
    set from the info JSON auto-joined any conf-added repo into the ADR-0020 gate, so landing repo3 would
    have closed the gate on an unproven arm at the first run. It also cuts the other way, deliberately: a
    repo REMOVED from the conf now fails this check instead of silently leaving the basis — which closes a
    pre-existing fail-open, because pulling the `repo2-*` lines (the A2-7 §7 rollback lever) used to leave
    `backup_freshness` reading GREEN on repo1 alone. See provisioning_invariants.GATE_BASIS_REPOS, whose
    assertion (8) measures `basis <= configured` against the real file so the pin cannot rot away from it.

    Any parse surprise fails CLOSED (a recency check that cannot read the evidence must not pass)."""
    basis = resolve_gate_basis_repos()  # fail closed on an absent or EMPTY pin, before anything is read
    try:
        repo_keys, newest_full = newest_full_per_repo(info_json, stanza=stanza)
    except InfoUnreadableError as exc:
        return (False, f"recency: could not read pgbackrest info json ({exc}) — fail closed")
    if not repo_keys:  # vet M2: an EMPTY repo array must never certify freshness against zero repos
        return (False, "recency: pgbackrest info lists NO repositories (fail closed)")
    configured = set(repo_keys)
    labels: list[str] = []
    for key in sorted(basis):
        if key not in configured:
            return (
                False,
                f"recency: repo{key} is in the pinned gate basis {sorted(basis)} but is NOT configured in "
                "pgbackrest (fail closed) — the gate would stand on a repository that does not exist. "
                "Restore it in the conf, or move the pin (GATE_BASIS_REPOS); a change is an auditable "
                "commit.",
            )
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
    note = _non_gating_note(sorted(configured - basis), newest_full, bound=bound, now=now)
    return (True, ", ".join(labels) + note)


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
