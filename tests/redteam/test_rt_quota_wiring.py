"""Red-team (Unit C4, cost-safety GO §6): the quota guard is enforced END TO END through the cloud put path
via QuotaGuardedBackend. A usage-sizing error on put DENIES the write (fail closed) — the upload never
happens and no spend row is written; and a cloud-DRIVER backend is quota-bound regardless of is_cloud (the
decoupling closed at the gate). The headline fail-closed probe is PURE (a fake repo), so it can never skip.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

import pytest
from sqlalchemy import text

from neuromancer_llm.registry.backends import QuotaGuardedBackend, guard_capture_backend
from neuromancer_llm.storage import quota

_PROD_URI = "https://neuromancerllm.blob.core.windows.net/artifacts-prod"


class _RaisingConn:
    def execute(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated: the SUM(size_bytes) sizing query failed on the put path")


class _RaisingEngine:
    @contextlib.contextmanager
    def begin(self):
        yield _RaisingConn()


class _FakeRepo:
    """A repo whose sizing query always raises; record_spend must NEVER run (the put is denied first)."""

    engine = _RaisingEngine()

    def record_spend(self, **kwargs: object) -> int:
        raise AssertionError("record_spend must not run when the quota consult fails closed")


class _SpyBackend:
    driver = "azure_blob"

    def __init__(self) -> None:
        self.put_called = False

    def put(self, key: str, data: bytes) -> str:
        self.put_called = True
        return key

    def get(self, key: str) -> bytes:  # pragma: no cover - not exercised
        raise KeyError(key)

    def exists(self, key: str) -> bool:  # pragma: no cover
        return False

    def delete(self, key: str) -> None:  # pragma: no cover
        pass


def test_rt_put_denied_on_quota_fail_closed():
    """A usage-sizing error on the cloud put path -> QuotaDeniedError (DENY) end to end: the upload does NOT
    happen and no spend row is written. A fail-OPEN wrapper (swallow the error and upload) is the exact
    vulnerability this closes; the price pin is the real committed one, isolating the sizing-error path."""
    inner = _SpyBackend()
    guarded = QuotaGuardedBackend(
        inner,
        repo=_FakeRepo(),
        prefix="artifacts-prod",
        backend_ids=[1],
        rate_card_id=1,
        rate=Decimal("0.0184"),
    )
    with pytest.raises(quota.QuotaDeniedError):
        guarded.put("shard-0000.bin", b"payload")
    assert inner.put_called is False, "the upload must NOT happen when the quota consult fails closed"


@pytest.mark.pg
def test_rt_cloud_driver_quota_bound_regardless_of_is_cloud(repo):
    """An azure_blob backend whose row carries is_cloud=FALSE (registered by DIRECT SQL, as the seam fixture
    does) is STILL wrapped/quota-bound — the gate keys on the cloud DRIVER, never is_cloud (they are
    decoupled). A gate keying on is_cloud would let this real-cloud row bypass the guard."""

    class _Mem:
        driver = "azure_blob"

        def put(self, key: str, data: bytes) -> str:
            return key

    with repo.engine.begin() as conn:
        bid = conn.execute(
            text(
                "INSERT INTO neuro.storage_backends (backend_key, driver, lane, base_uri, is_cloud) "
                "VALUES ('artifacts-prod', 'azure_blob', 'artifacts', :u, false) RETURNING backend_id"
            ),
            {"u": _PROD_URI},
        ).scalar_one()
    guarded = guard_capture_backend(
        repo=repo,
        backend_key="artifacts-prod",
        backend_id=bid,
        inner=_Mem(),
        rate_card_id=1,
        rate=Decimal("0.0184"),
    )
    assert isinstance(guarded, QuotaGuardedBackend), "cloud-driver row is quota-bound regardless of is_cloud"
