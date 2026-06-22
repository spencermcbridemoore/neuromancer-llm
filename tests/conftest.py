"""Shared fixtures + the marker detection/skip system.

The golden-snapshot + concurrency harness (Q15 KEEP) and the W1-W8 seam tests run against a
SESSION-SCOPED real-Postgres fixture (c1 finding 10): testcontainers `postgres:18` locally, or a
reused CI service container via NEURO_TEST_DATABASE_URL. There is no in-memory SQLite
(ADR-0039 Reconsidered 2026-06-17).

Marker detection hooks (gpu/api/network/pg/seam) skip VISIBLY when their capability is absent; the
expected-skips golden (tests/test_markers_expected_skips.py) pins the default behaviour so a marker can
never silently swallow the suite.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket

import pytest
from sqlalchemy import create_engine, text

# --- capability detection (the marker hooks) -------------------------------------------------------
_CAPABILITY_MARKERS = ("gpu", "api", "network", "pg", "seam")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def gpu_available() -> bool:
    return bool(os.environ.get("NEURO_TEST_GPU")) or shutil.which("nvidia-smi") is not None


def api_available() -> bool:
    return bool(os.environ.get("NEURO_TEST_API"))


def network_available() -> bool:
    return bool(os.environ.get("NEURO_TEST_NETWORK"))


def pg_available() -> bool:
    return bool(os.environ.get("NEURO_TEST_DATABASE_URL")) or _docker_available()


def _azurite_reachable() -> bool:
    # R7: require a REAL Azurite — an explicit connection string OR a live TCP socket on the default blob
    # endpoint. Docker being installed does NOT imply Azurite is running, so seam SKIPS visibly when
    # Azurite is absent (rather than erroring on connect).
    if os.environ.get("NEURO_TEST_AZURITE") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        return True
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", 10000), timeout=0.5):
        return True
    return False


def seam_available() -> bool:
    # W1-W8 needs both a real Postgres AND real Azurite (no docker-implies-azurite shortcut, R7).
    return pg_available() and _azurite_reachable()


def marker_status() -> dict[str, bool]:
    return {
        "gpu": gpu_available(),
        "api": api_available(),
        "network": network_available(),
        "pg": pg_available(),
        "seam": seam_available(),
    }


_DETECTORS = {
    "gpu": gpu_available,
    "api": api_available,
    "network": network_available,
    "pg": pg_available,
    "seam": seam_available,
}


@pytest.fixture
def markers_api():
    """Expose the detection functions + the declared marker set to tests (avoids import-path games)."""
    return {"status": marker_status, "detectors": dict(_DETECTORS), "markers": _CAPABILITY_MARKERS}


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker in _CAPABILITY_MARKERS:
            if marker in item.keywords and not _DETECTORS[marker]():
                item.add_marker(
                    pytest.mark.skip(reason=f"{marker}-unavailable (detection hook); skipped visibly")
                )


# --- the session-scoped real-Postgres fixture ------------------------------------------------------
def _normalize_psycopg(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _build_schema(url: str) -> None:
    # Drop + rebuild the neuro schema so a reused test DB starts clean, then run migrations-from-zero.
    eng = create_engine(url, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS neuro CASCADE"))
    eng.dispose()
    os.environ["NEURO_DATABASE_URL"] = url
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    # R1: provision a 'test' identity so golden + seam tests flow THROUGH the lanes-v2 write guard
    # (Repository / BundleRegistrar verify identity at construction), not around it.
    from neuromancer_llm.db.provision import provision

    prov_eng = create_engine(url, future=True)
    with prov_eng.connect() as conn:
        provision(conn, lane="test")
    prov_eng.dispose()


@pytest.fixture(scope="session")
def pg_url():
    explicit = os.environ.get("NEURO_TEST_DATABASE_URL")
    if explicit:
        url = _normalize_psycopg(explicit)
        _build_schema(url)
        yield url
        return
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("NEURO_TEST_DATABASE_URL unset and testcontainers not installed")
    with PostgresContainer("postgres:18", driver="psycopg") as container:
        url = container.get_connection_url()
        _build_schema(url)
        yield url


@pytest.fixture(scope="session")
def engine(pg_url):
    eng = create_engine(pg_url, future=True)
    yield eng
    eng.dispose()


def _truncate_all(eng) -> None:
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                DO $$ DECLARE r record; BEGIN
                    -- database_identity is PRESERVED across tests (the provisioned 'test' lane row the
                    -- write guard verifies); alembic_version is alembic's own bookkeeping.
                    FOR r IN SELECT tablename FROM pg_tables
                             WHERE schemaname = 'neuro'
                               AND tablename NOT IN ('alembic_version', 'database_identity') LOOP
                        EXECUTE format('TRUNCATE TABLE neuro.%I RESTART IDENTITY CASCADE', r.tablename);
                    END LOOP;
                END $$;
                """
            )
        )


@pytest.fixture
def repo(engine):
    """A clean, identity-verified Repository per test: every neuro table truncated (database_identity
    preserved), constructed THROUGH the lanes-v2 write guard (expected_lane='test')."""
    from neuromancer_llm.db.repository import Repository

    _truncate_all(engine)
    return Repository(engine, expected_lane="test")


# --- shared role provisioning (the SELECT-only reader + the registry/writer boundary) --------------
# R3: a password containing a single quote (and a double quote) — the old f-string SQL broke on this
# ('unterminated quoted string'); sql.Literal escaping must round-trip it. Kept so the regression stays live.
_ROLE_PW = "a'b\"c"


@pytest.fixture
def role_pw():
    return _ROLE_PW


@pytest.fixture
def provisioned_roles(engine, pg_url):
    """Create the four roles + apply grants.sql, idempotently. Roles are cluster-level and persist across
    the session; returns the base pg_url so a test can build a role-scoped connection URL."""
    from neuromancer_llm.db.provision import provision_roles

    with engine.connect() as conn:
        provision_roles(conn, password=_ROLE_PW)
    return pg_url


@pytest.fixture
def role_url():
    """A helper to build a role-scoped URL OBJECT (not a rendered string) so the password reaches psycopg
    verbatim with no URL percent-encoding in the middle to muddy a boundary test."""
    from sqlalchemy.engine import make_url

    def _make(base_url, role):
        return make_url(base_url).set(username=role, password=_ROLE_PW)

    return _make


@pytest.fixture
def seeded(repo):
    """Minimal actor + campaign + run so jobs can be enqueued (jobs.run_id is NOT NULL)."""
    actor_id = repo.create_actor("worker:test", kind="scheduled_worker")
    campaign_id = repo.create_campaign("c-test", actor_id)
    run_id = repo.create_run(
        "c-test/slug/dig", campaign_id=campaign_id, work_slug="slug", variant_digest="dig", actor_id=actor_id
    )
    return {"repo": repo, "actor_id": actor_id, "campaign_id": campaign_id, "run_id": run_id}
