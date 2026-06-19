"""`neuro probe` — run | report. Scheduled VM/desktop probes with a visible report (ADR-0015/0020). Stage 2."""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Operator probes: run | report.")


@app.command()
def run() -> None:
    """STAGE 2 — run a probe (backup freshness, WAL lag, desktop heartbeat, mirror age)."""
    stage2("probe run")


@app.command()
def report() -> None:
    """STAGE 2 — emit the latest probe report (system_health + probe_reports)."""
    stage2("probe report")
