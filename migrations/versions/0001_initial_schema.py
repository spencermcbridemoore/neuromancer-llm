"""initial schema — materializes tests/reference/phase3-ddl.sql (the frozen canonical DDL) exactly.

Migrations-from-zero target == phase3-ddl.sql (13 enums, 45 tables, the named CHECK/UNIQUE
constraints, the partial + functional indexes, and the load-bearing assign-once trigger). The full
CI lane runs `alembic upgrade head` from empty against real postgres:18 every PR/main; that IS the
migration test (phase0 Q14). Base.metadata.create_all is never used — the schema is owned here.

Bootstrapped from `alembic revision --autogenerate` over the verified ORM (db/orm.py), then finished
to match the DDL: explicit enum creation first (shared enums would double-CREATE inside create_table),
the cyclic/forward FKs deferred + named (assets<->sae_training_runs, methods->method_versions,
run_inputs->prompt_sets), and the neuro.assert_assign_once() trigger on runs.fingerprint_id.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEURO = "neuro"

# --- enum types (create_type=False: created explicitly below, referenced by columns) -------------
lane_kind = postgresql.ENUM(
    "canonical", "staging", "test", "unknown", name="lane_kind", schema=NEURO, create_type=False
)
actor_kind = postgresql.ENUM(
    "human", "agent", "scheduled_worker", "importer", name="actor_kind", schema=NEURO, create_type=False
)
run_kind = postgresql.ENUM(
    "experiment",
    "adhoc",
    "import",
    "replicate",
    "derivation",
    name="run_kind",
    schema=NEURO,
    create_type=False,
)
declared_mode = postgresql.ENUM(
    "deterministic_algo",
    "greedy",
    "seeded_sampling",
    "unseeded_sampling",
    name="declared_mode",
    schema=NEURO,
    create_type=False,
)
expected_level = postgresql.ENUM(
    "bitwise", "tolerance", "distributional", "none", name="expected_level", schema=NEURO, create_type=False
)
job_state = postgresql.ENUM(
    "blocked",
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "dead_letter",
    "cancelled",
    name="job_state",
    schema=NEURO,
    create_type=False,
)
job_error_class = postgresql.ENUM(
    "permanent",
    "transient",
    "resource_mismatch",
    "lease_expired",
    name="job_error_class",
    schema=NEURO,
    create_type=False,
)
bundle_state = postgresql.ENUM(
    "unsealed",
    "sealed",
    "registered",
    "tombstoned",
    "staged",
    "verified",
    name="bundle_state",
    schema=NEURO,
    create_type=False,
)
artifact_kind = postgresql.ENUM(
    "wire_payload",
    "dense_tensor",
    "derived_feature",
    "token_table",
    "prompt_spill",
    "export",
    "external_record",
    "sae_release",
    "other",
    name="artifact_kind",
    schema=NEURO,
    create_type=False,
)
retention_class = postgresql.ENUM(
    "ttl", "keep_forever", "pinned", name="retention_class", schema=NEURO, create_type=False
)
storage_lane = postgresql.ENUM("artifacts", "scratch", name="storage_lane", schema=NEURO, create_type=False)
health_status = postgresql.ENUM(
    "ok", "degraded", "blocked", name="health_status", schema=NEURO, create_type=False
)
edge_kind = postgresql.ENUM(
    "derived_from",
    "replicate_of",
    "promoted_from",
    "annotates",
    "curates",
    "paraphrase_of",
    "generated_by",
    "linked_call",
    name="edge_kind",
    schema=NEURO,
    create_type=False,
)

_ENUMS = [
    lane_kind,
    actor_kind,
    run_kind,
    declared_mode,
    expected_level,
    job_state,
    job_error_class,
    bundle_state,
    artifact_kind,
    retention_class,
    storage_lane,
    health_status,
    edge_kind,
]


def upgrade() -> None:
    bind = op.get_bind()  # Postgres-only (ADR-0039 Reconsidered 2026-06-17)
    op.execute("CREATE SCHEMA IF NOT EXISTS neuro")
    # No CREATE EXTENSION: gen_random_uuid() is core in PG18 (RULING 4); no pgvector (ADR-0044).
    # ENUM types first (tables reference them; create_type=False keeps create_table from re-creating).
    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)

    # --- tables (dependency order; the three cyclic/forward FKs are deferred below) ---------------
    op.create_table(
        "actors",
        sa.Column("actor_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("actor_key", sa.Text(), nullable=False),
        sa.Column("kind", actor_kind, nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("pg_role", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("actor_id"),
        sa.UniqueConstraint("actor_key"),
        schema=NEURO,
    )
    op.create_table(
        "assets",
        sa.Column("asset_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("asset_key", sa.Text(), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("loader_format", sa.Text(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=True),
        sa.Column("hf_repo", sa.Text(), nullable=True),
        sa.Column("hf_revision", sa.Text(), nullable=True),
        sa.Column("sae_training_run_id", sa.BigInteger(), nullable=True),  # FK deferred (assets<->sae cycle)
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.UniqueConstraint("asset_key"),
        schema=NEURO,
    )
    op.create_table(
        "database_identity",
        sa.Column("only_row", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("lane", lane_kind, nullable=False),
        sa.Column("instance_uuid", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "provisioned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("cloned_from", sa.Uuid(), nullable=True),
        sa.Column("schema_major", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("only_row", name="database_identity_singleton"),
        sa.PrimaryKeyConstraint("only_row"),
        schema=NEURO,
    )
    op.create_table(
        "expected_reproducibility_rules",
        sa.Column("rule_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("declared_mode", declared_mode, nullable=False),
        sa.Column("substrate_key", sa.Text(), nullable=False),
        sa.Column("expected", expected_level, nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("rule_id"),
        sa.UniqueConstraint("declared_mode", "substrate_key"),
        schema=NEURO,
    )
    op.create_table(
        "hook_points",
        sa.Column("hook_point_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("site_kind", sa.Text(), nullable=False),
        sa.Column("namespace", sa.Text(), server_default=sa.text("'core'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("hook_point_id"),
        sa.UniqueConstraint("canonical_name"),
        schema=NEURO,
    )
    op.create_table(
        "import_batches",
        sa.Column("import_batch_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("import_batch_id"),
        schema=NEURO,
    )
    op.create_table(
        "methods",
        sa.Column("method_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("method_key", sa.Text(), nullable=False),
        sa.Column(
            "active_version_id", sa.BigInteger(), nullable=True
        ),  # FK deferred (methods<->method_versions cycle)
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("method_id"),
        sa.UniqueConstraint("method_key"),
        schema=NEURO,
    )
    op.create_table(
        "metric_keys",
        sa.Column("metric_key", sa.Text(), nullable=False),
        sa.Column("value_kind", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("metric_key"),
        schema=NEURO,
    )
    op.create_table(
        "rate_cards",
        sa.Column("rate_card_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("backend_or_lane", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "effective_from", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("rate_card_id"),
        sa.UniqueConstraint("backend_or_lane", "effective_from"),
        schema=NEURO,
    )
    op.create_table(
        "residency_sets",
        sa.Column("residency_set_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("set_hash", sa.LargeBinary(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("residency_set_id"),
        sa.UniqueConstraint("set_hash"),
        schema=NEURO,
    )
    op.create_table(
        "storage_backends",
        sa.Column("backend_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("backend_key", sa.Text(), nullable=False),
        sa.Column("driver", sa.Text(), nullable=False),
        sa.Column("lane", storage_lane, nullable=False),
        sa.Column("base_uri", sa.Text(), nullable=False),
        sa.Column("is_cloud", sa.Boolean(), nullable=False),
        sa.Column("venue_purge_window", postgresql.INTERVAL(), nullable=True),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("backend_id"),
        sa.UniqueConstraint("backend_key"),
        schema=NEURO,
    )
    op.create_table(
        "system_health",
        sa.Column("health_key", sa.Text(), nullable=False),
        sa.Column("status", health_status, server_default=sa.text("'ok'"), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("stale_after", postgresql.INTERVAL(), nullable=True),
        sa.PrimaryKeyConstraint("health_key"),
        schema=NEURO,
    )
    op.create_table(
        "tokenizer_identities",
        sa.Column("tokenizer_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tokenizer_hash", sa.LargeBinary(), nullable=False),
        sa.Column("hf_repo", sa.Text(), nullable=True),
        sa.Column("hf_revision", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tokenizer_id"),
        sa.UniqueConstraint("tokenizer_hash"),
        schema=NEURO,
    )
    op.create_table(
        "campaigns",
        sa.Column("campaign_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("campaign_key", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["neuro.actors.actor_id"]),
        sa.PrimaryKeyConstraint("campaign_id"),
        sa.UniqueConstraint("campaign_key"),
        schema=NEURO,
    )
    op.create_table(
        "external_records",
        sa.Column("external_record_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_pk", sa.Text(), nullable=False),
        sa.Column("payload_text", sa.Text(), nullable=False),
        sa.Column("payload_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["neuro.import_batches.import_batch_id"]),
        sa.PrimaryKeyConstraint("external_record_id"),
        sa.UniqueConstraint("source_system", "source_table", "source_pk"),
        schema=NEURO,
    )
    op.create_index(
        "external_records_kind_idx",
        "external_records",
        [sa.literal_column("(payload_jsonb ->> 'kind')")],
        unique=False,
        schema=NEURO,
    )
    op.create_table(
        "hook_bindings",
        sa.Column("hook_binding_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("hook_point_id", sa.BigInteger(), nullable=False),
        sa.Column("framework", sa.Text(), nullable=False),
        sa.Column("version_range", sa.Text(), nullable=False),
        sa.Column("arch_family", sa.Text(), nullable=False),
        sa.Column("framework_site", sa.Text(), nullable=False),
        sa.Column("requires_eager_attn", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("requires_compat_mode", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(["hook_point_id"], ["neuro.hook_points.hook_point_id"]),
        sa.PrimaryKeyConstraint("hook_binding_id"),
        sa.UniqueConstraint("hook_point_id", "framework", "version_range", "arch_family"),
        schema=NEURO,
    )
    op.create_table(
        "method_versions",
        sa.Column("method_version_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("method_id", sa.BigInteger(), nullable=False),
        sa.Column("semver", sa.Text(), nullable=False),
        sa.Column("code_sha", sa.LargeBinary(), nullable=True),
        sa.Column(
            "registered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["method_id"], ["neuro.methods.method_id"]),
        sa.PrimaryKeyConstraint("method_version_id"),
        sa.UniqueConstraint("method_id", "semver"),
        schema=NEURO,
    )
    op.create_table(
        "model_identities",
        sa.Column("model_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("identity_hash", sa.LargeBinary(), nullable=False),
        sa.Column("hf_repo", sa.Text(), nullable=True),
        sa.Column("hf_revision", sa.Text(), nullable=True),
        sa.Column("dtype_quant", sa.Text(), nullable=False),
        sa.Column("tokenizer_id", sa.BigInteger(), nullable=False),
        sa.Column("serving_stack", sa.Text(), nullable=False),
        sa.Column("serving_version", sa.Text(), nullable=False),
        sa.Column("arch_family", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tokenizer_id"], ["neuro.tokenizer_identities.tokenizer_id"]),
        sa.PrimaryKeyConstraint("model_id"),
        sa.UniqueConstraint(
            "hf_repo",
            "hf_revision",
            "dtype_quant",
            "tokenizer_id",
            "serving_stack",
            "serving_version",
            "arch_family",
            name="model_components_uq",
            postgresql_nulls_not_distinct=True,
        ),
        sa.UniqueConstraint("identity_hash"),
        schema=NEURO,
    )
    op.create_table(
        "probe_reports",
        sa.Column("probe_report_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("probe_key", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("status", health_status, nullable=False),
        sa.Column("report_text", sa.Text(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["neuro.actors.actor_id"]),
        sa.PrimaryKeyConstraint("probe_report_id"),
        schema=NEURO,
    )
    op.create_table(
        "fingerprints",
        sa.Column("fingerprint_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("fingerprint_hash", sa.LargeBinary(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("declared_mode", declared_mode, nullable=False),
        sa.Column("semantic_config", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["neuro.model_identities.model_id"]),
        sa.PrimaryKeyConstraint("fingerprint_id"),
        sa.UniqueConstraint("fingerprint_hash"),
        schema=NEURO,
    )
    op.create_table(
        "intervention_specs",
        sa.Column("intervention_spec_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("spec_hash", sa.LargeBinary(), nullable=False),
        sa.Column("method_version_id", sa.BigInteger(), nullable=False),
        sa.Column("spec_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["method_version_id"], ["neuro.method_versions.method_version_id"]),
        sa.PrimaryKeyConstraint("intervention_spec_id"),
        sa.UniqueConstraint("spec_hash"),
        schema=NEURO,
    )
    op.create_table(
        "lineage_edges",
        sa.Column("lineage_edge_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("edge_kind", edge_kind, nullable=False),
        sa.Column("src_entity", sa.Text(), nullable=False),
        sa.Column("dst_entity", sa.Text(), nullable=False),
        sa.Column("method_version_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["method_version_id"], ["neuro.method_versions.method_version_id"]),
        sa.PrimaryKeyConstraint("lineage_edge_id"),
        sa.UniqueConstraint("edge_kind", "src_entity", "dst_entity"),
        schema=NEURO,
    )
    op.create_table(
        "promotions",
        sa.Column("promotion_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("external_record_id", sa.BigInteger(), nullable=False),
        sa.Column("promoted_kind", sa.Text(), nullable=False),
        sa.Column("promoted_pk", sa.BigInteger(), nullable=False),
        sa.Column("method_version_id", sa.BigInteger(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["external_record_id"], ["neuro.external_records.external_record_id"]),
        sa.ForeignKeyConstraint(["method_version_id"], ["neuro.method_versions.method_version_id"]),
        sa.PrimaryKeyConstraint("promotion_id"),
        sa.UniqueConstraint("external_record_id", "promoted_kind"),
        schema=NEURO,
    )
    op.create_table(
        "residency_set_members",
        sa.Column("residency_set_member_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("residency_set_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=True),
        sa.Column("asset_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "(model_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int = 1", name="residency_member_one_ref"
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["neuro.assets.asset_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["neuro.model_identities.model_id"]),
        sa.ForeignKeyConstraint(["residency_set_id"], ["neuro.residency_sets.residency_set_id"]),
        sa.PrimaryKeyConstraint("residency_set_member_id"),
        sa.UniqueConstraint(
            "residency_set_id",
            "model_id",
            "asset_id",
            name="residency_member_uq",
            postgresql_nulls_not_distinct=True,
        ),
        schema=NEURO,
    )
    op.create_index(
        "residency_set_one_base_model_uq",
        "residency_set_members",
        ["residency_set_id"],
        unique=True,
        schema=NEURO,
        postgresql_where=sa.text("model_id IS NOT NULL"),
    )
    op.create_table(
        "runs",
        sa.Column("run_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_key", sa.Text(), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("work_slug", sa.Text(), nullable=False),
        sa.Column("variant_digest", sa.Text(), nullable=False),
        sa.Column("run_kind", run_kind, nullable=False),
        sa.Column("fingerprint_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("is_unlabeled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("spec_hash", sa.LargeBinary(), nullable=True),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.Column("expected_level_override", expected_level, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["neuro.actors.actor_id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["neuro.campaigns.campaign_id"]),
        sa.ForeignKeyConstraint(["fingerprint_id"], ["neuro.fingerprints.fingerprint_id"]),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("run_key"),
        schema=NEURO,
    )
    op.create_index(
        "runs_experiment_variant_uq",
        "runs",
        ["campaign_id", "work_slug", "variant_digest", "invocation_id"],
        unique=True,
        schema=NEURO,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("run_kind = 'experiment'"),
    )
    op.create_index(
        "runs_unlabeled_idx",
        "runs",
        ["created_at"],
        unique=False,
        schema=NEURO,
        postgresql_where=sa.text("is_unlabeled"),
    )
    op.create_table(
        "bundles",
        sa.Column("bundle_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("bundle_uuid", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("state", bundle_state, server_default=sa.text("'unsealed'"), nullable=False),
        sa.Column("backend_id", sa.BigInteger(), nullable=False),
        sa.Column("manifest_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("venue_purge_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["backend_id"], ["neuro.storage_backends.backend_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("bundle_id"),
        sa.UniqueConstraint("bundle_uuid"),
        schema=NEURO,
    )
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_key", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("job_role", sa.Text(), nullable=True),
        sa.Column("shard_key", sa.Text(), nullable=True),
        sa.Column("shard_spec", sa.Text(), nullable=True),
        sa.Column("state", job_state, server_default=sa.text("'queued'"), nullable=False),
        sa.Column("queue", sa.Text(), server_default=sa.text("'default'"), nullable=False),
        sa.Column("gpu_class", sa.Text(), nullable=True),
        sa.Column("vram_needed_mb", sa.Integer(), nullable=True),
        sa.Column(
            "capabilities", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("residency_set_id", sa.BigInteger(), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claim_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_by", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expiry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("refusal_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_class", job_error_class, nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("shard_key IS NULL OR job_role IS NOT NULL", name="jobs_shard_role"),
        sa.ForeignKeyConstraint(["claimed_by"], ["neuro.actors.actor_id"]),
        sa.ForeignKeyConstraint(["residency_set_id"], ["neuro.residency_sets.residency_set_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("job_key"),
        schema=NEURO,
    )
    op.create_index(
        "jobs_claimable_idx",
        "jobs",
        ["queue", "gpu_class", "vram_needed_mb"],
        unique=False,
        schema=NEURO,
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(
        "jobs_shard_idempotency_uq",
        "jobs",
        ["run_id", "job_role", "shard_key"],
        unique=True,
        schema=NEURO,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("shard_key IS NOT NULL"),
    )
    op.create_table(
        "prompt_sets",
        sa.Column("prompt_set_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("set_kind", sa.Text(), nullable=False),
        sa.Column("derived_from_run_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["derived_from_run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("prompt_set_id"),
        sa.UniqueConstraint("content_hash"),
        schema=NEURO,
    )
    op.create_table(
        "replicate_links",
        sa.Column("replicate_link_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("original_run_id", sa.BigInteger(), nullable=False),
        sa.Column("replicate_run_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("original_run_id <> replicate_run_id", name="replicate_distinct"),
        sa.ForeignKeyConstraint(["original_run_id"], ["neuro.runs.run_id"]),
        sa.ForeignKeyConstraint(["replicate_run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("replicate_link_id"),
        sa.UniqueConstraint("original_run_id", "replicate_run_id"),
        schema=NEURO,
    )
    op.create_table(
        "run_metrics",
        sa.Column("run_metric_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("metric_key", sa.Text(), nullable=False),
        sa.Column("value_num", sa.Double(), nullable=True),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "value_json IS NULL OR octet_length(value_json) <= 8192", name="run_metrics_valve"
        ),
        sa.ForeignKeyConstraint(["metric_key"], ["neuro.metric_keys.metric_key"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("run_metric_id"),
        sa.UniqueConstraint("run_id", "metric_key"),
        schema=NEURO,
    )
    op.create_table(
        "run_plans",
        sa.Column("run_plan_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("est_gpu_hours", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("est_usd", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("est_su", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("run_plan_id"),
        schema=NEURO,
    )
    op.create_table(
        "sae_training_runs",
        sa.Column("sae_training_run_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("trainer_config_text", sa.Text(), nullable=False),
        sa.Column("dataset_identity_hash", sa.LargeBinary(), nullable=False),
        sa.Column("token_count", sa.BigInteger(), nullable=True),
        sa.Column("library_version", sa.Text(), nullable=False),
        sa.Column("produced_asset_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["produced_asset_id"], ["neuro.assets.asset_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("sae_training_run_id"),
        schema=NEURO,
    )
    op.create_table(
        "spend_entries",
        sa.Column("spend_entry_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("rate_card_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("is_standing", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["rate_card_id"], ["neuro.rate_cards.rate_card_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("spend_entry_id"),
        schema=NEURO,
    )
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("artifact_uuid", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("bundle_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", artifact_kind, nullable=False),
        sa.Column("backend_id", sa.BigInteger(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("shape", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("dtype", sa.Text(), nullable=True),
        sa.Column("retention", retention_class, server_default=sa.text("'ttl'"), nullable=False),
        sa.Column("recompute_recipe", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind NOT IN ('dense_tensor', 'derived_feature') OR (shape IS NOT NULL AND dtype IS NOT NULL)",
            name="artifacts_tensor_shape",
        ),
        sa.ForeignKeyConstraint(["backend_id"], ["neuro.storage_backends.backend_id"]),
        sa.ForeignKeyConstraint(["bundle_id"], ["neuro.bundles.bundle_id"]),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint("artifact_uuid"),
        sa.UniqueConstraint("backend_id", "uri"),
        schema=NEURO,
    )
    op.create_table(
        "divergence_measurements",
        sa.Column("divergence_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("replicate_link_id", sa.BigInteger(), nullable=False),
        sa.Column("method_version_id", sa.BigInteger(), nullable=False),
        sa.Column("max_abs_diff", sa.Double(), nullable=True),
        sa.Column("max_rel_diff", sa.Double(), nullable=True),
        sa.Column("argmax_flip_rate", sa.Double(), nullable=True),
        sa.Column("answer_letter_flip_rate", sa.Double(), nullable=True),
        sa.Column("near_tie_margin_nats", sa.Double(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["method_version_id"], ["neuro.method_versions.method_version_id"]),
        sa.ForeignKeyConstraint(["replicate_link_id"], ["neuro.replicate_links.replicate_link_id"]),
        sa.PrimaryKeyConstraint("divergence_id"),
        sa.UniqueConstraint("replicate_link_id", "method_version_id"),
        schema=NEURO,
    )
    op.create_table(
        "job_dependencies",
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("depends_on", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("job_id <> depends_on", name="job_dep_distinct"),
        sa.ForeignKeyConstraint(["depends_on"], ["neuro.jobs.job_id"]),
        sa.ForeignKeyConstraint(["job_id"], ["neuro.jobs.job_id"]),
        sa.PrimaryKeyConstraint("job_id", "depends_on"),
        schema=NEURO,
    )
    op.create_table(
        "run_inputs",
        sa.Column("run_input_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "prompt_set_id", sa.BigInteger(), nullable=True
        ),  # FK deferred (forward ref to prompt_sets)
        sa.Column("asset_id", sa.BigInteger(), nullable=True),
        sa.Column("intervention_spec_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(role = 'stimulus') = (prompt_set_id IS NOT NULL) AND "
            "(role = 'intervention') = (intervention_spec_id IS NOT NULL) AND "
            "(role = 'asset') = (asset_id IS NOT NULL)",
            name="run_inputs_role_referent",
        ),
        sa.CheckConstraint(
            "(prompt_set_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int + (intervention_spec_id IS NOT NULL)::int = 1",
            name="run_inputs_one_ref",
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["neuro.assets.asset_id"]),
        sa.ForeignKeyConstraint(["intervention_spec_id"], ["neuro.intervention_specs.intervention_spec_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("run_input_id"),
        sa.UniqueConstraint(
            "run_id",
            "role",
            "prompt_set_id",
            "asset_id",
            "intervention_spec_id",
            name="run_inputs_uq",
            postgresql_nulls_not_distinct=True,
        ),
        schema=NEURO,
    )
    op.create_table(
        "work_leases",
        sa.Column("lease_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=False),
        sa.Column("leased_by", sa.BigInteger(), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_heartbeat", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["neuro.jobs.job_id"]),
        sa.ForeignKeyConstraint(["leased_by"], ["neuro.actors.actor_id"]),
        sa.PrimaryKeyConstraint("lease_id"),
        schema=NEURO,
    )
    op.create_index(
        "work_leases_active_uq",
        "work_leases",
        ["job_id"],
        unique=True,
        schema=NEURO,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_table(
        "capture_events",
        sa.Column("capture_event_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("request_spill_artifact_id", sa.BigInteger(), nullable=True),
        sa.Column("response_spill_artifact_id", sa.BigInteger(), nullable=True),
        sa.Column("provenance_header", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(request_text IS NULL OR octet_length(request_text) <= 8192) AND "
            "(response_text IS NULL OR octet_length(response_text) <= 8192)",
            name="capture_inline_cap",
        ),
        sa.CheckConstraint(
            "request_text IS NOT NULL OR response_text IS NOT NULL "
            "OR request_spill_artifact_id IS NOT NULL OR response_spill_artifact_id IS NOT NULL",
            name="capture_has_payload",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["neuro.actors.actor_id"]),
        sa.ForeignKeyConstraint(["job_id"], ["neuro.jobs.job_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["neuro.model_identities.model_id"]),
        sa.ForeignKeyConstraint(["request_spill_artifact_id"], ["neuro.artifacts.artifact_id"]),
        sa.ForeignKeyConstraint(["response_spill_artifact_id"], ["neuro.artifacts.artifact_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("capture_event_id"),
        sa.UniqueConstraint("run_id", "event_key"),
        schema=NEURO,
    )
    op.create_table(
        "prompt_items",
        sa.Column("prompt_item_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("prompt_set_id", sa.BigInteger(), nullable=False),
        sa.Column("item_ordinal", sa.Integer(), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=True),
        sa.Column("spill_artifact_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "(prompt_text IS NOT NULL)::int + (spill_artifact_id IS NOT NULL)::int = 1",
            name="prompt_has_payload",
        ),
        sa.CheckConstraint(
            "prompt_text IS NULL OR octet_length(prompt_text) <= 8192", name="prompt_inline_cap"
        ),
        sa.ForeignKeyConstraint(["prompt_set_id"], ["neuro.prompt_sets.prompt_set_id"]),
        sa.ForeignKeyConstraint(["spill_artifact_id"], ["neuro.artifacts.artifact_id"]),
        sa.PrimaryKeyConstraint("prompt_item_id"),
        sa.UniqueConstraint("prompt_set_id", "item_ordinal"),
        schema=NEURO,
    )
    op.create_table(
        "table_manifests",
        sa.Column("table_manifest_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("dataset_name", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=True),
        sa.Column("hook_point_id", sa.BigInteger(), nullable=True),
        sa.Column("asset_id", sa.BigInteger(), nullable=True),
        sa.Column("schema_major", sa.Integer(), nullable=False),
        sa.Column("partition_path", sa.Text(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("footer_kv_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["neuro.artifacts.artifact_id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["neuro.assets.asset_id"]),
        sa.ForeignKeyConstraint(["hook_point_id"], ["neuro.hook_points.hook_point_id"]),
        sa.ForeignKeyConstraint(["model_id"], ["neuro.model_identities.model_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["neuro.runs.run_id"]),
        sa.PrimaryKeyConstraint("table_manifest_id"),
        sa.UniqueConstraint("dataset_name", "partition_path", "artifact_id"),
        schema=NEURO,
    )
    op.create_table(
        "mcq_items",
        sa.Column("mcq_item_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("prompt_item_id", sa.BigInteger(), nullable=False),
        sa.Column("correct_letter", sa.Text(), nullable=False),
        sa.Column("num_options", sa.Integer(), nullable=False),
        sa.CheckConstraint("num_options BETWEEN 2 AND 26", name="mcq_num_options"),
        sa.ForeignKeyConstraint(["prompt_item_id"], ["neuro.prompt_items.prompt_item_id"]),
        sa.PrimaryKeyConstraint("mcq_item_id"),
        sa.UniqueConstraint("prompt_item_id"),
        schema=NEURO,
    )
    op.create_table(
        "stimulus_structures",
        sa.Column("stimulus_structure_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("prompt_item_id", sa.BigInteger(), nullable=False),
        sa.Column("structure_kind", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.Text(), nullable=True),
        sa.Column("structure_text", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["prompt_item_id"], ["neuro.prompt_items.prompt_item_id"]),
        sa.PrimaryKeyConstraint("stimulus_structure_id"),
        sa.UniqueConstraint("prompt_item_id", "structure_kind"),
        schema=NEURO,
    )
    op.create_table(
        "mcq_options",
        sa.Column("mcq_option_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("mcq_item_id", sa.BigInteger(), nullable=False),
        sa.Column("letter", sa.Text(), nullable=False),
        sa.Column("option_text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["mcq_item_id"], ["neuro.mcq_items.mcq_item_id"]),
        sa.PrimaryKeyConstraint("mcq_option_id"),
        sa.UniqueConstraint("mcq_item_id", "letter"),
        schema=NEURO,
    )
    op.create_table(
        "mcq_permutations",
        sa.Column("mcq_permutation_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("mcq_item_id", sa.BigInteger(), nullable=False),
        sa.Column("permutation", sa.Text(), nullable=False),
        sa.Column("correct_position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["mcq_item_id"], ["neuro.mcq_items.mcq_item_id"]),
        sa.PrimaryKeyConstraint("mcq_permutation_id"),
        sa.UniqueConstraint("mcq_item_id", "permutation"),
        schema=NEURO,
    )

    # --- deferred / cyclic FKs (added after every table exists; names match phase3-ddl.sql) -------
    op.create_foreign_key(
        "methods_active_version_fk",
        "methods",
        "method_versions",
        ["active_version_id"],
        ["method_version_id"],
        source_schema=NEURO,
        referent_schema=NEURO,
    )
    op.create_foreign_key(  # breaks the assets<->sae_training_runs cycle
        "assets_sae_training_run_fk",
        "assets",
        "sae_training_runs",
        ["sae_training_run_id"],
        ["sae_training_run_id"],
        source_schema=NEURO,
        referent_schema=NEURO,
    )
    op.create_foreign_key(
        "run_inputs_prompt_set_fk",
        "run_inputs",
        "prompt_sets",
        ["prompt_set_id"],
        ["prompt_set_id"],
        source_schema=NEURO,
        referent_schema=NEURO,
    )

    # --- assign-once guard: ONE reusable function applied to runs.fingerprint_id (ADR-0005/0007) ---
    # LOAD-BEARING: fingerprint_id is experiment identity with no read-time backstop; this trigger is
    # its only defense — permits NULL->value once (adhoc adoption), refuses value->different/NULL.
    # NOTE: the SQL below is intentionally flush-left so the stored function body is byte-identical to
    # phase3-ddl.sql (pg_get_functiondef preserves body whitespace; the parity proof compares it).
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.assert_assign_once() RETURNS trigger AS $$
DECLARE
    col     text := TG_ARGV[0];
    old_val text := to_jsonb(OLD) ->> col;
    new_val text := to_jsonb(NEW) ->> col;
BEGIN
    IF old_val IS NOT NULL AND new_val IS DISTINCT FROM old_val THEN
        RAISE EXCEPTION 'assign-once violation on %.%: immutable once set (old=%, new=%)',
            TG_TABLE_NAME, col, old_val, new_val;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""")
    op.execute(
        """
        CREATE TRIGGER runs_fingerprint_assign_once
            BEFORE UPDATE OF fingerprint_id ON neuro.runs
            FOR EACH ROW EXECUTE FUNCTION neuro.assert_assign_once('fingerprint_id');
        """
    )

    # COMMENT ON — part of the frozen DDL of record. R6: keeps "migrations-from-zero == phase3-ddl.sql"
    # literally true, including pg_description (the parity test queries it). Strings are byte-identical
    # to the DDL; qualification (neuro.x) is cosmetic — pg_description stores only the comment text.
    op.execute(
        "COMMENT ON TABLE neuro.database_identity IS "
        "'ADR-0006: one-row positive identity; verified before any write; clone rewrites identity first.'"
    )
    op.execute(
        "COMMENT ON TABLE neuro.actors IS "
        "'phase0 Q13: actor/origin stamp on every run and capture from day one.'"
    )
    op.execute(
        "COMMENT ON TABLE neuro.method_versions IS "
        "'ADR-0011 governance trio: derived rows carry method_version_id; import-time registry/runtime parity asserted.'"
    )
    op.execute(
        "COMMENT ON TABLE neuro.hook_points IS "
        "'ADR-0016: lake hook columns must reference canonical_name; defends TL/nnsight vocab churn.'"
    )
    op.execute(
        "COMMENT ON TABLE neuro.fingerprints IS "
        "'ADR-0005: experiment identity. INSERT-only via grant. Distinct from sha256 (storage) and divergence (reproducibility).'"
    )
    op.execute(
        "COMMENT ON COLUMN neuro.artifacts.deleted_at IS "
        "'phase0 Q6: deletion recorded; artifact row + checksum + shape + recompute_recipe persist so provenance never dangles.'"
    )
    op.execute(
        "COMMENT ON TABLE neuro.capture_events IS "
        "'ADR-0002 cardinality ceiling: finest grain PG stores. Per-token data lives in the lake via table_manifests.'"
    )
    op.execute(
        "COMMENT ON TABLE neuro.system_health IS "
        "'ADR-0020: backup>8d stale or WAL lag>threshold => status=blocked => registrar/dispatch refuse loudly.'"
    )
    op.execute(
        "COMMENT ON TABLE neuro.lineage_edges IS "
        "'ADR-0043: edges only; identities live in typed tables. Enables ADR-0025 taint retrofit.'"
    )
    # Grants live in phase3-grants.sql, applied by provisioning AFTER roles exist (Finding 4; ADR-0007).


def downgrade() -> None:
    # Single-revision teardown (Postgres-only). Drop every neuro object EXCEPT the alembic version
    # table (so alembic's own bookkeeping survives the downgrade). CASCADE handles the cyclic FKs and
    # the trigger; enum types and the assign-once function are dropped explicitly.
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT tablename FROM pg_tables
                     WHERE schemaname = 'neuro' AND tablename <> 'alembic_version' LOOP
                EXECUTE format('DROP TABLE IF EXISTS neuro.%I CASCADE', r.tablename);
            END LOOP;
            FOR r IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                     WHERE n.nspname = 'neuro' AND t.typtype = 'e' LOOP
                EXECUTE format('DROP TYPE IF EXISTS neuro.%I CASCADE', r.typname);
            END LOOP;
            DROP FUNCTION IF EXISTS neuro.assert_assign_once() CASCADE;
        END $$;
        """
    )
