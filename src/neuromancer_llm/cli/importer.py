"""`neuro importer` — scan | promote | status. Thin delegates over the Appendix-A two-layer importer
(phase3-importer-spec.md; ADR-0038 one-implementation-per-concept). `promote` is the BUILT fused mapper
(mirror every question + promote the promotable, one re-runnable pass); `scan` (a future Layer-1-only mirror)
and `status` (a persisted coverage report) are DEFERRED stubs that FAIL LOUD — by fail-safe naming, a mistaken
verb is an error, never an unintended write."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="Appendix-A importer: scan | promote | status.")

# Module-level singleton (the spend.py `_LANE` idiom): a Path-typed `typer.Option(...)` inline in the signature
# trips ruff B008, so the sanctioned fix is to read the default from a module-level variable. `str`-typed options
# below stay inline (they do not trip it).
_CORPUS_ROOT = typer.Option(
    ...,
    help="directory holding questions/question_options/documents.parquet (the MCQ corpus root; REQUIRED, "
    "never hardcoded — a machine-absolute constant is the local-lake base_uri wedge this engagement paid "
    "for once)",
)


@app.command()
def promote(
    corpus_root: Path = _CORPUS_ROOT,
    source_system: str = typer.Option(
        ..., help="D2 provenance stamp (REQUIRED, no default): study-query-llm | local-fs"
    ),
    confidentiality: str = typer.Option(
        ...,
        help="D1 confidentiality stamp (REQUIRED, no default; a defaulted-clean grade IS the D1 fail-open): "
        "exam_restricted | open",
    ),
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before any write (fail closed)",
    ),
    note: str | None = typer.Option(None, help="optional note recorded on the import_batches row"),
    derived_by_predecessor: bool = typer.Option(
        False,
        help="mark every mirrored record derived-by-predecessor (D1; default False — MOSART authored the corpus)",
    ),
) -> None:
    """Import the MOSART MCQ corpus in one pass: mirror EVERY question (Layer 1) and promote the promotable (Layer 2); refusals are reported, never hidden.

    The rank-8b mapper fuses the two layers into a single re-runnable pass, so there is no Layer-1-only path:
    this mirrors every question into external_records AND promotes the promotable into the native stimulus
    cascade, keep-first idempotent. Every refused question stays mirrored in Layer 1 (an honest GAP) and is
    printed with its reason — the refusals are the point, so the tool shows the gap rather than hiding it.

    The D1/D2 stamp (`--source-system` / `--confidentiality`) is REQUIRED and fail-closed here exactly as at the
    ingress signature — a defaulted-clean flag would re-open the D1 fail-open the non-defaulted signature exists
    to close. Thin delegate: the whole mapping lives in importer/mcq.py; this is argument parsing plus the
    open-batch -> register-method (once per batch) -> map call, on a Repository built from a verified engine.
    """
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import IdentityMismatchError, Repository
    from ..db.session import make_verified_engine
    from ..governance.health import DurabilityGateError
    from ..importer.ingress import (
        Confidentiality,
        ImporterIngressError,
        SourceSystem,
        open_import_batch,
    )
    from ..importer.mcq import map_mcq_corpus
    from ..importer.promote import register_promotion_method

    try:
        # D1/D2 stamps validated fail-closed against the closed vocabularies (the storage.py driver-check idiom).
        # The option is `...` (required, no default) AND the value must be in-vocab — a defaulted or out-of-vocab
        # grade is the D1 fail-open the non-defaulted ingress signature exists to close.
        if confidentiality not in {c.value for c in Confidentiality}:
            raise ImporterIngressError(
                f"--confidentiality must be one of {sorted(c.value for c in Confidentiality)}, got "
                f"{confidentiality!r} — refusing (fail closed; D1, no default-clean grade)."
            )
        if source_system not in {s.value for s in SourceSystem}:
            raise ImporterIngressError(
                f"--source-system must be one of {sorted(s.value for s in SourceSystem)}, got "
                f"{source_system!r} — refusing (fail closed; D2 closed vocabulary)."
            )
        conf = Confidentiality(confidentiality)
        ssys = SourceSystem(source_system)
        engine = make_verified_engine(expected_lane=lane)
        repo = Repository(engine, expected_lane=lane)
        handle = open_import_batch(repo, source_system=ssys, confidentiality=conf, note=note)
        method = register_promotion_method(repo)
        result = map_mcq_corpus(
            repo,
            handle,
            method,
            corpus_root=corpus_root,
            derived_by_predecessor=derived_by_predecessor,
        )
    except (
        ConfigurationError,
        LaneAssertionError,
        DurabilityGateError,
        ImporterIngressError,
        IdentityMismatchError,
    ) as exc:
        typer.secho(f"importer promote failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"import batch {handle.import_batch_id} "
        f"(source_system={ssys.value}, confidentiality={conf.value}, lane={lane}):"
    )
    typer.echo(
        f"  inserted: external_records={result.external_records} prompt_sets={result.prompt_sets} "
        f"prompt_items={result.prompt_items} mcq_items={result.mcq_items} "
        f"mcq_options={result.mcq_options} promotions={result.promotions}"
    )
    if result.refusals:
        typer.secho(
            f"  refused {len(result.refusals)} question(s) (mirrored in Layer 1, NOT promoted — the honest GAP):",
            fg=typer.colors.YELLOW,
        )
        for refusal in result.refusals:
            typer.echo(f"    {refusal.question_id}: {refusal.reason}")
    else:
        typer.echo("  refusals: none")


@app.command()
def scan() -> None:
    """STAGE 2 — reserved for a future Layer-1-only mirror pass; today Layer 1 runs as part of `neuro importer promote`.

    Fail-closed by design (the rank-5 vocabulary deferred the scan CLI because no Layer-1-only reader exists).
    The rank-8b mapper fuses Layer 1 and Layer 2, so a Layer-1-only mirror has no entry point yet — the full
    import lives under `promote`. A mistaken verb must be an error, never an unintended write.
    """
    typer.secho(
        "`neuro importer scan` (a Layer-1-only mirror pass) is not yet built — it is deferred. The rank-8b "
        "mapper fuses Layer 1 into the promotion pass, so today the Layer-1 mirror runs as part of `neuro "
        "importer promote` (mirror every question + promote the promotable, one pass).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.command()
def status() -> None:
    """STAGE 2 — a persisted coverage report (deferred; `neuro importer promote` already reports coverage + refusals from the live result).

    Refusal reasons are not persisted — they exist only in the result `promote` returns and prints — and the
    library has no read-only coverage producer. A persisted-coverage `status` is a library-layer follow-on; it
    is deliberately not inlined here, which would put uncovered read logic into a thin delegate.
    """
    typer.secho(
        "`neuro importer status` (a persisted coverage report) is not yet built — it is deferred. Today `neuro "
        "importer promote` reports the coverage counts and every refusal (question_id + reason) from the live "
        "mapping result.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)
