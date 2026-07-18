"""`neuro runs` — new | adopt | finalize | show. `adopt` labels adhoc runs (ADR-0036). `show` is the BUILT
read verb (a thin delegate over db/run_report.py — the first read module); new/adopt/finalize stay Stage 2."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from . import stage2

if TYPE_CHECKING:
    from ..db.run_report import RunReport

app = typer.Typer(no_args_is_help=True, help="Runs: new | adopt | finalize | show.")

_LANE = typer.Option(
    "canonical",
    envvar="NEURO_EXPECTED_LANE",
    help="expected DB lane verified before any read (fail closed)",
)


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
def show(
    run_id: int | None = typer.Argument(None, help="the run to show (positional); or use --run-key"),
    run_key: str | None = typer.Option(None, "--run-key", help="select the run by its run_key instead"),
    lane: str = _LANE,
) -> None:
    """Show one run's DB-resident provenance read-only: identity, model, counts, inputs, metrics, lake pointers.

    A READ-ONLY render of a single run — the first `neuro` verb whose product is something a human looks at. It
    shows DB-resident facts ONLY: run + model identity (an unlabeled adhoc run shows fingerprint=none +
    unlabeled=yes honestly, ADR-0036), per-run counts, the input/config rows, the registered per-run metrics
    (ADR-0017), and storage POINTERS. It does NOT read artifacts/parquet back — that is unverifiable in-band
    (ADR-0009) and lives under `capture show --backend-key`; a pointer printed honestly beats a payload rendered
    unverifiably. Thin delegate: the read lives in db/run_report.py; this parses args and renders.
    """
    # Selector usage check FIRST — before any DB is opened — so a missing/ambiguous selector is a clean usage
    # error (exit 2), distinct from a well-formed request for a run that does not exist (exit 1, below).
    provided = [x for x in (run_id, run_key) if x is not None]
    if len(provided) != 1:
        typer.secho("give exactly one of RUN_ID (positional) or --run-key", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import IdentityMismatchError
    from ..db.run_report import RunNotFoundError, build_run_report
    from ..db.session import make_verified_engine

    try:
        engine = make_verified_engine(expected_lane=lane)
        report = build_run_report(engine, run_id=run_id, run_key=run_key, lane=lane)
    except (
        ConfigurationError,
        LaneAssertionError,
        IdentityMismatchError,
        RunNotFoundError,
    ) as exc:
        typer.secho(f"runs show failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _echo_report(report)


def _echo_report(report: RunReport) -> None:
    """Render a RunReport to stdout. Presentation only — no SQL, no DB. Payload-free by construction: the report
    carries counts and pointers, never wire/metric content."""
    typer.echo(f"run {report.run_id}  {report.run_key}  [{report.run_kind}]")
    typer.echo(f"  campaign:    {report.campaign_key}")
    typer.echo(f"  actor:       {report.actor_key} ({report.actor_display_name}) kind={report.actor_kind}")
    typer.echo(f"  lane:        {report.lane}")
    typer.echo(f"  origin:      {report.origin}")
    typer.echo(f"  status:      {report.status}")
    typer.echo(f"  created:     {report.created_at}")
    typer.echo(f"  finalized:   {report.finalized_at if report.finalized_at is not None else '—'}")
    if report.is_unlabeled:
        typer.secho(
            "  unlabeled:   yes (ADR-0036 adhoc — not yet adopted; `neuro runs adopt` labels it)",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo("  unlabeled:   no")
    typer.echo(f"  invocation:  {'canonical' if report.invocation_id is None else str(report.invocation_id)}")

    if report.model is None:
        # Describe ONLY the absent model identity. The labeling claim is the `unlabeled:` line's alone — a run
        # can be fingerprint-NULL while is_unlabeled=False (no CHECK ties them; repo.create_run makes exactly
        # that), so hardcoding "unlabeled" here would contradict `unlabeled: no` — a checkable falsehood.
        typer.echo("  fingerprint: none (no model identity yet)")
    else:
        m = report.model
        typer.echo(f"  fingerprint: {m.fingerprint_id} hash={m.fingerprint_hash_hex} mode={m.declared_mode}")
        typer.echo(
            f"  model:       {m.hf_repo or '(local/opaque)'}@{m.hf_revision or '—'} "
            f"dtype={m.dtype_quant} serving={m.serving_stack}@{m.serving_version} arch={m.arch_family}"
        )

    typer.echo(
        f"  counts:      events={report.event_count} (spilled={report.spilled_event_count})  "
        f"inputs={report.input_count}  metrics={report.metric_count}  lake-manifests={report.manifest_count}"
    )

    if report.inputs:
        typer.echo("  inputs:")
        for ref in report.inputs:
            typer.echo(f"    - {ref.role:<12} {ref.referent_kind}:{ref.referent_id}")
    if report.metrics:
        typer.echo("  metrics:")
        for metric in report.metrics:
            if metric.value_num is not None:
                typer.echo(f"    - {metric.metric_key} = {metric.value_num}")
            else:
                typer.echo(f"    - {metric.metric_key} = <json, {metric.value_json_bytes} bytes>")
    if report.storage_pointers:
        typer.echo("  storage pointers (lake; no read-back — ADR-0009):")
        for ptr in report.storage_pointers:
            rows = "?" if ptr.row_count is None else ptr.row_count
            typer.echo(f"    - {ptr.dataset_name} @ {ptr.backend_key}: {ptr.uri}  rows={rows}")
