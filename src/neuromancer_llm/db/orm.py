"""neuromancer-llm — ORM (SQLAlchemy 2.0 declarative).

Mirrors tests/reference/phase3-ddl.sql (the frozen canonical DDL) 1:1. This is the typed
query/identity layer; the schema is
owned by Alembic (migrations/). Postgres-only — there is no SQLite target
(ADR-0039 Reconsidered 2026-06-17); PG-specific types (INTERVAL / JSONB / ARRAY / Uuid),
NULLS NOT DISTINCT, and the assign-once trigger are used directly. Bindings preserved:

  * wire bodies / configs are Text, never JSON (ADR-0003) — JSON appears only on
    ExternalRecord.payload_jsonb (the import sidecar).
  * identity-bearing fields are real columns; component uniqueness is enforced by
    Index(..., unique=True) incl. a NULLS-NOT-DISTINCT backstop (ADR-0005).
  * fingerprints are INSERT-only — enforced by GRANT, mirrored here by a repository
    rule (no .update path is exposed for Fingerprint).
"""

from __future__ import annotations

import datetime as _dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, INTERVAL, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="neuro")  # real default schema (the prior metadata_schema attr was inert)


def _ts() -> Mapped[_dt.datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# --- enums (names match the PG types in phase3-ddl.sql) ---------------------
LaneKind = ENUM(
    "canonical", "staging", "test", "unknown", name="lane_kind", schema="neuro", create_type=False
)
ActorKind = ENUM(
    "human", "agent", "scheduled_worker", "importer", name="actor_kind", schema="neuro", create_type=False
)
RunKind = ENUM(
    "experiment",
    "adhoc",
    "import",
    "replicate",
    "derivation",
    name="run_kind",
    schema="neuro",
    create_type=False,
)
DeclaredMode = ENUM(
    "deterministic_algo",
    "greedy",
    "seeded_sampling",
    "unseeded_sampling",
    name="declared_mode",
    schema="neuro",
    create_type=False,
)
ExpectedLevel = ENUM(
    "bitwise", "tolerance", "distributional", "none", name="expected_level", schema="neuro", create_type=False
)
JobState = ENUM(
    "blocked",
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "dead_letter",
    "cancelled",
    name="job_state",
    schema="neuro",
    create_type=False,
)  # +C2 'blocked'
JobErrorClass = ENUM(
    "permanent",
    "transient",
    "resource_mismatch",
    "lease_expired",
    name="job_error_class",
    schema="neuro",
    create_type=False,
)
BundleState = ENUM(
    "unsealed",
    "sealed",
    "registered",
    "tombstoned",
    "staged",
    "verified",
    name="bundle_state",
    schema="neuro",
    create_type=False,
)
ArtifactKind = ENUM(
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
    schema="neuro",
    create_type=False,
)
RetentionClass = ENUM(
    "ttl", "keep_forever", "pinned", name="retention_class", schema="neuro", create_type=False
)
StorageLane = ENUM("artifacts", "scratch", name="storage_lane", schema="neuro", create_type=False)
HealthStatus = ENUM("ok", "degraded", "blocked", name="health_status", schema="neuro", create_type=False)
EdgeKind = ENUM(
    "derived_from",
    "replicate_of",
    "promoted_from",
    "annotates",
    "curates",
    "paraphrase_of",
    "generated_by",
    "linked_call",
    name="edge_kind",
    schema="neuro",
    create_type=False,
)  # +A7 'linked_call'


# === GROUP A — identity & actors (ADR-0006/0007) ============================
class DatabaseIdentity(Base):
    __tablename__ = "database_identity"
    __table_args__ = (CheckConstraint("only_row", name="database_identity_singleton"), {"schema": "neuro"})
    only_row: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    lane: Mapped[str] = mapped_column(LaneKind, nullable=False)
    instance_uuid: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, server_default=text("gen_random_uuid()")
    )
    provisioned_at: Mapped[_dt.datetime] = _ts()
    cloned_from: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    schema_major: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    note: Mapped[str | None] = mapped_column(Text)


