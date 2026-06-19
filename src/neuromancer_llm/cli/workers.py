"""`neuro workers` — run | claim | residency | preflight.

The SKIP-LOCKED claim path + lease/heartbeat/reaper (workers/) is built and golden-harnessed in
Stage 1; the operational worker CLI lands in Stage 2.
"""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Workers: run | claim | residency | preflight.")


@app.command()
def run() -> None:
    """STAGE 2 — run the worker runtime (lease/heartbeat/reaper owned here; checkpoint-first)."""
    stage2("workers run")


@app.command()
def claim() -> None:
    """STAGE 2 — claim one job (SKIP LOCKED) via the repository."""
    stage2("workers claim")


@app.command()
def residency() -> None:
    """STAGE 2 — resolve/budget a residency set against VRAM."""
    stage2("workers residency")


@app.command()
def preflight() -> None:
    """STAGE 2 — VRAM preflight: refuse-to-start, not OOM (phase0 Q4)."""
    stage2("workers preflight")
