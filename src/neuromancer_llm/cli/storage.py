"""`neuro storage` — put | mirror-audit | quota | sas-mint | pin. Stage 2."""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Storage: put | mirror-audit | quota | sas-mint | pin.")


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
