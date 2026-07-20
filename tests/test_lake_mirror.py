"""B-7 blob-lake mirror (2026-07-20): the catalog-driven Azure->desktop mirror driver + its freshness
PRODUCER. Seeds REAL artifacts through the W1-W8 registrar (the trusted writer path, the test_crawl_reconcile /
test_reader idiom); the source lake is a LocalFsBackend standing in for Azure (the driver reads it via the
injected source_backend_for seam); the sftp transport is a scripted CommandRunner executing batch files against
a fake remote store (the test_backup_driver _Seam idiom), so the diff/push/verify/prune logic runs for real.

Pins every pre-build-vet fold: the delta-verify catches a same-length torn transfer (C5); the manifest is
written ONLY on full success (a failed verify re-pushes next run, never empty-delta-bumps fresh); a typo'd lane
fails CLOSED while a registered-but-empty lane greens with checked=0; the backend_key=:lane scope filter is
DE-BLINDED by a second (artifacts-dev) lane; the None-seam is collected fail-closed; the off-cloud guard + the
seed-check fire BEFORE the driver.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.registrar import BundleRegistrar, TableManifestSpec
from neuromancer_llm.capture.adapters.vllm import LogprobSample
from neuromancer_llm.capture.events import logprob_parquet
from neuromancer_llm.capture.reader import manifest_artifacts
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.durability import seed_all
from neuromancer_llm.governance.lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
from neuromancer_llm.governance.lake_mirror import (
    LAKE_MANIFEST_NAME,
    LAKE_SUBDIR,
    LakeMirrorOutcome,
    LakeMirrorProbeError,
    make_lake_mirror_driver,
    run_lake_mirror_probe,
)
from neuromancer_llm.governance.sftp_transport import CommandResult
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg

_DEST = "D:/neuro-lake"  # passes the off-cloud guard's _WIN_DRIVE allow-shape (path-only)


# --- scripted sftp transport: a fake remote store the batch files run against for real -------------------
class _FakeSftp:
    def __init__(
        self,
        *,
        corrupt_on_get: str | None = None,
        fail_get: str | None = None,
        fail_puts: bool = False,
        fail_manifest_put: bool = False,
        fail_rm: bool = False,
    ) -> None:
        self.remote: dict[str, bytes] = {}
        self.corrupt_on_get = (
            corrupt_on_get  # a remote path whose FETCH-BACK returns torn bytes (same length)
        )
        self.fail_get = fail_get  # a remote path whose GET returns rc=1 (a fetch-back transport failure)
        self.fail_puts = fail_puts
        self.fail_manifest_put = fail_manifest_put
        self.fail_rm = fail_rm  # every -rm returns rc=1 (a prune transport failure; fires despite the '-')
        self.batches: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout_s: float) -> CommandResult:
        assert timeout_s > 0  # every sftp op carries a pinned timeout (the hang belt)
        assert argv[0] == "sftp" and argv[1] == "-b"
        lines = [ln for ln in Path(argv[2]).read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.batches.append(lines)
        rc = 0
        for line in lines:
            ignore = line.startswith("-")
            op, *args = line.lstrip("-").split(" ")
            if op == "put":
                if self.fail_puts or (self.fail_manifest_put and args[1].endswith(LAKE_MANIFEST_NAME)):
                    rc = 1
                    continue
                self.remote[args[1]] = Path(args[0]).read_bytes()
            elif op == "get":
                if self.fail_get == args[0]:  # a fetch-back transport failure (rc=1, nothing written)
                    rc = 1
                    continue
                if args[0] not in self.remote:
                    if not ignore:
                        rc = 1
                    continue
                data = self.remote[args[0]]
                if self.corrupt_on_get == args[0]:
                    data = bytes((b ^ 0xFF) for b in data)  # SAME-LENGTH torn -> the sha256 clause fires (C5)
                Path(args[1]).write_bytes(data)
            elif op == "rm":
                if self.fail_rm:  # a prune transport failure — fires even for '-rm' (the guard must catch it)
                    rc = 1
                    continue
                if args[0] in self.remote:
                    del self.remote[args[0]]
                elif not ignore:
                    rc = 1
            elif op == "mkdir":
                pass
            else:  # an unexpected batch op is a test bug — fail loud
                raise AssertionError(f"unexpected sftp batch op {line!r}")
        return CommandResult(rc, "", "")


class _BoomGet:
    """A source backend whose .get raises — the Azure-read-failed path (a partition mid-fetch)."""

    driver = "local_fs"

    def get(self, key: str) -> bytes:
        raise RuntimeError("source read failed (simulated Azure partition)")


def _seed_artifact(repo, backend, backend_id, run_id, *, shard: str, partition: str, generated: int) -> None:
    pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(8))
    pq_bytes, n, _ = logprob_parquet(
        LogprobSample(prompt="p", generated_token_id=generated, top_logprobs=pairs)
    )
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    reg.register(
        run_id=run_id,
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=partition,
        shards={shard: pq_bytes},
        artifact_kinds={shard: "token_table"},
        table_manifests=[TableManifestSpec(shard, "logprobs", row_count=n)],
    )


def _prod_lake(repo, tmp_path):
    """Register the artifacts-prod lane (a LocalFsBackend standing in for Azure) + seed ONE real artifact."""
    root = tmp_path / "prod-lake"
    root.mkdir()
    backend = LocalFsBackend(root)
    backend_id = repo.get_or_create_storage_backend(
        "artifacts-prod", driver="local_fs", lane="artifacts", base_uri=str(root), is_cloud=False
    )
    _seed_artifact(
        repo,
        backend,
        backend_id,
        repo_run(repo),
        shard="lp-prod-0.parquet",
        partition="logprobs/prod/0",
        generated=1000,
    )
    return root


def repo_run(repo) -> int:
    """The run_id the seeded fixture created (one run exists; the artifacts hang off it)."""
    with repo.engine.connect() as conn:
        return conn.execute(text("SELECT run_id FROM neuro.runs ORDER BY run_id LIMIT 1")).scalar_one()


def _source_for(_driver: str, base_uri: str):
    """The production seam shape: (driver, base_uri) -> a read backend. In tests the prod lane is local_fs, so
    rebuild a LocalFsBackend from the catalog base_uri (exactly what make_backend does for local_fs)."""
    return LocalFsBackend(base_uri)


def _driver(runner, *, lane_backend_key="artifacts-prod", source_backend_for=_source_for):
    return make_lake_mirror_driver(
        source_backend_for=source_backend_for,
        ssh_alias="neuro-desktop",
        lane_backend_key=lane_backend_key,
        runner=runner,
    )


# ---- the driver: clean / incremental / prune ------------------------------------------------------------


def test_clean_first_run_pushes_and_verifies_all(seeded, tmp_path):
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    sftp = _FakeSftp()
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok and out.checked == 1 and out.pushed == 1 and out.verified == 1 and out.deleted == 0
    art = manifest_artifacts(repo.engine, run_id=repo_run(repo))[0]
    assert f"{LAKE_SUBDIR}/{art.uri}" in sftp.remote  # blob landed under the lake/ subdir
    assert f"{LAKE_SUBDIR}/{LAKE_MANIFEST_NAME}" in sftp.remote  # manifest written


def test_incremental_run_pushes_nothing_new(seeded, tmp_path):
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    sftp = _FakeSftp()
    _driver(sftp)(repo.engine, _DEST)  # first run mirrors everything
    n_after_first = len(sftp.batches)
    out = _driver(sftp)(repo.engine, _DEST)  # second run: manifest matches catalog -> no re-push
    assert out.ok and out.pushed == 0 and out.verified == 0
    # the second run issues NO blob `put` (only the manifest fetch + re-put)
    second = sftp.batches[n_after_first:]
    blob_puts = [ln for b in second for ln in b if ln.startswith("put ") and LAKE_MANIFEST_NAME not in ln]
    assert blob_puts == []


def test_prunes_soft_deleted_artifact(seeded, tmp_path):
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    sftp = _FakeSftp()
    _driver(sftp)(repo.engine, _DEST)
    art = manifest_artifacts(repo.engine, run_id=repo_run(repo))[0]
    remote_path = f"{LAKE_SUBDIR}/{art.uri}"
    assert remote_path in sftp.remote
    with repo.engine.begin() as conn:  # soft-delete it -> next run prunes it from the mirror
        conn.execute(text("UPDATE neuro.artifacts SET deleted_at = now() WHERE uri = :u"), {"u": art.uri})
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok and out.checked == 0 and out.deleted == 1
    assert remote_path not in sftp.remote  # pruned from the mirror


# ---- the folds: verify / manifest-last / scope / typo / empty / None-seam -------------------------------


def test_torn_transfer_fails_closed_and_does_not_write_manifest(seeded, tmp_path):
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    art = manifest_artifacts(repo.engine, run_id=repo_run(repo))[0]
    sftp = _FakeSftp(corrupt_on_get=f"{LAKE_SUBDIR}/{art.uri}")  # fetch-back returns same-length torn bytes
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok is False and "MISMATCH" in out.detail
    assert f"{LAKE_SUBDIR}/{LAKE_MANIFEST_NAME}" not in sftp.remote  # manifest NOT written on a failed verify


def test_verify_fail_then_clean_rerun_repushes(seeded, tmp_path):
    # MINOR-2b: a failed-verify run must NOT record the manifest, so the NEXT (clean) run re-pushes the blob
    # rather than seeing an empty delta and bumping FRESH over a corrupt copy.
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    art = manifest_artifacts(repo.engine, run_id=repo_run(repo))[0]
    sftp = _FakeSftp(corrupt_on_get=f"{LAKE_SUBDIR}/{art.uri}")
    assert _driver(sftp)(repo.engine, _DEST).ok is False  # verify fails, no manifest written
    sftp.corrupt_on_get = None  # the transient corruption clears
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok and out.pushed == 1 and out.verified == 1  # re-pushed (NOT an empty-delta green)


def test_source_get_raises_fails_closed(seeded, tmp_path):
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    out = _driver(_FakeSftp(), source_backend_for=lambda _d, _u: _BoomGet())(repo.engine, _DEST)
    assert out.ok is False and "source get" in out.detail


def test_source_backend_none_seam_fails_closed(seeded, tmp_path):
    # the reconcile None-seam lesson: a source_backend_for that RETURNS None (the dict.get-style seam) must fail
    # the mirror CLOSED, never crash the probe.
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    out = _driver(_FakeSftp(), source_backend_for=lambda _d, _u: None)(repo.engine, _DEST)
    assert out.ok is False and "None" in out.detail


def test_sftp_put_failure_fails_closed(seeded, tmp_path):
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    out = _driver(_FakeSftp(fail_puts=True))(repo.engine, _DEST)
    assert out.ok is False and "put" in out.detail


def test_manifest_written_last_on_success(seeded, tmp_path):
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    sftp = _FakeSftp()
    _driver(sftp)(repo.engine, _DEST)
    # the manifest put is the LAST put across all batches (blobs first, manifest last)
    all_puts = [ln for b in sftp.batches for ln in b if ln.startswith("put ")]
    assert all_puts and LAKE_MANIFEST_NAME in all_puts[-1]
    assert not any(LAKE_MANIFEST_NAME in ln for ln in all_puts[:-1])


def test_non_dict_manifest_rebuilds_by_full_push(seeded, tmp_path):
    # a valid-but-NON-dict remote manifest (a JSON array via corruption/tamper) must self-heal by full push,
    # NOT crash on .items() (the isinstance guard). Pre-seed the remote manifest as `[]`.
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    sftp = _FakeSftp()
    sftp.remote[f"{LAKE_SUBDIR}/{LAKE_MANIFEST_NAME}"] = b"[]"  # valid JSON, not a dict
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok and out.pushed == 1 and out.verified == 1  # rebuilt by full push, did not crash


def test_manifest_write_failure_fails_closed(seeded, tmp_path):
    # the manifest-write guard: push + delta-verify + prune all succeed but the final manifest put fails ->
    # ok=False (never certify a mirror whose manifest never persisted).
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    out = _driver(_FakeSftp(fail_manifest_put=True))(repo.engine, _DEST)
    assert out.ok is False and "manifest write failed" in out.detail


def test_prune_failure_fails_closed(seeded, tmp_path):
    # the prune guard: a transport failure during the -rm batch must fail the mirror CLOSED (else an orphan is
    # leaked while the signal greens).
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    sftp = _FakeSftp()
    _driver(sftp)(repo.engine, _DEST)  # run 1 mirrors the artifact
    art = manifest_artifacts(repo.engine, run_id=repo_run(repo))[0]
    with repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.artifacts SET deleted_at = now() WHERE uri = :u"), {"u": art.uri})
    sftp.fail_rm = True  # the prune batch will fail on run 2
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok is False and "prune failed" in out.detail


def test_delta_verify_fetch_failure_fails_closed(seeded, tmp_path):
    # the delta-verify fetch-back branch (rc!=0 / not exists), distinct from the sha MISMATCH branch: a
    # fetch-back transport failure fails the mirror CLOSED.
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    art = manifest_artifacts(repo.engine, run_id=repo_run(repo))[0]
    sftp = _FakeSftp(fail_get=f"{LAKE_SUBDIR}/{art.uri}")  # the verify fetch-back of this blob returns rc=1
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok is False and "delta-verify fetch" in out.detail


def test_unregistered_lane_fails_closed(seeded, tmp_path):
    # MAJOR-3b: a typo'd / unregistered lane_backend_key must fail CLOSED (a ConfigurationError), never green a
    # mirror of a lane that does not exist.
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    with pytest.raises(ConfigurationError, match="unregistered/typo"):
        _driver(_FakeSftp(), lane_backend_key="artifact-prod")(repo.engine, _DEST)  # dropped 's'


def test_registered_but_empty_lane_greens_with_checked_zero(seeded, tmp_path):
    # MAJOR-3b: a REGISTERED-but-empty lane greens (nothing to mirror) but records checked=0 — distinguishable
    # from a populated fresh mirror, and it does not false-alarm at provisioning.
    repo = seeded["repo"]
    root = tmp_path / "empty-lake"
    root.mkdir()
    repo.get_or_create_storage_backend(
        "artifacts-prod", driver="local_fs", lane="artifacts", base_uri=str(root), is_cloud=False
    )
    out = _driver(_FakeSftp())(repo.engine, _DEST)
    assert out.ok and out.checked == 0 and out.pushed == 0 and "0 live artifacts" in out.detail


def test_mirror_excludes_other_lanes_deblind(seeded, tmp_path):
    # MAJOR-5a de-blind: a SECOND lane (artifacts-dev, storage_lane 'artifacts') with its own artifact must NOT
    # be mirrored — the backend_key=:lane filter is the SOLE enforcer of the prod-only ruling. A mutation to
    # `sb.lane='artifacts'` (which matches prod+dev) would push the dev blob and redden this.
    repo = seeded["repo"]
    _prod_lake(repo, tmp_path)
    dev_root = tmp_path / "dev-lake"
    dev_root.mkdir()
    dev_backend = LocalFsBackend(dev_root)
    dev_id = repo.get_or_create_storage_backend(
        "artifacts-dev", driver="local_fs", lane="artifacts", base_uri=str(dev_root), is_cloud=False
    )
    _seed_artifact(
        repo,
        dev_backend,
        dev_id,
        repo_run(repo),
        shard="lp-dev-0.parquet",
        partition="logprobs/dev/0",
        generated=2000,
    )
    sftp = _FakeSftp()
    out = _driver(sftp)(repo.engine, _DEST)
    assert out.ok and out.checked == 1 and out.pushed == 1  # ONLY the prod artifact
    # the dev artifact's blob is NOT anywhere in the mirror
    dev_art = next(r for r in _all_artifacts(repo) if r["base_uri"] == str(dev_root))
    assert not any(dev_art["uri"] in remote_path for remote_path in sftp.remote)


def _all_artifacts(repo):
    with repo.engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT a.uri, sb.base_uri FROM neuro.artifacts a "
                    "JOIN neuro.storage_backends sb ON sb.backend_id = a.backend_id "
                    "WHERE a.deleted_at IS NULL"
                )
            )
            .mappings()
            .all()
        )


# ---- the producer: seed-check / bump / persist-before-raise / off-cloud guard --------------------------


def _ok_driver(_engine, _dest):
    return LakeMirrorOutcome(ok=True, detail="scripted mirror ok", checked=1, pushed=1, deleted=0, verified=1)


def _blocked_driver(_engine, _dest):
    return LakeMirrorOutcome(
        ok=False, detail="scripted mirror blocked", checked=1, pushed=0, deleted=0, verified=0
    )


def _lake_status(engine):
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT status, measured_at FROM neuro.system_health WHERE health_key = :k"),
            {"k": LAKE_MIRROR_FRESHNESS_KEY},
        ).one()


def test_probe_unseeded_row_raises_before_driver(seeded):
    # the FOLD-4 order: the seed-check aborts BEFORE the injected driver runs (a real mirror is never orphaned
    # with nowhere to record it). The lake row is NOT seeded here even though the driver would succeed.
    repo = seeded["repo"]
    called = {"driver": False}

    def _spy(_engine, _dest):
        called["driver"] = True
        return _ok_driver(_engine, _dest)

    with pytest.raises(LakeMirrorProbeError, match="not seeded"):
        run_lake_mirror_probe(repo.engine, lake_mirror_driver=_spy, destination=_DEST)
    assert called["driver"] is False  # aborted before touching the driver


def test_probe_ok_bumps_measured_at(seeded):
    repo = seeded["repo"]
    with repo.engine.connect() as conn:
        seed_all(conn)
    before = _lake_status(repo.engine).measured_at
    out = run_lake_mirror_probe(repo.engine, lake_mirror_driver=_ok_driver, destination=_DEST)
    assert out.ok
    after = _lake_status(repo.engine)
    assert after.status == "ok" and after.measured_at > before  # advanced only on verified success


def test_probe_failure_records_blocked_and_raises(seeded):
    repo = seeded["repo"]
    with repo.engine.connect() as conn:
        seed_all(conn)
    before = _lake_status(repo.engine).measured_at  # born epoch
    with pytest.raises(LakeMirrorProbeError, match="FAILED/unverified"):
        run_lake_mirror_probe(repo.engine, lake_mirror_driver=_blocked_driver, destination=_DEST)
    status = _lake_status(repo.engine)
    assert status.status == "blocked"  # persisted BEFORE the raise (persist-before-raise)
    # ★ a blocked bump must LEAVE measured_at (staleness accrues from the last-good mirror) — else the daily
    #   escalation's `now()-measured_at > onset` never trips and the persistent-block re-alert silently dies.
    assert status.measured_at == before
    with repo.engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM neuro.probe_reports WHERE probe_key = :k AND status='blocked'"),
            {"k": LAKE_MIRROR_FRESHNESS_KEY},
        ).scalar_one()
    assert n == 1  # a human-audit probe_reports row landed


def test_probe_driver_raises_records_blocked_and_reraises(seeded):
    repo = seeded["repo"]
    with repo.engine.connect() as conn:
        seed_all(conn)

    def _raiser(_engine, _dest):
        raise RuntimeError("driver blew up")

    with pytest.raises(LakeMirrorProbeError, match="driver raised"):
        run_lake_mirror_probe(repo.engine, lake_mirror_driver=_raiser, destination=_DEST)
    assert _lake_status(repo.engine).status == "blocked"  # recorded before re-raising loud


def test_probe_rejects_azure_destination(seeded):
    # ADR-0014 off-cloud independence: an Azure-shaped destination is refused BEFORE the driver runs.
    repo = seeded["repo"]
    with repo.engine.connect() as conn:
        seed_all(conn)
    with pytest.raises(ConfigurationError):
        run_lake_mirror_probe(
            repo.engine,
            lake_mirror_driver=_ok_driver,
            destination="https://acct.blob.core.windows.net/lake",
        )
