-- ============================================================================
-- neuromancer-llm — Phase 3 canonical schema (PostgreSQL 18)
-- Synthesis DDL for checkpoint review. In Phase 4 the SCHEMA OBJECTS here (tables, constraints,
-- indexes, triggers) are produced BY Alembic (migrations-from-zero), not by hand; this file is the
-- authoritative target the initial migration materializes. Roles + GRANTs are a SEPARATE provisioning
-- step (phase3-grants.sql; see the Group L pointer below), applied after roles exist — so THIS file is
-- standalone-executable on an empty DB with no pre-existing roles (Finding 4).
--
-- Bindings honored throughout:
--   * identity-bearing fields => real columns or functional indexes (never parse strings)
--   * no per-token / per-feature tables in PG (cardinality law, ADR-0002)
--   * wire bodies are TEXT, never JSONB (ADR-0003); JSONB only in external_records sidecar
--   * UNKNOWN fails closed; positive lane assertion (ADR-0006)
--   * one queue, SKIP LOCKED, runtime-owned leases (ADR-0039)
--   * fingerprints INSERT-only via grant (ADR-0005/0007)
-- Each section cites the governing ADR(s).
-- ============================================================================

SET client_min_messages = warning;

-- No extensions required: gen_random_uuid() is core in PostgreSQL 18 (no pgcrypto needed; RULING 4),
-- and no pgvector day-one (ADR-0044, RESOLVED — embeddings are lake artifacts).

CREATE SCHEMA IF NOT EXISTS neuro;
SET search_path = neuro, public;

-- ----------------------------------------------------------------------------
-- ENUM / domain types
-- ----------------------------------------------------------------------------
CREATE TYPE lane_kind            AS ENUM ('canonical', 'staging', 'test', 'unknown');           -- ADR-0006
CREATE TYPE actor_kind           AS ENUM ('human', 'agent', 'scheduled_worker', 'importer');     -- phase0 Q13
CREATE TYPE run_kind             AS ENUM ('experiment', 'adhoc', 'import', 'replicate', 'derivation'); -- ADR-0036
CREATE TYPE declared_mode        AS ENUM ('deterministic_algo', 'greedy', 'seeded_sampling', 'unseeded_sampling'); -- ADR-0004
CREATE TYPE expected_level       AS ENUM ('bitwise', 'tolerance', 'distributional', 'none');     -- ADR-0004 (assessment, never identity)
CREATE TYPE job_state            AS ENUM ('blocked', 'queued', 'claimed', 'running', 'succeeded', 'failed', 'dead_letter', 'cancelled'); -- ADR-0039 (+C2 'blocked': non-claimable until deps succeed)
CREATE TYPE job_error_class      AS ENUM ('permanent', 'transient', 'resource_mismatch', 'lease_expired'); -- ADR-0039
CREATE TYPE bundle_state         AS ENUM ('unsealed', 'sealed', 'registered', 'tombstoned', 'staged', 'verified'); -- ADR-0010 (+0018 reserved staged/verified)
CREATE TYPE artifact_kind        AS ENUM ('wire_payload', 'dense_tensor', 'derived_feature', 'token_table', 'prompt_spill', 'export', 'external_record', 'sae_release', 'other'); -- ADR-0002/0003/0022
CREATE TYPE retention_class      AS ENUM ('ttl', 'keep_forever', 'pinned');                       -- phase0 Q6; ADR-0033/0034
CREATE TYPE storage_lane         AS ENUM ('artifacts', 'scratch');                                -- ADR-0040
CREATE TYPE health_status        AS ENUM ('ok', 'degraded', 'blocked');                           -- ADR-0020
CREATE TYPE edge_kind            AS ENUM ('derived_from', 'replicate_of', 'promoted_from', 'annotates', 'curates', 'paraphrase_of', 'generated_by', 'linked_call'); -- ADR-0043 (+A7 'linked_call': capture_event<->capture_event, e.g. OpenRouter GET /generation)

-- ============================================================================
-- GROUP A — Lanes v2 identity & actors (ADR-0006, ADR-0007, phase0 Q13)
-- ============================================================================

-- Singleton: exactly one row, written at provisioning, verified at connect.
CREATE TABLE database_identity (
    only_row        boolean      PRIMARY KEY DEFAULT true CONSTRAINT database_identity_singleton CHECK (only_row),   -- enforces single row
    lane            lane_kind    NOT NULL,
    instance_uuid   uuid         NOT NULL DEFAULT gen_random_uuid(),
    provisioned_at  timestamptz  NOT NULL DEFAULT now(),
    cloned_from     uuid         NULL,           -- set by clone/restore tooling, rewritten atomically-before-success
    schema_major    integer      NOT NULL DEFAULT 1,                          -- immutable path-tree root (ADR-0001 lake)
    note            text         NULL
);
-- UNKNOWN lane fails closed in code; it must never be written here as a usable canonical row.
COMMENT ON TABLE database_identity IS 'ADR-0006: one-row positive identity; verified before any write; clone rewrites identity first.';

CREATE TABLE actors (
    actor_id        bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_key       text         NOT NULL UNIQUE,            -- stable handle, e.g. 'owner', 'agent:composer', 'worker:desktop-4090'
    kind            actor_kind   NOT NULL,
    display_name    text         NOT NULL,
    pg_role         text         NULL,                       -- maps to the per-human / per-worker login (ADR-0007)
    created_at      timestamptz  NOT NULL DEFAULT now(),
    retired_at      timestamptz  NULL
);
COMMENT ON TABLE actors IS 'phase0 Q13: actor/origin stamp on every run and capture from day one.';

