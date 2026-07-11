"""Unit C3 (A2-4 cost-safety GO §5) — the driver->backend factory + the fail-CLOSED cloud identity checks +
the local-lake portable base_uri (the cutover-wedge fix). The factory maps a storage_backends row's `driver`
to its adapter; a cloud lane with no connection string, or a credential whose account host disagrees with the
registered base_uri, FAILS CLOSED; a cloud-driver row must carry is_cloud=True. Two probes need real Postgres
(`pg`); one needs Azurite (`seam`); the fail-closed/cross-check/factory-routing probes are pure.
"""

from __future__ import annotations

import pytest

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.registry.backends import (
    LOCAL_LAKE_BACKEND_KEY,
    LOCAL_LAKE_BASE_URI,
    make_backend,
)
from neuromancer_llm.storage.backends import (
    AZURITE_DEFAULT_CONNECTION_STRING,
    AzureBlobBackend,
    LocalFsBackend,
)

# A real-Azure-style base_uri for the account we provision (host = <account>.blob.core.windows.net).
_AZ_BASE = "https://neuromancerllm.blob.core.windows.net/artifacts-prod"


def test_factory_azure_blob_no_conn_string_raises(monkeypatch):
    """azure_blob with NO connection string -> ConfigurationError (fail closed), never the silent Azurite
    fallback of storage/backends.py:74-79 — a misconfigured cloud lane must not write to localhost."""
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    with pytest.raises(ConfigurationError):
        make_backend("azure_blob", base_uri=_AZ_BASE)


def test_factory_endpoint_base_uri_mismatch_raises():
    """The connection string's account endpoint host must MATCH the registered base_uri host; a mismatch ->
    ConfigurationError (the identity cross-check, independent of the credential in hand — this is exactly the
    predecessor `studyqueryllm` silent cross-account interleave hazard, closed)."""
    wrong_conn = (
        "DefaultEndpointsProtocol=https;AccountName=studyqueryllm;"
        "AccountKey=Zm9vYmFy==;EndpointSuffix=core.windows.net"
    )
    with pytest.raises(ConfigurationError):
        make_backend("azure_blob", base_uri=_AZ_BASE, connection_string=wrong_conn)


def test_factory_returns_adapter_per_driver(tmp_path):
    """The factory returns the right adapter per driver: local_fs -> LocalFsBackend; an unknown driver ->
    ConfigurationError; a local_fs with no root -> ConfigurationError (all fail closed)."""
    local = make_backend("local_fs", base_uri=LOCAL_LAKE_BASE_URI, local_root=tmp_path)
    assert isinstance(local, LocalFsBackend)
    with pytest.raises(ConfigurationError):
        make_backend("s3", base_uri="s3://bucket/key")
    with pytest.raises(ConfigurationError):
        make_backend("local_fs", base_uri=LOCAL_LAKE_BASE_URI)  # no local_root


@pytest.mark.pg
def test_local_lake_base_uri_portable_on_second_host(repo):
    """The capture lane records a host-INDEPENDENT base_uri for local-lake, so a second host does NOT trip
    get_or_create_storage_backend's raise-on-drift (the cutover wedge, audit 2026-07-02). Contrast: a
    machine-absolute base_uri (the pre-fix behavior) DOES drift across hosts."""
    a = repo.get_or_create_storage_backend(
        LOCAL_LAKE_BACKEND_KEY,
        driver="local_fs",
        lane="artifacts",
        base_uri=LOCAL_LAKE_BASE_URI,
        is_cloud=False,
    )
    b = repo.get_or_create_storage_backend(  # "a second host" registers the SAME portable base_uri
        LOCAL_LAKE_BACKEND_KEY,
        driver="local_fs",
        lane="artifacts",
        base_uri=LOCAL_LAKE_BASE_URI,
        is_cloud=False,
    )
    assert a == b, "host-independent base_uri -> no drift, idempotent"
    assert LOCAL_LAKE_BASE_URI == "file://local-lake"  # carries no machine-absolute path
    # CONTRAST: a machine-absolute base_uri (the wedge this fix closes) DOES drift across hosts.
    repo.get_or_create_storage_backend(
        "wedge-demo", driver="local_fs", lane="artifacts", base_uri="/host-a/_lake", is_cloud=False
    )
    with pytest.raises(IdentityMismatchError):
        repo.get_or_create_storage_backend(
            "wedge-demo", driver="local_fs", lane="artifacts", base_uri="/host-b/_lake", is_cloud=False
        )


