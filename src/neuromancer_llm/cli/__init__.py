"""The one `neuro` CLI (ADR-0038). Every module here is a THIN delegate — argument parsing plus one
call into a library module — so the same code serves the CLI, tests, notebooks, and probes
(one-implementation-per-concept). NEVER-AGAIN: 75-script sprawl.
"""

from __future__ import annotations

import typer


def stage2(command: str) -> None:
    """Stage-1 scaffold placeholder for a command whose logic lands in Stage 2 (the vertical slice)."""
    typer.secho(
        f"`neuro {command}` lands in Stage 2 (not built in the Stage-1 scaffold).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)
