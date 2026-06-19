"""`neuro runs` — new | adopt | finalize | show. `adopt` labels adhoc runs (ADR-0036). Stage 2."""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Runs: new | adopt | finalize | show.")


@app.command()
def new() -> None:
    """STAGE 2 — mint a labeled run via the composer."""
    stage2("runs new")


@app.command()
def adopt() -> None:
    """STAGE 2 — retroactively label an adhoc/unlabeled run (ADR-0036)."""
    stage2("runs adopt")


@app.command()
def finalize() -> None:
    """STAGE 2 — finalize a run."""
    stage2("runs finalize")


@app.command()
def show() -> None:
    """STAGE 2 — show a run + its inputs/metrics."""
    stage2("runs show")
