-- ============================================================================
-- neuromancer-llm — Phase 3 role grants (PostgreSQL)
-- SEPARATE PROVISIONING STEP — applied by `neuro db roles` AFTER the roles exist,
-- NOT part of the Alembic migration target and NOT emitted by the initial revision
-- (Finding 4). This file is therefore NOT standalone-runnable on its own: it presumes
-- the roles neuro_admin / neuro_writer / neuro_reader / neuro_registrar and the neuro schema already exist.
-- It is the SECURITY-CONTRACT companion to phase3-ddl.sql (ADR-0007); phase3-ddl.sql is
-- itself standalone-executable on an empty DB precisely because these grants live here.
-- ============================================================================
SET search_path = neuro, public;

-- Roles are created by provisioning before this file runs (VM-local, ADR-0007):
--   CREATE ROLE neuro_admin;  CREATE ROLE neuro_writer;  CREATE ROLE neuro_reader;  CREATE ROLE neuro_registrar;

-- reader: SELECT only, every consumption surface (phase0 Q12)
GRANT USAGE ON SCHEMA neuro TO neuro_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA neuro TO neuro_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA neuro GRANT SELECT ON TABLES TO neuro_reader;

-- writer (least-privilege; GPU/remote workers over Tailscale): INSERT on OPERATIONAL tables ONLY,
-- UPDATE scoped to lease/heartbeat AND lifecycle/state-transition columns; NO DELETE, NO registry INSERT (C3).
GRANT USAGE ON SCHEMA neuro TO neuro_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA neuro TO neuro_writer;
-- C3: a compromised worker must not be able to inject fake registry/identity rows, so INSERT is the
-- operational output surface only. lineage_edges + spend_entries are added (B2): workers legitimately write
-- linked_call edges + bundle-manifest lineage (non-identity per ADR-0043) and API workers keep the spend
-- ledger current (ADR-0021); neither reopens the C3 threat. (Adhoc auto-mint of runs, ADR-0036, is
-- control-plane-side via neuro_registrar, not on a worker.)
GRANT INSERT ON capture_events, bundles, artifacts, table_manifests, run_metrics,
                work_leases, replicate_links, divergence_measurements, probe_reports,
                lineage_edges, spend_entries TO neuro_writer;
-- Registry/identity tables (fingerprints, residency_sets, residency_set_members, the model/tokenizer/method/
-- asset/hook registries, campaigns/runs/jobs, stimulus family, ...): the registrar gets INSERT + SELECT only;
-- no non-admin role gets UPDATE/DELETE on them, and neuro_writer gets NO INSERT (default-deny). [4a]
-- Column-scoped UPDATE on lease/heartbeat AND lifecycle/state-transition columns only (ADR-0007/0039).
-- GRANT AUDIT (2026-06-16, Correction 1): every column below is lease, lifecycle, or operational state —
-- NOT identity, NOT wire-payload. capture_events / fingerprints / model_identities / tokenizer_identities /
-- prompt_* / residency_* get NO UPDATE grant (insert-then-immutable; their inserts are the registrar's, not the writer's). One identity-ADJACENT edge is flagged on runs.
GRANT UPDATE (state, claim_token, claim_seq, claimed_by, attempt_count, expiry_count,
              refusal_count, error_class, error_detail, checkpoint_ref, updated_at) ON jobs TO neuro_writer;  -- lease/claim/counters/state/checkpoint (A4)
GRANT UPDATE (last_heartbeat, expires_at, released_at) ON work_leases TO neuro_writer;          -- lease/heartbeat
GRANT UPDATE (state, manifest_sha256, sealed_at, registered_at, venue_purge_at) ON bundles TO neuro_writer;  -- seal/register lifecycle
GRANT UPDATE (deleted_at) ON artifacts TO neuro_writer;   -- tombstone STATE only; sha256/uri/shape (identity) NOT granted; never hard DELETE
-- runs: finalized_at + is_unlabeled are pure lifecycle/state. fingerprint_id is IDENTITY-ADJACENT — granted ONLY
-- for adhoc-run adoption (NULL->value labeling, ADR-0036). The assign-once trigger (runs_fingerprint_assign_once,
-- defined in phase3-ddl.sql) enforces NULL->value-once in-schema, so a writer can never re-key or unset it.
GRANT UPDATE (finalized_at, fingerprint_id, is_unlabeled) ON runs TO neuro_writer;
GRANT UPDATE (status, detail, measured_at) ON system_health TO neuro_writer;       -- operational health state (ADR-0015/0020)

-- registrar (trusted VM orchestrator, loopback only): INSERT on the registries + control plane the worker must
-- NOT touch — model/tokenizer/method/asset/hook/backend registries, residency_sets, fingerprints, campaigns/runs/
-- run_inputs, jobs/job_dependencies, stimulus family, spend, import/promotions, lineage, sae_training_runs, vocab.
-- INSERT ON ALL is acceptable here because the orchestrator is trusted + VM-local; the security boundary (C3) is
-- that GPU/remote WORKERS connect as neuro_writer, never neuro_registrar. The ONLY registrar UPDATE is the method
-- registry's active-version pointer below (the registrar registers the method + its versions, so it owns that
-- pointer); operational lifecycle UPDATEs remain the worker's.
GRANT USAGE ON SCHEMA neuro TO neuro_registrar;
GRANT INSERT ON ALL TABLES IN SCHEMA neuro TO neuro_registrar;
GRANT SELECT ON ALL TABLES IN SCHEMA neuro TO neuro_registrar;
-- Column-scoped to active_version_id ONLY (NOT identity: method_key/semver/code_sha stay insert-immutable);
-- set by register_method_version after the first version exists (ADR-0011 governance trio).
GRANT UPDATE (active_version_id) ON methods TO neuro_registrar;

-- admin: everything (DDL, migrations) — VM-local only.
GRANT ALL ON SCHEMA neuro TO neuro_admin;
GRANT ALL ON ALL TABLES IN SCHEMA neuro TO neuro_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA neuro TO neuro_admin;
