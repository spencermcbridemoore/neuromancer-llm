"""GO-D-timer (A2-16): the REAL off-cloud BackupDriver — scripted-seam probes + through-the-probe runs.

RED@7d680fb = ModuleNotFoundError (governance/backup_driver.py absent). Probes the §8 folds:
  - RECENCY FIRST (fold 3): an old-but-intact repo (or any repo missing a full backup) is blocked BEFORE
    verify/transfer — a mirror of an aging repo must never bump freshness; per-repo, so a stalled repo2
    cadence blocks even though the mirror reads repo1;
  - verify-before-transfer; a failed verify never mirrors;
  - the manifest-based mirror: first run pushes everything + the manifest LAST; a pruned local file is
    DELETED remotely on the next run (pgbackrest expire propagates — the mirror never diverges);
  - sha256 spot-verify catches a torn/corrupt transfer (fold: transfer-verified, never restore-verified);
  - a TIMED-OUT step is a fail-closed blocked outcome (fold 5), never a hang;
  - through run_backup_probe (pg): ok bumps freshness; a recency-blocked driver records blocked then raises.
The scripted CommandRunner maintains a fake remote store and executes sftp BATCH files for real, so the
diff/push/delete/spot-verify logic is exercised, not mocked away.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.backup_driver import (
    MANIFEST_NAME,
    CommandResult,
    make_pgbackrest_mirror_driver,
)
from neuromancer_llm.governance.freshness import seed_backup_freshness
from neuromancer_llm.governance.probes import BackupProbeError, run_backup_probe

_NOW = _dt.datetime(2026, 7, 11, 12, 0, 0, tzinfo=_dt.UTC)
_DEST = "D:/neuro-backups"  # passes the off-cloud guard's _WIN_DRIVE allow-shape (path-only, fold 1)


def _info_json(*, ages_days: dict[int, float | None]) -> str:
    """A pgbackrest info --output=json body: one 'neuro' stanza; per repo-key, a full backup that finished
    `age` days before _NOW (None = no full backup on that repo)."""
    backups = []
    for key, age in ages_days.items():
        if age is None:
            continue
        stop = (_NOW - _dt.timedelta(days=age)).timestamp()
        backups.append(
            {
                "type": "full",
                "label": f"20260711-full-r{key}",
                "database": {"repo-key": key},
                "timestamp": {"start": stop - 60, "stop": stop},
            }
        )
    stanza = {
        "name": "neuro",
        "repo": [{"key": k} for k in ages_days],
        "backup": backups,
    }
    return json.dumps([stanza])


class _Seam:
    """The scripted CommandRunner: pgbackrest answers are scripted; sftp batch files are EXECUTED against a
    fake remote store (put/get/rm/mkdir), so the mirror logic runs for real."""

    def __init__(
        self,
        *,
        info: CommandResult,
        verify: CommandResult | None = None,
        corrupt_on_get: str | None = None,
        fail_puts: bool = False,
        fail_manifest_put: bool = False,
    ) -> None:
        self.info = info
        self.verify = verify if verify is not None else CommandResult(0, "", "")
        self.remote: dict[str, bytes] = {}
        self.corrupt_on_get = corrupt_on_get
        self.fail_puts = fail_puts
        self.fail_manifest_put = fail_manifest_put
        self.calls: list[list[str]] = []
        self.batches: list[list[str]] = []  # the sftp batch LINES per call (order assertions ride these)

    def __call__(self, argv: list[str], *, timeout_s: float) -> CommandResult:
        self.calls.append(argv)
        assert timeout_s > 0  # every step carries a pinned timeout (fold 5)
        if argv[0] == "pgbackrest":
            return self.info if "info" in argv else self.verify
        assert argv[0] == "sftp" and argv[1] == "-b"
        batch_lines = [ln for ln in Path(argv[2]).read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.batches.append(batch_lines)
        rc = 0
        for line in batch_lines:
            ignore = line.startswith("-")
            cmd = line.lstrip("-")
            if not cmd.strip():
                continue
            op, *args = cmd.split(" ")
            if op == "put":
                if self.fail_puts or (self.fail_manifest_put and args[1] == MANIFEST_NAME):
                    rc = 1
                    continue
                self.remote[args[1]] = Path(args[0]).read_bytes()
            elif op == "get":
                if args[0] not in self.remote:
                    if not ignore:
                        rc = 1
                    continue
                data = self.remote[args[0]]
                if self.corrupt_on_get == args[0]:
                    data = b"CORRUPTED" + data
                Path(args[1]).write_bytes(data)
            elif op == "rm":
                if args[0] in self.remote:
                    del self.remote[args[0]]
                elif not ignore:
                    rc = 1
            elif op == "mkdir":
                pass
            else:  # an unexpected batch op is a test bug — fail loud
                raise AssertionError(f"unexpected sftp batch op {line!r}")
        return CommandResult(rc, "", "")


@pytest.fixture
def repo_tree(tmp_path):
    root = tmp_path / "pgbackrest-repo"
    (root / "backup" / "neuro").mkdir(parents=True)
    (root / "archive" / "neuro").mkdir(parents=True)
    (root / "backup" / "neuro" / "backup.info").write_bytes(b"info-bytes-v1")
    (root / "backup" / "neuro" / "20260711F.tgz").write_bytes(b"full-backup-bytes")
    (root / "archive" / "neuro" / "000000010000000000000042").write_bytes(b"wal-segment")
    return root


def _driver(seam, repo_tree, **kw):
    return make_pgbackrest_mirror_driver(
        repo_path=repo_tree, ssh_alias="neuro-desktop", runner=seam, now=_NOW, **kw
    )


# ---- recency FIRST (fold 3) -----------------------------------------------------------------------------


def test_stale_repo_blocks_before_verify_or_transfer(repo_tree):
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 10.0, 2: 1.0}), ""))
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "recency" in out.detail and "repo1" in out.detail
    assert len(seam.calls) == 1  # ONLY the info call: no verify, no sftp (blocked at step 0)


def test_stalled_repo2_cadence_blocks_even_though_the_mirror_reads_repo1(repo_tree):
    # A2-7 §4.4: a bare `pgbackrest backup` targets repo1 only — repo2's rot must go loud HERE.
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0, 2: 20.0}), ""))
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "repo2" in out.detail


def test_repo_with_no_full_backup_blocks(repo_tree):
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0, 2: None}), ""))
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "NO full backup" in out.detail


def test_empty_repo_list_blocks_never_vacuous(repo_tree):
    # vet M2: an info body whose stanza lists ZERO repositories must fail closed — a per-repo loop over an
    # empty set silently certifying freshness is the vacuous-pass edge.
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={}), ""))
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "NO repositories" in out.detail


def test_unreadable_info_json_blocks(repo_tree):
    seam = _Seam(info=CommandResult(0, "not json at all", ""))
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "fail closed" in out.detail


def test_info_failure_blocks(repo_tree):
    seam = _Seam(info=CommandResult(3, "", "stanza does not exist"))
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "info failed" in out.detail


# ---- verify before transfer ------------------------------------------------------------------------------


def test_failed_verify_never_mirrors(repo_tree):
    seam = _Seam(
        info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""),
        verify=CommandResult(41, "", "checksum error"),
    )
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "verify" in out.detail
    assert seam.remote == {}  # nothing was pushed


def test_timed_out_step_is_blocked_not_hung(repo_tree):
    # fold 5: the real runner converts TimeoutExpired into rc=-1 'timed out' — the driver fails closed.
    seam = _Seam(
        info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""),
        verify=CommandResult(-1, "", "timed out after 3600s"),
    )
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "timed out" in out.detail


# ---- the manifest mirror ---------------------------------------------------------------------------------


def test_first_run_mirrors_everything_manifest_last(repo_tree):
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""))
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is True, out.detail
    assert set(seam.remote) == {
        "backup/neuro/backup.info",
        "backup/neuro/20260711F.tgz",
        "archive/neuro/000000010000000000000042",
        MANIFEST_NAME,
    }
    manifest = json.loads(seam.remote[MANIFEST_NAME])
    assert manifest["backup/neuro/backup.info"] == len(b"info-bytes-v1")
    assert "transfer-verified only" in out.detail


def test_pruned_file_is_deleted_remotely(repo_tree):
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""))
    driver = _driver(seam, repo_tree)
    assert driver(_DEST).ok is True
    (repo_tree / "archive" / "neuro" / "000000010000000000000042").unlink()  # pgbackrest expire pruned it
    out = driver(_DEST)
    assert out.ok is True
    assert "archive/neuro/000000010000000000000042" not in seam.remote  # retention propagated
    assert "-1 files" in out.detail or "/-1" in out.detail


def test_spot_verify_catches_torn_transfer(repo_tree):
    seam = _Seam(
        info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""),
        corrupt_on_get="backup/neuro/20260711F.tgz",
    )
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "MISMATCH" in out.detail


def test_failed_push_is_blocked(repo_tree):
    # vet M3: the push-failure branch, exercised (rc!=0 from the mirror batch -> blocked, fail closed).
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""), fail_puts=True)
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "mirror push failed" in out.detail


def test_failed_manifest_write_is_blocked(repo_tree):
    # vet M3: a mirror whose MANIFEST write fails is NOT certified — next run re-diffs honestly (over-push).
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""), fail_manifest_put=True)
    out = _driver(seam, repo_tree)(_DEST)
    assert out.ok is False and "manifest write failed" in out.detail
    assert MANIFEST_NAME not in seam.remote


def test_manifest_put_is_the_final_sftp_operation(repo_tree):
    # vet M3 (the load-bearing ORDER): a manifest written before the pushes/spot-verify would make a torn
    # mirror ADOPTED (the next run diffs against a lying manifest and skips the corrupt files). The manifest
    # put must be the LAST sftp call, alone in its batch, AFTER every push and every spot-verify get.
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""))
    assert _driver(seam, repo_tree)(_DEST).ok is True
    last = seam.batches[-1]
    assert len(last) == 1 and last[0].startswith("put ") and last[0].endswith(f" {MANIFEST_NAME}")
    for earlier in seam.batches[:-1]:  # no earlier batch may touch the manifest
        assert not any(ln.startswith("put") and ln.endswith(f" {MANIFEST_NAME}") for ln in earlier)


def test_run_subprocess_converts_timeout_to_fail_closed():
    # fold 5's REAL half (vet nice-to-have 3): the actual subprocess runner converts TimeoutExpired into a
    # fail-closed CommandResult — never a hang, never an uncaught raise.
    import sys

    from neuromancer_llm.governance.backup_driver import run_subprocess

    res = run_subprocess([sys.executable, "-c", "import time; time.sleep(10)"], timeout_s=0.2)
    assert res.returncode == -1 and "timed out" in res.stderr


# ---- pins + through-the-probe (pg) ------------------------------------------------------------------------


def test_absent_cadence_pin_fails_closed(repo_tree, monkeypatch):
    monkeypatch.setattr("neuromancer_llm.governance.provisioning_invariants.BASE_BACKUP_INTERVAL", None)
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0}), ""))
    with pytest.raises(ConfigurationError, match="pin is absent"):
        _driver(seam, repo_tree)(_DEST)


@pytest.mark.pg
def test_healthy_driver_bumps_freshness_through_the_probe(repo, repo_tree):
    with repo.engine.connect() as conn:
        seed_backup_freshness(conn)
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 1.0, 2: 1.5}), ""))
    out = run_backup_probe(
        repo.engine, backup_driver=_driver(seam, repo_tree), destination=_DEST, actor_id=None
    )
    assert out.ok is True
    from sqlalchemy import text

    with repo.engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM neuro.system_health WHERE health_key='backup_freshness'")
        ).scalar_one()
    assert status == "ok"


@pytest.mark.pg
def test_recency_blocked_driver_records_blocked_then_raises(repo, repo_tree):
    with repo.engine.connect() as conn:
        seed_backup_freshness(conn)
    seam = _Seam(info=CommandResult(0, _info_json(ages_days={1: 30.0}), ""))
    with pytest.raises(BackupProbeError):
        run_backup_probe(
            repo.engine, backup_driver=_driver(seam, repo_tree), destination=_DEST, actor_id=None
        )
    from sqlalchemy import text

    with repo.engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, detail FROM neuro.system_health WHERE health_key='backup_freshness'")
        ).one()
    assert row.status == "blocked" and "recency" in row.detail
