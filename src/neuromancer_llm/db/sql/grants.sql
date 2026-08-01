-- ============================================================================
-- neuromancer-llm — Phase 3 role grants (PostgreSQL)
-- SEPARATE PROVISIONING STEP — applied by `neuro db roles` AFTER the roles exist,
-- NOT part of the Alembic migration target and NOT emitted by the initial revision
-- (Finding 4). This file is therefore NOT standalone-runnable on its own: it presumes
-- the roles neuro_admin / neuro_writer / neuro_reader / neuro_registrar and the neuro schema already exist.
-- It is the SECURITY-CONTRACT companion to phase3-ddl.sql (ADR-0007); phase3-ddl.sql is
-- itself standalone-executable on an empty DB precisely because these grants live here.
--
-- ⚠⚠ SINCE ADR-0046 SESSION C3 THIS FILE ALSO PRESUMES **SCHEMA >= MIGRATION 0004**, because it GRANTs
-- EXECUTE on and re-OWNs the SECURITY DEFINER queue functions that 0004 creates. MEASURED on postgres 18.4:
-- db/provision.py::apply_grants sends this file as ONE statement batch, and a GRANT EXECUTE naming a function
-- that does not exist raises UndefinedFunction and aborts the ENTIRE grants contract — not just that line.
-- ⇒ `alembic upgrade` MUST run STRICTLY BEFORE `neuro db roles`. The canonical runbook orders them.
--
-- ⚠⚠ AND THIS FILE NOW CONTAINS REVOKEs, WHICH IS NOT DECORATION. GRANT IS ADDITIVE: on an
-- already-provisioned database, deleting a column from a GRANT list changes NOTHING. The only way a
-- re-provision can WITHDRAW a privilege is an explicit REVOKE, so the C3 withdrawals are written as
-- REVOKE-then-re-GRANT-the-kept-set. MEASURED: `REVOKE UPDATE ON <table>` also clears the COLUMN-level
-- UPDATE privileges on that table (information_schema.column_privileges goes to zero rows), so a
-- per-column REVOKE list is unnecessary. Order matters — every REVOKE precedes the re-GRANT it pairs with.
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
-- ledger current (ADR-0021); neither reopens the C3 threat.
-- ⚠ `work_leases` was in this list and is REMOVED by ADR-0046 session C3: the lease INSERT is now performed
-- INSIDE neuro.claim_job (SECURITY DEFINER), so no worker needs to write a lease row directly. The REVOKE
-- below is what actually withdraws it on an already-provisioned database.
-- ⚠ Adhoc auto-mint of runs (ADR-0036) is CONTROL-PLANE-SIDE. That was written here as "via
-- neuro_registrar" and was doubly wrong, MEASURED at C3: `neuro_writer` held UPDATE(fingerprint_id) ON runs
-- (contradicting this very sentence) AND `neuro_registrar` holds no UPDATE on runs at all — its only UPDATE
-- anywhere is methods.active_version_id. Adoption is therefore ADMIN-ONLY until the deferred
-- `neuro runs adopt` verb lands, at which point it needs its own column-scoped grant. Nothing writes
-- runs.fingerprint_id in src/ today (measured: zero `UPDATE neuro.runs` statements repo-wide).
GRANT INSERT ON capture_events, bundles, artifacts, table_manifests, run_metrics,
                replicate_links, divergence_measurements, probe_reports,
                lineage_edges, spend_entries TO neuro_writer;
-- Registry/identity tables (fingerprints, residency_sets, residency_set_members, the model/tokenizer/method/
-- asset/hook registries, campaigns/runs/jobs, stimulus family, ...): the registrar gets INSERT + SELECT only;
-- no non-admin role gets UPDATE/DELETE on them, and neuro_writer gets NO INSERT (default-deny). [4a]
-- Column-scoped UPDATE on lease/heartbeat AND lifecycle/state-transition columns only (ADR-0007/0039).
-- GRANT AUDIT (2026-06-16, Correction 1): every column below is lease, lifecycle, or operational state —
-- NOT identity, NOT wire-payload. capture_events / fingerprints / model_identities / tokenizer_identities /
-- prompt_* / residency_* get NO UPDATE grant (insert-then-immutable; their inserts are the registrar's, not the writer's). One identity-ADJACENT edge is flagged on runs.
-- ★ ADR-0046 SESSION C3 — THE WORKER/REGISTRAR ROLE SPLIT. The walking kit is GONE from neuro_writer.
-- `jobs` held UPDATE (state, claim_token, claim_seq, claimed_by, attempt_count, expiry_count, refusal_count,
-- error_class, error_detail, checkpoint_ref, updated_at). That grant is what made the 0003 state ladder
-- WALKABLE one statement at a time — `queued -> claimed -> succeeded`, no token, no lease, no work — a
-- residual 0003 pinned as a positive probe and explicitly deferred to this split.
-- Every one of those columns is now written ONLY inside a SECURITY DEFINER function (migration 0004), so
-- after the reroute the writer needs NO direct jobs UPDATE at all and NOTHING is re-granted. Verified column
-- by column against src/: `refusal_count` (the VRAM refuse-to-start counter) has no producer anywhere yet —
-- when B-5 builds one it takes a function or its own grant, deliberately not a pre-emptive hole.
REVOKE UPDATE ON jobs FROM neuro_writer;
-- `work_leases`: INSERT is inside claim_job, the renewal UPDATE is inside renew_lease, and the releases are
-- inside complete_job / fail_job_permanent / cascade_cancel_jobs. The reaper's release is control-plane.
REVOKE INSERT, UPDATE ON work_leases FROM neuro_writer;
GRANT UPDATE (state, manifest_sha256, sealed_at, registered_at, venue_purge_at) ON bundles TO neuro_writer;  -- seal/register lifecycle
GRANT UPDATE (deleted_at) ON artifacts TO neuro_writer;   -- tombstone STATE only; sha256/uri/shape (identity) NOT granted; never hard DELETE
-- runs: finalized_at + is_unlabeled are pure lifecycle/state and are KEPT.
-- ★ fingerprint_id is REVOKED at C3. It is IDENTITY-ADJACENT, it is the only writer-updatable column that
-- touches experiment identity, and adoption is IRREVERSIBLE once taken (runs_fingerprint_assign_once then
-- refuses any correction), which is why 0003's docstring registers insert-then-adopt as an open obligation.
-- MEASURED no-break: there is no adoption writer in src/ at all. The narrow cut is deliberate — finalized_at
-- and is_unlabeled are equally unexercised, but they are pure lifecycle and a future `runs finalize` is
-- plausibly writer-side, so revoking them would be a guess dressed as a policy.
REVOKE UPDATE ON runs FROM neuro_writer;
GRANT UPDATE (finalized_at, is_unlabeled) ON runs TO neuro_writer;
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

