# Schema reference (generated)

Generated from `neuromancer_llm.db.orm` by `neuro docs build`. The schema is owned by Alembic
(migrations-from-zero against real Postgres 18); this file mirrors the ORM. Do not edit by hand.

## Enum types

- `actor_kind`: human, agent, scheduled_worker, importer
- `artifact_kind`: wire_payload, dense_tensor, derived_feature, token_table, prompt_spill, export, external_record, sae_release, other
- `bundle_state`: unsealed, sealed, registered, tombstoned, staged, verified
- `declared_mode`: deterministic_algo, greedy, seeded_sampling, unseeded_sampling
- `edge_kind`: derived_from, replicate_of, promoted_from, annotates, curates, paraphrase_of, generated_by, linked_call
- `expected_level`: bitwise, tolerance, distributional, none
- `health_status`: ok, degraded, blocked
- `job_error_class`: permanent, transient, resource_mismatch, lease_expired
- `job_state`: blocked, queued, claimed, running, succeeded, failed, dead_letter, cancelled
- `lane_kind`: canonical, staging, test, unknown
- `retention_class`: ttl, keep_forever, pinned
- `run_kind`: experiment, adhoc, import, replicate, derivation
- `storage_lane`: artifacts, scratch

_45 tables, 13 enum types._

## Tables

### `actors`

| column | type | null | default |
|---|---|---|---|
| `actor_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `actor_key` | `TEXT` | NOT NULL |  |
| `kind` | `neuro.actor_kind` | NOT NULL |  |
| `display_name` | `TEXT` | NOT NULL |  |
| `pg_role` | `TEXT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `retired_at` | `TIMESTAMP WITH TIME ZONE` |  |  |

- PRIMARY KEY: actor_id
- UNIQUE: actor_key

### `artifacts`

| column | type | null | default |
|---|---|---|---|
| `artifact_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `artifact_uuid` | `UUID` | NOT NULL | gen_random_uuid() |
| `bundle_id` | `BIGINT` |  |  |
| `kind` | `neuro.artifact_kind` | NOT NULL |  |
| `backend_id` | `BIGINT` | NOT NULL |  |
| `uri` | `TEXT` | NOT NULL |  |
| `sha256` | `BYTEA` | NOT NULL |  |
| `size_bytes` | `BIGINT` | NOT NULL |  |
| `shape` | `INTEGER[]` |  |  |
| `dtype` | `TEXT` |  |  |
| `retention` | `neuro.retention_class` | NOT NULL | 'ttl' |
| `recompute_recipe` | `TEXT` |  |  |
| `deleted_at` | `TIMESTAMP WITH TIME ZONE` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- CHECK `artifacts_tensor_shape`: kind NOT IN ('dense_tensor', 'derived_feature') OR (shape IS NOT NULL AND dtype IS NOT NULL)
- FOREIGN KEY: (backend_id) -> neuro.storage_backends.backend_id
- FOREIGN KEY: (bundle_id) -> neuro.bundles.bundle_id
- PRIMARY KEY: artifact_id
- UNIQUE: artifact_uuid
- UNIQUE: backend_id, uri

### `assets`

| column | type | null | default |
|---|---|---|---|
| `asset_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `asset_key` | `TEXT` | NOT NULL |  |
| `asset_type` | `TEXT` | NOT NULL |  |
| `loader_format` | `TEXT` | NOT NULL |  |
| `sha256` | `BYTEA` |  |  |
| `hf_repo` | `TEXT` |  |  |
| `hf_revision` | `TEXT` |  |  |
| `sae_training_run_id` | `BIGINT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY `assets_sae_training_run_fk`: (sae_training_run_id) -> neuro.sae_training_runs.sae_training_run_id
- PRIMARY KEY: asset_id
- UNIQUE: asset_key

### `bundles`

| column | type | null | default |
|---|---|---|---|
| `bundle_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `bundle_uuid` | `UUID` | NOT NULL | gen_random_uuid() |
| `run_id` | `BIGINT` | NOT NULL |  |
| `state` | `neuro.bundle_state` | NOT NULL | 'unsealed' |
| `backend_id` | `BIGINT` | NOT NULL |  |
| `manifest_sha256` | `BYTEA` |  |  |
| `venue_purge_at` | `TIMESTAMP WITH TIME ZONE` |  |  |
| `sealed_at` | `TIMESTAMP WITH TIME ZONE` |  |  |
| `registered_at` | `TIMESTAMP WITH TIME ZONE` |  |  |
| `tombstoned_at` | `TIMESTAMP WITH TIME ZONE` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (backend_id) -> neuro.storage_backends.backend_id
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: bundle_id
- UNIQUE: bundle_uuid

