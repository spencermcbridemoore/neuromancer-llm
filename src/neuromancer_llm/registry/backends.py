"""storage_backends factory — driver -> adapter, data-driven (ADR-0040; A2-4, cost-safety GO §5).

Maps a `storage_backends` row's `driver` to its adapter, threading `base_uri` -> (container, endpoint host)
and the connection string from the environment. Cloud drivers FAIL CLOSED: an azure_blob backend with no
connection string RAISES (never the silent Azurite fallback of storage/backends.py:74-79 — a misconfigured
cloud lane must not write to localhost), and the connection string's account endpoint HOST must MATCH the
host in the registered base_uri (an identity cross-check independent of the credential in hand, closing the
predecessor `studyqueryllm` silent cross-account interleave hazard; AzureBlobBackend auto-adopts an existing
container, backends.py:82-84). Local drivers take a host-specific root path passed by the caller, so the
recorded base_uri stays a host-INDEPENDENT logical id (LOCAL_LAKE_BASE_URI) — the capture lane no longer
records a machine-absolute path under the stable `local-lake` key (the cutover wedge, audit 2026-07-02),
which would trip get_or_create_storage_backend's raise-on-drift on the first VM capture after the DB flip.

This module is env-behavior-free (it reads AZURE_STORAGE_CONNECTION_STRING as INFRASTRUCTURE config only —
the credential DSN, never a behavior switch); the driver policy lives in the storage_backends table, not env.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import text

from ..db.lanes import ConfigurationError
from ..storage.backends import AzureBlobBackend, LocalFsBackend, StorageBackend
from ..storage.quota import QuotaDeniedError, budget_group_for_prefix, check_quota

if TYPE_CHECKING:
    from ..db.repository import Repository

# The portable, host-INDEPENDENT base_uri the capture lane records for the local lake. The PHYSICAL root is
# host-specific (passed to LocalFsBackend as local_root); recording it in base_uri would drift across hosts
# and trip get_or_create_storage_backend's raise-on-drift (FIX #8) on the first VM capture. This logical id
# does not, so the local-lake identity is stable everywhere.
LOCAL_LAKE_BACKEND_KEY = "local-lake"
LOCAL_LAKE_BASE_URI = "file://local-lake"


def _azure_endpoint_host(connection_string: str) -> str:
    """The account's blob-endpoint HOST from an Azure connection string — from an explicit BlobEndpoint if
    present (Azurite path-style), else AccountName + '.blob.' + EndpointSuffix (real-Azure vhost-style).
    Fails closed if the string carries neither (cannot run the base_uri cross-check without it)."""
    parts = dict(kv.split("=", 1) for kv in connection_string.split(";") if "=" in kv)
    blob_endpoint = parts.get("BlobEndpoint")
    if blob_endpoint:
        host = urlparse(blob_endpoint).hostname
        if host:
            return host.lower()  # DNS hosts are case-insensitive; normalize for the cross-check compare
    account = parts.get("AccountName")
    if account:
        suffix = parts.get("EndpointSuffix", "core.windows.net")
        return f"{account}.blob.{suffix}".lower()
    raise ConfigurationError(
        "Azure connection string carries neither BlobEndpoint nor AccountName — cannot derive the account "
        "endpoint host for the base_uri cross-check (fail closed)."
    )


def make_backend(
    driver: str,
    *,
    base_uri: str,
    local_root: str | os.PathLike[str] | None = None,
    connection_string: str | None = None,
) -> StorageBackend:
    """Construct the storage adapter for a registered backend's `driver` (ADR-0040 seam; A2-4).

    local_fs  -> LocalFsBackend(local_root): the caller passes the host-specific root; base_uri is the
      logical id, not the path. A missing local_root fails closed.
    azure_blob -> AzureBlobBackend(container, connection_string): the container is base_uri's last path
      segment; the connection string comes from the argument or AZURE_STORAGE_CONNECTION_STRING and is
      REQUIRED (NO Azurite fallback — a cloud lane with no credential fails closed); the credential's account
      endpoint host must MATCH base_uri's host (identity cross-check).
    Any other driver fails closed.
    """
    if driver == "local_fs":
        if local_root is None:
            raise ConfigurationError(
                f"local_fs backend (base_uri={base_uri!r}) requires a local_root path (fail closed)."
            )
        return LocalFsBackend(local_root)

    if driver == "azure_blob":
        parsed = urlparse(base_uri)
        container = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not parsed.hostname or not container:
            raise ConfigurationError(
                f"azure_blob base_uri {base_uri!r} must be a URL with a host and a container path segment "
                "(e.g. https://<account>.blob.core.windows.net/<container>) — fail closed."
            )
        conn = connection_string or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if not conn:
            raise ConfigurationError(
                f"azure_blob backend for base_uri {base_uri!r} has NO connection string "
                "(AZURE_STORAGE_CONNECTION_STRING unset and none passed) — refusing to fall back to the "
                "local Azurite emulator for a cloud lane (fail closed; A2-4). A misconfigured cloud lane "
                "must not silently write to localhost."
            )
        cred_host = _azure_endpoint_host(conn)
        if cred_host != parsed.hostname:
            raise ConfigurationError(
                f"azure_blob endpoint mismatch: the connection string's account host {cred_host!r} does not "
                f"match the registered base_uri host {parsed.hostname!r} (fail closed; identity cross-check "
                "closing the silent cross-account interleave hazard)."
            )
        return AzureBlobBackend(container, connection_string=conn)

    raise ConfigurationError(f"unknown storage driver {driver!r} (fail closed; known: local_fs, azure_blob).")


# 1 GB == 10^9 bytes (decimal) — the usd_per_gb rate basis (matches the price meter's "1 GB/Month").
_GB_DECIMAL = Decimal(10**9)


class QuotaGuardedBackend:
    """Wraps a CLOUD StorageBackend so every put() CONSULTS the fail-CLOSED quota guard (C1) before the
    upload and RECORDS a spend ledger row (C2) — so no cloud byte is written over budget, and no cloud cost
    lands without a ledger row (A2-5). Because both the registrar and capture/_spill write via put(), one
    wrapper guards BOTH surfaces uniformly, touching neither. get/exists/delete delegate unchanged; `.driver`
    mirrors the inner cloud driver so downstream introspection is unchanged. Applied ONLY to cloud backends
    (guard_capture_backend); local writes are returned unwrapped and are not quota-bound.

    Order is fail-safe for cost: consult (deny -> nothing written) -> record the ledger row -> upload. The
    row therefore precedes the cost (the GO's 'no cost without a ledger row'); the rare cost of a record that
    is followed by a failed upload is an over-count (the conservative direction; the reconcile catches it).
    Per-object storage $ is sub-cent and rounds toward 0 at Numeric(18,6) — the row is provenance; the honest
    cost signal is the aggregate + the monthly reconcile (the C1 honest-limit note)."""

    def __init__(
        self,
        inner: StorageBackend,
        *,
        repo: Repository,
        prefix: str,
        backend_ids: list[int],
        rate_card_id: int,
        rate: str | int | float | Decimal,
    ) -> None:
        self._inner = inner
        self.driver = inner.driver  # gate/introspection still sees the cloud driver
        self._repo = repo
        self._prefix = prefix
        self._backend_ids = list(backend_ids)
        self._rate_card_id = rate_card_id
        self._rate = Decimal(str(rate))

    def put(self, key: str, data: bytes) -> str:
        n = len(data)
        with self._repo.engine.begin() as conn:
            check_quota(conn, prefix=self._prefix, backend_ids=self._backend_ids, incoming_bytes=n)
        gb = Decimal(n) / _GB_DECIMAL
        self._repo.record_spend(
            run_id=None,
            rate_card_id=self._rate_card_id,
            quantity=gb,
            amount=gb * self._rate,
            is_standing=True,
        )
        return self._inner.put(key, data)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def delete(self, key: str) -> None:
        self._inner.delete(key)


def _resolve_group_backend_ids(repo: Repository, *, prefix: str, writing_backend_id: int) -> list[int]:
    """Resolve the prefix's R3 budget group to the REGISTERED backend_ids to measure (the C1 bogus-id
    existence check, C4 side): an un-budgeted prefix fails closed (budget_group_for_prefix raises
    QuotaDeniedError); each group member that is registered is included; the writing backend is always
    measured; and the set is never empty/bogus (fail closed) — so check_quota is never handed absence of
    evidence. A member prefix not yet registered contributes 0 usage and is simply omitted (no under-count)."""
    members = budget_group_for_prefix(prefix)  # QuotaDeniedError (fail closed) if the prefix is un-budgeted
    ids: list[int] = []
    with repo.engine.connect() as conn:
        for member in members:
            backend_id = conn.execute(
                text("SELECT backend_id FROM neuro.storage_backends WHERE backend_key = :k"),
                {"k": member},
            ).scalar_one_or_none()
            if backend_id is not None:
                ids.append(int(backend_id))
    if writing_backend_id not in ids:
        ids.append(int(writing_backend_id))  # the backend being written is always measured
    if not ids:  # defensive: never certify a write against an empty/bogus backend set (the C1 fail-open)
        raise QuotaDeniedError(
            f"no registered backend resolves for the budget group of prefix {prefix!r} — refusing to guard "
            "a cloud write against an empty backend set (fail closed)."
        )
    return ids


def guard_capture_backend(
    repo: Repository | None,
    *,
    backend_key: str,
    backend_id: int,
    inner: StorageBackend,
    rate_card_id: int,
    rate: str | int | float | Decimal,
) -> StorageBackend:
    """Wrap `inner` in a QuotaGuardedBackend IFF it is a CLOUD driver (driver == 'azure_blob'), gating on the
    DRIVER not is_cloud (they are decoupled — a real-cloud row can carry is_cloud=False, tests/seam). A
    local_fs backend is returned UNWRAPPED (not quota-bound); its repo is never consulted. For a cloud
    backend, resolve the R3 budget group's registered backend_ids first (fail closed on an un-budgeted prefix
    or an empty set — the C1 bogus-id existence check)."""
    if inner.driver != "azure_blob":
        return inner
    budget_group_for_prefix(
        backend_key
    )  # fail closed (QuotaDeniedError) on an un-budgeted prefix, before repo
    assert repo is not None, "a cloud (azure_blob) backend requires a repo for the quota consult + spend"
    backend_ids = _resolve_group_backend_ids(repo, prefix=backend_key, writing_backend_id=backend_id)
    return QuotaGuardedBackend(
        inner,
        repo=repo,
        prefix=backend_key,
        backend_ids=backend_ids,
        rate_card_id=rate_card_id,
        rate=rate,
    )
