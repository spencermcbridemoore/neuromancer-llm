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
from urllib.parse import urlparse

from ..db.lanes import ConfigurationError
from ..storage.backends import AzureBlobBackend, LocalFsBackend, StorageBackend

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
