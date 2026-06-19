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
def quota() -> None:
    """STAGE 2 — per-prefix dollar-calibrated quota report (fails closed, ADR-0040)."""
    stage2("storage quota")


@app.command("sas-mint")
def sas_mint() -> None:
    """STAGE 2 — mint a user-delegation SAS with surfaced expiry (ADR-0013)."""
    stage2("storage sas-mint")


@app.command()
def pin() -> None:
    """STAGE 2 — promote bytes to cloud now (ADR-0034)."""
    stage2("storage pin")