class Actor(Base):
    __tablename__ = "actors"
    __table_args__ = {"schema": "neuro"}
    actor_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(ActorKind, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    pg_role: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = _ts()
    retired_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))


# === GROUP B — registries ===================================================
class TokenizerIdentity(Base):
    __tablename__ = "tokenizer_identities"
    __table_args__ = {"schema": "neuro"}
    tokenizer_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tokenizer_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    hf_repo: Mapped[str | None] = mapped_column(Text)
    hf_revision: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = _ts()


class ModelIdentity(Base):
    __tablename__ = "model_identities"
    __table_args__ = (
        # NULLS NOT DISTINCT backstop over the 7 components (ADR-0005) — table UNIQUE constraint (matches DDL class)
        UniqueConstraint(
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
        {"schema": "neuro"},
    )
    model_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    identity_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    hf_repo: Mapped[str | None] = mapped_column(Text)
    hf_revision: Mapped[str | None] = mapped_column(Text)
    dtype_quant: Mapped[str] = mapped_column(Text, nullable=False)
    tokenizer_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.tokenizer_identities.tokenizer_id"), nullable=False
    )
    serving_stack: Mapped[str] = mapped_column(Text, nullable=False)
    serving_version: Mapped[str] = mapped_column(Text, nullable=False)
    arch_family: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[_dt.datetime] = _ts()


class Method(Base):
    __tablename__ = "methods"
    __table_args__ = {"schema": "neuro"}
    method_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    method_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # use_alter breaks the methods<->method_versions cycle for metadata emit order (DDL parity)
    active_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "neuro.method_versions.method_version_id", use_alter=True, name="methods_active_version_fk"
        )
    )
    created_at: Mapped[_dt.datetime] = _ts()


class MethodVersion(Base):
    __tablename__ = "method_versions"
    __table_args__ = (UniqueConstraint("method_id", "semver"), {"schema": "neuro"})
    method_version_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("neuro.methods.method_id"), nullable=False)
    semver: Mapped[str] = mapped_column(Text, nullable=False)
    code_sha: Mapped[bytes | None] = mapped_column(LargeBinary)
    registered_at: Mapped[_dt.datetime] = _ts()


class InterventionSpec(Base):
    __tablename__ = "intervention_specs"
    __table_args__ = {"schema": "neuro"}
    intervention_spec_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    spec_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    method_version_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.method_versions.method_version_id"), nullable=False
    )
    spec_text: Mapped[str] = mapped_column(Text, nullable=False)  # TEXT, not JSON (ADR-0003)
    created_at: Mapped[_dt.datetime] = _ts()


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = {"schema": "neuro"}
    asset_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    asset_type: Mapped[str] = mapped_column(Text, nullable=False)
    loader_format: Mapped[str] = mapped_column(Text, nullable=False)  # MANDATORY (ADR-0031)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    hf_repo: Mapped[str | None] = mapped_column(Text)  # NULL for local-trained (ADR-0032)
    hf_revision: Mapped[str | None] = mapped_column(Text)
    # use_alter breaks the assets<->sae_training_runs cycle for metadata emit order
    sae_training_run_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "neuro.sae_training_runs.sae_training_run_id", use_alter=True, name="assets_sae_training_run_fk"
        )
    )
    created_at: Mapped[_dt.datetime] = _ts()


class StorageBackend(Base):
    __tablename__ = "storage_backends"
    __table_args__ = {"schema": "neuro"}
    backend_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    backend_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    driver: Mapped[str] = mapped_column(Text, nullable=False)
    lane: Mapped[str] = mapped_column(StorageLane, nullable=False)
    base_uri: Mapped[str] = mapped_column(Text, nullable=False)
    is_cloud: Mapped[bool] = mapped_column(Boolean, nullable=False)
    venue_purge_window: Mapped[_dt.timedelta | None] = mapped_column(INTERVAL)  # ADR-0010
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[_dt.datetime] = _ts()