### `campaigns`

| column | type | null | default |
|---|---|---|---|
| `campaign_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `campaign_key` | `TEXT` | NOT NULL |  |
| `actor_id` | `BIGINT` | NOT NULL |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (actor_id) -> neuro.actors.actor_id
- PRIMARY KEY: campaign_id
- UNIQUE: campaign_key

### `capture_events`

| column | type | null | default |
|---|---|---|---|
| `capture_event_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `run_id` | `BIGINT` | NOT NULL |  |
| `job_id` | `BIGINT` |  |  |
| `event_key` | `TEXT` | NOT NULL |  |
| `model_id` | `BIGINT` | NOT NULL |  |
| `actor_id` | `BIGINT` | NOT NULL |  |
| `origin` | `TEXT` | NOT NULL |  |
| `request_text` | `TEXT` |  |  |
| `response_text` | `TEXT` |  |  |
| `request_spill_artifact_id` | `BIGINT` |  |  |
| `response_spill_artifact_id` | `BIGINT` |  |  |
| `provenance_header` | `TEXT` |  |  |
| `captured_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- CHECK `capture_has_payload`: request_text IS NOT NULL OR response_text IS NOT NULL OR request_spill_artifact_id IS NOT NULL OR response_spill_artifact_id IS NOT NULL
- CHECK `capture_inline_cap`: (request_text IS NULL OR octet_length(request_text) <= 8192) AND (response_text IS NULL OR octet_length(response_text) <= 8192)
- FOREIGN KEY: (actor_id) -> neuro.actors.actor_id
- FOREIGN KEY: (job_id) -> neuro.jobs.job_id
- FOREIGN KEY: (model_id) -> neuro.model_identities.model_id
- FOREIGN KEY: (request_spill_artifact_id) -> neuro.artifacts.artifact_id
- FOREIGN KEY: (response_spill_artifact_id) -> neuro.artifacts.artifact_id
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: capture_event_id
- UNIQUE: run_id, event_key

### `database_identity`

| column | type | null | default |
|---|---|---|---|
| `only_row` | `BOOLEAN` | NOT NULL | true |
| `lane` | `neuro.lane_kind` | NOT NULL |  |
| `instance_uuid` | `UUID` | NOT NULL | gen_random_uuid() |
| `provisioned_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `cloned_from` | `UUID` |  |  |
| `schema_major` | `INTEGER` | NOT NULL | 1 |
| `note` | `TEXT` |  |  |

- CHECK `database_identity_singleton`: only_row
- PRIMARY KEY: only_row

### `divergence_measurements`

| column | type | null | default |
|---|---|---|---|
| `divergence_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `replicate_link_id` | `BIGINT` | NOT NULL |  |
| `method_version_id` | `BIGINT` | NOT NULL |  |
| `max_abs_diff` | `DOUBLE PRECISION` |  |  |
| `max_rel_diff` | `DOUBLE PRECISION` |  |  |
| `argmax_flip_rate` | `DOUBLE PRECISION` |  |  |
| `answer_letter_flip_rate` | `DOUBLE PRECISION` |  |  |
| `near_tie_margin_nats` | `DOUBLE PRECISION` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (method_version_id) -> neuro.method_versions.method_version_id
- FOREIGN KEY: (replicate_link_id) -> neuro.replicate_links.replicate_link_id
- PRIMARY KEY: divergence_id
- UNIQUE: replicate_link_id, method_version_id

### `expected_reproducibility_rules`

| column | type | null | default |
|---|---|---|---|
| `rule_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `declared_mode` | `neuro.declared_mode` | NOT NULL |  |
| `substrate_key` | `TEXT` | NOT NULL |  |
| `expected` | `neuro.expected_level` | NOT NULL |  |
| `note` | `TEXT` |  |  |

