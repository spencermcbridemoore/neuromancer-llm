"""The one `neuro` CLI (ADR-0038). Every module here is a THIN delegate — argument parsing plus one
call into a library module — so the same code serves the CLI, tests, notebooks, and probes
(one-implementation-per-concept). NEVER-AGAIN: 75-script sprawl.
"""

from __future__ import annotations

import typer


def stage2(command: str) -> None:
    """Placeholder for a command whose logic is not yet built (deferred to its planned gate; the name
    `stage2` is historical — kept so call sites stay stable)."""
    typer.secho(
        f"`neuro {command}` is not yet built (deferred; see the deferred-obligation register).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)
