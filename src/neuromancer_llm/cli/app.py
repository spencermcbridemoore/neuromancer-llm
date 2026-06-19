"""`neuro` root — wires the thin subcommand delegates. Entry point: [project.scripts] neuro."""

from __future__ import annotations

import typer

from .. import __version__
from . import bundles, capture, db, derive, docs, importer, probe, runs, spend, storage, workers

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="neuromancer-llm — capture, provenance, and export discipline for LLM interpretability. NO UI.",
)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """The single console entrypoint; subcommands are thin delegates over the library."""


app.add_typer(db.app, name="db")
app.add_typer(capture.app, name="capture")
app.add_typer(runs.app, name="runs")
app.add_typer(bundles.app, name="bundles")
app.add_typer(workers.app, name="workers")
app.add_typer(storage.app, name="storage")
app.add_typer(derive.app, name="derive")
app.add_typer(importer.app, name="importer")
app.add_typer(spend.app, name="spend")
app.add_typer(probe.app, name="probe")
app.add_typer(docs.app, name="docs")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