- PRIMARY KEY: rule_id
- UNIQUE: declared_mode, substrate_key

### `external_records`

| column | type | null | default |
|---|---|---|---|
| `external_record_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `import_batch_id` | `BIGINT` | NOT NULL |  |
| `source_system` | `TEXT` | NOT NULL |  |
| `source_table` | `TEXT` | NOT NULL |  |
| `source_pk` | `TEXT` | NOT NULL |  |
| `payload_text` | `TEXT` | NOT NULL |  |
| `payload_jsonb` | `JSONB` |  |  |
| `imported_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (import_batch_id) -> neuro.import_batches.import_batch_id
- PRIMARY KEY: external_record_id
- UNIQUE: source_system, source_table, source_pk
- INDEX `external_records_kind_idx` ((expression))

### `fingerprints`

| column | type | null | default |
|---|---|---|---|
| `fingerprint_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `fingerprint_hash` | `BYTEA` | NOT NULL |  |
| `model_id` | `BIGINT` | NOT NULL |  |
| `declared_mode` | `neuro.declared_mode` | NOT NULL |  |
| `semantic_config` | `TEXT` | NOT NULL |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (model_id) -> neuro.model_identities.model_id
- PRIMARY KEY: fingerprint_id
- UNIQUE: fingerprint_hash

### `hook_bindings`

| column | type | null | default |
|---|---|---|---|
| `hook_binding_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `hook_point_id` | `BIGINT` | NOT NULL |  |
| `framework` | `TEXT` | NOT NULL |  |
| `version_range` | `TEXT` | NOT NULL |  |
| `arch_family` | `TEXT` | NOT NULL |  |
| `framework_site` | `TEXT` | NOT NULL |  |
| `requires_eager_attn` | `BOOLEAN` | NOT NULL | false |
| `requires_compat_mode` | `BOOLEAN` | NOT NULL | false |

- FOREIGN KEY: (hook_point_id) -> neuro.hook_points.hook_point_id
- PRIMARY KEY: hook_binding_id
- UNIQUE: hook_point_id, framework, version_range, arch_family

### `hook_points`

| column | type | null | default |
|---|---|---|---|
| `hook_point_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `canonical_name` | `TEXT` | NOT NULL |  |
| `site_kind` | `TEXT` | NOT NULL |  |
| `namespace` | `TEXT` | NOT NULL | 'core' |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- PRIMARY KEY: hook_point_id
- UNIQUE: canonical_name

### `import_batches`

| column | type | null | default |
|---|---|---|---|
| `import_batch_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `source_system` | `TEXT` | NOT NULL |  |
| `note` | `TEXT` |  |  |
| `started_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `finished_at` | `TIMESTAMP WITH TIME ZONE` |  |  |

- PRIMARY KEY: import_batch_id

### `intervention_specs`

| column | type | null | default |
|---|---|---|---|
| `intervention_spec_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `spec_hash` | `BYTEA` | NOT NULL |  |
| `method_version_id` | `BIGINT` | NOT NULL |  |
| `spec_text` | `TEXT` | NOT NULL |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (method_version_id) -> neuro.method_versions.method_version_id
- PRIMARY KEY: intervention_spec_id
- UNIQUE: spec_hash

### `job_dependencies`

| column | type | null | default |
|---|---|---|---|
| `job_id` | `BIGINT` | NOT NULL |  |
| `depends_on` | `BIGINT` | NOT NULL |  |

