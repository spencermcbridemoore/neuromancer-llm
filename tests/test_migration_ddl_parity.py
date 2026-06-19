"""The headline buildability proof, resident in CI: Alembic revision 0001 == phase3-ddl.sql.

Builds the canonical schema TWO ways on the real Postgres fixture — `alembic upgrade
0001_initial_schema` (migrations-from-zero) and `tests/reference/phase3-ddl.sql` — into sibling
databases, then structurally diffs the catalogs (enums, tables, columns, constraints, named CHECKs,
indexes, triggers, functions). Scoped to revision 0001 (the immutable initial target) so it stays valid
as later migrations evolve the schema away from the frozen DDL.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.pg

DDL_FILE = Path(__file__).parent / "reference" / "phase3-ddl.sql"

_QUERIES = {
    "enums": "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder)::text "
    "FROM pg_type t JOIN pg_enum e ON e.enumtypid=t.oid JOIN pg_namespace n ON n.oid=t.typnamespace "
    "WHERE n.nspname='neuro' GROUP BY t.typname ORDER BY 1",
    "tables": "SELECT tablename FROM pg_tables WHERE schemaname='neuro' AND tablename<>'alembic_version' ORDER BY 1",
    "columns": "SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull, a.attidentity, "
    "pg_get_expr(ad.adbin, ad.adrelid) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
    "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum "
    "WHERE n.nspname='neuro' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped AND c.relname<>'alembic_version' "
    "ORDER BY 1,2",
    "constraint_defs": "SELECT t.relname, c.contype::text, pg_get_constraintdef(c.oid) FROM pg_constraint c "
    "JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=c.connamespace "
    "WHERE n.nspname='neuro' AND t.relname<>'alembic_version' ORDER BY 1,2,3",
    "check_names": "SELECT t.relname, c.conname FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
    "JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='neuro' AND c.contype='c' "
    "AND t.relname<>'alembic_version' ORDER BY 1,2",
    "indexes": "SELECT indexdef FROM pg_indexes WHERE schemaname='neuro' AND tablename<>'alembic_version' ORDER BY 1",
    "triggers": "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='neuro' AND NOT t.tgisinternal ORDER BY 1,2",
    "functions": "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
    "WHERE n.nspname='neuro' ORDER BY 1",
    # R6: pg_description (table + column COMMENTs) — both builds must carry the 9 frozen-DDL comments,
    # so the parity claim stops overclaiming. objsubid=0 -> table comment (no attname); else column.
    "comments": "SELECT c.relname, COALESCE(a.attname, '') AS col, d.description "
    "FROM pg_description d JOIN pg_class c ON c.oid=d.objoid JOIN pg_namespace n ON n.oid=c.relnamespace "
    "LEFT JOIN pg_attribute a ON a.attrelid=d.objoid AND a.attnum=d.objsubid "
    "WHERE n.nspname='neuro' AND c.relname<>'alembic_version' ORDER BY 1,2,3",
}


def _sibling_url(base: str, dbname: str) -> str:
    return make_url(base).set(database=dbname).render_as_string(hide_password=False)


def _recreate_db(base: str, dbname: str) -> None:
    admin = create_engine(_sibling_url(base, "postgres"), future=True, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{dbname}' AND pid <> pg_backend_pid()"
            )
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{dbname}"')
            conn.exec_driver_sql(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.dispose()


def _is_comment_only(stmt: str) -> bool:
    return all(not ln.strip() or ln.lstrip().startswith("--") for ln in stmt.splitlines())


def _split_sql(sql: str) -> list[str]:
    """Split a SQL script into statements on ';' — but only in NORMAL state, never inside a single-quoted
    string (COMMENT bodies hold ';'), a $$...$$ block (the assign-once function), or a -- line comment.
    """
    out: list[str] = []
    buf: list[str] = []
    in_squote = in_dollar = in_comment = False
    i, n = 0, len(sql)
    while i < n:
        ch, two = sql[i], sql[i : i + 2]
        if in_comment:
            buf.append(ch)
            if ch == "\n":
                in_comment = False
            i += 1
        elif in_squote:
            buf.append(ch)
            if ch == "'" and sql[i + 1 : i + 2] == "'":  # '' is an escaped literal quote
                buf.append("'")
                i += 2
            else:
                if ch == "'":
                    in_squote = False
                i += 1
        elif in_dollar:
            if two == "$$":
                in_dollar = False
                buf.append("$$")
                i += 2
            else:
                buf.append(ch)
                i += 1
        elif two == "--":
            in_comment = True
            buf.append(two)
            i += 2
        elif two == "$$":
            in_dollar = True
            buf.append("$$")
            i += 2
        elif ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt and not _is_comment_only(stmt):
                out.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail and not _is_comment_only(tail):
        out.append(tail)
    return out


def _snapshot(url: str) -> dict[str, set]:
    eng = create_engine(url, future=True)
    try:
        with eng.connect() as conn:
            return {key: {tuple(r) for r in conn.execute(text(sql))} for key, sql in _QUERIES.items()}
    finally:
        eng.dispose()


def test_migration_0001_matches_phase3_ddl(pg_url):
    try:
        _recreate_db(pg_url, "parity_alembic")
        _recreate_db(pg_url, "parity_ddl")
    except Exception as exc:  # noqa: BLE001 — needs CREATE DATABASE; skip cleanly where not permitted
        pytest.skip(f"cannot create sibling databases for parity ({exc})")

    # build A: alembic migrations-from-zero to the immutable initial revision
    alembic_url = _sibling_url(pg_url, "parity_alembic")
    prev = os.environ.get("NEURO_DATABASE_URL")
    os.environ["NEURO_DATABASE_URL"] = alembic_url
    try:
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "0001_initial_schema")
    finally:
        if prev is not None:
            os.environ["NEURO_DATABASE_URL"] = prev
        else:
            os.environ.pop("NEURO_DATABASE_URL", None)

    # build B: the frozen DDL of record
    ddl_url = _sibling_url(pg_url, "parity_ddl")
    eng = create_engine(ddl_url, future=True)
    try:
        with eng.begin() as conn:
            for stmt in _split_sql(DDL_FILE.read_text(encoding="utf-8")):
                # psycopg parses % as a placeholder; the assign-once RAISE format string holds literal
                # '%.%'. Escaping %->%% lets the extended protocol de-escape back to % server-side.
                conn.exec_driver_sql(stmt.replace("%", "%%"))
    finally:
        eng.dispose()

    a, d = _snapshot(alembic_url), _snapshot(ddl_url)
    for key in _QUERIES:
        only_a = sorted(a[key] - d[key])
        only_d = sorted(d[key] - a[key])
        assert not only_a and not only_d, (
            f"{key} differs:\n  only-in-alembic: {only_a[:5]}\n  only-in-ddl: {only_d[:5]}"
        )