class HookPoint(Base):
    __tablename__ = "hook_points"
    __table_args__ = {"schema": "neuro"}
    hook_point_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    site_kind: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'core'"))
    created_at: Mapped[_dt.datetime] = _ts()


class HookBinding(Base):
    __tablename__ = "hook_bindings"
    __table_args__ = (
        UniqueConstraint("hook_point_id", "framework", "version_range", "arch_family"),
        {"schema": "neuro"},
    )
    hook_binding_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hook_point_id: Mapped[int] = mapped_column(ForeignKey("neuro.hook_points.hook_point_id"), nullable=False)
    framework: Mapped[str] = mapped_column(Text, nullable=False)
    version_range: Mapped[str] = mapped_column(Text, nullable=False)
    arch_family: Mapped[str] = mapped_column(Text, nullable=False)
    framework_site: Mapped[str] = mapped_column(Text, nullable=False)
    requires_eager_attn: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    requires_compat_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


# --- residency (Finding 1: canonical VRAM-budget/routing SET; jobs.residency_set_id FKs here) ---
class ResidencySet(Base):
    __tablename__ = "residency_sets"
    __table_args__ = {"schema": "neuro"}
    residency_set_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    set_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)  # hash-addressed reuse
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = _ts()


class ResidencySetMember(Base):
    __tablename__ = "residency_set_members"
    __table_args__ = (
        # a member is exactly one of: the base model, or an asset (SAE / steering vector / transcoder)
        CheckConstraint(
            "(model_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int = 1", name="residency_member_one_ref"
        ),
        # NULL-safe: a member appears at most once per set — table UNIQUE constraint (matches DDL class)
        UniqueConstraint(
            "residency_set_id",
            "model_id",
            "asset_id",
            name="residency_member_uq",
            postgresql_nulls_not_distinct=True,
        ),
        # B6: AT MOST one base model per set (>=1 existence is a composer/VRAM invariant, not DB-enforced)
        Index(
            "residency_set_one_base_model_uq",
            "residency_set_id",
            unique=True,
            postgresql_where=text("model_id IS NOT NULL"),
        ),
        {"schema": "neuro"},
    )
    residency_set_member_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    residency_set_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.residency_sets.residency_set_id"), nullable=False
    )
    model_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.model_identities.model_id"))
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.assets.asset_id"))


# === GROUP C — fingerprints, campaigns, runs ================================
class Fingerprint(Base):
    """INSERT-only (ADR-0005). The repository exposes no update() for this model."""

    __tablename__ = "fingerprints"
    __table_args__ = {"schema": "neuro"}
    fingerprint_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    fingerprint_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("neuro.model_identities.model_id"), nullable=False)
    declared_mode: Mapped[str] = mapped_column(DeclaredMode, nullable=False)
    semantic_config: Mapped[str] = mapped_column(Text, nullable=False)  # TEXT (ADR-0003)
    created_at: Mapped[_dt.datetime] = _ts()


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = {"schema": "neuro"}
    campaign_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    campaign_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("neuro.actors.actor_id"), nullable=False)
    created_at: Mapped[_dt.datetime] = _ts()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "runs_experiment_variant_uq",
            "campaign_id",
            "work_slug",
            "variant_digest",
            "invocation_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("run_kind = 'experiment'"),
        ),  # C1
        Index("runs_unlabeled_idx", "created_at", postgresql_where=text("is_unlabeled")),
        {"schema": "neuro"},
    )
    run_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("neuro.campaigns.campaign_id"), nullable=False)
    work_slug: Mapped[str] = mapped_column(Text, nullable=False)  # component column (ADR-0037)
    variant_digest: Mapped[str] = mapped_column(Text, nullable=False)  # component column (ADR-0037)
    run_kind: Mapped[str] = mapped_column(RunKind, nullable=False)
    fingerprint_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.fingerprints.fingerprint_id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("neuro.actors.actor_id"), nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    is_unlabeled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )  # ADR-0036
    spec_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid
    )  # C1: NULL=canonical run; non-NULL=re-invocation (force-new-run)
    expected_level_override: Mapped[str | None] = mapped_column(
        ExpectedLevel
    )  # A6: per-run override (never identity)
    created_at: Mapped[_dt.datetime] = _ts()
    finalized_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))


