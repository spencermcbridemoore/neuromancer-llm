"""`neuro db` — provision | verify | migrate | restore-drill | roles. Thin delegate over db/."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import typer

from . import stage2

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
def verify(lane: str = typer.Option(..., help="the lane this connection must positively be")) -> None:
    """Positively verify the connected DB's identity; fail closed on any mismatch (ADR-0006)."""
    from ..db.lanes import assert_lane
    from ..db.session import make_engine

    with _clean_fail():
        with make_engine().connect() as conn:
            identity = assert_lane(conn, expected_lane=lane)
        typer.echo(f"verified: lane={identity['lane']} instance_uuid={identity['instance_uuid']}")


@app.command("restore-drill")
def restore_drill() -> None:
    """Scripted quarterly restore drill (ADR-0007 durability) — Stage 2."""
    stage2("db restore-drill")
