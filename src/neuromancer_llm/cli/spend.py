"""`neuro spend` — report | plan | reconcile. The A2-15 spend ledger (SU/$ per-run report doubles as
Jetstream2 renewal evidence). Thin delegates over Repository.{spend_report,record_run_plan,reconcile_spend}
(one-implementation-per-concept; the logic + probes live in db/repository.py + tests/test_spend.py).
"""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, help="Spend governance: report | plan | reconcile.")

_LANE = typer.Option(
    "canonical",
    envvar="NEURO_EXPECTED_LANE",
    help="expected DB lane verified before any access (fail closed)",
)


@app.command()
def report(lane: str = _LANE) -> None:
    """Per-run SU/$ + standing spend from the ledger (Jetstream2 renewal evidence)."""
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import Repository
    from ..db.session import make_verified_engine

    try:
        engine = make_verified_engine(expected_lane=lane)
        rep = Repository(engine, expected_lane=lane).spend_report()
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"spend report failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"standing spend: ${rep.standing_usd}")
    typer.echo("per-run spend:")
    if rep.per_run:
        for run_id, amount in rep.per_run:
            typer.echo(f"  run_id={run_id}  ${amount}")
    else:
        typer.echo("  (none)")
    typer.echo(f"unattributed spend: ${rep.unattributed_usd}")
    typer.echo(f"total: ${rep.total_usd}")


@app.command()
def plan(
    justification: str = typer.Option(
        ..., help="why this run's spend is justified (required above the budget threshold; renewal evidence)"
    ),
    run_id: int | None = typer.Option(None, help="run this plan justifies (nullable)"),
    est_usd: float | None = typer.Option(None, help="estimated USD"),
    est_su: float | None = typer.Option(None, help="estimated SU (Jetstream2 allocation units)"),
    est_gpu_hours: float | None = typer.Option(None, help="estimated GPU-hours"),
    lane: str = _LANE,
) -> None:
    """Record a run_plans justification artifact (required above the budget threshold)."""
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import Repository
    from ..db.session import make_verified_engine

    try:
        engine = make_verified_engine(expected_lane=lane)
        run_plan_id = Repository(engine, expected_lane=lane).record_run_plan(
            run_id=run_id,
            justification=justification,
            est_usd=est_usd,
            est_su=est_su,
            est_gpu_hours=est_gpu_hours,
        )
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"spend plan failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"recorded run_plan_id={run_plan_id} (run_id={run_id})")


@app.command()
def reconcile(
    billed_usd: float = typer.Option(
        ..., help="ACTUAL billed USD (from the Azure Cost Management export; no live query exists pre-A2-1)"
    ),
    threshold_pct: float = typer.Option(
        20.0, help="flag a BREACH when |divergence| exceeds this percent (owner-adjustable; R4c)"
    ),
    backend_or_lane: str | None = typer.Option(
        None, help="optional: reconcile ONE backend_or_lane's rate cards only (default: the whole ledger)"
    ),
    lane: str = _LANE,
) -> None:
    """Reconcile ACTUAL billed spend against the ledger-predicted total; flag divergence (R4c)."""
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import Repository
    from ..db.session import make_verified_engine

    try:
        engine = make_verified_engine(expected_lane=lane)
        result = Repository(engine, expected_lane=lane).reconcile_spend(
            billed_usd=billed_usd, threshold_pct=threshold_pct, backend_or_lane=backend_or_lane
        )
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"spend reconcile failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    scope = backend_or_lane or "ALL"
    typer.echo(
        f"reconcile [{scope}]: predicted=${result.predicted_usd} billed=${result.billed_usd} "
        f"divergence={result.divergence_pct:.2f}% (threshold {result.threshold_pct}%)"
    )
    if result.breach:
        typer.secho(
            "BREACH: billed diverges from predicted beyond the threshold", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)
    typer.secho("within threshold", fg=typer.colors.GREEN)