class RunInput(Base):
    __tablename__ = "run_inputs"
    __table_args__ = (
        # NULL-safe dedup (Postgres NULL <> NULL) — table UNIQUE constraint (matches DDL class)
        UniqueConstraint(
            "run_id",
            "role",
            "prompt_set_id",
            "asset_id",
            "intervention_spec_id",
            name="run_inputs_uq",
            postgresql_nulls_not_distinct=True,
        ),
        # exactly one referent FK per row (stimulus=prompt_set, intervention=intervention_spec, asset=asset)
        CheckConstraint(
            "(prompt_set_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int + (intervention_spec_id IS NOT NULL)::int = 1",
            name="run_inputs_one_ref",
        ),
        # B4: bind role to its referent + constrain the vocab
        CheckConstraint(
            "(role = 'stimulus') = (prompt_set_id IS NOT NULL) AND "
            "(role = 'intervention') = (intervention_spec_id IS NOT NULL) AND "
            "(role = 'asset') = (asset_id IS NOT NULL)",
            name="run_inputs_role_referent",
        ),
        {"schema": "neuro"},
    )
    run_input_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("neuro.runs.run_id"), nullable=False)
    prompt_set_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.prompt_sets.prompt_set_id"))
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.assets.asset_id"))
    intervention_spec_id: Mapped[int | None] = mapped_column(
        ForeignKey("neuro.intervention_specs.intervention_spec_id")
    )
    role: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # 'stimulus' | 'intervention' | 'asset' (residency dropped -> residency_sets)


class ReplicateLink(Base):
    __tablename__ = "replicate_links"
    __table_args__ = (
        UniqueConstraint("original_run_id", "replicate_run_id"),
        CheckConstraint("original_run_id <> replicate_run_id", name="replicate_distinct"),
        {"schema": "neuro"},
    )
    replicate_link_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    original_run_id: Mapped[int] = mapped_column(ForeignKey("neuro.runs.run_id"), nullable=False)
    replicate_run_id: Mapped[int] = mapped_column(ForeignKey("neuro.runs.run_id"), nullable=False)


class DivergenceMeasurement(Base):
    __tablename__ = "divergence_measurements"
    __table_args__ = (
        UniqueConstraint(
            "replicate_link_id", "method_version_id"
        ),  # one measurement per (replicate pair, method)
        {"schema": "neuro"},
    )
    divergence_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    replicate_link_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.replicate_links.replicate_link_id"), nullable=False
    )
    method_version_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.method_versions.method_version_id"), nullable=False
    )
    max_abs_diff: Mapped[float | None] = mapped_column(Double)
    max_rel_diff: Mapped[float | None] = mapped_column(Double)
    argmax_flip_rate: Mapped[float | None] = mapped_column(Double)
    answer_letter_flip_rate: Mapped[float | None] = mapped_column(Double)  # ADR-0004
    near_tie_margin_nats: Mapped[float | None] = mapped_column(Double)
    created_at: Mapped[_dt.datetime] = _ts()