-- ============================================================================
-- GROUP B — Registries: models, tokenizers, methods, assets, backends, hooks
-- ============================================================================

-- Tokenizer identity, created BEFORE model_identities so the tokenizer_id FK is inline (cleanup).
CREATE TABLE tokenizer_identities (
    tokenizer_id    bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tokenizer_hash  bytea        NOT NULL UNIQUE,            -- durable identity
    hf_repo         text         NULL,
    hf_revision     text         NULL,
    note            text         NULL,
    created_at      timestamptz  NOT NULL DEFAULT now()
);

-- Model identity is 7-component first-class (ADR-0005). identity_hash is the durable key;
-- the NULLS NOT DISTINCT backstop catches composer drift even when a component is null.
CREATE TABLE model_identities (
    model_id        bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identity_hash   bytea        NOT NULL UNIQUE,            -- sha256 over the 7 components (canonical serialization)
    hf_repo         text         NULL,                       -- NULL allowed for locally-trained / API-opaque (ADR-0032)
    hf_revision     text         NULL,
    dtype_quant     text         NOT NULL,                   -- e.g. 'bf16', 'fp8-e4m3', 'int4-awq'
    tokenizer_id    bigint       NOT NULL REFERENCES tokenizer_identities(tokenizer_id),
    serving_stack   text         NOT NULL,                   -- e.g. 'vllm', 'vllm-lens', 'nnsight', 'openrouter'
    serving_version text         NOT NULL,
    arch_family     text         NOT NULL,                   -- e.g. 'llama', 'gemma2', 'qwen3'
    created_at      timestamptz  NOT NULL DEFAULT now(),
    -- backstop: the component tuple is unique even with nulls present (drift -> violation)
    CONSTRAINT model_components_uq UNIQUE NULLS NOT DISTINCT
        (hf_repo, hf_revision, dtype_quant, tokenizer_id, serving_stack, serving_version, arch_family)
);

