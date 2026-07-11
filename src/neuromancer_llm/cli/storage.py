"""`neuro storage` — seed | put | mirror-audit | quota | sas-mint | pin.

`seed` (GO-D-cost GO-input 5, owner-ruled 2026-07-11) is the shipped provisioning surface for the
cost-safety rows the row-bound resolver fails closed on: the storage_backends row + (for a cloud driver)
the `azure-blob-storage` rate card, rate derived from the committed STORAGE_PRICE_PIN — so A2-17 is a
configuration event run through THIS command, never hand-SQL (the GO-D-seed `neuro db durability seed`
precedent). `quota` is the live per-prefix report; the rest are Stage 2 stubs."""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Storage: seed | put | mirror-audit | quota | sas-mint | pin.")


@app.command()
def seed(
    backend_key: str = typer.Option(
        ..., help="storage_backends natural key (== the R3 budget prefix for cloud)"
    ),
    driver: str = typer.Option(..., help="storage driver: local_fs | azure_blob"),
    base_uri: str = typer.Option(
        ...,
        help="the registered base_uri (cloud: the account-endpoint URL incl. container, cross-checked "
        "against the credential at resolve time; local: the host-independent logical id)",
    ),
    storage_lane: str = typer.Option("artifacts", help="storage_backends.lane: artifacts | scratch"),
    is_cloud: bool | None = typer.Option(
        None, help="storage_backends.is_cloud (defaults to driver == azure_blob; decoupled deliberately)"
    ),
    effective_from: str | None = typer.Option(
        None,
        help="rate card effective_from (ISO date; cloud only) — defaults to the STORAGE_PRICE_PIN's "
        "retrieved_at, so re-seeding is idempotent and the row is bound to the pin version",
    ),
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before any write (fail closed)",
    ),
) -> None:
    """Seed the cost-safety provisioning rows (idempotent; registrar/admin): the storage_backends row and,
    for a cloud driver, the `azure-blob-storage` rate card with the PIN-derived rate (GO-input 2: the row is
    the auditable sentinel, the pin is the authority — re-seeding the same key with drifted values raises).
    A reprice = bump the pin + re-run with the new pin's date (one auditable commit + one seed)."""
    import datetime as _dt

    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import IdentityMismatchError, Repository
    from ..db.session import make_verified_engine
    from ..registry.backends import STORAGE_RATE_CARD_KEY
    from ..storage.price_pin import STORAGE_PRICE_PIN, resolve_price

    try:
        if driver not in ("local_fs", "azure_blob"):
            raise ConfigurationError(
                f"unknown storage driver {driver!r} (fail closed; known: local_fs, azure_blob)."
            )
        if storage_lane not in ("artifacts", "scratch"):
            raise ConfigurationError(
                f"unknown storage lane {storage_lane!r} (fail closed; known: artifacts, scratch)."
            )
        engine = make_verified_engine(expected_lane=lane)
        repo = Repository(engine, expected_lane=lane)
        cloud = driver == "azure_blob"
        backend_id = repo.get_or_create_storage_backend(
            backend_key,
            driver=driver,
            lane=storage_lane,
            base_uri=base_uri,
            is_cloud=cloud if is_cloud is None else is_cloud,
        )
        typer.echo(f"storage seed (lane={lane}): backend_key={backend_key!r} -> backend_id={backend_id}")
        if cloud:
            rate = resolve_price()  # ConfigurationError if the pin is absent/expired (fail closed)
            assert STORAGE_PRICE_PIN is not None  # resolve_price() proved it
            raw_from = effective_from or STORAGE_PRICE_PIN["retrieved_at"]
            try:
                parsed = _dt.datetime.fromisoformat(raw_from)
            except ValueError as exc:
                raise ConfigurationError(
                    f"--effective-from {raw_from!r} is not an ISO date/datetime (fail closed)."
                ) from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.UTC)
            rate_card_id = repo.get_or_create_rate_card(
                backend_or_lane=STORAGE_RATE_CARD_KEY,
                unit="usd_per_gb",
                rate=rate,
                effective_from=parsed,
            )
            typer.echo(
                f"  rate card {STORAGE_RATE_CARD_KEY!r} -> rate_card_id={rate_card_id} "
                f"(usd_per_gb {rate}, effective_from {parsed.date().isoformat()}; pin-derived)"
            )
    except (ConfigurationError, LaneAssertionError, IdentityMismatchError) as exc:
        typer.secho(f"storage seed failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def put() -> None:
    """STAGE 2 — put bytes to a registered backend (quota fails closed)."""
    stage2("storage put")


@app.command("mirror-audit")
def mirror_audit() -> None:
    """STAGE 2 — monthly full-hash audit against the desktop NVMe mirror (ADR-0014)."""
    stage2("storage mirror-audit")


@app.command()
def quota(
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before the read (fail closed)",
    ),
) -> None:
    """Per-prefix dollar-calibrated storage quota report (fails closed, ADR-0040)."""
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_verified_engine
    from ..storage.quota import quota_report

    try:
        engine = make_verified_engine(expected_lane=lane)
        with engine.connect() as conn:
            lines = quota_report(conn)
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"storage quota failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for line in lines:
        pct = (line.used_bytes / line.byte_ceiling * 100) if line.byte_ceiling else 0.0
        typer.echo(
            f"{line.prefix}: ${line.usd_ceiling} ceiling = {line.byte_ceiling} B; "
            f"used {line.used_bytes} B ({pct:.2f}%)"
        )


@app.command("sas-mint")
def sas_mint() -> None:
    """STAGE 2 — mint a user-delegation SAS with surfaced expiry (ADR-0013)."""
    stage2("storage sas-mint")


@app.command()
def pin() -> None:
    """STAGE 2 — promote bytes to cloud now (ADR-0034)."""
    stage2("storage pin")