class ExpectedReproducibilityRule(Base):
    __tablename__ = "expected_reproducibility_rules"
    __table_args__ = (UniqueConstraint("declared_mode", "substrate_key"), {"schema": "neuro"})
    rule_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    declared_mode: Mapped[str] = mapped_column(DeclaredMode, nullable=False)
    substrate_key: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[str] = mapped_column(ExpectedLevel, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


# === GROUP D — queue (ADR-0039) =============================================
class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "jobs_claimable_idx",
            "queue",
            "gpu_class",
            "vram_needed_mb",
            postgresql_where=text("state = 'queued'"),
        ),  # excludes 'blocked' by construction (C2)
        Index(
            "jobs_shard_idempotency_uq",
            "run_id",
            "job_role",
            "shard_key",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("shard_key IS NOT NULL"),
        ),  # A3 + B1 (NULL-safe)
        CheckConstraint("shard_key IS NULL OR job_role IS NOT NULL", name="jobs_shard_role"),  # B1
        {"schema": "neuro"},
    )
    job_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("neuro.runs.run_id"), nullable=False)
    job_role: Mapped[str | None] = mapped_column(Text)  # A3 component (composer sets it)
    shard_key: Mapped[str | None] = mapped_column(Text)  # A3 component (composer sets it)
    shard_spec: Mapped[str | None] = mapped_column(Text)  # A3: canonical shard spec (TEXT; ADR-0003)
    state: Mapped[str] = mapped_column(JobState, nullable=False, server_default=text("'queued'"))
    queue: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    gpu_class: Mapped[str | None] = mapped_column(Text)
    vram_needed_mb: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    residency_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("neuro.residency_sets.residency_set_id")
    )  # Finding 1
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claim_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    claimed_by: Mapped[int | None] = mapped_column(ForeignKey("neuro.actors.actor_id"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    expiry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    refusal_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_class: Mapped[str | None] = mapped_column(JobErrorClass)
    error_detail: Mapped[str | None] = mapped_column(Text)
    checkpoint_ref: Mapped[str | None] = mapped_column(Text)  # A4: resume-after-preemption pointer
    enqueued_at: Mapped[_dt.datetime] = _ts()
    updated_at: Mapped[_dt.datetime] = _ts()


class JobDependency(Base):
    __tablename__ = "job_dependencies"
    __table_args__ = (CheckConstraint("job_id <> depends_on", name="job_dep_distinct"), {"schema": "neuro"})
    job_id: Mapped[int] = mapped_column(ForeignKey("neuro.jobs.job_id"), primary_key=True)
    depends_on: Mapped[int] = mapped_column(ForeignKey("neuro.jobs.job_id"), primary_key=True)


class WorkLease(Base):
    __tablename__ = "work_leases"
    __table_args__ = (
        Index("work_leases_active_uq", "job_id", unique=True, postgresql_where=text("released_at IS NULL")),
        {"schema": "neuro"},
    )
    lease_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("neuro.jobs.job_id"), nullable=False)
    claim_token: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    leased_by: Mapped[int] = mapped_column(ForeignKey("neuro.actors.actor_id"), nullable=False)
    leased_at: Mapped[_dt.datetime] = _ts()
    expires_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat: Mapped[_dt.datetime] = _ts()
    released_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))


# === GROUP E — bundles, artifacts, manifests, capture =======================
class Bundle(Base):
    __tablename__ = "bundles"
    __table_args__ = {"schema": "neuro"}
    bundle_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bundle_uuid: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, unique=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[int] = mapped_column(ForeignKey("neuro.runs.run_id"), nullable=False)
    state: Mapped[str] = mapped_column(BundleState, nullable=False, server_default=text("'unsealed'"))
    backend_id: Mapped[int] = mapped_column(ForeignKey("neuro.storage_backends.backend_id"), nullable=False)
    manifest_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    venue_purge_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))  # ADR-0010
    sealed_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))
    tombstoned_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[_dt.datetime] = _ts()


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("backend_id", "uri"),
        # A1: tensor artifacts must carry shape + dtype; other kinds unaffected
        CheckConstraint(
            "kind NOT IN ('dense_tensor', 'derived_feature') OR (shape IS NOT NULL AND dtype IS NOT NULL)",
            name="artifacts_tensor_shape",
        ),
        {"schema": "neuro"},
    )
    artifact_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    artifact_uuid: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, unique=True, server_default=text("gen_random_uuid()")
    )
    bundle_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.bundles.bundle_id"))
    kind: Mapped[str] = mapped_column(ArtifactKind, nullable=False)
    backend_id: Mapped[int] = mapped_column(ForeignKey("neuro.storage_backends.backend_id"), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # storage integrity (ADR-0005)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    shape: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    dtype: Mapped[str | None] = mapped_column(Text)
    retention: Mapped[str] = mapped_column(RetentionClass, nullable=False, server_default=text("'ttl'"))
    recompute_recipe: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))  # deletion RECORDED
    created_at: Mapped[_dt.datetime] = _ts()


