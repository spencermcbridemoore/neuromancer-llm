"""`neuro spend` — report | plan | reconcile. SU/$ per-run report (renewal evidence). Stage 2."""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Spend governance: report | plan | reconcile.")


@app.command()
def report() -> None:
    """STAGE 2 — per-run SU/$ report (doubles as Jetstream2 renewal evidence)."""
    stage2("spend report")


@app.command()
def plan() -> None:
    """STAGE 2 — record a run_plan justification artifact (required above budget threshold)."""
    stage2("spend plan")


@app.command()
def reconcile() -> None:
    """STAGE 2 — nightly spend reconciliation."""
    stage2("spend reconcile")
