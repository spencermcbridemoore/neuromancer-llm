"""GO-D-cost C2+C4+I2: the row-bound wiring resolver + the `neuro storage seed` provisioning surface.

RED@4286e43 = ImportError (resolve_capture_backend / STORAGE_RATE_CARD_KEY absent; `neuro storage seed`
does not exist). Probes:
  - the resolver fails closed on a MISSING storage_backends row (never creates one), an UNKNOWN driver row,
    and an UN-BUDGETED cloud prefix — each BEFORE any cloud I/O (fold 8: the typed error, not a connection
    error, proves make_backend was never reached);
  - the rate basis is ROW-BOUND + PIN-CHECKED (GO-input 2): missing row / future-dated-only row / wrong
    unit / pin drift each fail closed; the stored Numeric(18,6) quantization compares EQUAL to the pin
    (fold 7 — Decimal-vs-Decimal, '0.018400' == '0.0184');
  - a local_fs key resolves UNWRAPPED and row-bound;
  - `neuro storage seed` provisions the row (+ the pin-derived rate card for cloud) idempotently, fails
    closed on lane mismatch / unknown driver / drifted re-seed — the SHIPPED path A2-17 runs (GO-input 5).
The cloud HAPPY path (a resolve returning a stamped QuotaGuardedBackend over real Azurite) is probed in
tests/test_a2_17_acceptance.py + the re-keyed seam fixture.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from neuromancer_llm.cli.app import app as cli_app
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.registry.backends import (
    STORAGE_RATE_CARD_KEY,
    _resolve_cloud_rate,
    resolve_capture_backend,
)
from neuromancer_llm.storage.backends import LocalFsBackend
from neuromancer_llm.storage.quota import QuotaDeniedError

_runner = CliRunner()
_PIN_DATE = _dt.datetime(2026, 7, 5, tzinfo=_dt.UTC)  # STORAGE_PRICE_PIN retrieved_at
# A DEAD endpoint: if the resolver ever reached make_backend/AzureBlobBackend on a fail-closed path, the
# construction would raise a connection error, not the typed ConfigurationError these probes assert.
_DEAD_CONN = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=x;"
    "BlobEndpoint=http://127.0.0.1:1/devstoreaccount1;"
)
_DEAD_BASE_URI = "http://127.0.0.1:1/devstoreaccount1/dev"


def _seed_cloud_row(repo, key: str = "artifacts-dev", base_uri: str = _DEAD_BASE_URI) -> int:
    return repo.get_or_create_storage_backend(
        key, driver="azure_blob", lane="artifacts", base_uri=base_uri, is_cloud=True
    )


def _seed_rate(
    repo, rate: str = "0.0184", effective_from: _dt.datetime = _PIN_DATE, unit: str = "usd_per_gb"
):
    return repo.get_or_create_rate_card(
        backend_or_lane=STORAGE_RATE_CARD_KEY, unit=unit, rate=Decimal(rate), effective_from=effective_from
    )


# ---- the resolver: fail-closed row binding ------------------------------------------------------------


@pytest.mark.pg
def test_missing_row_fails_closed(repo):
    with pytest.raises(ConfigurationError, match="no storage_backends row"):
        resolve_capture_backend(repo, backend_key="never-seeded")


@pytest.mark.pg
def test_local_key_resolves_unwrapped_and_row_bound(repo, tmp_path):
    bid = repo.get_or_create_storage_backend(
        "local-lake", driver="local_fs", lane="artifacts", base_uri="file://local-lake", is_cloud=False
    )
    backend_id, backend = resolve_capture_backend(repo, backend_key="local-lake", local_root=tmp_path)
    assert backend_id == bid  # row-bound id, not a caller arg
    assert isinstance(backend, LocalFsBackend)  # unwrapped: local writes are not quota-bound


@pytest.mark.pg
def test_unknown_driver_row_fails_closed(repo):
    with repo.engine.begin() as conn:  # the DDL deliberately has NO driver CHECK (ADR-0040 data-driven)
        conn.execute(
            text(
                "INSERT INTO neuro.storage_backends (backend_key, driver, lane, base_uri, is_cloud) "
                "VALUES ('s3-someday', 's3', 'artifacts', 's3://bucket/x', true)"
            )
        )
    with pytest.raises(ConfigurationError, match="unknown driver 's3'"):
        resolve_capture_backend(repo, backend_key="s3-someday")


@pytest.mark.pg
def test_unbudgeted_cloud_prefix_fails_closed_before_any_io(repo):
    _seed_cloud_row(repo, key="not-a-budgeted-prefix")
    _seed_rate(repo)
    with pytest.raises(QuotaDeniedError):  # budget_group_for_prefix — BEFORE the rate read or make_backend
        resolve_capture_backend(repo, backend_key="not-a-budgeted-prefix", connection_string=_DEAD_CONN)


# ---- the rate basis: row = sentinel, pin = authority (GO-input 2) ---------------------------------------


@pytest.mark.pg
def test_missing_rate_card_fails_closed_before_any_cloud_io(repo):
    _seed_cloud_row(repo)
    with pytest.raises(ConfigurationError, match="no effective rate_cards row"):
        resolve_capture_backend(repo, backend_key="artifacts-dev", connection_string=_DEAD_CONN)


@pytest.mark.pg
def test_future_dated_rate_card_is_not_effective(repo):
    _seed_cloud_row(repo)
    _seed_rate(repo, effective_from=_dt.datetime(2099, 1, 1, tzinfo=_dt.UTC))
    with pytest.raises(ConfigurationError, match="no effective rate_cards row"):
        resolve_capture_backend(repo, backend_key="artifacts-dev", connection_string=_DEAD_CONN)


@pytest.mark.pg
def test_wrong_unit_fails_closed(repo):
    _seed_cloud_row(repo)
    _seed_rate(repo, unit="usd_per_hr")
    with pytest.raises(ConfigurationError, match="usd_per_gb"):
        resolve_capture_backend(repo, backend_key="artifacts-dev", connection_string=_DEAD_CONN)


@pytest.mark.pg
def test_pin_drift_fails_closed(repo):
    _seed_cloud_row(repo)
    _seed_rate(repo, rate="0.02")  # != the committed STORAGE_PRICE_PIN
    with pytest.raises(ConfigurationError, match="drift"):
        resolve_capture_backend(repo, backend_key="artifacts-dev", connection_string=_DEAD_CONN)


@pytest.mark.pg
def test_quantized_stored_rate_equals_the_pin(repo):
    # fold 7: rate_cards.rate is Numeric(18,6) -> '0.018400' comes back; Decimal('0.018400') ==
    # Decimal('0.0184') so the drift check passes; a string compare would spuriously fail closed.
    rc = _seed_rate(repo)
    rate_card_id, rate = _resolve_cloud_rate(repo)
    assert rate_card_id == rc and rate == Decimal("0.0184")


@pytest.mark.pg
def test_latest_effective_row_wins(repo):
    _seed_rate(repo, effective_from=_dt.datetime(2020, 1, 1, tzinfo=_dt.UTC))  # an older identical rate
    newest = _seed_rate(repo)  # the pin-date row
    rate_card_id, _ = _resolve_cloud_rate(repo)
    assert rate_card_id == newest


# ---- `neuro storage seed` — the shipped provisioning surface (GO-input 5) -------------------------------


@pytest.mark.pg
def test_cli_seed_local_row(repo):
    r = _runner.invoke(
        cli_app,
        [
            "storage",
            "seed",
            "--backend-key",
            "local-lake",
            "--driver",
            "local_fs",
            "--base-uri",
            "file://local-lake",
            "--lane",
            "test",
        ],
    )
    assert r.exit_code == 0 and "backend_id=" in r.stdout
    with repo.engine.connect() as conn:
        n_cards = conn.execute(text("SELECT count(*) FROM neuro.rate_cards")).scalar_one()
    assert n_cards == 0  # a local row seeds NO rate card


@pytest.mark.pg
def test_cli_seed_cloud_row_and_pin_derived_rate_card_idempotent(repo):
    args = [
        "storage",
        "seed",
        "--backend-key",
        "artifacts-dev",
        "--driver",
        "azure_blob",
        "--base-uri",
        _DEAD_BASE_URI,
        "--lane",
        "test",
    ]
    r = _runner.invoke(cli_app, args)
    assert r.exit_code == 0 and "rate_card_id=" in r.stdout and "0.0184" in r.stdout
    assert _runner.invoke(cli_app, args).exit_code == 0  # idempotent re-run (get_or_create both halves)
    with repo.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rate_card_id, unit, rate, effective_from FROM neuro.rate_cards "
                "WHERE backend_or_lane = :k"
            ),
            {"k": STORAGE_RATE_CARD_KEY},
        ).one()  # exactly ONE row (idempotent), pin-derived
    assert row.unit == "usd_per_gb" and Decimal(str(row.rate)) == Decimal("0.0184")
    assert row.effective_from == _PIN_DATE  # defaults to the pin's retrieved_at (bound to the pin version)
    # and the seeded pair is exactly what the resolver's rate half needs
    rate_card_id, _ = _resolve_cloud_rate(repo)
    assert rate_card_id == row.rate_card_id


@pytest.mark.pg
def test_cli_seed_unknown_driver_fails_closed(repo):
    r = _runner.invoke(
        cli_app,
        [
            "storage",
            "seed",
            "--backend-key",
            "x",
            "--driver",
            "gcs",
            "--base-uri",
            "gs://x",
            "--lane",
            "test",
        ],
    )
    assert r.exit_code == 1
    with repo.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM neuro.storage_backends")).scalar_one() == 0


@pytest.mark.pg
def test_cli_seed_lane_mismatch_fails_closed_no_write(repo):
    # default --lane canonical against the lane='test' session DB -> fail closed at verify, NO write.
    r = _runner.invoke(
        cli_app,
        ["storage", "seed", "--backend-key", "x", "--driver", "local_fs", "--base-uri", "file://x"],
    )
    assert r.exit_code == 1
    with repo.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM neuro.storage_backends")).scalar_one() == 0


@pytest.mark.pg
def test_cli_seed_drifted_rekey_fails_loud(repo):
    base = ["storage", "seed", "--backend-key", "artifacts-dev", "--driver", "azure_blob", "--lane", "test"]
    assert _runner.invoke(cli_app, [*base, "--base-uri", _DEAD_BASE_URI]).exit_code == 0
    r = _runner.invoke(cli_app, [*base, "--base-uri", "http://127.0.0.1:1/devstoreaccount1/OTHER"])
    assert r.exit_code == 1  # raise-on-drift: a registered row is never silently re-pointed