class TableManifest(Base):
    __tablename__ = "table_manifests"
    __table_args__ = (UniqueConstraint("dataset_name", "partition_path", "artifact_id"), {"schema": "neuro"})
    table_manifest_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    dataset_name: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.runs.run_id"), nullable=False
    )  # partition col as FK
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("neuro.model_identities.model_id")
    )  # partition col as FK
    hook_point_id: Mapped[int | None] = mapped_column(
        ForeignKey("neuro.hook_points.hook_point_id")
    )  # ADR-0016
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("neuro.assets.asset_id")
    )  # A5: links SAE-feature parquet to its SAE
    schema_major: Mapped[int] = mapped_column(Integer, nullable=False)
    partition_path: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("neuro.artifacts.artifact_id"), nullable=False)
    footer_kv_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[_dt.datetime] = _ts()


class CaptureEvent(Base):
    __tablename__ = "capture_events"
    __table_args__ = (
        UniqueConstraint("run_id", "event_key"),
        CheckConstraint(
            "(request_text IS NULL OR octet_length(request_text) <= 8192) AND "
            "(response_text IS NULL OR octet_length(response_text) <= 8192)",
            name="capture_inline_cap",
        ),
        CheckConstraint(
            "request_text IS NOT NULL OR response_text IS NOT NULL "
            "OR request_spill_artifact_id IS NOT NULL OR response_spill_artifact_id IS NOT NULL",
            name="capture_has_payload",
        ),
        {"schema": "neuro"},
    )
    capture_event_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("neuro.runs.run_id"), nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.jobs.job_id"))  # nullable = shard provenance
    event_key: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[int] = mapped_column(ForeignKey("neuro.model_identities.model_id"), nullable=False)
    actor_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.actors.actor_id"), nullable=False
    )  # RULING 1: stamp on every capture (phase0 Q13)
    origin: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # jobs.claimed_by actor for job/GPU; run actor for adhoc
    request_text: Mapped[str | None] = mapped_column(Text)  # TRUE wire bytes, TEXT (ADR-0003)
    response_text: Mapped[str | None] = mapped_column(Text)
    # A2 dual spill: request and response each get their own spill FK
    request_spill_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.artifacts.artifact_id"))
    response_spill_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.artifacts.artifact_id"))
    provenance_header: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[_dt.datetime] = _ts()


class MetricKey(Base):
    __tablename__ = "metric_keys"
    __table_args__ = {"schema": "neuro"}
    metric_key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_kind: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class RunMetric(Base):
    __tablename__ = "run_metrics"
    __table_args__ = (
        UniqueConstraint("run_id", "metric_key"),
        CheckConstraint(
            "value_json IS NULL OR octet_length(value_json) <= 8192", name="run_metrics_valve"
        ),  # ADR-0017
        {"schema": "neuro"},
    )
    run_metric_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("neuro.runs.run_id"), nullable=False)
    metric_key: Mapped[str] = mapped_column(ForeignKey("neuro.metric_keys.metric_key"), nullable=False)
    value_num: Mapped[float | None] = mapped_column(Double)
    value_json: Mapped[str | None] = mapped_column(Text)  # TEXT, capped


# === GROUP F — stimulus family (ADR-0023) ===================================
class PromptSet(Base):
    __tablename__ = "prompt_sets"
    __table_args__ = {"schema": "neuro"}
    prompt_set_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)  # phase0 Q1/Q3
    set_kind: Mapped[str] = mapped_column(Text, nullable=False)
    derived_from_run_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.runs.run_id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = _ts()