-- methods + versions: register-first, fail-loud method identity (ADR-0005, NEVER speculative).
CREATE TABLE methods (
    method_id           bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    method_key          text     NOT NULL UNIQUE,            -- e.g. 'answer_letter_extraction', 'logit_lens'
    active_version_id   bigint   NULL,                       -- FK set after first version (deferred)
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE method_versions (
    method_version_id   bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    method_id           bigint   NOT NULL REFERENCES methods(method_id),
    semver              text     NOT NULL,
    code_sha            bytea    NULL,                       -- git SHA / content hash of the registered implementation
    registered_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (method_id, semver)
);
ALTER TABLE methods
    ADD CONSTRAINT methods_active_version_fk
    FOREIGN KEY (active_version_id) REFERENCES method_versions(method_version_id);
COMMENT ON TABLE method_versions IS 'ADR-0011 governance trio: derived rows carry method_version_id; import-time registry/runtime parity asserted.';

-- intervention specs: idempotent by spec_hash with force flag (ADR-0005).
CREATE TABLE intervention_specs (
    intervention_spec_id bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spec_hash           bytea    NOT NULL UNIQUE,
    method_version_id   bigint   NOT NULL REFERENCES method_versions(method_version_id),
    spec_text           text     NOT NULL,                   -- canonical serialized spec (TEXT, not JSONB; ADR-0003)
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- assets: SAE / steering-vector / transcoder registry. loader_format MANDATORY (ADR-0031),
-- sha256 + hf_revision are durable identity; library names are NOT identity.
CREATE TABLE assets (
    asset_id        bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_key       text         NOT NULL UNIQUE,
    asset_type      text         NOT NULL,                   -- 'sae' | 'steering_vector' | 'transcoder' | 'probe'
    loader_format   text         NOT NULL,                   -- e.g. 'saelens', 'qwen_scope_pt', 'raw_safetensors' (ADR-0031)
    sha256          bytea        NULL,                       -- durable identity when materialized
    hf_repo         text         NULL,                       -- NULL for locally-trained releases (ADR-0032)
    hf_revision     text         NULL,
    sae_training_run_id bigint   NULL,       -- FK deferred (sae_training_runs below; breaks the assets<->sae cycle)
    created_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE storage_backends (
    backend_id      bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    backend_key     text         NOT NULL UNIQUE,            -- 'azure-artifacts-prod', 'desktop-nvme', 'jetstream-scratch'
    driver          text         NOT NULL,                   -- 'azure_blob' | 'local_fs' | 's3' (s3 deferred; ADR-0040)
    lane            storage_lane NOT NULL,
    base_uri        text         NOT NULL,
    is_cloud        boolean      NOT NULL,
    venue_purge_window interval  NULL,                       -- ADR-0010: venue's own deletion clock for scratch
    quota_bytes     bigint       NULL,                       -- per-prefix dollar-calibrated, fail-closed (ADR-0040)
    created_at      timestamptz  NOT NULL DEFAULT now()
    -- NOTE: deliberately NO CHECK pinning driver to 'azure' (ADR-0040 data-driven).
);

-- Hook-point vocabulary registry: canonical site grammar, validated at shard close (ADR-0016).
CREATE TABLE hook_points (
    hook_point_id   bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name  text         NOT NULL UNIQUE,            -- 'L12.resid.post', 'L3.attn.pattern', 'unembed.logits'
    site_kind       text         NOT NULL,                   -- 'resid' | 'attn' | 'mlp' | 'embed' | 'unembed'
    namespace       text         NOT NULL DEFAULT 'core',
    created_at      timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE hook_points IS 'ADR-0016: lake hook columns must reference canonical_name; defends TL/nnsight vocab churn.';

-- Per-(framework, version-range, arch-family) binding rows + requires-flags (a1 pattern as data).
CREATE TABLE hook_bindings (
    hook_binding_id bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hook_point_id   bigint       NOT NULL REFERENCES hook_points(hook_point_id),
    framework       text         NOT NULL,                   -- 'transformer_lens' | 'nnsight' | 'vllm_lens'
    version_range   text         NOT NULL,                   -- semver range the binding is valid for
    arch_family     text         NOT NULL,
    framework_site  text         NOT NULL,                   -- the framework's own (churning) name
    requires_eager_attn boolean  NOT NULL DEFAULT false,
    requires_compat_mode boolean NOT NULL DEFAULT false,
    UNIQUE (hook_point_id, framework, version_range, arch_family)
);

-- Residency: a hash-addressed SET of artifacts (base model + SAEs + steering vectors) budgeted
-- against VRAM (phase0 Q5, ADR-0005/0039). The canonical routing/VRAM-budget entity; jobs.residency_set_id
-- FKs here. Residency lives here ALONE — run_inputs no longer carries a 'residency_member' role.
CREATE TABLE residency_sets (
    residency_set_id bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    set_hash        bytea        NOT NULL UNIQUE,            -- hash-addressed reuse of an identical residency SET
    note            text         NULL,
    created_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE residency_set_members (
    residency_set_member_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    residency_set_id bigint      NOT NULL REFERENCES residency_sets(residency_set_id),
    model_id        bigint       NULL REFERENCES model_identities(model_id),
    asset_id        bigint       NULL REFERENCES assets(asset_id),
    -- a member is exactly one of: the base model, or an asset (SAE / steering vector / transcoder)
    CONSTRAINT residency_member_one_ref CHECK ((model_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int = 1),
    -- NULL-safe: a member appears at most once per set
    CONSTRAINT residency_member_uq UNIQUE NULLS NOT DISTINCT (residency_set_id, model_id, asset_id)
);
-- B6: a residency set has AT MOST one base model (DB-enforced here); the >=1 existence half is a
-- composer/VRAM invariant, not DB-enforced. Adapters are the asset members.
CREATE UNIQUE INDEX residency_set_one_base_model_uq
    ON residency_set_members (residency_set_id) WHERE model_id IS NOT NULL;

-- ============================================================================
-- GROUP C — Fingerprints, campaigns, runs, inputs (ADR-0005, ADR-0037)
-- ============================================================================

-- INSERT-only (enforced by grant: REVOKE UPDATE/DELETE from writer). Raise-on-mismatch is app logic.
CREATE TABLE fingerprints (
    fingerprint_id  bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fingerprint_hash bytea       NOT NULL UNIQUE,            -- hash of the SEMANTIC config section wholesale
    model_id        bigint       NOT NULL REFERENCES model_identities(model_id),
    declared_mode   declared_mode NOT NULL,                  -- participates in identity (ADR-0004/0005)
    semantic_config text         NOT NULL,                   -- canonical serialized semantic section (TEXT; ADR-0003)
    created_at      timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE fingerprints IS 'ADR-0005: experiment identity. INSERT-only via grant. Distinct from sha256 (storage) and divergence (reproducibility).';

CREATE TABLE campaigns (
    campaign_id     bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_key    text         NOT NULL UNIQUE,            -- human-readable (ADR-0037)
    actor_id        bigint       NOT NULL REFERENCES actors(actor_id),
    created_at      timestamptz  NOT NULL DEFAULT now()
);

-- run_key components stored as REAL COLUMNS (never parse the display string; KEEP rule, ADR-0037).
CREATE TABLE runs (
    run_id          bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_key         text         NOT NULL UNIQUE,            -- '{campaign_key}/{work_slug}/{variant_digest}[/inv-{uuid8}]'
    campaign_id     bigint       NOT NULL REFERENCES campaigns(campaign_id),
    work_slug       text         NOT NULL,                   -- human-readable domain coordinates (component column)
    variant_digest  text         NOT NULL,                   -- short uniqueness suffix (component column)
    run_kind        run_kind     NOT NULL,
    fingerprint_id  bigint       NULL REFERENCES fingerprints(fingerprint_id), -- NULL for adhoc until labeled
    actor_id        bigint       NOT NULL REFERENCES actors(actor_id),
    origin          text         NOT NULL,                   -- origin stamp (host/session)
    is_unlabeled    boolean      NOT NULL DEFAULT false,     -- ADR-0036 adhoc auto-mint flag
    spec_hash       bytea        NULL,                       -- inputs hash for cache reuse (ADR-0005)
    invocation_id   uuid         NULL,                       -- C1: NULL = canonical run; non-NULL = explicit re-invocation (force-new-run, ADR-0005)
    expected_level_override expected_level NULL,             -- A6: per-run override of the heuristic expected level (phase0 Q10; never identity)
    created_at      timestamptz  NOT NULL DEFAULT now(),
    finalized_at    timestamptz  NULL
);
-- Per-kind partial unique index (ADR-0005). NULLS NOT DISTINCT so the canonical run (invocation_id NULL)
-- stays unique per campaign/slug/digest, while explicit re-invocations (non-NULL invocation_id, the
-- force-new-run path of ADR-0005) coexist — without reopening the NULL-duplicate bug (C1; ADR-0037 /inv- maps here).
CREATE UNIQUE INDEX runs_experiment_variant_uq
    ON runs (campaign_id, work_slug, variant_digest, invocation_id) NULLS NOT DISTINCT
    WHERE run_kind = 'experiment';
CREATE INDEX runs_unlabeled_idx ON runs (created_at) WHERE is_unlabeled;  -- banner counter (ADR-0036)

CREATE TABLE run_inputs (
    run_input_id    bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint       NOT NULL REFERENCES runs(run_id),
    prompt_set_id   bigint       NULL,        -- FK deferred (prompt_sets in Group F; see end-of-file block)
    asset_id        bigint       NULL REFERENCES assets(asset_id),
    intervention_spec_id bigint  NULL REFERENCES intervention_specs(intervention_spec_id),
    role            text         NOT NULL,                   -- 'stimulus' | 'intervention' | 'asset' (residency dropped -> residency_sets)
    -- exactly one referent FK per row (stimulus=prompt_set, intervention=intervention_spec, asset=asset)
    CONSTRAINT run_inputs_one_ref CHECK (
        (prompt_set_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int + (intervention_spec_id IS NOT NULL)::int = 1
    ),
    -- B4: bind role to its referent + constrain the vocab ('bogus'/'residency_member' cannot satisfy this with one_ref)
    CONSTRAINT run_inputs_role_referent CHECK (
        (role = 'stimulus')     = (prompt_set_id IS NOT NULL) AND
        (role = 'intervention') = (intervention_spec_id IS NOT NULL) AND
        (role = 'asset')        = (asset_id IS NOT NULL)
    ),
    -- NULL-safe dedup (Postgres NULL <> NULL): NULLS NOT DISTINCT so duplicate (run, role, referent) collide
    CONSTRAINT run_inputs_uq UNIQUE NULLS NOT DISTINCT (run_id, role, prompt_set_id, asset_id, intervention_spec_id)
);

-- Replicate links + measured divergence (ADR-0004 MEASURED layer).
CREATE TABLE replicate_links (
    replicate_link_id bigint     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    original_run_id bigint       NOT NULL REFERENCES runs(run_id),
    replicate_run_id bigint      NOT NULL REFERENCES runs(run_id),
    UNIQUE (original_run_id, replicate_run_id),
    CONSTRAINT replicate_distinct CHECK (original_run_id <> replicate_run_id)
);

CREATE TABLE divergence_measurements (
    divergence_id   bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    replicate_link_id bigint     NOT NULL REFERENCES replicate_links(replicate_link_id),
    method_version_id bigint     NOT NULL REFERENCES method_versions(method_version_id), -- registered divergence method
    max_abs_diff    double precision NULL,
    max_rel_diff    double precision NULL,
    argmax_flip_rate double precision NULL,
    answer_letter_flip_rate double precision NULL,           -- first-class MCQ position-bias data (ADR-0004)
    near_tie_margin_nats double precision NULL,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (replicate_link_id, method_version_id)   -- one measurement per (replicate pair, divergence method)
);

-- EXPECTED reproducibility heuristic table — NEVER identity, overridable (ADR-0004).
CREATE TABLE expected_reproducibility_rules (
    rule_id         bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    declared_mode   declared_mode NOT NULL,
    substrate_key   text         NOT NULL,                   -- e.g. 'vllm-bi-on@sm89', 'api-openrouter'
    expected        expected_level NOT NULL,
    note            text         NULL,
    UNIQUE (declared_mode, substrate_key)
);

-- ============================================================================
-- GROUP D — Queue: one jobs table, SKIP LOCKED, runtime-owned leases (ADR-0039)
-- ============================================================================
CREATE TABLE jobs (
    job_id          bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_key         text         NOT NULL UNIQUE,            -- display key; components are REAL columns below (never parsed; ADR-0037 KEEP)
    run_id          bigint       NOT NULL REFERENCES runs(run_id),   -- jobs.run_id FK inverts predecessor
    job_role        text         NULL,                       -- A3 component: 'capture' | 'derive' | 'replicate' | ... (composer sets it)
    shard_key       text         NULL,                       -- A3 component: shard identifier within the run (composer sets it)
    shard_spec      text         NULL,                       -- A3: canonical serialized shard spec (TEXT; ADR-0003)
    state           job_state    NOT NULL DEFAULT 'queued',
    -- typed routing columns (JSONB routing rejected: GIN cannot index vram <= free)
    queue           text         NOT NULL DEFAULT 'default',
    gpu_class       text         NULL,                       -- '4090' | 'a100-40' | 'h100' | NULL (cpu/api)
    vram_needed_mb  integer      NULL,
    capabilities    text[]       NOT NULL DEFAULT '{}',
    residency_set_id bigint      NULL REFERENCES residency_sets(residency_set_id),  -- typed routing + VRAM budget (Finding 1)
    -- fencing + CAS
    claim_token     uuid         NULL,
    claim_seq       bigint       NOT NULL DEFAULT 0,         -- monotonic fence
    claimed_by      bigint       NULL REFERENCES actors(actor_id),
    -- counters split (ADR-0039)
    attempt_count   integer      NOT NULL DEFAULT 0,         -- worker-reported failures only
    expiry_count    integer      NOT NULL DEFAULT 0,         -- reaper
    refusal_count   integer      NOT NULL DEFAULT 0,         -- VRAM preflight refuse-to-start
    error_class     job_error_class NULL,
    error_detail    text         NULL,
    checkpoint_ref  text         NULL,                       -- A4: resume-after-preemption pointer (checkpoint-first, c1/ADR-0039)
    enqueued_at     timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    -- B1: a sharded job (shard_key present) must carry a job_role, so the idempotency key is never (.., NULL, shard)
    CONSTRAINT jobs_shard_role CHECK (shard_key IS NULL OR job_role IS NOT NULL)
);
-- Claimable set excludes 'blocked' by construction (state = 'queued'); a job flips 'blocked'->'queued'
-- CAS-guarded when its last dependency succeeds (C2; ADR-0039).
CREATE INDEX jobs_claimable_idx ON jobs (queue, gpu_class, vram_needed_mb)
    WHERE state = 'queued';
-- A3/B1: shard-level idempotency where it matters — a (run, role, shard) is enqueued at most once.
-- NULLS NOT DISTINCT (B1) closes the NULL hole; the jobs_shard_role CHECK keeps job_role non-NULL when sharded.
CREATE UNIQUE INDEX jobs_shard_idempotency_uq
    ON jobs (run_id, job_role, shard_key) NULLS NOT DISTINCT
    WHERE shard_key IS NOT NULL;

CREATE TABLE job_dependencies (
    job_id          bigint       NOT NULL REFERENCES jobs(job_id),
    depends_on      bigint       NOT NULL REFERENCES jobs(job_id),
    PRIMARY KEY (job_id, depends_on),
    CONSTRAINT job_dep_distinct CHECK (job_id <> depends_on)
);  -- recursive cascade-cancellation of dependents is app logic over this edge set

-- Lease/heartbeat: 120s lease / 40s renew / 60s reaper, server-side now() (ADR-0039).
CREATE TABLE work_leases (
    lease_id        bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id          bigint       NOT NULL REFERENCES jobs(job_id),
    claim_token     uuid         NOT NULL,                   -- must match jobs.claim_token (ownership check)
    leased_by       bigint       NOT NULL REFERENCES actors(actor_id),
    leased_at       timestamptz  NOT NULL DEFAULT now(),
    expires_at      timestamptz  NOT NULL,                   -- server-side now() + 120s
    last_heartbeat  timestamptz  NOT NULL DEFAULT now(),
    released_at     timestamptz  NULL
);
CREATE UNIQUE INDEX work_leases_active_uq ON work_leases (job_id) WHERE released_at IS NULL;

-- ============================================================================
-- GROUP E — Bundles, artifacts, manifests, capture (ADR-0001/0002/0003/0010)
-- ============================================================================

-- Worker output = bundle contract; DB registers AFTER blobs+manifest land (W1-W8, capture contract §4).
CREATE TABLE bundles (
    bundle_id       bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bundle_uuid     uuid         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    run_id          bigint       NOT NULL REFERENCES runs(run_id),
    state           bundle_state NOT NULL DEFAULT 'unsealed',
    backend_id      bigint       NOT NULL REFERENCES storage_backends(backend_id),
    manifest_sha256 bytea        NULL,                       -- set at seal
    venue_purge_at  timestamptz  NULL,                       -- ADR-0010 sealed-on-scratch purge clock
    sealed_at       timestamptz  NULL,
    registered_at   timestamptz  NULL,
    tombstoned_at   timestamptz  NULL,                       -- tombstone BEFORE blob removal (capture contract §4)
    created_at      timestamptz  NOT NULL DEFAULT now()
);
-- GC exemption is enforced in code: never delete WHERE state IN ('sealed','registered',...) (ADR-0010).

CREATE TABLE artifacts (
    artifact_id     bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artifact_uuid   uuid         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    bundle_id       bigint       NULL REFERENCES bundles(bundle_id),
    kind            artifact_kind NOT NULL,
    backend_id      bigint       NOT NULL REFERENCES storage_backends(backend_id),
    uri             text         NOT NULL,
    sha256          bytea        NOT NULL,                   -- storage integrity (distinct hash role; ADR-0005)
    size_bytes      bigint       NOT NULL,
    shape           integer[]    NULL,                       -- verified on read (binding); dense lane per ADR-0008
    dtype           text         NULL,
    retention       retention_class NOT NULL DEFAULT 'ttl',
    recompute_recipe text        NULL,                       -- survives deletion (phase0 Q6); TEXT
    deleted_at      timestamptz  NULL,                       -- deletion RECORDED; row survives w/ checksum+shape+recipe
    created_at      timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (backend_id, uri),
    -- A1: tensor artifacts MUST carry shape + dtype, making "sha256+size+shape verified" structural for tensors;
    -- other kinds (wire_payload, prompt_spill, export, ...) are unaffected.
    CONSTRAINT artifacts_tensor_shape CHECK (
        kind NOT IN ('dense_tensor', 'derived_feature') OR (shape IS NOT NULL AND dtype IS NOT NULL)
    )
);
COMMENT ON COLUMN artifacts.deleted_at IS 'phase0 Q6: deletion recorded; artifact row + checksum + shape + recompute_recipe persist so provenance never dangles.';

-- The lake export contract: partition columns are REAL FK columns (ADR-0001).
CREATE TABLE table_manifests (
    table_manifest_id bigint     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_name    text         NOT NULL,                   -- e.g. 'logprobs', 'sae_features', 'wire_payloads'
    run_id          bigint       NOT NULL REFERENCES runs(run_id),       -- partition column as FK
    model_id        bigint       NULL REFERENCES model_identities(model_id), -- partition column as FK
    hook_point_id   bigint       NULL REFERENCES hook_points(hook_point_id), -- canonical hook name (ADR-0016)
    asset_id        bigint       NULL REFERENCES assets(asset_id),           -- A5: SAE-feature parquet links to its producing SAE (workflow 4, no migration)
    schema_major    integer      NOT NULL,                   -- immutable path tree (ADR-0001)
    partition_path  text         NOT NULL,                   -- hive partition prefix
    row_count       bigint       NULL,
    artifact_id     bigint       NOT NULL REFERENCES artifacts(artifact_id), -- the parquet file
    footer_kv_sha256 bytea       NULL,                       -- parquet-footer self-description (ADR-0010 from C)
    created_at      timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (dataset_name, partition_path, artifact_id)
);

-- capture_events: the cardinality grain (ADR-0002). UNIQUE(run_id, event_key). Wire bodies TEXT,
-- spilled to artifact FK above the 8 KB cap (ADR-0003/0022). job_id nullable = shard provenance.
CREATE TABLE capture_events (
    capture_event_id bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint       NOT NULL REFERENCES runs(run_id),
    job_id          bigint       NULL REFERENCES jobs(job_id),
    event_key       text         NOT NULL,
    model_id        bigint       NOT NULL REFERENCES model_identities(model_id),
    -- actor/origin stamp on EVERY capture (phase0 Q13, RULING 1) — parallel to the run-level stamp.
    -- Populated at capture time: jobs.claimed_by actor for job/GPU captures, the run's actor for adhoc.
    actor_id        bigint       NOT NULL REFERENCES actors(actor_id),
    origin          text         NOT NULL,                    -- executing host/session
    -- TRUE wire payload, verbatim bytes. Inline up to 8 KB; else spill_artifact_id is set.
    request_text    text         NULL,
    response_text   text         NULL,
    -- A2 dual spill: request and response can BOTH exceed 8 KB, so each gets its own spill FK.
    request_spill_artifact_id  bigint NULL REFERENCES artifacts(artifact_id),
    response_spill_artifact_id bigint NULL REFERENCES artifacts(artifact_id),
    provenance_header text       NULL,                       -- subset of provider headers (TEXT)
    captured_at     timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (run_id, event_key),
    -- enforce the inline cap + spill discipline (ADR-0003/0022)
    CONSTRAINT capture_inline_cap CHECK (
        (request_text IS NULL OR octet_length(request_text) <= 8192) AND
        (response_text IS NULL OR octet_length(response_text) <= 8192)
    ),
    CONSTRAINT capture_has_payload CHECK (
        request_text IS NOT NULL OR response_text IS NOT NULL
        OR request_spill_artifact_id IS NOT NULL OR response_spill_artifact_id IS NOT NULL
    )
);
COMMENT ON TABLE capture_events IS 'ADR-0002 cardinality ceiling: finest grain PG stores. Per-token data lives in the lake via table_manifests.';

-- Per-run scalar summaries only; metric_key FK to vocabulary + 8 KB valve (ADR-0017).
CREATE TABLE metric_keys (
    metric_key      text         PRIMARY KEY,
    value_kind      text         NOT NULL,                   -- 'scalar' | 'json'
    description     text         NOT NULL
);

CREATE TABLE run_metrics (
    run_metric_id   bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint       NOT NULL REFERENCES runs(run_id),
    metric_key      text         NOT NULL REFERENCES metric_keys(metric_key),  -- registered vocab (ADR-0017)
    value_num       double precision NULL,
    value_json      text         NULL,                       -- TEXT, capped
    UNIQUE (run_id, metric_key),
    CONSTRAINT run_metrics_valve CHECK (value_json IS NULL OR octet_length(value_json) <= 8192)  -- ADR-0017
);

-- ============================================================================
-- GROUP F — Stimulus family (always-on PG, ADR-0023); prompt sets content-hashed
-- ============================================================================
CREATE TABLE prompt_sets (
    prompt_set_id   bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_hash    bytea        NOT NULL UNIQUE,            -- phase0 Q1/Q3: content-hash identity (incl. derived sets)
    set_kind        text         NOT NULL,                   -- 'authored' | 'derived' | 'imported'
    derived_from_run_id bigint   NULL REFERENCES runs(run_id),  -- generative-inference lineage (phase0 Q1)
    note            text         NULL,
    created_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE prompt_items (
    prompt_item_id  bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prompt_set_id   bigint       NOT NULL REFERENCES prompt_sets(prompt_set_id),
    item_ordinal    integer      NOT NULL,
    prompt_text     text         NULL,                       -- inline if <= 8 KB
    spill_artifact_id bigint     NULL REFERENCES artifacts(artifact_id),   -- ADR-0022 prompt spill
    UNIQUE (prompt_set_id, item_ordinal),
    CONSTRAINT prompt_inline_cap CHECK (prompt_text IS NULL OR octet_length(prompt_text) <= 8192),
    -- B5: exactly one of inline prompt_text / spill_artifact_id (mirrors capture_events discipline)
    CONSTRAINT prompt_has_payload CHECK (
        (prompt_text IS NOT NULL)::int + (spill_artifact_id IS NOT NULL)::int = 1
    )
);

-- Typed stimulus-structure metadata; representation_hierarchy.py prototype (ADR-0023, phase0 Q2).
CREATE TABLE stimulus_structures (
    stimulus_structure_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prompt_item_id  bigint       NOT NULL REFERENCES prompt_items(prompt_item_id),
    structure_kind  text         NOT NULL,                   -- 'mcq' | 'physics_hierarchy' | ...
    difficulty      text         NULL,
    structure_text  text         NULL,                       -- typed metadata (TEXT; wired when consumed)
    UNIQUE (prompt_item_id, structure_kind)
);

CREATE TABLE mcq_items (
    mcq_item_id     bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prompt_item_id  bigint       NOT NULL UNIQUE REFERENCES prompt_items(prompt_item_id),
    correct_letter  text         NOT NULL,
    num_options     integer      NOT NULL CONSTRAINT mcq_num_options CHECK (num_options BETWEEN 2 AND 26)
);

CREATE TABLE mcq_options (
    mcq_option_id   bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mcq_item_id     bigint       NOT NULL REFERENCES mcq_items(mcq_item_id),
    letter          text         NOT NULL,
    option_text     text         NOT NULL,
    UNIQUE (mcq_item_id, letter)
);

CREATE TABLE mcq_permutations (
    mcq_permutation_id bigint    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mcq_item_id     bigint       NOT NULL REFERENCES mcq_items(mcq_item_id),
    permutation     text         NOT NULL,                   -- e.g. 'BCAD' positional encoding
    correct_position integer     NOT NULL,
    UNIQUE (mcq_item_id, permutation)
);
-- NOTE: mcq_responses (derived answer-extraction satellite) is DELIBERATELY ABSENT.
-- It is promotion-on-demand only (ADR-0011 / C1). Promotion machinery lives in the importer/derive
-- module; the table is created by a future, demand-triggered migration paying the governance trio.

-- ============================================================================
-- GROUP G — Spend governance (from A; ADR-0020 durability gate uses system_health)
-- ============================================================================
CREATE TABLE rate_cards (
    rate_card_id    bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    backend_or_lane text         NOT NULL,                   -- 'jetstream:g3.xl' | 'vast:4090' | 'azure-blob-egress' | 'vm:standing'
    unit            text         NOT NULL,                   -- 'su_per_hr' | 'usd_per_hr' | 'usd_per_gb'
    rate            numeric(18,6) NOT NULL,
    effective_from  timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (backend_or_lane, effective_from)
);

CREATE TABLE run_plans (
    run_plan_id     bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint       NULL REFERENCES runs(run_id),
    justification   text         NOT NULL,                   -- required above budget threshold (phase0 Q4)
    est_gpu_hours   numeric(18,4) NULL,
    est_usd         numeric(18,4) NULL,
    est_su          numeric(18,4) NULL,
    approved_at     timestamptz  NULL,
    created_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE spend_entries (
    spend_entry_id  bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint       NULL REFERENCES runs(run_id),
    rate_card_id    bigint       NOT NULL REFERENCES rate_cards(rate_card_id),
    quantity        numeric(18,6) NOT NULL,
    amount          numeric(18,6) NOT NULL,
    is_standing     boolean      NOT NULL DEFAULT false,     -- standing-cost row so VM burn appears in renewal report
    recorded_at     timestamptz  NOT NULL DEFAULT now()
);

-- ============================================================================
-- GROUP H — System health & probes (ADR-0015, ADR-0019, ADR-0020)
-- ============================================================================
CREATE TABLE system_health (
    health_key      text         PRIMARY KEY,                -- 'backup_freshness' | 'wal_lag' | 'desktop_heartbeat' | 'sas_expiry'
    status          health_status NOT NULL DEFAULT 'ok',
    detail          text         NULL,
    measured_at     timestamptz  NOT NULL DEFAULT now(),
    stale_after     interval     NULL                        -- ADR-0020: backup >8d, etc.
);
COMMENT ON TABLE system_health IS 'ADR-0020: backup>8d stale or WAL lag>threshold => status=blocked => registrar/dispatch refuse loudly.';

CREATE TABLE probe_reports (
    probe_report_id bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    probe_key       text         NOT NULL,                   -- 'restore_drill' | 'desktop_agent' | 'mirror_audit'
    actor_id        bigint       NULL REFERENCES actors(actor_id),  -- ADR-0015 desktop agent writes via neuro_writer
    status          health_status NOT NULL,
    report_text     text         NULL,
    reported_at     timestamptz  NOT NULL DEFAULT now()
);

-- ============================================================================
-- GROUP I — Import / external records (phase0 Q11; ADR-0025); JSONB allowed HERE ONLY
-- ============================================================================
CREATE TABLE import_batches (
    import_batch_id bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_system   text         NOT NULL,                   -- 'study-query-llm'
    note            text         NULL,
    started_at      timestamptz  NOT NULL DEFAULT now(),
    finished_at     timestamptz  NULL
);

CREATE TABLE external_records (
    external_record_id bigint    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_batch_id bigint       NOT NULL REFERENCES import_batches(import_batch_id),
    source_system   text         NOT NULL,
    source_table    text         NOT NULL,
    source_pk       text         NOT NULL,                   -- original IDs recorded
    payload_text    text         NOT NULL,                   -- byte-exact preserved (phase0 Q11)
    payload_jsonb   jsonb        NULL,                       -- sidecar for querying — JSONB ALLOWED HERE ONLY (ADR-0003)
    imported_at     timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_table, source_pk)
);
-- Functional JSON index allowed ONLY on the import sidecar (binding: JSON functional indexes HERE ONLY).
CREATE INDEX external_records_kind_idx ON external_records ((payload_jsonb ->> 'kind'));

CREATE TABLE promotions (
    promotion_id    bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_record_id bigint    NOT NULL REFERENCES external_records(external_record_id),
    promoted_kind   text         NOT NULL,                   -- 'model_identity' | 'mcq_stimulus' | 'run_summary'
    promoted_pk     bigint       NOT NULL,                   -- the native row id it became
    method_version_id bigint     NOT NULL REFERENCES method_versions(method_version_id),  -- governance trio
    promoted_at     timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (external_record_id, promoted_kind)
);

-- ============================================================================
-- GROUP J — SAE training provenance (schema-yes/code-later, ADR-0032)
-- ============================================================================
CREATE TABLE sae_training_runs (
    sae_training_run_id bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trainer_config_text text     NOT NULL,                   -- TEXT (ADR-0003)
    dataset_identity_hash bytea  NOT NULL,
    token_count     bigint       NULL,
    library_version text         NOT NULL,                   -- e.g. 'saelens@6.x'
    produced_asset_id bigint     NULL REFERENCES assets(asset_id),  -- the resulting local sae_release
    run_id          bigint       NULL REFERENCES runs(run_id),
    created_at      timestamptz  NOT NULL DEFAULT now()
);
-- Forward-declared FK fixups (assets.sae_training_run_id) — declared after both tables exist.
-- (In the Alembic migration these are emitted in dependency order; see phase3-alembic.md.)

-- ============================================================================
-- GROUP K — Lineage (relationship-edges-only, ADR-0043)
-- ============================================================================
CREATE TABLE lineage_edges (
    lineage_edge_id bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edge_kind       edge_kind    NOT NULL,
    src_entity      text         NOT NULL,                   -- typed-entity ref, e.g. 'run:1234'
    dst_entity      text         NOT NULL,
    method_version_id bigint     NULL REFERENCES method_versions(method_version_id),
    note            text         NULL,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (edge_kind, src_entity, dst_entity)
);
COMMENT ON TABLE lineage_edges IS 'ADR-0043: edges only; identities live in typed tables. Enables ADR-0025 taint retrofit.';

-- ----------------------------------------------------------------------------
-- Deferred foreign keys: targets are created later in this script, so the FK is
-- added here (after every table exists) to keep the file executable top-to-bottom
-- and to break the genuine assets<->sae_training_runs cycle. (model_identities.tokenizer_id
-- is now inline — tokenizer_identities is created first.) The Alembic initial revision
-- emits these identically (phase3-alembic.md).
-- ----------------------------------------------------------------------------
ALTER TABLE assets
    ADD CONSTRAINT assets_sae_training_run_fk
    FOREIGN KEY (sae_training_run_id) REFERENCES sae_training_runs(sae_training_run_id);
ALTER TABLE run_inputs
    ADD CONSTRAINT run_inputs_prompt_set_fk
    FOREIGN KEY (prompt_set_id) REFERENCES prompt_sets(prompt_set_id);

-- ----------------------------------------------------------------------------
-- Guard triggers. ONE reusable assign-once function (one-implementation-per-concept),
-- currently applied to runs.fingerprint_id ONLY.
--
-- LOAD-BEARING, not optional: runs.fingerprint_id is experiment identity (ADR-0005) and is the
-- single neuro_writer-updatable column that touches identity (granted only for adhoc-run adoption,
-- ADR-0036). Unlike artifacts/bundles it has NO read-time verification backstop (no sha256 to
-- re-check), so this trigger is its only defense against a writer re-keying or unsetting a labeled
-- run's identity. Permits NULL->value once (adoption); refuses value->different and value->NULL.
-- ----------------------------------------------------------------------------
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

CREATE TRIGGER runs_fingerprint_assign_once
    BEFORE UPDATE OF fingerprint_id ON neuro.runs
    FOR EACH ROW EXECUTE FUNCTION neuro.assert_assign_once('fingerprint_id');

-- ============================================================================
-- GROUP L — Roles & grants: SEPARATE PROVISIONING STEP (NOT in this migration target)
-- ============================================================================
-- The role grants + the writer-column GRANT AUDIT live in phase3-grants.sql (ADR-0007), applied by
-- provisioning AFTER the roles (neuro_admin / neuro_writer / neuro_reader / neuro_registrar) exist. They are NOT emitted
-- by the Alembic initial revision and are NOT part of this file — so phase3-ddl.sql is standalone-
-- executable on an empty DB with no pre-existing roles (Finding 4). See phase3-grants.sql.

-- ============================================================================
-- End of canonical schema. Postgres-only (ADR-0039 Reconsidered 2026-06-17): the repository is
-- single-backend; the schema is owned by Alembic against real Postgres (no SQLite target).
-- ============================================================================
