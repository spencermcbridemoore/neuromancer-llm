"""`neuro derive` — run | reparity. Promotion-on-demand re-derive + parity probe (ADR-0011). Stage 2."""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Derived-satellite promotion: run | reparity.")


@app.command()
def run() -> None:
    """STAGE 2 — re-derive a promoted satellite (each row carries method_version_id)."""
    stage2("derive run")


@app.command()
def reparity() -> None:
    """STAGE 2 — parity probe comparing a satellite to its lake source."""
    stage2("derive reparity")