class PromptItem(Base):
    __tablename__ = "prompt_items"
    __table_args__ = (
        UniqueConstraint("prompt_set_id", "item_ordinal"),
        CheckConstraint(
            "prompt_text IS NULL OR octet_length(prompt_text) <= 8192", name="prompt_inline_cap"
        ),  # ADR-0022
        # B5: exactly one of inline prompt_text / spill_artifact_id
        CheckConstraint(
            "(prompt_text IS NOT NULL)::int + (spill_artifact_id IS NOT NULL)::int = 1",
            name="prompt_has_payload",
        ),
        {"schema": "neuro"},
    )
    prompt_item_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    prompt_set_id: Mapped[int] = mapped_column(ForeignKey("neuro.prompt_sets.prompt_set_id"), nullable=False)
    item_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_text: Mapped[str | None] = mapped_column(Text)
    spill_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.artifacts.artifact_id"))


class StimulusStructure(Base):
    __tablename__ = "stimulus_structures"
    __table_args__ = (UniqueConstraint("prompt_item_id", "structure_kind"), {"schema": "neuro"})
    stimulus_structure_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    prompt_item_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.prompt_items.prompt_item_id"), nullable=False
    )
    structure_kind: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str | None] = mapped_column(Text)
    structure_text: Mapped[str | None] = mapped_column(Text)


class McqItem(Base):
    __tablename__ = "mcq_items"
    __table_args__ = (
        CheckConstraint("num_options BETWEEN 2 AND 26", name="mcq_num_options"),
        {"schema": "neuro"},
    )
    mcq_item_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    prompt_item_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.prompt_items.prompt_item_id"), nullable=False, unique=True
    )
    correct_letter: Mapped[str] = mapped_column(Text, nullable=False)
    num_options: Mapped[int] = mapped_column(Integer, nullable=False)


class McqOption(Base):
    __tablename__ = "mcq_options"
    __table_args__ = (UniqueConstraint("mcq_item_id", "letter"), {"schema": "neuro"})
    mcq_option_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    mcq_item_id: Mapped[int] = mapped_column(ForeignKey("neuro.mcq_items.mcq_item_id"), nullable=False)
    letter: Mapped[str] = mapped_column(Text, nullable=False)
    option_text: Mapped[str] = mapped_column(Text, nullable=False)


class McqPermutation(Base):
    __tablename__ = "mcq_permutations"
    __table_args__ = (UniqueConstraint("mcq_item_id", "permutation"), {"schema": "neuro"})
    mcq_permutation_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    mcq_item_id: Mapped[int] = mapped_column(ForeignKey("neuro.mcq_items.mcq_item_id"), nullable=False)
    permutation: Mapped[str] = mapped_column(Text, nullable=False)
    correct_position: Mapped[int] = mapped_column(Integer, nullable=False)


# NOTE: NO McqResponse model. Promotion-on-demand only (ADR-0011 / C1). When the first
# experiment consumes it, a demand-triggered migration adds the table + this model, and the
# row type carries method_version_id (governance trio).


# === GROUP G — spend governance =============================================
class RateCard(Base):
    __tablename__ = "rate_cards"
    __table_args__ = (UniqueConstraint("backend_or_lane", "effective_from"), {"schema": "neuro"})
    rate_card_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    backend_or_lane: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    rate: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    effective_from: Mapped[_dt.datetime] = _ts()


class RunPlan(Base):
    __tablename__ = "run_plans"
    __table_args__ = {"schema": "neuro"}
    run_plan_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.runs.run_id"))
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    est_gpu_hours: Mapped[float | None] = mapped_column(Numeric(18, 4))
    est_usd: Mapped[float | None] = mapped_column(Numeric(18, 4))
    est_su: Mapped[float | None] = mapped_column(Numeric(18, 4))
    approved_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[_dt.datetime] = _ts()