- CHECK `job_dep_distinct`: job_id <> depends_on
- FOREIGN KEY: (depends_on) -> neuro.jobs.job_id
- FOREIGN KEY: (job_id) -> neuro.jobs.job_id
- PRIMARY KEY: job_id, depends_on

### `jobs`

| column | type | null | default |
|---|---|---|---|
| `job_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `job_key` | `TEXT` | NOT NULL |  |
| `run_id` | `BIGINT` | NOT NULL |  |
| `job_role` | `TEXT` |  |  |
| `shard_key` | `TEXT` |  |  |
| `shard_spec` | `TEXT` |  |  |
| `state` | `neuro.job_state` | NOT NULL | 'queued' |
| `queue` | `TEXT` | NOT NULL | 'default' |
| `gpu_class` | `TEXT` |  |  |
| `vram_needed_mb` | `INTEGER` |  |  |
| `capabilities` | `TEXT[]` | NOT NULL | '{}' |
| `residency_set_id` | `BIGINT` |  |  |
| `claim_token` | `UUID` |  |  |
| `claim_seq` | `BIGINT` | NOT NULL | 0 |
| `claimed_by` | `BIGINT` |  |  |
| `attempt_count` | `INTEGER` | NOT NULL | 0 |
| `expiry_count` | `INTEGER` | NOT NULL | 0 |
| `refusal_count` | `INTEGER` | NOT NULL | 0 |
| `error_class` | `neuro.job_error_class` |  |  |
| `error_detail` | `TEXT` |  |  |
| `checkpoint_ref` | `TEXT` |  |  |
| `enqueued_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- CHECK `jobs_shard_role`: shard_key IS NULL OR job_role IS NOT NULL
- FOREIGN KEY: (claimed_by) -> neuro.actors.actor_id
- FOREIGN KEY: (residency_set_id) -> neuro.residency_sets.residency_set_id
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: job_id
- UNIQUE: job_key
- INDEX `jobs_claimable_idx` (queue, gpu_class, vram_needed_mb) WHERE state = 'queued'
- UNIQUE INDEX `jobs_shard_idempotency_uq` (run_id, job_role, shard_key) NULLS NOT DISTINCT WHERE shard_key IS NOT NULL

### `lineage_edges`

| column | type | null | default |
|---|---|---|---|
| `lineage_edge_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `edge_kind` | `neuro.edge_kind` | NOT NULL |  |
| `src_entity` | `TEXT` | NOT NULL |  |
| `dst_entity` | `TEXT` | NOT NULL |  |
| `method_version_id` | `BIGINT` |  |  |
| `note` | `TEXT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (method_version_id) -> neuro.method_versions.method_version_id
- PRIMARY KEY: lineage_edge_id
- UNIQUE: edge_kind, src_entity, dst_entity

### `mcq_items`

| column | type | null | default |
|---|---|---|---|
| `mcq_item_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `prompt_item_id` | `BIGINT` | NOT NULL |  |
| `correct_letter` | `TEXT` | NOT NULL |  |
| `num_options` | `INTEGER` | NOT NULL |  |

- CHECK `mcq_num_options`: num_options BETWEEN 2 AND 26
- FOREIGN KEY: (prompt_item_id) -> neuro.prompt_items.prompt_item_id
- PRIMARY KEY: mcq_item_id
- UNIQUE: prompt_item_id

### `mcq_options`

| column | type | null | default |
|---|---|---|---|
| `mcq_option_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `mcq_item_id` | `BIGINT` | NOT NULL |  |
| `letter` | `TEXT` | NOT NULL |  |
| `option_text` | `TEXT` | NOT NULL |  |

- FOREIGN KEY: (mcq_item_id) -> neuro.mcq_items.mcq_item_id
- PRIMARY KEY: mcq_option_id
- UNIQUE: mcq_item_id, letter

### `mcq_permutations`

