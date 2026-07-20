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
from ..storage.price_pin import resolve_price
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
    read_timeout: float | None = None,
) -> StorageBackend:
    """Construct the storage adapter for a registered backend's `driver` (ADR-0040 seam; A2-4).

    local_fs  -> LocalFsBackend(local_root): the caller passes the host-specific root; base_uri is the
      logical id, not the path. A missing local_root fails closed.
    azure_blob -> AzureBlobBackend(container, connection_string): the container is base_uri's last path
      segment; the connection string comes from the argument or AZURE_STORAGE_CONNECTION_STRING and is
      REQUIRED (NO Azurite fallback — a cloud lane with no credential fails closed); the credential's account
      endpoint host must MATCH base_uri's host (identity cross-check); `read_timeout` (opt-in; default None ->
      the SDK default) bounds each socket read — B-7's lake mirror passes it so a hung Azure fetch fails closed.
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
        return AzureBlobBackend(container, connection_string=conn, read_timeout=read_timeout)

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
        backend_id: int,
        backend_ids: list[int],
        rate_card_id: int,
        rate: str | int | float | Decimal,
    ) -> None:
        self._inner = inner
        self.driver = inner.driver  # gate/introspection still sees the cloud driver
        # GO-D-cost fold 4: the wrapper is STAMPED with the storage_backends row it was resolved for, so the
        # choke points that also receive a backend_id (write_capture_event / registrar.register) can
        # cross-check `backend_id == backend.backend_id` and fail closed on a mis-threaded pair — the row
        # BINDING is structural, not just the wrap.
        self.backend_id = int(backend_id)
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


def _backend_row(conn, backend_key: str):
    """The ONE storage_backends row lookup by backend_key (GO-D-cost fold 10; one implementation per
    concept). Returns the (backend_id, driver, base_uri) row or None. `storage/quota.py::quota_report`
    carries the sanctioned second copy — the import direction (quota must not import registry) forbids
    sharing it."""
    return conn.execute(
        text("SELECT backend_id, driver, base_uri FROM neuro.storage_backends WHERE backend_key = :k"),
        {"k": backend_key},
    ).one_or_none()


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
            row = _backend_row(conn, member)
            if row is not None:
                ids.append(int(row.backend_id))
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
    """Wrap `inner` in a QuotaGuardedBackend when it is a CLOUD driver, gating on the DRIVER not is_cloud
    (they are decoupled — a real-cloud row can carry is_cloud=False, tests/seam). A local_fs backend is
    returned UNWRAPPED (not quota-bound); its repo is never consulted. An UNKNOWN or missing driver FAILS
    CLOSED (GO-D-cost fold 3 — the make_backend posture, mirrored: a default-allow `!= 'azure_blob'` gate
    would wave a future s3/typo'd driver through unguarded). For a cloud backend, resolve the R3 budget
    group's registered backend_ids first (fail closed on an un-budgeted prefix or an empty set — the C1
    bogus-id existence check) and STAMP the wrapper with its storage_backends row id (fold 4)."""
    driver = getattr(inner, "driver", None)
    if driver == "local_fs":
        return inner
    if driver != "azure_blob":
        raise ConfigurationError(
            f"guard_capture_backend: unknown/missing storage driver {driver!r} (fail closed; known: "
            "local_fs, azure_blob) — a driver this guard cannot classify must not reach a put."
        )
    budget_group_for_prefix(
        backend_key
    )  # fail closed (QuotaDeniedError) on an un-budgeted prefix, before repo
    assert repo is not None, "a cloud (azure_blob) backend requires a repo for the quota consult + spend"
    backend_ids = _resolve_group_backend_ids(repo, prefix=backend_key, writing_backend_id=backend_id)
    return QuotaGuardedBackend(
        inner,
        repo=repo,
        prefix=backend_key,
        backend_id=backend_id,
        backend_ids=backend_ids,
        rate_card_id=rate_card_id,
        rate=rate,
    )


def assert_cloud_put_guarded(backend: StorageBackend) -> None:
    """The C1 structural cost guard (GO-D-cost; the A2-17 invariant made positive): a backend headed for a
    put choke point that is NOT a plain local_fs backend MUST be a QuotaGuardedBackend — else no quota is
    consulted and no spend row lands for its cloud writes. ALLOWLIST, fail closed (fold 3): only the literal
    'local_fs' driver passes unguarded; 'azure_blob', any future/typo'd driver, and a MISSING `.driver` all
    refuse unless wrapped (the wrapper mirrors `.driver` and passes the isinstance — a concrete-class check
    on purpose; a runtime_checkable-Protocol isinstance is structurally spoofable). Called at ALL THREE
    cloud-put choke points: BundleRegistrar.__init__ (beside R1), capture_logprob entry (beside the A2-11b
    lane cross-check), and write_capture_event entry (covers the _spill put, which fires before any
    registrar exists). Raises ConfigurationError — a wiring/misconfiguration refusal, distinct from
    QuotaDeniedError (the guard's own budget verdict)."""
    if isinstance(backend, QuotaGuardedBackend):
        return
    if getattr(backend, "driver", None) == "local_fs":
        return
    raise ConfigurationError(
        f"cost-safety refusal (C1): backend {type(backend).__name__} with driver "
        f"{getattr(backend, 'driver', None)!r} is not a QuotaGuardedBackend — no cloud put without the "
        "fail-closed quota consult + spend ledger row. Resolve backends through "
        "registry.backends.resolve_capture_backend (never construct/inject a raw cloud backend)."
    )


# The rate_cards natural key for Azure Blob storage (INPUT #2; repository.get_or_create_rate_card seeds it).
STORAGE_RATE_CARD_KEY = "azure-blob-storage"


def _resolve_cloud_rate(repo: Repository) -> tuple[int, Decimal]:
    """Resolve the LEDGER rate basis from the LATEST effective `azure-blob-storage` rate_cards row —
    row-bound (I2: never a free param), fail closed on absence, and cross-checked against the committed
    STORAGE_PRICE_PIN (GO-input 2, owner-ruled 2026-07-11: the ROW is the auditable sentinel the ledger FKs
    to; the PIN is the authority — the freshness.py stale_after/BACKUP_STALE_AFTER shape). A reprice is ONE
    auditable commit: a new pin + a new effective_from row (`neuro storage seed` writes it); a row that
    drifts from the pin fails closed here. Decimal-vs-Decimal compare (fold 7): rate_cards.rate is
    Numeric(18,6), and Decimal('0.018400') == Decimal('0.0184') — a string compare would spuriously block."""
    pin = resolve_price()  # ConfigurationError if the pin is absent/expired — BEFORE any row read
    with repo.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rate_card_id, unit, rate FROM neuro.rate_cards "
                "WHERE backend_or_lane = :k AND effective_from <= now() "
                "ORDER BY effective_from DESC LIMIT 1"
            ),
            {"k": STORAGE_RATE_CARD_KEY},
        ).one_or_none()
    if row is None:
        raise ConfigurationError(
            f"no effective rate_cards row for {STORAGE_RATE_CARD_KEY!r} — the cloud spend ledger has no "
            "priced basis (fail closed). Seed it (`neuro storage seed`, registrar/admin) at provisioning."
        )
    if row.unit != "usd_per_gb":
        raise ConfigurationError(
            f"rate_cards row {row.rate_card_id} for {STORAGE_RATE_CARD_KEY!r} has unit {row.unit!r}, not "
            "'usd_per_gb' — the wrong basis for a bytes-derived quantity (fail closed)."
        )
    rate = Decimal(str(row.rate))
    if rate != pin:
        raise ConfigurationError(
            f"rate_cards row {row.rate_card_id} rate {rate} != the pinned STORAGE_PRICE_PIN {pin} (drift) — "
            "the row is the sentinel, the pin is the authority (GO-input 2). A reprice is one auditable "
            "commit: bump the pin AND seed a new effective_from row (`neuro storage seed`)."
        )
    return int(row.rate_card_id), rate