@pytest.mark.pg
def test_azure_blob_requires_is_cloud_true(repo):
    """An azure_blob storage_backend row registered with is_cloud=False is REFUSED at the registration choke
    point (the driver/is_cloud decoupling closed at the source, so a cost gate keying on the cloud driver /
    is_cloud cannot be fooled by a decoupled row). is_cloud=True is accepted."""
    ok = repo.get_or_create_storage_backend(
        "cloud-ok", driver="azure_blob", lane="artifacts", base_uri=_AZ_BASE, is_cloud=True
    )
    assert ok > 0
    with pytest.raises(IdentityMismatchError):
        repo.get_or_create_storage_backend(
            "cloud-bad", driver="azure_blob", lane="artifacts", base_uri=_AZ_BASE, is_cloud=False
        )


@pytest.mark.seam
def test_factory_azure_blob_adapter_against_azurite():
    """(Disclosed extra; seam) azure_blob -> AzureBlobBackend constructed against Azurite (path-style
    endpoint host 127.0.0.1 matches the base_uri host), with a working put/get round-trip — the factory's
    azure adapter path exercised end to end."""
    base = "http://127.0.0.1:10000/devstoreaccount1/factory-adapter-test"
    backend = make_backend("azure_blob", base_uri=base, connection_string=AZURITE_DEFAULT_CONNECTION_STRING)
    assert isinstance(backend, AzureBlobBackend)
    backend.put("k.bin", b"factory-roundtrip")
    assert backend.get("k.bin") == b"factory-roundtrip"


def test_capture_write_commands_record_portable_base_uri():
    """§5.2(a) is only fixed if the wedge fix is APPLIED in the capture WRITE commands: cli/capture.py must
    record the portable LOCAL_LAKE_BASE_URI and build through the ROW-BOUND resolver (GO-D-cost C2 — which
    itself constructs via the factory, so this is the §5.2(a) guard STRENGTHENED), NOT a machine-absolute
    str(Path(lake_root).resolve()) or a hand-rolled backend. Guards a revert of either rewire (which the
    repo-layer portability probe alone would NOT catch — it never exercises capture.py)."""
    import pathlib

    from neuromancer_llm import cli

    src = (pathlib.Path(cli.__file__).parent / "capture.py").read_text(encoding="utf-8")
    assert "LOCAL_LAKE_BASE_URI" in src, "the capture WRITE commands must register the portable base_uri"
    assert "resolve_capture_backend(" in src, (
        "the capture WRITE commands must build the backend via the row-bound resolver (GO-D-cost C2)"
    )
    assert "make_backend(" not in src, (
        "no direct factory call in cli/capture.py — the resolver is the ONE production path (C2)"
    )
    assert "str(Path(lake_root).resolve())" not in src, "the machine-absolute base_uri wedge must be gone"


def test_factory_endpoint_host_is_case_insensitive():
    """DNS hosts are case-insensitive: a mixed-case AccountName must not spuriously FAIL the endpoint
    cross-check (which compares against urlparse's already-lowercased base_uri host). _azure_endpoint_host
    normalizes both branches, so a genuinely matching account is never over-rejected on case alone."""
    from neuromancer_llm.registry.backends import _azure_endpoint_host

    conn = (
        "DefaultEndpointsProtocol=https;AccountName=NeuromancerLLM;"
        "EndpointSuffix=Core.Windows.Net;AccountKey=Zm9vYmFy=="
    )
    assert _azure_endpoint_host(conn) == "neuromancerllm.blob.core.windows.net"
