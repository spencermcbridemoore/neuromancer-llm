"""`neuro runs` — new | adopt | finalize | show | position-bias. `adopt` labels adhoc runs (ADR-0036). `show`
and `position-bias` are the BUILT read verbs (thin delegates over db/run_report.py and
db/position_bias_report.py); new/adopt/finalize stay Stage 2.

`position-bias` lives under `runs` rather than in a new top-level group because the measure IS over runs (one
per answer-order permutation), selected by campaign — and `db/run_report.py` + `neuro runs show` is the exact
precedent pair for a read module plus its thin delegate."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from . import stage2

if TYPE_CHECKING:
    from ..db.position_bias_report import PositionBiasReport
    from ..db.run_report import RunReport

app = typer.Typer(no_args_is_help=True, help="Runs: new | adopt | finalize | show | position-bias.")

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


@app.command("position-bias")
def position_bias(
    campaign_key: str = typer.Option(..., "--campaign-key", help="the capture campaign to measure"),
    corpus_root: str | None = typer.Option(
        None,
        "--corpus-root",
        help="path to the ESTELA JSONL (same flag name as `capture campaign`); enables ACCURACY + the "
        "coverage/answer-key validation. Omit for the content-vs-position measure alone.",
    ),
    per_question: bool = typer.Option(False, "--per-question", help="also print the per-question table"),
    max_k: int = typer.Option(5, help="max option count in scope — must match the campaign's own --max-k"),
    lane: str = _LANE,
) -> None:
    """Measure answer-position/order bias over a capture campaign's answer-letter projections. READ-ONLY.

    Computes, per question and aggregated by arity k: `content_consistency` (modal SOURCE-OPTION share — 1.0
    under perfect content-tracking, 1/k under pure position bias) and `position_concentration` (modal LETTER
    share — 1/k under perfect content-tracking, 1.0 under pure position bias). Both nulls are EXACT, not
    simulated: the enumeration is complete, so each source option occupies each letter position exactly (k-1)!
    times. With --corpus-root it also reports accuracy and accuracy CONDITIONED ON the letter the answer sat at.

    WRITES NOTHING — the per-question aggregate is computed on demand (ADR-0017 render-not-compute); no
    run_metrics row and no divergence_measurements row is created. Thin delegate: the read + measure live in
    db/position_bias_report.py; this parses args and renders.
    """
    from pathlib import Path

    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.position_bias_report import PositionBiasError, build_position_bias_report
    from ..db.repository import IdentityMismatchError
    from ..db.session import make_verified_engine

    try:
        engine = make_verified_engine(expected_lane=lane)
        report = build_position_bias_report(
            engine,
            campaign_key=campaign_key,
            corpus_path=Path(corpus_root) if corpus_root is not None else None,
            max_k=max_k,
            lane=lane,
        )
    except (
        ConfigurationError,
        LaneAssertionError,
        IdentityMismatchError,
        PositionBiasError,
        OSError,
    ) as exc:
        typer.secho(f"runs position-bias failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _echo_position_bias(report, per_question=per_question)


def _echo_position_bias(report: PositionBiasReport, *, per_question: bool) -> None:
    """Render a PositionBiasReport. Presentation only — no SQL, no DB, no payload (the projection content is
    reduced to counts and derived rates before it ever reaches here)."""
    typer.echo(f"position-bias  campaign={report.campaign_key}  lane={report.lane}")
    typer.echo(f"  metric:      {report.metric_key}")
    typer.echo(
        f"  scope:       questions={report.question_count}  runs={report.run_count}  "
        f"scored={report.scored_count}"
    )
    typer.echo(
        f"  QA:          ties-excluded={report.tie_count}  unscoreable={report.unscoreable_count}  "
        f"censored-cells={report.censored_cell_count}"
    )
    if report.tie_count:
        typer.secho(
            "               (exact argmax ties are EXCLUDED and counted — a letter-ordered tie-break would "
            "inject position bias into a position-bias measure)",
            fg=typer.colors.YELLOW,
        )

    if report.corpus is not None:
        c = report.corpus
        typer.echo(f"  corpus:      {c.corpus_path}")
        typer.echo(
            f"               in-scope={c.corpus_scope_count}  dropped-duplicate-option="
            f"{len(c.dropped_duplicate_option_uids)}"
        )
        # EVERY outlet of CorpusValidation is rendered. A category printed nowhere would let an in-scope
        # question be silently absent from the accuracy denominator under three green ticks.
        for label, uids in (
            ("in DB, not in corpus scope", c.uids_in_db_not_corpus),
            ("in corpus scope, not in DB", c.uids_in_corpus_not_db),
            ("answer-key mismatch (answer_text != choices[answer_index])", c.answer_key_mismatch_uids),
            ("in scope with NO answer_index (no accuracy)", c.uids_without_answer_key),
            (
                "no answer_text — *_shuffled cross-check COULD NOT RUN (accuracy withheld)",
                c.uids_without_answer_text,
            ),
            ("corpus arity != captured arity (wrong corpus export?)", c.arity_mismatch_uids),
        ):
            if uids:
                typer.secho(
                    f"               ⚠ {len(uids)} {label}: {', '.join(uids[:5])}", fg=typer.colors.YELLOW
                )
            else:
                typer.echo(f"               ✓ 0 {label}")

    for agg in report.by_k:
        typer.echo(f"  k={agg.k}  ({agg.question_count} questions, {agg.scored_count} scored permutations)")
        typer.echo(f"    null share (exact):        {agg.null_share:.4f}")
        typer.echo(
            f"    content_consistency mean:  {_fmt(agg.mean_content_consistency)}   (1.0 = pure content)"
        )
        typer.echo(
            f"    position_concentration:    {_fmt(agg.mean_position_concentration)}   "
            f"(null {agg.null_share:.4f}; 1.0 = pure position)"
        )
        typer.echo(
            f"    above attainable floor:    {agg.questions_above_position_null}/{agg.question_count}"
            "   (floor = ceil(n/k)/n, NOT 1/k — see the module note)"
        )
        if agg.tie_count or agg.unscoreable_count or agg.hole_count:
            typer.secho(
                f"    ⚠ denominator reduced by:  ties={agg.tie_count}  unscoreable={agg.unscoreable_count}  "
                f"missing-permutations={agg.hole_count}",
                fg=typer.colors.YELLOW,
            )
        letters = "  ".join(
            f"{chr(ord('A') + i)}={share:.3f}" for i, share in enumerate(agg.pooled_letter_shares)
        )
        # A share is only meaningful when something was scored; 0.000 across the board would otherwise read as
        # a measured flat distribution rather than "nothing to measure".
        typer.echo(
            f"    pooled letter shares:      {letters if agg.scored_count else '— (nothing scoreable)'}"
        )
        if agg.mean_accuracy is not None:
            # The denominator is stated: mean_accuracy is over the questions that HAVE a usable key, which is a
            # subset of question_count (keyless / uncheckable / mismatched keys are excluded above).
            typer.echo(
                f"    accuracy mean:             {_fmt(agg.mean_accuracy)}   "
                f"(over {agg.accuracy_question_count}/{agg.question_count} questions with a usable key)"
            )
        if agg.accuracy_by_position is not None:
            by_pos = "  ".join(
                f"{chr(ord('A') + i)}={_fmt(v)}" for i, v in enumerate(agg.accuracy_by_position)
            )
            typer.echo(f"    accuracy by key position:  {by_pos}")

    if per_question:
        typer.echo("  per question (uid, k, scored, content, position, accuracy):")
        for q in report.questions:
            hole = (
                "" if q.perm_count == q.expected_perm_count else f"  ⚠ {q.perm_count}/{q.expected_perm_count}"
            )
            typer.echo(
                f"    {q.uid}  k={q.k}  n={q.scored_count}  "
                f"content={_fmt(q.content_consistency)}  position={_fmt(q.position_concentration)}  "
                f"acc={_fmt(q.accuracy)}{hole}"
            )


def _fmt(value: float | None) -> str:
    """Render a rate, or an honest em dash when it does not exist (never a fabricated 0.0)."""
    return "—" if value is None else f"{value:.4f}"


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
