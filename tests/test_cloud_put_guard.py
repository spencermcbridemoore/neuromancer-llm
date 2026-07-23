"""GO-D-cost C1: the structural cost guard — "no cloud PUT except through a QuotaGuardedBackend".

RED@4286e43 = ImportError (assert_cloud_put_guarded absent). Probes the owner-ruled ALLOWLIST shape
(§9 fold 3: only the literal 'local_fs' driver passes unguarded; 'azure_blob', any future/typo'd driver,
and a MISSING `.driver` all refuse unless wrapped — the permanent form of the driver='s3'/driver-less
mutation checks) at the helper AND at all three choke points (BundleRegistrar construction /
capture_logprob entry / write_capture_event entry), plus the fold-4 row binding (a guarded backend is
STAMPED with its storage_backends row; a mis-threaded backend_id fails closed BEFORE any write — the wrap
alone is not the invariant, the row binding is).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from neuromancer_llm.bundles.registrar import BundleRegistrar
from neuromancer_llm.capture.events import capture_logprob, write_capture_event
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.registry.backends import (
    QuotaGuardedBackend,
    assert_cloud_put_guarded,
    guard_capture_backend,
)
from neuromancer_llm.storage.backends import LocalFsBackend


class _FakeBackend:
    """A Protocol-satisfying backend with an arbitrary driver (or none) — the C1 adversary."""

    def __init__(self, driver: str | None) -> None:
        if driver is not None:
            self.driver = driver

    def put(self, key: str, data: bytes) -> str:
        return key

    def get(self, key: str) -> bytes:
        return b""

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None:
        pass


class _DudClient:
    """A vLLM stand-in that MUST NOT be reached — fold 9: the C1 refusal precedes any adapter/wire call."""

    def __init__(self) -> None:
        self.calls = 0

    def served_model(self) -> str:
        self.calls += 1
        raise AssertionError("C1 must refuse the raw cloud backend BEFORE any adapter call")


def _guarded(driver: str = "azure_blob", backend_id: int = 1) -> QuotaGuardedBackend:
    # Direct construction is fine in a PURE probe (no put ever runs here); production goes via the resolver.
    return QuotaGuardedBackend(
        _FakeBackend(driver),
        repo=None,  # type: ignore[arg-type] — never consulted (no put in these probes)
        prefix="artifacts-prod",
        backend_id=backend_id,
        backend_ids=[backend_id],
        rate_card_id=1,
        rate=Decimal("0.0184"),
    )


# ---- pure: the helper's allowlist (fold 3) ------------------------------------------------------------


def test_local_fs_passes_unguarded(tmp_path):
    assert assert_cloud_put_guarded(LocalFsBackend(tmp_path)) is None


def test_wrapped_cloud_passes():
    assert assert_cloud_put_guarded(_guarded()) is None  # the wrapper mirrors .driver and passes isinstance


@pytest.mark.parametrize("driver", ["azure_blob", "s3", None])
def test_raw_or_unknown_or_missing_driver_refused(driver):
    # 'azure_blob' raw = the headline C1; 's3' = the allowlist discriminator (a denylist on the literal
    # 'azure_blob' waves a future cloud driver through); None = getattr fails CLOSED (an introspection-less
    # backend could be cloud).
    with pytest.raises(ConfigurationError, match="not a QuotaGuardedBackend"):
        assert_cloud_put_guarded(_FakeBackend(driver))


@pytest.mark.parametrize("driver", ["s3", None])
def test_guard_capture_backend_refuses_unknown_and_missing_drivers(driver):
    # fold 3 second half: the WRAP side mirrors the posture — unknown/missing drivers RAISE, never
    # return-unwrapped (the old `!= 'azure_blob' -> pass through` default-allow shape).
    with pytest.raises(ConfigurationError, match="unknown/missing storage driver"):
        guard_capture_backend(
            None,
            backend_key="artifacts-prod",
            backend_id=1,
            inner=_FakeBackend(driver),
            rate_card_id=1,
            rate=Decimal("0.0184"),
        )


def test_guarded_wrapper_is_stamped_with_its_row():
    assert _guarded(backend_id=42).backend_id == 42  # fold 4: the row binding rides the wrapper


# ---- pg: the three choke points refuse a raw/mis-threaded backend --------------------------------------


@pytest.mark.pg
@pytest.mark.parametrize("driver", ["azure_blob", "s3", None])
def test_registrar_construction_refuses_unguarded_cloud(repo, driver):
    with pytest.raises(ConfigurationError):
        BundleRegistrar(repo.engine, _FakeBackend(driver), expected_lane="test")


@pytest.mark.pg
def test_registrar_register_refuses_misthreaded_backend_id(repo):
    # fold 4: the guarded backend is stamped backend_id=42; register(backend_id=7) is the split-brain
    # pointer — refused BEFORE any bundle row or blob write.
    reg = BundleRegistrar(repo.engine, _guarded(backend_id=42), expected_lane="test")
    with pytest.raises(ConfigurationError, match="disagrees with the resolved backend"):
        reg.register(run_id=1, backend_id=7, dataset_name="x", partition_path="x/p0", shards={"s.bin": b"b"})
    with repo.engine.connect() as conn:
        from sqlalchemy import text

        assert conn.execute(text("SELECT count(*) FROM neuro.bundles")).scalar_one() == 0  # nothing written


@pytest.mark.pg
@pytest.mark.parametrize("driver", ["azure_blob", "s3", None])
def test_write_capture_event_refuses_raw_cloud(repo, driver):
    with pytest.raises(ConfigurationError, match="not a QuotaGuardedBackend"):
        write_capture_event(
            repo.engine,
            _FakeBackend(driver),
            run_id=1,
            event_key="ek",
            model_id=1,
            actor_id=1,
            origin="t",
            request_body=b"r",
            response_body=b"r",
            backend_id=1,
            partition_path="p",
        )


@pytest.mark.pg
def test_write_capture_event_refuses_misthreaded_backend_id(repo):
    with pytest.raises(ConfigurationError, match="disagrees with the resolved backend"):
        write_capture_event(
            repo.engine,
            _guarded(backend_id=42),
            run_id=1,
            event_key="ek",
            model_id=1,
            actor_id=1,
            origin="t",
            request_body=b"r",
            response_body=b"r",
            backend_id=7,
            partition_path="p",
        )


@pytest.mark.pg
@pytest.mark.parametrize("driver", ["azure_blob", "s3", None])
def test_capture_logprob_refuses_raw_cloud_before_any_wire_call(repo, driver):
    # fold 9: the C1 callsite sits after the lane cross-check and BEFORE served_model() — a mis-wired cloud
    # lane costs zero adapter I/O (the dud client asserts it was never reached).
    client = _DudClient()
    with pytest.raises(ConfigurationError, match="not a QuotaGuardedBackend"):
        capture_logprob(
            repo=repo,
            backend=_FakeBackend(driver),
            backend_id=1,
            client=client,  # type: ignore[arg-type] — duck-typed stand-in; must never be called
            expected_lane="test",
            hf_repo="org/model",
            hf_revision="deadbeef",
            dtype_quant="bf16",
            tokenizer_hash=b"\x00" * 32,
            campaign_key="c",
            work_slug="w",
            variant_digest="v",
            actor_key="a",
            origin="t",
        )
    assert client.calls == 0