-- ============================================================================
-- ★ ADR-0046 SESSION C3 — THE SECURITY DEFINER QUEUE SURFACE (migration 0004)
-- The privileges the writer LOST above are returned as five named, auditable ENTRY POINTS instead of as
-- raw column UPDATEs. This is the whole trade: a worker can still claim, heartbeat, checkpoint, complete
-- and dead-letter its work, but only through statements it cannot rewrite.
-- ============================================================================
-- THE OWNER **IS** THE PRIVILEGE for a SECURITY DEFINER function, so it is set here rather than left as
-- whoever ran the migration — on canonical that is `neuro_boot`, a SUPERUSER, which is an unacceptable
-- DEFINER owner. `neuro_admin` is the narrowest EXISTING role class holding the writes these bodies need
-- (GRANT ALL ON ALL TABLES, above); a purpose-built least-privilege role class would change
-- db/provision.py::ROLES and is a registered follow-on, not a silent fifth role.
-- It is set HERE and not in the migration because a migration must not reference provisioned roles
-- (the Finding-4 header at the top of this file). MEASURED on postgres 18.4: this ownership change
-- PRESERVES the migration's `REVOKE ALL ... FROM PUBLIC` and any GRANT EXECUTE — the ACL keeps its
-- entries and only rewrites the grantor.
-- The two INVOKER internals are re-owned too, and granted to NOBODY: the DEFINER callers run AS
-- neuro_admin, so they can execute them, while neuro_writer is refused.
ALTER FUNCTION neuro.claim_job(bigint, text, text, integer)      OWNER TO neuro_admin;
ALTER FUNCTION neuro.renew_lease(bigint, uuid, integer)          OWNER TO neuro_admin;
ALTER FUNCTION neuro.checkpoint_job(bigint, uuid, text)          OWNER TO neuro_admin;
ALTER FUNCTION neuro.complete_job(bigint, uuid)                  OWNER TO neuro_admin;
ALTER FUNCTION neuro.fail_job_permanent(bigint, uuid, text)      OWNER TO neuro_admin;
ALTER FUNCTION neuro.unblock_ready_dependents(bigint)            OWNER TO neuro_admin;
ALTER FUNCTION neuro.cascade_cancel_jobs(bigint, boolean)        OWNER TO neuro_admin;
ALTER FUNCTION neuro.assert_job_claim_fencing()                  OWNER TO neuro_admin;

-- EXECUTE on the FIVE worker entry points ONLY. unblock_ready_dependents and cascade_cancel_jobs are
-- deliberately absent: they are reachable only THROUGH complete_job / fail_job_permanent, so a worker can
-- cascade-cancel a dependent subtree only as a consequence of dead-lettering a job it actually holds the
-- token for — never as a bare call against an arbitrary job_id.
GRANT EXECUTE ON FUNCTION neuro.claim_job(bigint, text, text, integer)  TO neuro_writer;
GRANT EXECUTE ON FUNCTION neuro.renew_lease(bigint, uuid, integer)      TO neuro_writer;
GRANT EXECUTE ON FUNCTION neuro.checkpoint_job(bigint, uuid, text)      TO neuro_writer;
GRANT EXECUTE ON FUNCTION neuro.complete_job(bigint, uuid)              TO neuro_writer;
GRANT EXECUTE ON FUNCTION neuro.fail_job_permanent(bigint, uuid, text)  TO neuro_writer;

-- ⚠ RESIDUAL, REGISTERED NOT CLOSED (ADR-0046 C3, deferred to P-7). `GRANT SELECT ON ALL TABLES` above lets
-- any neuro_writer READ another worker's jobs.claim_token and then call complete_job / fail_job_permanent
-- against it. This is NOT a C3 regression — before C3 the writer could mark any job succeeded with no token
-- at all — but C3 promotes the token from a concurrency fence to an AUTHORIZATION value, which it cannot
-- bear while the role can read it. MEASURED: column-scoping SELECT on jobs/work_leases to exclude
-- claim_token does work, and it BREAKS `SELECT *` (Repository.get_job). The real fix is P-7 per-worker
-- LOGIN roles, which let a function authorize on `current_user` instead of on a shared secret.
