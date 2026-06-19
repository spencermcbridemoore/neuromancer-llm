"""`neuro docs` — build [--check]. The byte-stable generated-docs gate (phase0 Q14)."""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, help="Generated docs (schema/CLI/env/governance) — docs cannot rot.")


@app.command()
def build(
    check: bool = typer.Option(
        False, "--check", help="regenerate and FAIL if anything differs from what's committed (CI gate)"
    ),
) -> None:
    """Regenerate the docs from the live models/CLI. With --check, fail loudly on any drift (byte-stable)."""
    from ..governance.docs_gen import build_docs

    raise typer.Exit(code=build_docs(check=check))
