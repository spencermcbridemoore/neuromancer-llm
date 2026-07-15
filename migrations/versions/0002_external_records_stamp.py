"""external_records confidentiality + derived_by_predecessor stamp (importer rank 5 / D1).

The FIRST schema change since 0001. Adds the fail-closed D1 per-record stamp home to external_records: a NOT-NULL
confidentiality grade (closed vocabulary via a named CHECK) + a NOT-NULL derived_by_predecessor marking, so the DB
itself refuses an unstamped/out-of-vocab row (option B, owner ruling 2026-07-15 — a second belt under the rank-4 ingress
choke point; ADR-0049 Addendum "coverage by MECHANISM, a gap reads DIRTY").

external_records is EMPTY (no importer has run), so NOT NULL with NO server_default is legal and needs no backfill — and
a server_default would be a "defaulted-clean" confidentiality, the exact D1 fail-open the stamp exists to prevent.

Parity note: tests/reference/phase3-ddl.sql is the frozen 0001 schema and test_migration_ddl_parity upgrades only to
0001_initial_schema, so this migration is INVISIBLE to the parity gate and the frozen reference DDL is deliberately left
UNTOUCHED (adding these columns there would make the parity build diverge and BREAK).

Revision ID: 0002_external_records_stamp
Revises: 0001_initial_schema
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_external_records_stamp"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEURO = "neuro"


def upgrade() -> None:
    # Empty table → NOT NULL without a server_default is legal (no rows to violate); no backfill.
    op.add_column("external_records", sa.Column("confidentiality", sa.Text(), nullable=False), schema=NEURO)
    op.add_column(
        "external_records", sa.Column("derived_by_predecessor", sa.Boolean(), nullable=False), schema=NEURO
    )
    # Closed vocab (lock-step with importer/ingress.py::Confidentiality). A named CHECK, NOT a PG enum, so the
    # head-migrated schema keeps enums == 13 (tests/redteam/test_rt_migrations.py).
    op.create_check_constraint(
        "external_records_confidentiality_vocab",
        "external_records",
        "confidentiality IN ('exam_restricted', 'open')",
        schema=NEURO,
    )


def downgrade() -> None:
    op.drop_constraint(
        "external_records_confidentiality_vocab", "external_records", schema=NEURO, type_="check"
    )
    op.drop_column("external_records", "derived_by_predecessor", schema=NEURO)
    op.drop_column("external_records", "confidentiality", schema=NEURO)