| column | type | null | default |
|---|---|---|---|
| `mcq_permutation_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `mcq_item_id` | `BIGINT` | NOT NULL |  |
| `permutation` | `TEXT` | NOT NULL |  |
| `correct_position` | `INTEGER` | NOT NULL |  |

- FOREIGN KEY: (mcq_item_id) -> neuro.mcq_items.mcq_item_id
- PRIMARY KEY: mcq_permutation_id
- UNIQUE: mcq_item_id, permutation

### `method_versions`

| column | type | null | default |
|---|---|---|---|
| `method_version_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `method_id` | `BIGINT` | NOT NULL |  |
| `semver` | `TEXT` | NOT NULL |  |
| `code_sha` | `BYTEA` |  |  |
| `registered_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (method_id) -> neuro.methods.method_id
- PRIMARY KEY: method_version_id
- UNIQUE: method_id, semver

### `methods`

| column | type | null | default |
|---|---|---|---|
| `method_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `method_key` | `TEXT` | NOT NULL |  |
| `active_version_id` | `BIGINT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY `methods_active_version_fk`: (active_version_id) -> neuro.method_versions.method_version_id
- PRIMARY KEY: method_id
- UNIQUE: method_key

### `metric_keys`

| column | type | null | default |
|---|---|---|---|
| `metric_key` | `TEXT` | NOT NULL |  |
| `value_kind` | `TEXT` | NOT NULL |  |
| `description` | `TEXT` | NOT NULL |  |

- PRIMARY KEY: metric_key

### `model_identities`

| column | type | null | default |
|---|---|---|---|
| `model_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `identity_hash` | `BYTEA` | NOT NULL |  |
| `hf_repo` | `TEXT` |  |  |
| `hf_revision` | `TEXT` |  |  |
| `dtype_quant` | `TEXT` | NOT NULL |  |
| `tokenizer_id` | `BIGINT` | NOT NULL |  |
| `serving_stack` | `TEXT` | NOT NULL |  |
| `serving_version` | `TEXT` | NOT NULL |  |
| `arch_family` | `TEXT` | NOT NULL |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (tokenizer_id) -> neuro.tokenizer_identities.tokenizer_id
- PRIMARY KEY: model_id
- UNIQUE: identity_hash
- UNIQUE NULLS NOT DISTINCT `model_components_uq`: hf_repo, hf_revision, dtype_quant, tokenizer_id, serving_stack, serving_version, arch_family

### `probe_reports`

| column | type | null | default |
|---|---|---|---|
| `probe_report_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `probe_key` | `TEXT` | NOT NULL |  |
| `actor_id` | `BIGINT` |  |  |
| `status` | `neuro.health_status` | NOT NULL |  |
| `report_text` | `TEXT` |  |  |
| `reported_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (actor_id) -> neuro.actors.actor_id
- PRIMARY KEY: probe_report_id

### `promotions`

| column | type | null | default |
|---|---|---|---|
| `promotion_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `external_record_id` | `BIGINT` | NOT NULL |  |
| `promoted_kind` | `TEXT` | NOT NULL |  |
| `promoted_pk` | `BIGINT` | NOT NULL |  |
| `method_version_id` | `BIGINT` | NOT NULL |  |
| `promoted_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (external_record_id) -> neuro.external_records.external_record_id
- FOREIGN KEY: (method_version_id) -> neuro.method_versions.method_version_id
- PRIMARY KEY: promotion_id
- UNIQUE: external_record_id, promoted_kind

### `prompt_items`