class SpendEntry(Base):
    __tablename__ = "spend_entries"
    __table_args__ = {"schema": "neuro"}
    spend_entry_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.runs.run_id"))
    rate_card_id: Mapped[int] = mapped_column(ForeignKey("neuro.rate_cards.rate_card_id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    is_standing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    recorded_at: Mapped[_dt.datetime] = _ts()


# === GROUP H — health & probes ==============================================
class SystemHealth(Base):
    __tablename__ = "system_health"
    __table_args__ = {"schema": "neuro"}
    health_key: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(HealthStatus, nullable=False, server_default=text("'ok'"))
    detail: Mapped[str | None] = mapped_column(Text)
    measured_at: Mapped[_dt.datetime] = _ts()
    stale_after: Mapped[_dt.timedelta | None] = mapped_column(INTERVAL)


class ProbeReport(Base):
    __tablename__ = "probe_reports"
    __table_args__ = {"schema": "neuro"}
    probe_report_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    probe_key: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.actors.actor_id"))
    status: Mapped[str] = mapped_column(HealthStatus, nullable=False)
    report_text: Mapped[str | None] = mapped_column(Text)
    reported_at: Mapped[_dt.datetime] = _ts()


# === GROUP I — import / external (JSONB sidecar allowed HERE ONLY) ==========
class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = {"schema": "neuro"}
    import_batch_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[_dt.datetime] = _ts()
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ExternalRecord(Base):
    __tablename__ = "external_records"
    __table_args__ = (
        UniqueConstraint("source_system", "source_table", "source_pk"),
        Index(
            "external_records_kind_idx", text("(payload_jsonb ->> 'kind')")
        ),  # functional JSON index — HERE ONLY
        {"schema": "neuro"},
    )
    external_record_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.import_batches.import_batch_id"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_pk: Mapped[str] = mapped_column(Text, nullable=False)
    payload_text: Mapped[str] = mapped_column(Text, nullable=False)  # byte-exact (phase0 Q11)
    payload_jsonb: Mapped[dict | None] = mapped_column(JSONB)  # sidecar only (ADR-0003 exception)
    imported_at: Mapped[_dt.datetime] = _ts()


class Promotion(Base):
    __tablename__ = "promotions"
    __table_args__ = (UniqueConstraint("external_record_id", "promoted_kind"), {"schema": "neuro"})
    promotion_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    external_record_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.external_records.external_record_id"), nullable=False
    )
    promoted_kind: Mapped[str] = mapped_column(Text, nullable=False)
    promoted_pk: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method_version_id: Mapped[int] = mapped_column(
        ForeignKey("neuro.method_versions.method_version_id"), nullable=False
    )
    promoted_at: Mapped[_dt.datetime] = _ts()


# === GROUP J — SAE training provenance (ADR-0032) ===========================
class SaeTrainingRun(Base):
    __tablename__ = "sae_training_runs"
    __table_args__ = {"schema": "neuro"}
    sae_training_run_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    trainer_config_text: Mapped[str] = mapped_column(Text, nullable=False)  # TEXT (ADR-0003)
    dataset_identity_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_count: Mapped[int | None] = mapped_column(BigInteger)
    library_version: Mapped[str] = mapped_column(Text, nullable=False)
    produced_asset_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.assets.asset_id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("neuro.runs.run_id"))
    created_at: Mapped[_dt.datetime] = _ts()


# === GROUP K — lineage (edges only, ADR-0043) ===============================
class LineageEdge(Base):
    __tablename__ = "lineage_edges"
    __table_args__ = (UniqueConstraint("edge_kind", "src_entity", "dst_entity"), {"schema": "neuro"})
    lineage_edge_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    edge_kind: Mapped[str] = mapped_column(EdgeKind, nullable=False)
    src_entity: Mapped[str] = mapped_column(Text, nullable=False)
    dst_entity: Mapped[str] = mapped_column(Text, nullable=False)
    method_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("neuro.method_versions.method_version_id")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = _ts()
