"""Unit C4 (A2-5 cost-safety GO §6) — wire C1's quota guard + C2's spend into the cloud put path via a
QuotaGuardedBackend wrapper (guards register() AND _spill uniformly at the put() boundary; touches neither
the registrar nor the seam tests). guard_capture_backend wraps ONLY a cloud-DRIVER (azure_blob) backend
(gated on the driver, not is_cloud) and resolves the R3 budget group's REGISTERED backend_ids (the C1
bogus-id existence check); a local_fs backend is returned unwrapped (not quota-bound). Plus the real
`neuro storage quota` per-prefix report. Most probes need real Postgres (`pg`); the local + unknown-prefix
gates are pure.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from neuromancer_llm.registry.backends import QuotaGuardedBackend, guard_capture_backend
from neuromancer_llm.storage import quota
from neuromancer_llm.storage.backends import LocalFsBackend

_EF = dt.datetime(2026, 7, 5, tzinfo=dt.UTC)


class _MemBackend:
    """A minimal in-memory cloud-driver backend (put succeeds, stores bytes) — stands in for AzureBlobBackend
    so the wrapper's quota+spend behavior is exercised without a real cloud/Azurite endpoint."""

    driver = "azure_blob"

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        self.store[key] = data
        return key

    def get(self, key: str) -> bytes:
        return self.store[key]

    def exists(self, key: str) -> bool:
        return key in self.store

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


def _seed_rate_card(repo) -> int:
    return repo.get_or_create_rate_card(
        backend_or_lane="azure-blob-storage", unit="usd_per_gb", rate="0.0184", effective_from=_EF
    )


def _register_cloud(repo, prefix: str) -> int:
    return repo.get_or_create_storage_backend(
        prefix,
        driver="azure_blob",
        lane="artifacts",
        base_uri=f"https://neuromancerllm.blob.core.windows.net/{prefix}",
        is_cloud=True,
    )


def _insert_artifact(conn, *, backend_id: int, uri: str, size_bytes: int) -> None:
    conn.execute(
        text(
            "INSERT INTO neuro.artifacts (kind, backend_id, uri, sha256, size_bytes, retention) "
            "VALUES ('wire_payload', :be, :uri, :sha, :sz, 'ttl')"
        ),
        {"be": backend_id, "uri": uri, "sha": b"\x00" * 32, "sz": size_bytes},
    )


def test_local_driver_put_not_quota_bound(tmp_path):
    """A local_fs backend is returned UNWRAPPED — local writes are not quota-bound (gated on the cloud
    driver). The builder returns the bare backend before touching the DB, so repo is never consulted."""
    inner = LocalFsBackend(tmp_path)
    result = guard_capture_backend(
        repo=None, backend_key="local-lake", backend_id=1, inner=inner, rate_card_id=1, rate=Decimal("0.0184")
    )
    assert result is inner
    assert not isinstance(result, QuotaGuardedBackend)


def test_guard_fails_closed_on_unknown_prefix():
    """(Disclosed extra) the C4 obligation: resolving an UN-BUDGETED cloud prefix -> QuotaDeniedError (fail
    closed), so a cloud backend is never guarded against a bogus/empty backend-id set. Pure — budget_group_
    for_prefix raises before any DB access."""
    inner = _MemBackend()
    with pytest.raises(quota.QuotaDeniedError):
        guard_capture_backend(
            repo=None,
            backend_key="not-a-budgeted-prefix",
            backend_id=1,
            inner=inner,
            rate_card_id=1,
            rate=Decimal("0.0184"),
        )