| column | type | null | default |
|---|---|---|---|
| `prompt_item_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `prompt_set_id` | `BIGINT` | NOT NULL |  |
| `item_ordinal` | `INTEGER` | NOT NULL |  |
| `prompt_text` | `TEXT` |  |  |
| `spill_artifact_id` | `BIGINT` |  |  |

- CHECK `prompt_has_payload`: (prompt_text IS NOT NULL)::int + (spill_artifact_id IS NOT NULL)::int = 1
- CHECK `prompt_inline_cap`: prompt_text IS NULL OR octet_length(prompt_text) <= 8192
- FOREIGN KEY: (prompt_set_id) -> neuro.prompt_sets.prompt_set_id
- FOREIGN KEY: (spill_artifact_id) -> neuro.artifacts.artifact_id
- PRIMARY KEY: prompt_item_id
- UNIQUE: prompt_set_id, item_ordinal

### `prompt_sets`

| column | type | null | default |
|---|---|---|---|
| `prompt_set_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `content_hash` | `BYTEA` | NOT NULL |  |
| `set_kind` | `TEXT` | NOT NULL |  |
| `derived_from_run_id` | `BIGINT` |  |  |
| `note` | `TEXT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (derived_from_run_id) -> neuro.runs.run_id
- PRIMARY KEY: prompt_set_id
- UNIQUE: content_hash

### `rate_cards`

| column | type | null | default |
|---|---|---|---|
| `rate_card_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `backend_or_lane` | `TEXT` | NOT NULL |  |
| `unit` | `TEXT` | NOT NULL |  |
| `rate` | `NUMERIC(18, 6)` | NOT NULL |  |
| `effective_from` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- PRIMARY KEY: rate_card_id
- UNIQUE: backend_or_lane, effective_from

### `replicate_links`

| column | type | null | default |
|---|---|---|---|
| `replicate_link_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `original_run_id` | `BIGINT` | NOT NULL |  |
| `replicate_run_id` | `BIGINT` | NOT NULL |  |

- CHECK `replicate_distinct`: original_run_id <> replicate_run_id
- FOREIGN KEY: (original_run_id) -> neuro.runs.run_id
- FOREIGN KEY: (replicate_run_id) -> neuro.runs.run_id
- PRIMARY KEY: replicate_link_id
- UNIQUE: original_run_id, replicate_run_id

### `residency_set_members`

| column | type | null | default |
|---|---|---|---|
| `residency_set_member_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `residency_set_id` | `BIGINT` | NOT NULL |  |
| `model_id` | `BIGINT` |  |  |
| `asset_id` | `BIGINT` |  |  |