def resolve_capture_backend(
    repo: Repository,
    *,
    backend_key: str,
    local_root: str | os.PathLike[str] | None = None,
    connection_string: str | None = None,
) -> tuple[int, StorageBackend]:
    """THE row-bound wiring resolver (GO-D-cost C2+C4+I2): the ONE production path from a `backend_key` to a
    put-ready backend. Reads the storage_backends ROW (fail closed if missing — provisioning seeds rows via
    `neuro storage seed`; the resolver never creates them) -> make_backend on the ROW's driver/base_uri (the
    C4 endpoint cross-check now binds to the row, never a caller arg) -> for cloud, binds the rate-card row
    (I2) and wraps in the quota/spend guard stamped with the row id (fold 4). Returns (backend_id, backend).

    Ordering (fold 8): for the cloud branch every fail-closed check (budget group, rate card, unit, pin
    drift) runs BEFORE make_backend — AzureBlobBackend's constructor connects and auto-creates a missing
    container, and a REFUSED configuration must not perform cloud I/O first."""
    with repo.engine.connect() as conn:
        row = _backend_row(conn, backend_key)
    if row is None:
        raise ConfigurationError(
            f"no storage_backends row for backend_key {backend_key!r} (fail closed) — the resolver never "
            "creates rows; seed it at provisioning (`neuro storage seed`, registrar/admin)."
        )
    backend_id = int(row.backend_id)
    if row.driver == "local_fs":
        return backend_id, make_backend("local_fs", base_uri=row.base_uri, local_root=local_root)
    if row.driver == "azure_blob":
        budget_group_for_prefix(backend_key)  # fail closed on an un-budgeted prefix, BEFORE any cloud I/O
        rate_card_id, rate = _resolve_cloud_rate(repo)  # fail closed BEFORE any cloud I/O (fold 8)
        inner = make_backend("azure_blob", base_uri=row.base_uri, connection_string=connection_string)
        guarded = guard_capture_backend(
            repo,
            backend_key=backend_key,
            backend_id=backend_id,
            inner=inner,
            rate_card_id=rate_card_id,
            rate=rate,
        )
        return backend_id, guarded
    raise ConfigurationError(
        f"storage_backends row {backend_key!r} carries unknown driver {row.driver!r} (fail closed; known: "
        "local_fs, azure_blob)."
    )