@pytest.mark.pg
def test_cloud_put_writes_spend_row(repo):
    """A cloud put within budget uploads AND lands a matching spend_entries row whose quantity/amount reflect
    the object's BYTES (no cloud cost without a ledger row; the bytes are counted into the ledger)."""
    bid = _register_cloud(repo, "artifacts-prod")
    rc = _seed_rate_card(repo)
    inner = _MemBackend()
    guarded = guard_capture_backend(
        repo=repo,
        backend_key="artifacts-prod",
        backend_id=bid,
        inner=inner,
        rate_card_id=rc,
        rate=Decimal("0.0184"),
    )
    payload = (
        b"x" * 1_000_000
    )  # 1 MB -> 0.001 GB; amount = 0.001 * $0.0184 = $0.0000184 (0.000018 @ Numeric(18,6))
    key = guarded.put("shard-0000.bin", payload)
    assert inner.store[key] == payload, "the upload happened (within budget)"
    with repo.engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT quantity, amount, is_standing, run_id FROM neuro.spend_entries "
                    "WHERE rate_card_id = :rc"
                ),
                {"rc": rc},
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1, "a cloud put lands exactly one spend_entries row"
    assert Decimal(str(rows[0]["quantity"])) == Decimal("0.001"), "quantity reflects the object's GB"
    assert Decimal(str(rows[0]["amount"])) == Decimal("0.000018"), (
        "amount = GB * rate (rounded at Numeric(18,6))"
    )
    assert rows[0]["is_standing"] is True and rows[0]["run_id"] is None  # storage = standing infra charge


@pytest.mark.pg
def test_cloud_put_denied_over_budget_through_wrapper(repo):
    """The wrapper's headline — no cloud byte is written OVER budget. With usage seeded just under the ceiling,
    a put whose incoming bytes push the group AT/OVER the ceiling is DENIED end to end (exercising the
    wrapper's incoming_bytes=n wiring + the projected>=ceiling branch), and the upload does NOT happen."""
    bid = _register_cloud(repo, "upload-staging")  # the smallest R3 ceiling
    rc = _seed_rate_card(repo)
    ceiling = quota.byte_ceiling_for_prefix("upload-staging")
    with repo.engine.begin() as conn:
        _insert_artifact(conn, backend_id=bid, uri="seed", size_bytes=ceiling - 5)  # usage = ceiling - 5
    inner = _MemBackend()
    guarded = guard_capture_backend(
        repo=repo,
        backend_key="upload-staging",
        backend_id=bid,
        inner=inner,
        rate_card_id=rc,
        rate=Decimal("0.0184"),
    )
    with pytest.raises(quota.QuotaDeniedError):
        guarded.put("over.bin", b"x" * 10)  # (ceiling - 5) + 10 = ceiling + 5 >= ceiling -> DENY
    assert inner.store == {}, "the upload must NOT happen when the put would exceed the ceiling"


@pytest.mark.pg
def test_storage_quota_report_renders(repo):
    """`neuro storage quota` (quota_report) renders the real per-prefix report: every budgeted R3 prefix, its
    pin-resolved byte ceiling, and the current usage from the DB catalog (0 for an unregistered prefix)."""
    prod = _register_cloud(repo, "artifacts-prod")
    dev = _register_cloud(repo, "artifacts-dev")
    stage = _register_cloud(repo, "artifacts-stage")
    with repo.engine.begin() as conn:
        _insert_artifact(conn, backend_id=prod, uri="p", size_bytes=1000)
        _insert_artifact(conn, backend_id=dev, uri="d", size_bytes=200)  # dev + stage SHARE one ceiling
        _insert_artifact(conn, backend_id=stage, uri="s", size_bytes=300)
    with repo.engine.connect() as conn:
        lines = quota.quota_report(conn)
    by_prefix = {line.prefix: line for line in lines}
    assert set(by_prefix) == {
        "artifacts-prod",
        "artifacts-dev",
        "artifacts-stage",
        "db-backups",
        "upload-staging",
    }
    assert by_prefix["artifacts-prod"].used_bytes == 1000
    assert by_prefix["artifacts-prod"].byte_ceiling > 1000
    assert by_prefix["db-backups"].used_bytes == 0  # unregistered prefix -> 0 usage, still reported
    # the SHARED dev+stage group reports the COMBINED 500 B on BOTH lines (no false per-prefix headroom)
    assert by_prefix["artifacts-dev"].used_bytes == 500
    assert by_prefix["artifacts-stage"].used_bytes == 500