- CHECK `residency_member_one_ref`: (model_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int = 1
- FOREIGN KEY: (asset_id) -> neuro.assets.asset_id
- FOREIGN KEY: (model_id) -> neuro.model_identities.model_id
- FOREIGN KEY: (residency_set_id) -> neuro.residency_sets.residency_set_id
- PRIMARY KEY: residency_set_member_id
- UNIQUE NULLS NOT DISTINCT `residency_member_uq`: residency_set_id, model_id, asset_id
- UNIQUE INDEX `residency_set_one_base_model_uq` (residency_set_id) WHERE model_id IS NOT NULL

### `residency_sets`

| column | type | null | default |
|---|---|---|---|
| `residency_set_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `set_hash` | `BYTEA` | NOT NULL |  |
| `note` | `TEXT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- PRIMARY KEY: residency_set_id
- UNIQUE: set_hash

### `run_inputs`

| column | type | null | default |
|---|---|---|---|
| `run_input_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `run_id` | `BIGINT` | NOT NULL |  |
| `prompt_set_id` | `BIGINT` |  |  |
| `asset_id` | `BIGINT` |  |  |
| `intervention_spec_id` | `BIGINT` |  |  |
| `role` | `TEXT` | NOT NULL |  |

- CHECK `run_inputs_one_ref`: (prompt_set_id IS NOT NULL)::int + (asset_id IS NOT NULL)::int + (intervention_spec_id IS NOT NULL)::int = 1
- CHECK `run_inputs_role_referent`: (role = 'stimulus') = (prompt_set_id IS NOT NULL) AND (role = 'intervention') = (intervention_spec_id IS NOT NULL) AND (role = 'asset') = (asset_id IS NOT NULL)
- FOREIGN KEY: (asset_id) -> neuro.assets.asset_id
- FOREIGN KEY: (intervention_spec_id) -> neuro.intervention_specs.intervention_spec_id
- FOREIGN KEY: (prompt_set_id) -> neuro.prompt_sets.prompt_set_id
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: run_input_id
- UNIQUE NULLS NOT DISTINCT `run_inputs_uq`: run_id, role, prompt_set_id, asset_id, intervention_spec_id

### `run_metrics`

| column | type | null | default |
|---|---|---|---|
| `run_metric_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `run_id` | `BIGINT` | NOT NULL |  |
| `metric_key` | `TEXT` | NOT NULL |  |
| `value_num` | `DOUBLE PRECISION` |  |  |
| `value_json` | `TEXT` |  |  |

- CHECK `run_metrics_valve`: value_json IS NULL OR octet_length(value_json) <= 8192
- FOREIGN KEY: (metric_key) -> neuro.metric_keys.metric_key
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: run_metric_id
- UNIQUE: run_id, metric_key

### `run_plans`

| column | type | null | default |
|---|---|---|---|
| `run_plan_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `run_id` | `BIGINT` |  |  |
| `justification` | `TEXT` | NOT NULL |  |
| `est_gpu_hours` | `NUMERIC(18, 4)` |  |  |
| `est_usd` | `NUMERIC(18, 4)` |  |  |
| `est_su` | `NUMERIC(18, 4)` |  |  |
| `approved_at` | `TIMESTAMP WITH TIME ZONE` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: run_plan_id

### `runs`

| column | type | null | default |
|---|---|---|---|
| `run_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `run_key` | `TEXT` | NOT NULL |  |
| `campaign_id` | `BIGINT` | NOT NULL |  |
| `work_slug` | `TEXT` | NOT NULL |  |
| `variant_digest` | `TEXT` | NOT NULL |  |
| `run_kind` | `neuro.run_kind` | NOT NULL |  |
| `fingerprint_id` | `BIGINT` |  |  |
| `actor_id` | `BIGINT` | NOT NULL |  |
| `origin` | `TEXT` | NOT NULL |  |
| `is_unlabeled` | `BOOLEAN` | NOT NULL | false |
| `spec_hash` | `BYTEA` |  |  |
| `invocation_id` | `UUID` |  |  |
| `expected_level_override` | `neuro.expected_level` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `finalized_at` | `TIMESTAMP WITH TIME ZONE` |  |  |

- FOREIGN KEY: (actor_id) -> neuro.actors.actor_id
- FOREIGN KEY: (campaign_id) -> neuro.campaigns.campaign_id
- FOREIGN KEY: (fingerprint_id) -> neuro.fingerprints.fingerprint_id
- PRIMARY KEY: run_id
- UNIQUE: run_key
- INDEX `runs_unlabeled_idx` (created_at) WHERE is_unlabeled
- UNIQUE INDEX `runs_experiment_variant_uq` (campaign_id, work_slug, variant_digest, invocation_id) NULLS NOT DISTINCT WHERE run_kind = 'experiment'

### `sae_training_runs`

| column | type | null | default |
|---|---|---|---|
| `sae_training_run_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `trainer_config_text` | `TEXT` | NOT NULL |  |
| `dataset_identity_hash` | `BYTEA` | NOT NULL |  |
| `token_count` | `BIGINT` |  |  |
| `library_version` | `TEXT` | NOT NULL |  |
| `produced_asset_id` | `BIGINT` |  |  |
| `run_id` | `BIGINT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (produced_asset_id) -> neuro.assets.asset_id
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: sae_training_run_id

### `spend_entries`

| column | type | null | default |
|---|---|---|---|
| `spend_entry_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `run_id` | `BIGINT` |  |  |
| `rate_card_id` | `BIGINT` | NOT NULL |  |
| `quantity` | `NUMERIC(18, 6)` | NOT NULL |  |
| `amount` | `NUMERIC(18, 6)` | NOT NULL |  |
| `is_standing` | `BOOLEAN` | NOT NULL | false |
| `recorded_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (rate_card_id) -> neuro.rate_cards.rate_card_id
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: spend_entry_id

### `stimulus_structures`

| column | type | null | default |
|---|---|---|---|
| `stimulus_structure_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `prompt_item_id` | `BIGINT` | NOT NULL |  |
| `structure_kind` | `TEXT` | NOT NULL |  |
| `difficulty` | `TEXT` |  |  |
| `structure_text` | `TEXT` |  |  |

- FOREIGN KEY: (prompt_item_id) -> neuro.prompt_items.prompt_item_id
- PRIMARY KEY: stimulus_structure_id
- UNIQUE: prompt_item_id, structure_kind

### `storage_backends`

| column | type | null | default |
|---|---|---|---|
| `backend_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `backend_key` | `TEXT` | NOT NULL |  |
| `driver` | `TEXT` | NOT NULL |  |
| `lane` | `neuro.storage_lane` | NOT NULL |  |
| `base_uri` | `TEXT` | NOT NULL |  |
| `is_cloud` | `BOOLEAN` | NOT NULL |  |
| `venue_purge_window` | `INTERVAL` |  |  |
| `quota_bytes` | `BIGINT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- PRIMARY KEY: backend_id
- UNIQUE: backend_key

### `system_health`

| column | type | null | default |
|---|---|---|---|
| `health_key` | `TEXT` | NOT NULL |  |
| `status` | `neuro.health_status` | NOT NULL | 'ok' |
| `detail` | `TEXT` |  |  |
| `measured_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `stale_after` | `INTERVAL` |  |  |

- PRIMARY KEY: health_key

### `table_manifests`

| column | type | null | default |
|---|---|---|---|
| `table_manifest_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `dataset_name` | `TEXT` | NOT NULL |  |
| `run_id` | `BIGINT` | NOT NULL |  |
| `model_id` | `BIGINT` |  |  |
| `hook_point_id` | `BIGINT` |  |  |
| `asset_id` | `BIGINT` |  |  |
| `schema_major` | `INTEGER` | NOT NULL |  |
| `partition_path` | `TEXT` | NOT NULL |  |
| `row_count` | `BIGINT` |  |  |
| `artifact_id` | `BIGINT` | NOT NULL |  |
| `footer_kv_sha256` | `BYTEA` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- FOREIGN KEY: (artifact_id) -> neuro.artifacts.artifact_id
- FOREIGN KEY: (asset_id) -> neuro.assets.asset_id
- FOREIGN KEY: (hook_point_id) -> neuro.hook_points.hook_point_id
- FOREIGN KEY: (model_id) -> neuro.model_identities.model_id
- FOREIGN KEY: (run_id) -> neuro.runs.run_id
- PRIMARY KEY: table_manifest_id
- UNIQUE: dataset_name, partition_path, artifact_id

### `tokenizer_identities`

| column | type | null | default |
|---|---|---|---|
| `tokenizer_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `tokenizer_hash` | `BYTEA` | NOT NULL |  |
| `hf_repo` | `TEXT` |  |  |
| `hf_revision` | `TEXT` |  |  |
| `note` | `TEXT` |  |  |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |

- PRIMARY KEY: tokenizer_id
- UNIQUE: tokenizer_hash

### `work_leases`

| column | type | null | default |
|---|---|---|---|
| `lease_id` | `BIGINT` | NOT NULL | IDENTITY (always) |
| `job_id` | `BIGINT` | NOT NULL |  |
| `claim_token` | `UUID` | NOT NULL |  |
| `leased_by` | `BIGINT` | NOT NULL |  |
| `leased_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `expires_at` | `TIMESTAMP WITH TIME ZONE` | NOT NULL |  |
| `last_heartbeat` | `TIMESTAMP WITH TIME ZONE` | NOT NULL | now() |
| `released_at` | `TIMESTAMP WITH TIME ZONE` |  |  |

- FOREIGN KEY: (job_id) -> neuro.jobs.job_id
- FOREIGN KEY: (leased_by) -> neuro.actors.actor_id
- PRIMARY KEY: lease_id
- UNIQUE INDEX `work_leases_active_uq` (job_id) WHERE released_at IS NULL
