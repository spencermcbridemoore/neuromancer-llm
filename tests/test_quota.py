"""Unit C1 (A2-3 cost-safety GO §3) — the per-prefix, dollar-calibrated storage quota guard fails CLOSED.

These pin the guard's cost-safety contract: it DENIES at/over a prefix's dollar ceiling, an absent/expired
price pin raises ConfigurationError BEFORE any DB sizing, and usage is the DB catalog's BILLABLE footprint
(SUM(artifacts.size_bytes) including tombstoned-but-unpurged rows — a logical deletion frees no billed
bytes). The fail-OPEN-on-usage-error headline lives in tests/redteam/test_rt_quota.py (a pure probe that can
never skip). Two probes here need real Postgres (`pg`); the price-pin probe is pure (a fake connection).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.storage import price_pin, quota


class _RaisingConn:
    """A stand-in DB connection whose sizing query always raises — proves the pin check runs BEFORE any
    DB sizing (if sizing ran, this AssertionError would surface instead of the expected ConfigurationError)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def execute(self, *args: object, **kwargs: object) -> object:
        raise self._exc


def _make_backend(repo, prefix: str) -> int:
    """Register one cloud storage_backend row == one container prefix (per-prefix == per-backend_id)."""
    return repo.get_or_create_storage_backend(
        prefix,
        driver="azure_blob",
        lane="artifacts",
        base_uri=f"https://neuromancerllm.blob.core.windows.net/{prefix}",
        is_cloud=True,
    )


def _insert_artifact(conn, *, backend_id: int, uri: str, size_bytes: int, deleted: bool) -> None:
    conn.execute(
        text(
            "INSERT INTO neuro.artifacts (kind, backend_id, uri, sha256, size_bytes, retention, deleted_at) "
            "VALUES ('wire_payload', :be, :uri, :sha, :sz, 'ttl', :del)"
        ),
        {
            "be": backend_id,
            "uri": uri,
            "sha": b"\x00" * 32,
            "sz": size_bytes,
            "del": dt.datetime.now(dt.UTC) if deleted else None,
        },
    )


@pytest.mark.pg
def test_quota_denies_at_or_over_ceiling(repo):
    """usage AT or OVER a prefix's dollar-calibrated byte ceiling -> QuotaDeniedError; just under -> allow."""
    backend_id = _make_backend(repo, "upload-staging")
    ceiling = quota.byte_ceiling_for_prefix("upload-staging")  # floor($0.50 / $0.0184) * 1e9
    assert ceiling > 0
    with repo.engine.begin() as conn:
        # just UNDER the ceiling -> allowed (returns None, no raise)
        _insert_artifact(conn, backend_id=backend_id, uri="under", size_bytes=ceiling - 1, deleted=False)
        assert quota.check_quota(conn, prefix="upload-staging", backend_ids=[backend_id]) is None
        # push AT the ceiling -> denied ("at or over")
        _insert_artifact(conn, backend_id=backend_id, uri="at", size_bytes=1, deleted=False)
        with pytest.raises(quota.QuotaDeniedError):
            quota.check_quota(conn, prefix="upload-staging", backend_ids=[backend_id])
        # and clearly OVER -> still denied
        _insert_artifact(conn, backend_id=backend_id, uri="over", size_bytes=ceiling, deleted=False)
        with pytest.raises(quota.QuotaDeniedError):
            quota.check_quota(conn, prefix="upload-staging", backend_ids=[backend_id])


@pytest.mark.pg
def test_quota_usage_counts_billable_bytes(repo):
    """usage_bytes sums size_bytes from the DB catalog; a logical tombstone (deleted_at) does NOT reduce it
    (tombstoned-but-unpurged bytes still cost) — and it is independent of any blob-store contents."""
    backend_id = _make_backend(repo, "artifacts-prod")
    with repo.engine.begin() as conn:
        _insert_artifact(conn, backend_id=backend_id, uri="live-1", size_bytes=100, deleted=False)
        _insert_artifact(conn, backend_id=backend_id, uri="tomb", size_bytes=200, deleted=True)
        _insert_artifact(conn, backend_id=backend_id, uri="live-2", size_bytes=300, deleted=False)
        used = quota.usage_bytes(conn, backend_id)
    assert used == 600, "tombstoned-but-unpurged bytes (200) must still be counted, not freed by deleted_at"


def test_quota_absent_price_pin_raises(monkeypatch):
    """An absent OR expired price pin -> ConfigurationError, raised BEFORE any DB sizing (a fail-closed
    config error, distinct from a per-write QuotaDeniedError). The guard resolves the pin first, so the
    tripwire connection (which would raise if sizing ran) is never touched."""
    original = price_pin.STORAGE_PRICE_PIN
    assert original is not None, "the committed pin must be present at rest"
    tripwire = _RaisingConn(
        AssertionError("DB sizing ran before the price-pin check (must fail closed first)")
    )

    # absent pin -> ConfigurationError, before any sizing
    monkeypatch.setattr(price_pin, "STORAGE_PRICE_PIN", None)
    with pytest.raises(ConfigurationError):
        quota.check_quota(tripwire, prefix="artifacts-prod", backend_ids=[1])

    # expired pin (past the not_after staleness bound) -> ConfigurationError, before any sizing
    expired = dict(original)
    expired["not_after"] = "2000-01-01T00:00:00Z"
    monkeypatch.setattr(price_pin, "STORAGE_PRICE_PIN", expired)
    with pytest.raises(ConfigurationError):
        quota.check_quota(tripwire, prefix="artifacts-prod", backend_ids=[1])


# ---- the repo3 budget line: NO FAKE GUARD (§A·72, 2026-08-28) --------------------------------------------


def test_no_non_azure_repo_prefix_has_entered_the_R3_map():
    """★ THE HONEST-REGISTER PIN (precedent E-4 made falsifiable): repo3's spend is bounded by a
    PROVIDER-SIDE spending cap and an owner-ruled figure in ops/runbook-repo3-b2.md, NOT by this map — and a
    future author must meet that reasoning before adding a prefix here.

    ⚠ READ THE REASON, BECAUSE THE OBVIOUS ONE IS WRONG. It is NOT "nothing routes pgbackrest bytes through
    QuotaGuardedBackend": that is true, but `db-backups` — the repo2 Azure container, written by pgbackrest
    and by nothing else — is ALREADY in this map, and `test_quota_wiring` pins that it reports
    `used_bytes == 0` for exactly that reason. So a documentary budget line for a pgbackrest cloud repo is an
    ACCEPTED shape here and db-backups is the precedent. What does not transfer is the CEILING'S DERIVATION:
    `byte_ceiling_for_prefix` resolves `storage/price_pin.py`, an Azure Hot-LRS eastus2 meter, so a B2 prefix
    would silently inherit a byte ceiling computed from the wrong provider's rate — a number that looks
    authoritative and is not.

    Adding any such prefix reddens this."""
    prefixes = {p.lower() for p in quota._CEILING_USD}
    for token in quota._NON_AZURE_REPO_PREFIXES_DELIBERATELY_ABSENT:
        assert not any(token in p for p in prefixes), (
            f"a prefix matching {token!r} entered _BUDGET_GROUPS: its byte ceiling would be derived from the "
            "Azure price pin, which is the wrong provider's rate. See the comment beside _BUDGET_GROUPS."
        )
    # non-vacuity (the D3 empty-glob idiom): the scan must actually be looking at a populated map, or the
    # loop above passes green while examining nothing.
    assert "db-backups" in prefixes and len(prefixes) == 5
