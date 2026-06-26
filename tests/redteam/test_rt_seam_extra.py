"""Red-team: W1-W8 seam crash-safety, the untested branches (L9).

* tombstone CAS resurrection — a 'tombstoned' bundle cannot be re-sealed/registered (SeamStateError);
  SeamStateError + the 'tombstoned' state had ZERO coverage.
* per-artifact sha compare (defense-in-depth) — a stored artifact sha that diverges from the blob is
  caught at registrar.py's last-line per-artifact compare (the earlier manifest guard normally fires
  first, leaving this branch dead-untested).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.registrar import BundleRegistrar, SeamIntegrityError, SeamStateError
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg

SHARDS = {"shard-0000.bin": b"first shard payload", "shard-0001.bin": b"second shard payload"}


def _register(seeded, tmp_path, *, ds="seam_rt", pp="seam_rt/p0"):
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    bid = reg.register(
        run_id=seeded["run_id"], backend_id=backend_id, dataset_name=ds, partition_path=pp, shards=SHARDS
    )
    return reg, bid


def test_rt_tombstoned_bundle_cannot_be_resurrected(seeded, tmp_path):
    """L9: a 'tombstoned' bundle (a real enum value) can never be re-sealed/registered — the register state
    CAS raises SeamStateError. SeamStateError + the 'tombstoned' state were entirely uncovered."""
    repo = seeded["repo"]
    reg, bid = _register(seeded, tmp_path)
    assert reg.bundle_state(bid) == "registered"
    with repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state='tombstoned' WHERE bundle_id=:b"), {"b": bid})
    with pytest.raises(SeamStateError):  # re-register a tombstoned bundle -> refused (no resurrection)
        reg.register(
            run_id=seeded["run_id"],
            backend_id=repo.get_or_create_storage_backend(
                "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
            ),
            dataset_name="seam_rt",
            partition_path="seam_rt/p0",
            shards=SHARDS,
        )


def test_rt_per_artifact_sha_divergence_raises(seeded, tmp_path):
    """L9 (defense-in-depth): if a stored artifact sha256 diverges from the blob while the manifest still
    matches, the per-artifact compare (registrar's last line) raises SeamIntegrityError. We force the
    inconsistent state (sealed bundle + tampered stored sha) so the normally-shadowed branch fires."""
    repo = seeded["repo"]
    reg, bid = _register(seeded, tmp_path, ds="seam_pa", pp="seam_pa/p0")
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    with repo.engine.begin() as conn:
        # roll the bundle back to 'sealed' so a re-register re-enters the artifacts loop, and tamper ONE
        # stored artifact sha so it disagrees with its (unchanged) blob bytes.
        conn.execute(text("UPDATE neuro.bundles SET state='sealed' WHERE bundle_id=:b"), {"b": bid})
        conn.execute(
            text("UPDATE neuro.artifacts SET sha256=:s WHERE bundle_id=:b AND uri LIKE '%shard-0001.bin'"),
            {"s": b"\x00" * 32, "b": bid},
        )
    with pytest.raises(SeamIntegrityError):
        reg.register(
            run_id=seeded["run_id"],
            backend_id=backend_id,
            dataset_name="seam_pa",
            partition_path="seam_pa/p0",
            shards=SHARDS,
        )
