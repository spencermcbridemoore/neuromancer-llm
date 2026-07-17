"""`neuro importer` — scan | promote | status. Appendix-A two-layer import (phase3-importer-spec.md). Stage 2."""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Appendix-A importer: scan | promote | status.")


@app.command()
def scan() -> None:
    """STAGE 2 — Layer 1 faithful external-records upsert (idempotent on the natural key)."""
    stage2("importer scan")


@app.command()
def promote() -> None:
    """STAGE 2 — Layer 2 selective promotion (importer/promote.py: a batch-scoped governance stamp; ADR-0011's trio does not attach to the stimulus family)."""
    stage2("importer promote")


@app.command()
def status() -> None:
    """STAGE 2 — coverage report (mirrored / promoted / derived-by-predecessor)."""
    stage2("importer status")
