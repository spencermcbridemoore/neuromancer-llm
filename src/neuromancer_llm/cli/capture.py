"""`neuro capture` — logprob | replay | show. The Phase 4 VERTICAL SLICE lives behind `capture logprob`.

STAGE 2: "logprob capture done right" against a small local model — model-identity registration ->
fingerprint -> run auto-mint/label -> capture_events with true wire payload -> logprob parquet shard ->
manifest -> seal -> register (W1-W8) -> DuckDB read via reader role -> divergence replicate. Not built
in the Stage-1 scaffold.
"""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Capture (Stage 2 vertical slice): logprob | replay | show.")


@app.command()
def logprob() -> None:
    """STAGE 2 — the 'logprob capture done right' vertical slice."""
    stage2("capture logprob")


@app.command()
def replay() -> None:
    """STAGE 2 — replicate a captured run for divergence measurement."""
    stage2("capture replay")


@app.command()
def show() -> None:
    """STAGE 2 — show a capture_events row + its spilled payloads."""
    stage2("capture show")
