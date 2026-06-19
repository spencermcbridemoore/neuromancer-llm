"""`neuro bundles` — seal | register | gc | crawl | verify-golden.

The W1-W8 write-ordering machinery (bundles/registrar.py) is built and kill-tested in Stage 1; the
operational CLI wiring lands in Stage 2.
"""

from __future__ import annotations

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Bundles: seal | register | gc | crawl | verify-golden.")


@app.command()
def seal() -> None:
    """STAGE 2 — seal a bundle (manifest_sha256 set, state=sealed; GC-exempt, ADR-0010)."""
    stage2("bundles seal")


@app.command()
def register() -> None:
    """STAGE 2 — register a sealed bundle (W1-W8 last step, single transaction)."""
    stage2("bundles register")


@app.command()
def gc() -> None:
    """STAGE 2 — GC unsealed bundles; sealed are exempt (ADR-0010)."""
    stage2("bundles gc")


@app.command()
def crawl() -> None:
    """STAGE 2 — crawl + quarterly crawl-rebuild drill + golden corpus."""
    stage2("bundles crawl")


@app.command("verify-golden")
def verify_golden() -> None:
    """STAGE 2 — verify against the manifest golden corpus."""
    stage2("bundles verify-golden")
