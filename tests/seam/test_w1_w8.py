"""W1-W8 write-ordering kill-tests against real Azurite + Postgres (the seam is a correctness property).

Crash between every pair of ordered steps and assert the left-state + recovery:
  * unsealed windows (W2/W3/W5)  -> reaper GCs the bundle (never registered)
  * sealed windows  (W6/W7)      -> bundle is GC-EXEMPT; a resumed worker re-registers idempotently
  * after register  (W8)         -> fully durable; only job-complete remains

Plus integrity (R2/R-GC): re-register with DIFFERENT bytes raises; the GC grace window protects a bundle
a worker is mid-registering.

GO-D-cost (GO-input 4, owner-ruled 2026-07-11): the fixture backend is built through the REAL guarded path
(resolve_capture_backend on the budgeted 'artifacts-dev' prefix, explicit Azurite conn string per C5), so
the registrar's C1 guard passes and every seam put ALSO consults quota + writes a spend_entries row inside
the crash-injection kill windows — deliberate and harmless: the W1-W8 assertions count bundles/artifacts,
never spend_entries, and the crash points fire inside register() exactly as before (pinned by
test_kill_windows_fire_through_the_guarded_backend below).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.gc import collect_unsealed
from neuromancer_llm.bundles.registrar import BundleRegistrar, CrashInjected, SeamIntegrityError
from neuromancer_llm.registry.backends import (
    STORAGE_RATE_CARD_KEY,
    QuotaGuardedBackend,
    resolve_capture_backend,
)
from neuromancer_llm.storage.backends import AZURITE_DEFAULT_CONNECTION_STRING
from neuromancer_llm.storage.price_pin import resolve_price

pytestmark = pytest.mark.seam

SHARDS = {"shard-0000.bin": b"\x00\x01\x02logprob-bytes", "shard-0001.bin": b"second shard payload"}

# Explicit Azurite credential (C5: the backend class no longer reads env / falls back — fixtures thread it).
_AZURITE_CONN = os.environ.get("NEURO_TEST_AZURITE") or AZURITE_DEFAULT_CONNECTION_STRING
# The registered base_uri must cross-check against the credential's endpoint host (make_backend, C4):
# Azurite is path-style — host 127.0.0.1, container = the last path segment.
_SEAM_BASE_URI = "http://127.0.0.1:10000/devstoreaccount1/seamtest"


@pytest.fixture
def seam_env(seeded):
    repo = seeded["repo"]
    engine = repo.engine
    # The budgeted R3 prefix (GO-input 4a: 'artifacts-dev', never an unruled _BUDGET_GROUPS edit) + the
    # rate card the spend ledger FKs to. Registered through the repo helper, which ENFORCES driver<->is_cloud
    # coherence (the decoupled is_cloud=False posture is pinned separately, via direct SQL, in
    # test_rt_quota_wiring::test_rt_cloud_driver_quota_bound_regardless_of_is_cloud).
    repo.get_or_create_storage_backend(
        "artifacts-dev", driver="azure_blob", lane="scratch", base_uri=_SEAM_BASE_URI, is_cloud=True
    )
    repo.get_or_create_rate_card(
        backend_or_lane=STORAGE_RATE_CARD_KEY,
        unit="usd_per_gb",
        rate=resolve_price(),
        effective_from=datetime(2026, 7, 5, tzinfo=UTC),  # == the pin's retrieved_at (the seed CLI default)
    )
    backend_id, backend = resolve_capture_backend(
        repo, backend_key="artifacts-dev", connection_string=_AZURITE_CONN
    )
    assert isinstance(backend, QuotaGuardedBackend)  # the seam now runs the REAL guarded cloud path
    reg = BundleRegistrar(engine, backend, expected_lane="test")
    return {
        "engine": engine,
        "backend": backend,
        "backend_id": backend_id,
        "run_id": seeded["run_id"],
        "reg": reg,
    }


def test_happy_path_registers(seam_env):
    reg = seam_env["reg"]
    bid = reg.register(
        run_id=seam_env["run_id"],
        backend_id=seam_env["backend_id"],
        dataset_name="seam_happy",
        partition_path="seam_happy/p0",
        shards=SHARDS,
    )
    assert reg.bundle_state(bid) == "registered"
    assert reg.artifact_count(bid) == len(SHARDS)
    assert seam_env["backend"].exists("seam_happy/p0/manifest.json")
    # FIX #7: shard keys are content-addressed (run/partition prefix preserved); resolve via the FK, not path
    with seam_env["engine"].connect() as conn:
        uris = (
            conn.execute(text("SELECT uri FROM neuro.artifacts WHERE bundle_id = :b"), {"b": bid})
            .scalars()
            .all()
        )
    assert len(uris) == len(SHARDS)
    for uri in uris:
        assert uri.startswith("seam_happy/p0/") and seam_env["backend"].exists(uri)


@pytest.mark.parametrize("crash_at", ["after_first_shard", "after_shards", "before_seal"])
def test_unsealed_window_is_reaped(seam_env, crash_at):
    reg = seam_env["reg"]
    ds = f"seam_unsealed_{crash_at}"
    with pytest.raises(CrashInjected):
        reg.register(
            run_id=seam_env["run_id"],
            backend_id=seam_env["backend_id"],
            dataset_name=ds,
            partition_path=f"{ds}/p0",
            shards=SHARDS,
            crash_at=crash_at,
        )
    # left-state: unsealed, nothing registered
    with seam_env["engine"].connect() as conn:
        state = conn.execute(
            text(
                "SELECT state FROM neuro.bundles WHERE bundle_uuid IS NOT NULL ORDER BY bundle_id DESC LIMIT 1"
            )
        ).scalar_one()
        artifacts = conn.execute(text("SELECT count(*) FROM neuro.artifacts")).scalar_one()
    assert state == "unsealed"
    assert artifacts == 0
    # the reaper collects the unsealed bundle once past its grace window (grace=0 simulates aged-out)
    assert collect_unsealed(seam_env["engine"], grace=timedelta(0)) >= 1
    with seam_env["engine"].connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM neuro.bundles WHERE state = 'unsealed'")
        ).scalar_one()
    assert remaining == 0


def test_gc_grace_window_protects_inflight(seam_env):
    """R-GC: a fresh unsealed bundle (a worker mid-registration) is NOT collected within the grace window."""
    reg = seam_env["reg"]
    with pytest.raises(CrashInjected):
        reg.register(
            run_id=seam_env["run_id"],
            backend_id=seam_env["backend_id"],
            dataset_name="seam_grace",
            partition_path="seam_grace/p0",
            shards=SHARDS,
            crash_at="before_seal",
        )
    # default grace (1h): the just-created unsealed bundle is protected — NOT collected.
    assert collect_unsealed(seam_env["engine"]) == 0
    with seam_env["engine"].connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM neuro.bundles WHERE state = 'unsealed'")).scalar_one()
            == 1
        )
    # aged past the window (grace=0), it IS collectible.
    assert collect_unsealed(seam_env["engine"], grace=timedelta(0)) >= 1


@pytest.mark.parametrize("crash_at", ["before_register", "mid_register"])
def test_sealed_survives_gc_and_resumes(seam_env, crash_at):
    reg = seam_env["reg"]
    ds = f"seam_sealed_{crash_at}"
    pp = f"{ds}/p0"
    with pytest.raises(CrashInjected):
        reg.register(
            run_id=seam_env["run_id"],
            backend_id=seam_env["backend_id"],
            dataset_name=ds,
            partition_path=pp,
            shards=SHARDS,
            crash_at=crash_at,
        )
    with seam_env["engine"].connect() as conn:
        bid = conn.execute(text("SELECT bundle_id FROM neuro.bundles WHERE state = 'sealed'")).scalar_one()
    assert reg.bundle_state(bid) == "sealed"
    assert reg.artifact_count(bid) == 0  # W7 txn rolled back / W6 never ran

    # sealed bundle is GC-EXEMPT (ADR-0010) — even with grace=0 the reaper must NOT eat it
    assert collect_unsealed(seam_env["engine"], grace=timedelta(0)) == 0
    assert reg.bundle_state(bid) == "sealed"

    # resume: idempotent re-register completes (same stable bundle_uuid, IDENTICAL bytes)
    bid2 = reg.register(
        run_id=seam_env["run_id"],
        backend_id=seam_env["backend_id"],
        dataset_name=ds,
        partition_path=pp,
        shards=SHARDS,
    )
    assert bid2 == bid
    assert reg.bundle_state(bid) == "registered"
    assert reg.artifact_count(bid) == len(SHARDS)


def test_reregister_divergent_bytes_raises(seam_env):
    """R2: re-register is idempotent for IDENTICAL bytes but fails loud for DIFFERENT bytes (same identity)."""
    reg = seam_env["reg"]
    ds, pp = "seam_divergent", "seam_divergent/p0"
    reg.register(
        run_id=seam_env["run_id"],
        backend_id=seam_env["backend_id"],
        dataset_name=ds,
        partition_path=pp,
        shards=SHARDS,
    )
    # same logical identity (run/dataset/partition) but DIFFERENT shard bytes -> integrity violation
    divergent = dict(SHARDS, **{"shard-0000.bin": b"TAMPERED bytes for the same key"})
    with pytest.raises(SeamIntegrityError):
        reg.register(
            run_id=seam_env["run_id"],
            backend_id=seam_env["backend_id"],
            dataset_name=ds,
            partition_path=pp,
            shards=divergent,
        )
    # the original blob bytes were NOT clobbered (the check fails loud BEFORE any overwrite). FIX #7: the
    # shard key is content-addressed, so the original blob lives under its own sha256 directory.
    from neuromancer_llm.bundles.bundlespec import sha256_hex

    orig_key = f"seam_divergent/p0/{sha256_hex(SHARDS['shard-0000.bin'])}/shard-0000.bin"
    assert seam_env["backend"].get(orig_key) == SHARDS["shard-0000.bin"]


def test_after_register_is_durable(seam_env):
    reg = seam_env["reg"]
    with pytest.raises(CrashInjected):
        reg.register(
            run_id=seam_env["run_id"],
            backend_id=seam_env["backend_id"],
            dataset_name="seam_w8",
            partition_path="seam_w8/p0",
            shards=SHARDS,
            crash_at="after_register",
        )
    # W8: the register txn already committed — fully durable; only job-complete was pending
    with seam_env["engine"].connect() as conn:
        bid = conn.execute(
            text("SELECT bundle_id FROM neuro.bundles WHERE state = 'registered'")
        ).scalar_one()
    assert reg.artifact_count(bid) == len(SHARDS)


def test_kill_windows_fire_through_the_guarded_backend(seam_env):
    """GO-input 4's crash-window pin: the quota/spend guard wrapping the seam backend must not absorb or
    reorder the kill windows. A W2 crash still fires AND the first shard's put ran the full guarded path —
    a spend_entries row landed BEFORE the crash (consult -> record -> upload -> CrashInjected)."""
    with seam_env["engine"].connect() as conn:
        spends_before = conn.execute(text("SELECT count(*) FROM neuro.spend_entries")).scalar_one()
    with pytest.raises(CrashInjected):
        seam_env["reg"].register(
            run_id=seam_env["run_id"],
            backend_id=seam_env["backend_id"],
            dataset_name="seam_guarded_kill",
            partition_path="seam_guarded_kill/p0",
            shards=SHARDS,
            crash_at="after_first_shard",
        )
    with seam_env["engine"].connect() as conn:
        spends_after = conn.execute(text("SELECT count(*) FROM neuro.spend_entries")).scalar_one()
    assert spends_after == spends_before + 1  # exactly the first (crashed-after) shard's ledger row
