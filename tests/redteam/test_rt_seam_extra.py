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

from neuromancer_llm.bundles.bundlespec import sha256_hex
from neuromancer_llm.bundles.registrar import (
    BundleRegistrar,
    CrashInjected,
    SeamIntegrityError,
    SeamStateError,
)
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


def test_rt_bundle_backend_id_drift_raises(seeded, tmp_path):
    """C2 (audit 2026-07-02): on a bundle_uuid conflict the existing row's backend_id (and run_id) must
    be COMPARED to the request — the only registry not following the raise-on-drift idiom (FIX #8 / R2 /
    C10a). A re-register of the same bundle identity under a DIFFERENT backend raises SeamIntegrityError
    BEFORE any blob write (never adopt, never update); the stored pointer is unchanged."""
    repo = seeded["repo"]
    reg, bid = _register(seeded, tmp_path, ds="seam_be", pp="seam_be/p0")
    with repo.engine.connect() as conn:
        original_backend = conn.execute(
            text("SELECT backend_id FROM neuro.bundles WHERE bundle_id=:b"), {"b": bid}
        ).scalar_one()
    backend_b = repo.get_or_create_storage_backend(
        "lake-b", driver="local_fs", lane="artifacts", base_uri=str(tmp_path / "b"), is_cloud=False
    )
    assert backend_b != original_backend
    with pytest.raises(SeamIntegrityError):  # same bundle identity, different backend -> drift, refused
        reg.register(
            run_id=seeded["run_id"],
            backend_id=backend_b,
            dataset_name="seam_be",
            partition_path="seam_be/p0",
            shards=SHARDS,
        )
    with repo.engine.connect() as conn:  # the stored pointer was neither adopted nor re-pointed
        assert (
            conn.execute(
                text("SELECT backend_id FROM neuro.bundles WHERE bundle_id=:b"), {"b": bid}
            ).scalar_one()
            == original_backend
        )


def test_rt_per_artifact_sha_divergence_raises(seeded, tmp_path):
    """L9 (defense-in-depth): if a stored artifact sha256 diverges from the blob while the manifest still
    matches, the per-artifact compare (registrar's last line) raises SeamIntegrityError.

    ⚠ THE SETUP REACHES 'sealed' THE LEGAL WAY. It used to roll a fully-registered bundle BACK with
    `UPDATE ... SET state='sealed'`, which migration 0003 now refuses in-schema (registered->sealed is the
    seam-state resurrection L9 exists to forbid, and the trigger is faithful to the app: registrar.py's
    own CAS refuses it too). The equivalent legal shape is the crash window the seam already ships —
    `crash_at="before_register"` raises AFTER `_seal` has committed, leaving the bundle 'sealed' with NO
    artifacts — and the tampered artifact row is then inserted by hand at the registrar's OWN
    content-addressed key so the re-register's `ON CONFLICT (backend_id, uri)` lands on it.
    The first register call must BE the crashing one: on an already-registered bundle `_seal` returns
    early (idempotent re-seal) and the crash would leave the state 'registered', not 'sealed'."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    kw = {
        "run_id": seeded["run_id"],
        "backend_id": backend_id,
        "dataset_name": "seam_pa",
        "partition_path": "seam_pa/p0",
        "shards": SHARDS,
    }
    with pytest.raises(CrashInjected):  # W6: sealed, blobs on disk, nothing registered
        reg.register(**kw, crash_at="before_register")
    with repo.engine.begin() as conn:
        bid = conn.execute(text("SELECT bundle_id FROM neuro.bundles WHERE state='sealed'")).scalar_one()
        # The registrar's own key: {partition}/{sha256hex(bytes)}/{name} (FIX #7 content-addressing).
        # Deriving it here rather than hardcoding keeps the collision honest if the formula ever moves.
        name = "shard-0001.bin"
        uri = f"seam_pa/p0/{sha256_hex(SHARDS[name])}/{name}"
        conn.execute(
            text(
                "INSERT INTO neuro.artifacts (bundle_id, kind, backend_id, uri, sha256, size_bytes) "
                "VALUES (:b, 'other', :be, :u, :s, :sz)"
            ),
            {"b": bid, "be": backend_id, "u": uri, "s": b"\x00" * 32, "sz": len(SHARDS[name])},
        )
    # `match=` pins the BRANCH: three separate paths in register() raise SeamIntegrityError, so a bare
    # raises-check would go green if an earlier guard started firing and this branch went dead again.
    with pytest.raises(SeamIntegrityError, match="already registered with a different sha256"):
        reg.register(**kw)
