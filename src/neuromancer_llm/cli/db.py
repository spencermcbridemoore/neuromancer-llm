"""`neuro db` — provision | verify | migrate | restore-drill | roles. Thin delegate over db/."""

from __future__ import annotations

import contextlib
import uuid as _uuid
from collections.abc import Iterator

import typer

app = typer.Typer(no_args_is_help=True, help="Database control plane: migrate, provision, roles, verify.")


@contextlib.contextmanager
def _clean_fail() -> Iterator[None]:
    """Translate the typed domain errors (ConfigurationError / LaneAssertionError) into a clean
    typer.Exit — an expected fail-closed path reads as intentional, not as a crashed traceback (R8)."""
    from ..db.lanes import ConfigurationError, LaneAssertionError

    try:
        yield
    except (ConfigurationError, LaneAssertionError) as exc:
        typer.secho(f"error (fail closed): {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None


@app.command()
def migrate() -> None:
    """Run `alembic upgrade head` — materialize the canonical schema (migrations own it, never create_all)."""
    from alembic import command
    from alembic.config import Config

    with _clean_fail():
        command.upgrade(Config("alembic.ini"), "head")
        typer.echo("migrated to head")


@app.command()
def provision(
    lane: str = typer.Option(..., help="canonical | staging | test  (UNKNOWN is never written)"),
    note: str | None = typer.Option(None, help="free-text note on the identity row"),
) -> None:
    """Write the singleton lanes-v2 identity row on a provably-empty, just-migrated DB (ADR-0006)."""
    from ..db.provision import provision as _provision
    from ..db.session import make_engine

    with _clean_fail():
        with make_engine().connect() as conn:
            _provision(conn, lane=lane, note=note)
        typer.echo(f"provisioned: lane={lane}")


@app.command()
def roles(
    password: str | None = typer.Option(
        None, help="dev/CI only: create the four roles as LOGIN with this password (else NOLOGIN classes)"
    ),
) -> None:
    """Create the four roles (admin/writer/reader/registrar) then apply phase3-grants.sql (ADR-0007)."""
    from ..db.provision import provision_roles
    from ..db.session import make_engine

    with _clean_fail():
        with make_engine().connect() as conn:
            provision_roles(conn, password=password)
        typer.echo("roles created + grants applied")


@app.command()
def verify(
    lane: str = typer.Option(..., help="the lane this connection must positively be"),
    expected_uuid: str | None = typer.Option(
        None,
        help="instance_uuid the DB must hold; --lane canonical auto-resolves the repo pin "
        "(db/canonical_instance.py) when omitted — an absent pin fails closed (A1-15)",
    ),
) -> None:
    """Positively verify the connected DB's identity; fail closed on any mismatch (ADR-0006)."""
    from ..db.canonical_instance import expected_uuid_for_lane
    from ..db.lanes import ConfigurationError, assert_lane
    from ..db.session import make_engine

    with _clean_fail():
        if expected_uuid is not None:
            # parse via uuid.UUID so case/braces normalize (assert_lane compares canonical str forms);
            # a malformed value fails closed with the CLI's uniform error style, not a raw ValueError.
            try:
                pin: _uuid.UUID | None = _uuid.UUID(expected_uuid)
            except ValueError:
                raise ConfigurationError(
                    f"--expected-uuid {expected_uuid!r} is not a valid UUID (fail closed)."
                ) from None
        else:
            pin = expected_uuid_for_lane(lane)
        with make_engine().connect() as conn:
            identity = assert_lane(conn, expected_lane=lane, expected_uuid=pin)
        typer.echo(f"verified: lane={identity['lane']} instance_uuid={identity['instance_uuid']}")


@app.command("restore-drill")
def restore_drill(
    scratch_url: str = typer.Option(
        ..., help="the SCRATCH DB DSN to scrub — must NOT resolve to the canonical NEURO_DATABASE_URL"
    ),
    confirm_scratch: bool = typer.Option(
        False,
        "--confirm-scratch",
        help="affirm the target is a throwaway (required — the drill rewrites database_identity)",
    ),
) -> None:
    """Restore drill: scrub a restored clone's identity so a verbatim canonical backup verifies NON-canonical (A2-9)."""
    from ..db.restore import restore_drill as _drill

    with _clean_fail():
        result = _drill(scratch_url, confirm_scratch=confirm_scratch)
        typer.echo(
            f"restore-drill: scrubbed clone -> non-canonical "
            f"(instance_uuid {result['old_uuid']} -> {result['new_uuid']}; cloned_from recorded)"
        )


# --- durability rows (ADR-0020): the one provisioning surface (GO-D-seed) --------------------------------
durability_app = typer.Typer(
    no_args_is_help=True, help="Durability-gate rows (ADR-0020): seed | reconcile | status."
)
app.add_typer(durability_app, name="durability")


@durability_app.command()
def seed(
    lane: str = typer.Option("canonical", help="the lane this DB must positively be before seeding"),
) -> None:
    """Seed the ADR-0020 durability rows born fail-closed (idempotent; registrar/admin). A2-16 provisioning."""
    from ..db.session import make_verified_engine
    from ..governance.durability import seed_all

    with _clean_fail():
        # verified write engine (R1): lane + repo-pinned uuid checked at construction, fail-closed — the
        # durability rows are POST-provision state, so this is a verified write path, not pre-identity bootstrap.
        with make_verified_engine(expected_lane=lane).connect() as conn:
            inserted = seed_all(conn)
        new = sorted(k for k, v in inserted.items() if v)
        present = sorted(k for k, v in inserted.items() if not v)
        typer.echo(
            f"durability seed (lane={lane}): {len(new)} inserted, {len(present)} already present"
            + (f"; inserted={new}" if new else "")
            + (f"; present={present}" if present else "")
        )


@durability_app.command()
def reconcile(
    lane: str = typer.Option("canonical", help="the lane this DB must positively be before reconciling"),
) -> None:
    """Re-align each durability row's stale_after sentinel to the pinned bound (ADMIN-ONLY). Run after a
    bound-constant change (auditable commit + ADR); never fills a NULL (that is `seed`'s job)."""
    from ..db.session import make_verified_engine
    from ..governance.durability import reconcile_all

    with _clean_fail():
        with make_verified_engine(expected_lane=lane).connect() as conn:
            result = reconcile_all(conn)
        missing = sorted(k for k, (is_present, _) in result.items() if not is_present)
        reconciled = sum(cnt for _, cnt in result.values())
        typer.echo(f"durability reconcile (lane={lane}): {reconciled} sentinel(s) re-aligned to the pin")
        if missing:
            typer.secho(
                f"durability reconcile: MISSING rows (run `neuro db durability seed`): {missing}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)


@durability_app.command()
def status(
    lane: str = typer.Option("canonical", help="the lane this DB must positively be"),
) -> None:
    """Report each durability row (present / status / stale_after drift). Exit non-zero on any missing or
    drifted row — the CI / A2-16-provisioning check."""
    from ..db.session import make_verified_engine
    from ..governance.durability import status_all

    with _clean_fail():
        # verified engine: confirms this is THE canonical instance (lane + repo-pinned uuid) before reporting;
        # status only reads, but a provisioning/CI check should assert it is looking at the intended DB.
        with make_verified_engine(expected_lane=lane).connect() as conn:
            rows = status_all(conn)
        for r in rows:
            if not r.present:
                typer.secho(f"  {r.health_key}: MISSING (run `seed`)", fg=typer.colors.RED)
            elif r.drift and r.stale_after is None:
                # a present-but-NULL sentinel (gate branch-2, unprovisioned): reconcile CANNOT fill a NULL (M1),
                # so the remedy is a reseed / admin repair, NOT `reconcile` — don't send the operator to a dead end.
                typer.secho(
                    f"  {r.health_key}: UNPROVISIONED stale_after=NULL (reseed / admin repair)",
                    fg=typer.colors.RED,
                )
            elif r.drift:
                typer.secho(
                    f"  {r.health_key}: DRIFT status={r.status} stale_after={r.stale_after} (run `reconcile`)",
                    fg=typer.colors.YELLOW,
                )
            else:
                typer.echo(f"  {r.health_key}: ok status={r.status} stale_after={r.stale_after}")
        bad = [r.health_key for r in rows if not r.present or r.drift]
        if bad:
            typer.secho(
                f"durability status: NOT provisioned/consistent: {bad}", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(code=1)
        typer.echo(f"durability status (lane={lane}): all {len(rows)} row(s) provisioned + consistent")
