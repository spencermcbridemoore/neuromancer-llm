"""Registry-driven storage adapters (ADR-0040). Azure (Azurite in CI) + local_fs now; S3 deferred
behind this seam. No Azure-only assumptions leak above this layer; the backend registry
(storage_backends table) chooses the driver. Quota guard fails CLOSED (storage/quota.py — Stage 2).
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from typing import Protocol, runtime_checkable

# The well-known Azurite account (devstoreaccount1). NOT a secret — Azurite's published default.
AZURITE_DEFAULT_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)


@runtime_checkable
class StorageBackend(Protocol):
    driver: str  # the registry driver id ('local_fs' / 'azure_blob'); the cost gate + factory dispatch on it

    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalFsBackend:
    """desktop-nvme / venue-scratch lane (also the default for unit tests without Azurite)."""

    driver = "local_fs"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)

    def _resolve(self, key: str) -> pathlib.Path:
        # R9: reject path traversal / absolute keys — the resolved path MUST stay within root. This
        # catches '..' escapes and absolute/drive-qualified keys (both make the resolution leave root).
        root = self.root.resolve()
        resolved = (self.root / key).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"unsafe storage key {key!r}: escapes the backend root")
        return resolved

    def put(self, key: str, data: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.exists():
            path.unlink()


class AzureBlobBackend:
    """azure-artifacts / db-backups lane. In CI this targets Azurite ONLY (never cloud; ADR-0040).

    C5 (GO-D-cost, owner-ruled 2026-07-11): the connection string is REQUIRED — no env read, no Azurite
    fallback. The class is credential-explicit; the ONE env read (AZURE_STORAGE_CONNECTION_STRING as
    infrastructure config) lives in registry/backends.py::make_backend, which also runs the endpoint
    cross-check — so construct THROUGH the factory (a src-scan pins this), and test fixtures pass the
    Azurite string explicitly (NEURO_TEST_AZURITE / AZURITE_DEFAULT_CONNECTION_STRING)."""

    driver = "azure_blob"

    def __init__(self, container: str, connection_string: str) -> None:
        # lazy imports keep the module importable without azure installed
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import BlobServiceClient

        if not connection_string:
            raise ValueError(
                "AzureBlobBackend requires an explicit connection string (C5: no env read, no Azurite "
                "fallback — a misconfigured cloud lane must fail closed, never write to localhost). "
                "Construct through registry.backends.make_backend."
            )
        self._service = BlobServiceClient.from_connection_string(connection_string)
        self._container = self._service.get_container_client(container)
        # R9: suppress ONLY the "container already exists" conflict — any other error propagates.
        with contextlib.suppress(ResourceExistsError):
            self._container.create_container()

    def put(self, key: str, data: bytes) -> str:
        self._container.upload_blob(name=key, data=data, overwrite=True)
        return key

    def get(self, key: str) -> bytes:
        return self._container.download_blob(key).readall()

    def exists(self, key: str) -> bool:
        return self._container.get_blob_client(key).exists()

    def delete(self, key: str) -> None:
        client = self._container.get_blob_client(key)
        if client.exists():
            client.delete_blob()
