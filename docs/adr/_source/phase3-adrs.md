# neuromancer-llm — Phase 3 Architecture Decision Records (2026-06-16)

Numbered ADRs synthesizing the approved B-chassis hybrid into binding design decisions. **Source-of-record precedence:** `phase2-answers-of-record.md` > `phase0-answers-of-record.md` > engagement memory binding constraints > Phase 1 research (`phase1-digest.md` + reports). Where an ADR resolves an explicitly-parked degree of freedom rather than restating a banked decision, it is tagged **`AUTHOR-DISCRETION`** and is a one-line owner redirect at the checkpoint.

Status legend: **Accepted** (banked decision, restated as architecture) · **Signed-deviation** (one of the 4 ADRs the owner signed at the checkpoint) · **Author-discretion** (parked DoF resolved by the synthesis author) · **Reserved-seam** (designed but inert until a later trigger).

In Phase 4 these split into `docs/adr/NNNN-*.md` with a generated index (`neuro docs build`); here they are consolidated for checkpoint review. ADR numbers are permanent identity once assigned.

---

## Group 1 — Architecture spine

### ADR-0001 — Adopt the B-chassis hybrid
**Status:** Accepted · **Source:** phase2 V1, checkpoint "Chassis (B)".
**Decision.** Thin Postgres is the control plane (identity core + queue + per-run scalars + manifest rows). Bulk lives in a **hive-partitioned parquet lake** (per-token / per-feature scalars) and a **safetensors TTL dense lane** (residual/attention tensors, compute-local). Postgres never holds per-token rows. The export contract is `table_manifests` with partition columns materialized as real FK columns; DuckDB reads the lake directly through the read-only role.
**Consequences.** The Postgres/parquet seam is the design's central risk surface → addressed wholesale by ADR-0002/0010/0011 and the W1–W8 machinery (capture contract §4). The m3.small is retained (no resize; ADR-0042). Two stores that can disagree means write-ordering is a correctness property, not a convenience.

### ADR-0002 — Cardinality law as schema law
**Status:** Accepted (from pole A) · **Source:** checkpoint "From A".
**Decision.** The finest grain Postgres ever stores is `capture_events` (one row per model interaction — API call or GPU forward batch). No table is permitted to grow O(tokens) or O(features). Any per-token/per-feature data is a lake artifact registered by a manifest row. This is enforced socially by review and structurally by the absence of any per-token table in the DDL.
**Consequences.** `capture_events` is the cardinality ceiling; sizing math (capture contract §7) is bounded by interactions, not tokens. The derived-satellite exception (ADR-0011) is the *only* sanctioned path to per-response PG rows, and only on demand.

### ADR-0003 — Byte-exact TEXT wire bodies, never JSONB
**Status:** Accepted (from pole A) · **Source:** checkpoint "From A"; phase0 Q9.
**Decision.** Captured wire payloads (request/response) are stored as **`TEXT`** holding the verbatim bytes as transmitted, never `JSONB`. JSONB normalizes key order, whitespace, and numeric forms — destroying the "TRUE wire payload" guarantee and the ability to recompute a request hash. The sole place JSON-typed columns + functional JSON indexes are allowed is `external_records.payload_jsonb` (import sidecar only; see also ADR-0025).
**Consequences.** Querying inside payloads is a lake/DuckDB concern after export, not a PG index concern. Large bodies spill to blob with an artifact FK (capture contract §3); the inline cap is 8 KB (threshold shared with ADR-0017; also governs prompts via ADR-0022).

### ADR-0004 — Three-layer determinism model replaces the enum
**Status:** Accepted · **Source:** phase0 Q10; phase1 d2.
**Decision.** Determinism is three independent things, never one enum:
1. **DECLARED mode** — `deterministic_algo | greedy | seeded_sampling | unseeded_sampling`, *derived from the captured wire payload* (what was requested AND what the provider honored), participates in the semantic config and therefore the fingerprint.
2. **EXPECTED reproducibility level** — `bitwise | tolerance | distributional | none`, a maintained heuristic rule table keyed (declared_mode × substrate); **never touches identity**; overridable per-run via `runs.expected_level_override` (A6).
3. **MEASURED reproducibility** — replicate runs linked to the original via `replicate_links`, storing divergence metrics (max abs/rel diff, argmax-flip, `answer_letter_flip_rate`, near-tie nat-margin buckets). Answer flips are first-class MCQ-position-bias data.
**Consequences.** Three DDL homes: `fingerprints` (declared in the hashed semantic config), `expected_reproducibility_rules` (heuristic table), `divergence_measurements` + `replicate_links` (measured). The bitwise-vs-tolerance *default* (E6; detailed in capture contract §6) is a runtime serving-config default, not schema — the schema seats both branches.

### ADR-0005 — Identity & fingerprint semantics
**Status:** Accepted · **Source:** phase0 Q10; phase1 d1.
**Decision.** Model identity is 7-component first-class: hf_repo + revision + dtype/quant + tokenizer_hash + serving_stack + serving_version + (architecture family). `model_identities.identity_hash` is the durable key with a `UNIQUE NULLS NOT DISTINCT` backstop over the component columns. **Fingerprints are a separate INSERT-only table** (`runs.fingerprint_id` FK; no-UPDATE grant), raise on mismatch, with an explicit force-new-run path. The fingerprint hashes the **semantic** config section wholesale; scheduling config is excluded. Three hash roles stay distinct and are never conflated in code or schema: **fingerprint** = experiment identity; **sha256** = storage integrity; **replay divergence** = reproducibility. Run identity and cache reuse key on **spec_hash (inputs)**, never output equality; reuse gating follows declared mode (greedy/seeded reusable; unseeded never silently).
**Consequences.** Composer drift is caught by per-kind partial unique indexes on run-component tuples (materialized in phase3-ddl.sql Group C). `intervention_specs.spec_hash` gives interventions idempotency-by-input with a force flag.

### ADR-0006 — Lanes v2: positive identity, UNKNOWN fails closed
**Status:** Accepted · **Source:** phase0 Q8; phase1 e.
**Decision.** A singleton `database_identity` row (lane, instance_uuid, provisioned_at, cloned_from) is written at provisioning and **positively verified before any write path exists — once per engine construction at every writer entry point** (R1: `verify_engine` / `make_verified_engine`; `Repository` and `BundleRegistrar` construct only from a verified engine), via a mandatory `expected_lane` kwarg (NOT an env var). Canonical check = lane AND repo-pinned uuid match. **UNKNOWN fails closed for every intent.** Sole escape is `neuro db provision` on a provably empty DB. Clone/restore tooling rewrites identity atomically-before-success. Destructive ops are a typed-confirmation CLI; canonical is hard-refused. (Postgres-only — the lane-identity mechanism has no SQLite variant; ADR-0039 Reconsidered 2026-06-17.)
**Consequences.** Closes the predecessor's confirmed bidirectional lane inversion. `NEURO_DATABASE_URL` + optional connections file is the entire env surface. No `allowed_intents` column — policy stays in code (ADR-0040's backend-registry posture mirrors this).
**Amended 2026-07-02 (audit correction — wording reconciled to the built, intended posture).** The original "verified at every DBAPI connect" phrasing overstated the mechanism: identity is verified ONCE per engine construction at every writer entry point, not per-connection. Known caveat: `pool_pre_ping` recycles a dead pooled connection without re-verification — acceptable while a DSN targets a single server; a per-connect event listener (true per-connect verification) is an option deliberately DEFERRED to the ADR-0046 isolation / worker-role-split bundle.

### ADR-0007 — Least-privilege roles
**Status:** Accepted · **Source:** phase0 Q8; phase1 e.
**Decision.** Four Postgres roles: `neuro_admin` (DDL, migrations, VM-local only), `neuro_writer` (INSERT on OPERATIONAL tables only — capture_events / bundles / artifacts / table_manifests / run_metrics / work_leases / replicate_links / divergence_measurements / probe_reports / lineage_edges / spend_entries — plus column-scoped UPDATE on lease/heartbeat **and** lifecycle/state-transition columns — job/bundle state, lease renewal, run finalization, adhoc-run adoption, artifact tombstone, health status; **never identity-bearing, wire-payload, or fingerprint-record columns; no registry INSERT; no DELETE, no DDL**), `neuro_registrar` (trusted VM-local orchestrator: INSERT on the registries + control plane — model/tokenizer/method/asset/hook/backend registries, residency_sets, fingerprints, campaigns/runs/jobs, stimulus family, import/promotions, lineage_edges, spend_entries — effectively INSERT on ALL tables, a superset NOT exclusive of the writer's: lineage_edges + spend_entries are SHARED with neuro_writer per B2 — so a compromised remote worker cannot inject fake identity rows; C3), `neuro_reader` (SELECT only — all consumption surfaces). Per-human and per-worker logins; no shared password. pgaudit `'ddl, role'`. Workers connect over Tailscale/SSH as `neuro_writer`-class roles. Workers MUST be able to complete jobs, seal/register bundles, write health, and adopt adhoc runs — those are lifecycle state transitions, not identity mutations.
**Consequences.** Makes the predecessor's April-wipe class structurally impossible. The writer can advance lifecycle (complete jobs, seal/register bundles, write health, finalize and adopt adhoc runs) but cannot mutate identity-bearing columns or perform destructive ops. One audited edge: `runs.fingerprint_id` is writer-updatable solely for adhoc-run adoption (the NULL→value labeling of ADR-0036) — flagged as identity-adjacent and enforced in-schema by the assign-once trigger (materialized by the initial migration) so it can only go NULL→value, never be repointed (see the GRANT AUDIT in phase3-grants.sql). Grants are not part of the migration target — they live in `phase3-grants.sql`, applied by provisioning after the roles exist (Finding 4).
**Enforcement.** The assign-once trigger is canonical enforcement on Postgres: a compromised direct-SQL writer cannot re-key or unset a labeled run's identity (`runs.fingerprint_id` has no read-time hash backstop), so the trigger is its only defense. It is verified in the full CI lane against real Postgres. (Postgres is the only backend — ADR-0039 Reconsidered 2026-06-17 — so the earlier not-mirrored-on-SQLite carve-out is moot.)
**Amended 2026-07-02 (audit correction — deviation absorbed).** One column-scoped registrar UPDATE exists beyond the original grant matrix: `GRANT UPDATE (active_version_id) ON methods TO neuro_registrar` (`db/sql/grants.sql:60-62`). This is the method registry's active-version POINTER — registry bookkeeping the registrar itself owns (it registers the method and its versions, ADR-0011), NOT an identity mutation: `method_key` / `semver` / `code_sha` stay insert-immutable and are permission-denied. The boundary is pinned by `tests/test_security.py:183` (`test_registrar_active_version_grant_is_column_scoped`: the pointer UPDATE succeeds as `neuro_registrar`; every identity-column UPDATE is denied). Flagged live at the Stage-2 gate (2026-06-22); absorbed into this ADR by the 2026-07-02 audit correction pass.

---

## Group 2 — The four signed deviation ADRs

### ADR-0008 — mmap dense-shard read-verification softening
**Status:** Signed-deviation (ADR-1) · **Source:** phase2 ADR-1 "Sign as written".
**Decision.** Dense safetensors shards are verified by **post-transfer hash + first-read-per-host + monthly mirror audit**, NOT hash-on-every-open. Per-open hashing would forfeit mmap random access on the dense lane.
**Consequences.** Residual = corruption between audits on an already-verified host; bounded by monthly cadence and the lane being TTL/recomputable. This is a sanctioned deviation from the "sha256 verified on read" binding, scoped to the dense mmap lane only. No tightening (e.g. weekly) was taken.

### ADR-0009 — DuckDB direct reads unverifiable in-band; accepted
**Status:** Signed-deviation (ADR-2) · **Source:** phase2 ADR-2 "Sign".
**Decision.** Direct DuckDB-over-https ranged reads of lake parquet **cannot** be sha256-verified in-band; this is accepted. Residual is bounded by the mirror audit (ADR-0014) + quarterly cloud spot-sample. Docs state the residual honestly.
**Consequences.** The direct-DuckDB product surface is preserved. **The offered parquet-page-checksum tightening was NOT taken** (C5) — lake writers are not required to enable page checksums. (If the owner later wants it, it is an additive writer-config change, not a schema change.)

### ADR-0010 — Sealed-bundle GC exemption + venue purge-window
**Status:** Signed-deviation (ADR-3) · **Source:** phase2 ADR-3 "Sign".
**Decision.** GC **never** deletes a *sealed* (registered) bundle. Sealed bundles residing on venue scratch carry a `venue_purge_window` column so TTL math respects the venue's own deletion clock. Unsealed (unregistered) bundles remain reaper-collectible.
**Consequences.** The system's own reaper can never delete registered-but-not-yet-promoted data. **The offered upload-deadline / forced-promotion-alert tightening was NOT taken** (C5). The purge-window keeps TTL honest about what the venue destroys regardless; cluster scratch (Jetstream /tmp, ARCC scratch) is in-scope TTL territory governed by this column (E9 sub-part).
**Closed decision (2026-06-16) — bundle fields are deliberately NOT trigger-guarded.** Unlike `runs.fingerprint_id` (ADR-0007), the writer-updatable bundle lifecycle fields (`manifest_sha256`, `sealed_at`, `registered_at`) and `bundles.state` get NO assign-once trigger: `manifest_sha256` is hash-verifiable on read, and state transitions are repository-CAS-guarded (ADR-0039), so a trigger would be redundant. Adding non-load-bearing machinery is itself the study-query-llm smell we are greenfielding away from. Decided, not deferred.
**Addendum 2026-07-27 — the `bundles.state` half of that closed decision is SUPERSEDED by migration `0003` (ADR-0046 P-4); the `manifest_sha256` half STANDS.** The 2026-06-16 decision rested on a premise it stated openly: *"state transitions are repository-CAS-guarded (ADR-0039), so a trigger would be redundant."* That premise holds only while every writer reaches the table THROUGH `Repository` — and the worker/registrar role split, which ADR-0046's bundle exists to enable, is precisely the topology that falsifies it: `grants.sql` gives `neuro_writer` `UPDATE (state, …)` **and full-row INSERT** on `bundles`, so an untrusted worker connecting directly bypasses the CAS entirely. Measured before the guard existed: as `neuro_writer`, a bare `INSERT INTO neuro.bundles (…, state) VALUES (…, 'registered')` lands unopposed, producing a row that is GC-exempt (`gc.py`'s `COLLECTIBLE_STATES` is `('unsealed',)`), unreclaimable in-role (the writer holds no `DELETE`), and that wedges its `(run, dataset, partition)` out of registration forever. So `bundles.state` now carries two triggers — `bundles_state_transition` (BEFORE UPDATE OF state) and `bundles_state_insert` (BEFORE INSERT) — and the machinery is load-bearing rather than redundant: the thing it duplicates is reachable around, which is the distinction the original decision could not have drawn before the split was on the table. **`manifest_sha256` is NOT superseded here** — the "hash-verifiable on read" argument for it is untouched by this migration, and an assign-once guard on it is a *registered follow-on* that lands with the role split and must supersede this clause on its own grounds when it does. `sealed_at`/`registered_at` likewise keep no trigger.

### ADR-0011 — Promotion-on-demand satellite doctrine
**Status:** Signed-deviation (ADR-4) · **Source:** phase2 ADR-4 "Sign".
**Decision.** Derived Postgres tables (e.g. `mcq_responses`) exist **only** via explicit promotion when a concrete experiment consumes them — never always-on. Each promoted satellite pays the **governance trio**: `method_version_id` on every derived row + a `neuro derive` re-derive CLI + a parity probe comparing the satellite to its lake source. **`mcq_responses` is NOT pre-created in the Phase 3 DDL** (C1); the promotion *machinery* is specified, the first satellite waits for demand.
**Consequences.** Keeps A's most useful pattern (fast SQL on derived MCQ data) without A's standing silent-wrongness surface. Promotion is a deliberate, audited act. Distinct from the MCQ *stimulus* family, which is always-on first-class PG (ADR-0023).

---

## Group 3 — The thirteen panel conditions

### ADR-0012 — Registration hashing of cloud-bound shards (condition 1)
**Status:** Accepted. **Decision.** Every cloud-bound shard is sha256-hashed **at registration** (seconds per bundle). Hash deferral is permitted only for local dense shards under ADR-0008. **Consequences.** The registration transaction is the durability boundary for cloud artifacts.

### ADR-0013 — SAS credential lifecycle (condition 2)
**Status:** Accepted. **Decision.** Per-human **user-delegation SAS** minted by the CLI with a stated TTL; expiry surfaced in the daily probe AND the generated views-file header; `_staging/` lives in its own container. **Consequences.** SAS/credential lifecycle is the #1 cross-pole rot surface (checkpoint watch-list) — surfacing expiry in two places is the mitigation. Topic/secret handling parallels ADR-0019.

### ADR-0014 — Audit against the desktop mirror (condition 3)
**Status:** Accepted. **Decision.** The monthly full-hash audit runs against the **desktop NVMe mirror** (free reads) + a quarterly cloud spot-sample. The read-verification residual is restated honestly in docs; an egress line is added to the cost model. **Consequences.** Bounds the ADR-0008/0009 residuals affordably; mirror is load-bearing for both the audit and the pin-at-publication default (ADR-0034).

### ADR-0015 — Desktop-half health surfacing (condition 4)
**Status:** Accepted. **Decision.** A desktop agent writes a daily heartbeat + reaper/disk/mirror-age rows to PG via `neuro_writer`; the preflight banner shows desktop-probe age; the Windows Scheduler task is named in the runbook. **Consequences.** The Windows desktop is the weakest-observed host (checkpoint watch-list); the agent makes its silence visible. Runs under WSL2 (ADR-0035).

### ADR-0016 — Hook-vocabulary pinning (condition 5)
**Status:** Accepted. **Decision.** Lake hook columns carry **registry-canonical** names (the `hook_points` site grammar: `embed.out`, `L{l}.resid.{pre|mid|post}`, `L{l}.attn.{q|k|v|z|scores|pattern}`, `L{l}.mlp.{in|act|out}`, `unembed.logits`), validated at shard close. **Consequences.** Defends against TransformerLens 4.0 / nnsight vocabulary churn (a1). The `hook_points` registry is day-one (resolves d1 open-Q4 toward "registry exists").

### ADR-0017 — run_metrics valve closure (condition 6)
**Status:** Accepted. **Decision.** `run_metrics.metric_key` is an FK to a registered `metric_keys` vocabulary; `CHECK (octet_length(value_json) <= 8192)`. **Consequences.** Closes the "metadata_json reborn" valve — run_metrics cannot become an unbounded JSON dumping ground. Per-run scalars only (no per-shard width; B posture).

### ADR-0018 — ARCC relay gating (condition 7)
**Status:** Reserved-seam. **Decision.** Reserve the `{staged, verified}` enum values in the bundle lifecycle; **gate** the relay registrar branch + its kill-tests behind the Phase 4 ARCC bring-up outcome (outbound-network + Apptainer check). **Consequences.** All three poles were dinged for prebuilding this — so it is designed-but-inert until bring-up confirms no-egress. No relay code ships in Phase 3 beyond the reserved enum.

### ADR-0019 — Probe alert channel is a blocking precondition (condition 8 + E15)
**Status:** Accepted. **Decision.** The alert channel is **ntfy.sh push** (zero-account topic → phone + desktop); probes `curl` it on `OnFailure`. **The topic name is a secret** — stored like one (not in git, not in generated docs), referenced via env/connections file. The durability-staleness gate (ADR-0020) and desktop agent (ADR-0015) use the same channel. **Consequences.** Probe value collapses if failures are folder-only (checkpoint watch-list); this unblocks the binding. Channel is swappable (msmtp/webhook) behind one `notify` seam.

### ADR-0020 — Durability-staleness gate (condition 9)
**Status:** Accepted. **Decision.** If the DB backup is **>8 days stale** OR WAL-archive lag exceeds threshold, `system_health` flips and the registrar/dispatch **refuse loudly**. **Consequences.** Generalizes A-entropy's critical WAL-rot finding — the system stops accepting new canonical writes when its own durability is unproven, rather than silently accumulating unrecoverable state.

### ADR-0021 — API-lane incremental registration (condition 10)
**Status:** Accepted. **Decision.** Online API jobs insert `capture_events` **per shard rotation** (PG is reachable by definition on CPU/API lanes), so the spend ledger stays current and paid wire payloads become durable early — not only at job completion. **Consequences.** Fixes C's critical "paid payloads GC-able mid-sweep / budget ledger stale for the sweep duration" finding as it applies to the API lane. Differs from the GPU bundle lane, which registers at bundle seal.

### ADR-0022 — Prompt spill path (condition 11)
**Status:** Accepted. **Decision.** Any hand-authored prompt **>8 KB** takes the artifact-FK spill path (blob + artifact row), regardless of origin. **Consequences.** Prompts obey the same 8 KB inline cap as wire bodies (ADR-0003); large stimuli never bloat the canonical row.

### ADR-0023 — MCQ stimulus family stays in PG (condition 12)
**Status:** Accepted. **Decision.** The MCQ stimulus family (items, options, permutations, correct letters, difficulty/structure metadata) is **always-on first-class Postgres** per the d1 census; C's D2 (push it to files) is rejected. **Consequences.** Join-critical stimulus identity is relational and DB-enforced. `representation_hierarchy.py` is the prototype for `stimulus_structures` typed metadata, wired when an experiment consumes it (reserve-until-consumed posture, as in ADR-0031).

### ADR-0024 — CI / branch-protection precondition (condition 13 + E14)
**Status:** Accepted. **Decision.** The repository is **public** — satisfying the governance binding's precondition (rulesets + branch protection enforceable, unlimited Actions minutes). Exam data never lives in git by design; the exam-text soft rule is untouched. **Consequences.** A repository ruleset on `main` requires PR + required status checks **{fast, full}** — the actual CI check contexts (the job ids in `.github/workflows/{fast,full}.yml`; corrected 2026-07-02: the wheel smoke and the Docker/compose boot are *steps inside* `full`, not separate contexts, so a ruleset requiring the earlier `tests-full`/`wheel-smoke`/`docker` names would wait forever on checks that never report) — blocks force-push, restricts deletion; required reviews stay OFF (solo + agents).

---

## Group 4 — Queue, storage, topology

### ADR-0039 — One queue: SKIP LOCKED claim, runtime-owned leases
**Status:** Accepted · **Source:** NEVER-AGAIN (dual claim systems); phase1 c1.
**Decision.** Exactly one `jobs` table. Claim = `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING` with **typed routing columns** (queue, gpu_class, vram_needed_mb, capabilities `text[]`, residency_set_id) — JSONB routing rejected (GIN cannot index `vram <= free`). `claim_token uuid` + monotonic `claim_seq` fencing; all mutations CAS-guarded with rowcount checks. `attempt_count` / `expiry_count` / `refusal_count` split. Error taxonomy `permanent | transient | resource_mismatch | lease_expired` with immediate dead-letter for permanent + enqueue-time strict validation. Leases: 120s / renew 40s (runtime-owned thread) / reaper 60s, all server-side `now()`. Checkpoint-first preemption (vast.ai kills with no signal). The repository is **single-backend (Postgres)** — there is no SQLite fallback (see Reconsidered 2026-06-17 below).
**Consequences.** The golden-snapshot harness is ported with a steal-attempt scenario. No pgqueuer/procrastinate/Hatchet/River (all fail binding requirements). This is the single claim path — SKIP LOCKED on Postgres (single-backend repository; no Python-loop / SQLite fallback). `residency_set_id` is a real FK to `residency_sets` — a hash-addressed SET of model + asset members budgeted against VRAM (phase0 Q5); residency is expressed there alone, and the former `run_inputs.role='residency_member'` is dropped (one home per concept, Finding 1). Dependency gating (C2): `job_state` carries a non-claimable `'blocked'` value; the claim predicate `state = 'queued'` never selects blocked or in-flight jobs, and a job flips `'blocked' -> 'queued'` (CAS-guarded) when its last dependency succeeds — so nothing is claimable before its dependencies have succeeded.
**Reconsidered 2026-06-17 — SQLite dropped; Postgres-only (owner-approved reversal of the SQLite parts of ADR-0006/0007/0039).** The earlier design kept a SQLite test target behind one repository interface (with Alembic batch mode and `create_all` for unit tests). The re-audit showed this cannot hold: the schema is irreducibly PG-specific — `INTERVAL` / `JSONB` / `ARRAY` types, `NULLS NOT DISTINCT` constraints and indexes, the `assert_assign_once` trigger, and `now()` / `gen_random_uuid()` defaults — so a faithful SQLite mirror is impossible and a lossy one would BE the silent test/prod divergence the golden-snapshot harness exists to prevent. Evidence: 5 independent **PostgreSQL 18.4** builds of `phase3-ddl.sql` PASS, while SQLite `create_all` fails on INTERVAL/JSONB/ARRAY and lacks `now()` / `gen_random_uuid()`. **Resolution:** the repository is single-backend (Postgres); the golden-snapshot harness (Q15 KEEP: plan → claim → checkpoint → steal-attempt → complete/CAS-fail → cascade) and all concurrency tests run against a real-Postgres fixture (session-scoped testcontainers `postgres:18` or a reused CI service container; c1 finding 10 already placed concurrency on PG). Only the zero-infra local unit loop is given up — acceptable: Docker/WSL2 are already in the stack and the canonical store is always Postgres.

### ADR-0040 — Storage backend registry; Azurite-only CI
**Status:** Accepted · **Source:** phase0 Q7; phase1 e/D1.
**Decision.** Backend/URI policy is data-driven (`storage_backends` registry + adapters): Azure now, S3/NAS addable without migration, **no Azure-only CHECK constraint**. CI uses **Azurite ONLY** — MinIO community is dead (archived ~Apr 2026). An S3 emulator is deferred behind the backend registry seam (Garage / SeaweedFS / digest-pinned last-good image / moto chosen *then*). Two object-storage lanes only: `artifacts` (canonical, per-prefix fail-closed dollar-calibrated quota) and `scratch` (freely deletable). Quota guard fails **closed**.
**Consequences.** Fixes the predecessor's fail-OPEN-on-listing-error bug. CI never touches cloud. The "MinIO is a fine default" training prior is explicitly overridden by June-2026 research (D-discipline).

### ADR-0041 — `AUTHOR-DISCRETION`: native PGDG Postgres on the VM
**Status:** Author-discretion — **RESOLVED 2026-06-16, owner ACCEPTED** (no longer an open DoF) · **Source:** digest DoF "Postgres topology"; e-finding 35 (both viable).
**Decision.** Run **native PGDG-packaged Postgres 18** on the m3.small VM (pgbackrest, pgaudit, WAL archiving configured natively). The compose file owns app/orchestrator services only; CI uses a `postgres:18` **service container** for migrations-from-zero.
**Reasons.** (1) The m3.small is the smallest machine in the fleet; native avoids container overhead on the one durability-critical service. (2) pgbackrest + WAL archiving + pgaudit + quarterly restore drills are materially simpler and better-documented native. (3) Restore-image parity (the predecessor's habit) is preserved **in CI**, where it is actually exercised, without paying container cost on prod. (4) Provisioning scripts capture the install (docs-that-cannot-rot).
**Owner ruling (2026-06-16):** ACCEPTED — native PGDG Postgres on the VM is the decision; this axis is closed.

### ADR-0042 — VM resize deferral; Cinder volume instead
**Status:** Accepted (from pole A, resize NOT adopted) · **Source:** checkpoint "From A".
**Decision.** Attach a **zero-SU 150 GB Cinder volume** for PGDATA/WAL (unbinds the 20 GB root disk with no resize, pole-independent win). The VM-resize itself is **NOT adopted** — only a `vm_resize` trigger table is reserved (deferral ADR), so the m3.small stands (ADR-0001). A `capture_events` partitioning trigger is likewise reserved as a deferral ADR, not pre-applied.
**Consequences.** Avoids A's ~35k-SU resize that scored A down on owner-fit. Partitioning is a documented trigger that fires only if `capture_events` cardinality demands it.

---

## Group 5 — Escalation outcomes & retrofit seams

### ADR-0025 — No restricted-flag day one; taint-query retrofit path
**Status:** Superseded by ADR-0049 (2026-07-12) · **Source:** phase0 Q3.
**Decision.** No `restricted` flag in the day-one schema; access control is roles/credentials. Because every export/payload/derived artifact carries lineage to its prompt set, restriction is retroactively computable (one migration + one taint query). Soft rule: exam-derived raw text is never posted publicly. Content-hash identity for prompt sets stays regardless.
**Consequences.** This ADR *is* the recorded retrofit path. Lineage completeness (ADR-0043's `lineage_edges`) is what makes the taint query possible later.

**Superseded 2026-07-12 by ADR-0049 (owner-ruled withdrawal of the taint-query retrofit obligation; the retrofit path remains technically open via ADR-0043 lineage).**

### ADR-0026 — Tracker-emit default OFF; post-finalize emitter seam
**Status:** Reserved-seam · **Source:** phase0 Q12; checkpoint.
**Decision.** No MLflow/W&B emission by default. A post-finalize emitter seam is reserved (run summaries can be emitted as a *viewing* layer later). Heavy-tracker/thin-Postgres is ruled out permanently.
**Consequences.** The Postgres provenance core is the only source of truth; trackers, if ever enabled, are downstream and non-authoritative.

### ADR-0027 — NDIF documented seam only
**Status:** Reserved-seam · **Source:** phase2 E1.
**Decision.** No NDIF account; design the remote-execution adapter **slot** (registry row shape) but build no active adapter. The lane stays paper until a concrete 70B/405B experiment is planned; acceptability-for-publishable-work and the retention-policy inquiry are deferred to that moment.
**Consequences.** The hook-registry adapter pattern (ADR-0016) is the seam NDIF would later fill.

### ADR-0028 — Neuronpedia deferred to workflow 4
**Status:** Reserved-seam · **Source:** phase2 E2.
**Decision.** Decide hosted-API-vs-self-host at SAE-browsing (workflow 4) bring-up. Until then: deep-links to **public** model/feature pages only; **no prompt text to the Neuronpedia API at all**, and exam-derived text never to any third-party API.
**Consequences.** Phase 3 sends nothing to Neuronpedia; the restricted-corpora precedent is parked with the decision.

### ADR-0029 — Modal deferred to Phase 4
**Status:** Reserved-seam · **Source:** phase2 E3.
**Decision.** No Modal account today; the `storage_backends` + `rate_cards` registries reserve the row. Decide at Phase 4 bring-up when a real big-VRAM burst is concrete.
**Consequences.** No new billed provider is approved in Phase 3.

### ADR-0030 — Quantization: no policy bar; fingerprint-recorded
**Status:** Accepted · **Source:** phase2 E5.
**Decision.** No publication policy bar on quantization. Any quant level is publishable so long as the fingerprint records it (ADR-0005) and **pooling never crosses quant boundaries silently**. Reviewers judge case by case.
**Consequences.** Cross-quant comparison is itself an experiment, never an accident — enforced by the fingerprint participating in run identity. Maximizes usable rented-GPU work.

### ADR-0031 — Qwen-Scope deferred to an empty registry row
**Status:** Reserved-seam · **Source:** phase2 E7.
**Decision.** Consolidate SAE-era work on Gemma-2/3 + Llama-3.1-8B (fully tooled, permissive). Qwen-Scope is a registry row with `loader_format` recorded; the bespoke `.pt` loader is built when a Qwen experiment is real.
**Consequences.** `assets.loader_format` is mandatory day-one precisely so this row can exist inert (Qwen-Scope is non-SAELens `.pt`).

### ADR-0032 — SAE training: schema-yes / code-later
**Status:** Accepted (schema) / Reserved-seam (code) · **Source:** phase2 E8.
**Decision.** Phase 3 DDL carries `sae_training_runs` provenance (trainer config, dataset identity, token count, library version, resulting local `sae_release`) + the local-release asset case (no HF repo). **Zero trainer code** until a training run is justified.
**Consequences.** DDL-only reservation — distinct from the code-prebuild pattern the panel dinged (ARCC, ADR-0018). Locally-trained releases have no `hf_repo`; `assets` accommodates the null.

### ADR-0033 — Dense-lane ≤500 GB; single-layer default for 8–9B
**Status:** Accepted · **Source:** phase2 E9.
**Decision.** The TTL dense lane consumes **≤500 GB** desktop NVMe. **Single-layer capture is the default for 8–9B models**; all-layer is reserved for explicitly planned sweeps; TTLs are short (days). Cluster scratch is in-scope TTL territory (ADR-0010 purge-window).
**Consequences.** Sizing constant for the worker's VRAM/disk preflight and the TTL reaper. Drives the capture contract's default `layer_selection`.

### ADR-0034 — Pin = promote-at-publication + manual `pin now`
**Status:** Accepted · **Source:** phase2 E10.
**Decision.** Default: promote tensors to cloud at **publication** time; the recompute recipe covers the loss window. Plus an explicit `neuro pin` CLI that uploads immediately when bytes are deemed irreplaceable. Budget stays lazy by default; quota guard sized for the lazy default.
**Consequences.** A desktop failure before publication/pin loses dense bytes → falls back to the recorded recompute recipe (artifact row survives with checksum+shape+recipe).

### ADR-0035 — 4090 host: WSL2, HF cache on ext4
**Status:** Accepted · **Source:** phase2 E11.
**Decision.** The desktop stays Windows; the worker runs under **WSL2** with the HF cache + dense lane on **ext4 inside WSL2** (avoids the 9p I/O penalty). cuda-checkpoint/CRIU stays off the table (acceptable — checkpoint-first design targets vast.ai). The Windows Scheduler hosts the desktop health agent (ADR-0015).
**Consequences.** Cold-start row of the worker math table assumes ext4-resident cache; the `[research]` benchmark (WSL2-ext4 vs native) is an open implementation item, not a blocker.

### ADR-0036 — Ad-hoc capture: auto-mint + label-later
**Status:** Accepted · **Source:** phase2 E12.
**Decision.** `capture_events.run_id` is NOT NULL; every uncontexted call auto-mints into an `adhoc` session run (closes the `repository=None` bypass). Adhoc rows are flagged `unlabeled`; the preflight banner counts them; `neuro runs adopt` retroactively labels them.
**Consequences.** The engagement's answer to the "interactive inline lane grows via ad-hoc auto-minting" rot-watch — completeness without ceremony, with a visible nag and a cheap fix.

### ADR-0037 — Display keys: hybrid slug + digest
**Status:** Accepted · **Source:** phase2 E13.
**Decision.** Run-key grammar `{campaign_key}/{work_slug}/{variant_digest}[/inv-{uuid8}]`, where `work_slug` carries human-readable domain coordinates by convention and `variant_digest` is the short uniqueness suffix. Components are **also stored as real columns** (never parse the string to recover structure — KEEP rule).
**Consequences.** Greppable in logs/paths, bounded length, uniform across run kinds. Locked into the composer module, blob path templates, and bundle layouts. The optional `[/inv-{uuid8}]` segment maps to the real column `runs.invocation_id` (uuid, NULL for the canonical run); re-invocations ARE allowed (the force-new-run path of ADR-0005) and coexist because `runs_experiment_variant_uq` is `NULLS NOT DISTINCT` over (campaign, slug, digest, invocation_id) — the NULL canonical stays unique while non-NULL re-invocations never collide (C1). The composer sets `invocation_id`; it is never recovered by parsing the key.

### ADR-0038 — CLI name is `neuro`
**Status:** Accepted · **Source:** phase2 E16.
**Decision.** `neuro` is the single console entrypoint in `[project.scripts]`, runbooks, systemd/scheduler units, generated docs, and CI smoke commands.
**Consequences.** Footnote: legacy PyPI `neuro-cli` historically claimed the `neuro` command — PATH collision only if that tool is ever installed alongside.

### ADR-0043 — Lineage as relationship-edges-only
**Status:** Accepted · **Source:** phase0 Q15 KEEP; phase1 d1.
**Decision.** `lineage_edges` holds **relationship edges only** (src/dst typed-entity references + edge_kind); identities are evicted to typed tables. Edges survive for curation, annotation, derived-set provenance (paraphrase→source links), and the taint-query reserve (ADR-0025; obligation withdrawn by ADR-0049).
**Consequences.** No identity data hides in a generic graph. Generative-inference derived sets (phase0 Q1) get content-hash identity + lineage edges to source set and generating run.

### ADR-0044 — `AUTHOR-DISCRETION`: no pgvector day-one
**Status:** Author-discretion — **RESOLVED 2026-06-16, owner ACCEPTED** (no longer an open DoF) · **Source:** digest DoF "pgvector use"; b (no justified day-one use).
**Decision.** No pgvector extension day-one. Embeddings are **lake artifacts** (parquet derived-feature lane); similarity search is a DuckDB/numpy concern on export, not a PG index. If an in-DB ANN need materializes, it is a later additive migration.
**Reasons.** Embeddings are bulk → they obey the cardinality law (ADR-0002) and live in the lake, not PG. Adding pgvector now is a speculative registry (NEVER-AGAIN). The predecessor's pgvector use was not load-bearing for the chosen export-discipline surface.
**Owner ruling (2026-06-16):** ACCEPTED — no pgvector day-one is the decision; embeddings stay lake artifacts; this axis is closed.

### ADR-0045 — Content-addressed wire-spill + seam shard storage keys
**Status:** Accepted — Phase 5 red-team correction (FIX #1 + #7) · **Source:** phase5 matrix #1/#7; panel #1 C1.
**Decision.** The storage KEY of a verbatim wire-payload spill (`capture/events.py:_spill`) and of a bundle seam shard (`bundles/registrar.py:register`) is a FUNCTION of the content sha256, not of the run/dataset/partition coordinates. Wire spills land at `{partition}/wire/{sha256}.json`; seam shards at `{partition}/{sha256}/{name}`. The run/partition PREFIX is preserved (browsable, and the queryable parquet keeps its meaningful partition path in `table_manifests`); only the filename/segment becomes content-derived. The `capture_events`→artifact and `table_manifests`→artifact linkages stay on the FK, NEVER on the path.
**Reasons.** With coordinate-derived keys, two DIVERGENT byte payloads for the same logical event mapped to the SAME key, so a non-atomic `put`-before-`INSERT` gap left an orphan blob that a later divergent spill silently overwrote (#1), and two concurrent registers on one `bundle_uuid` clobbered each other's blob so the committed `artifacts.sha256` disagreed with the bytes on disk (#7). Content-addressing makes a divergent overwrite AT THE SAME KEY unrepresentable (different bytes ⇒ different key); identical bytes are idempotent (one artifact, no re-write).
**Consequences.** A committed artifact's blob always matches its `sha256`. The torn-blob reconcile (a committed sha that can still disagree with a clobbered blob after a concurrency loss elsewhere) remains the unbuilt `crawl.py`'s job. Content-addressing yields cross-event dedup of identical wire payloads, which is acceptable.

### ADR-0046 — `READ COMMITTED` isolation + targeted row-locking for contended paths
**Status:** Accepted — targeted row-locking IMPLEMENTED (sessions C1/C2, 2026-07-27); the worker/registrar role split and its two column-immutability arms remain open · **Source:** phase5 panel #1 cross-cutting (opus root-cause); owner ruling 2026-06-23.
**Decision.** Record (do NOT yet implement) the structural direction for the concurrency family: the session engine (`db/session.py`) sets no isolation level, so every guard is software CAS / SELECT-then-act over `READ COMMITTED`. The point-fixes #6 (heartbeat `expires_at >= now()` + atomic reaper), #7 (content-addressed seam keys), and #10 (single-statement GC delete with delete-time recheck) are the concrete regression net NOW. The structural change — a targeted `SERIALIZABLE`/`REPEATABLE READ` on the contended transactions, OR `FOR UPDATE` on the contended lease/bundle rows — is a LATER decision so the NEXT concurrent path inherits the guarantee rather than being a fresh hole.
**Reasons.** Four separate fail-opens (#6/#7/#10 + the capture/register races) were all instances of one root cause. Four point-fixes close the known holes but do not make the class safe by construction; a deliberate isolation/locking policy would. Doing it now, with no untrusted-worker topology and the point-fixes already landed, would be a large change ahead of the need (the worker/registrar split is itself deferred).
**Consequences.** The build's concurrency probes (`tests/redteam/test_rt_concurrency.py`) are the regression net regardless of which way this resolves. When the worker runtime / role split lands, revisit alongside the in-schema-trigger question for the jobs/bundles state columns (Deferred-Obligation Register).
**Scope addition 2026-07-02 (audit-confirmed vector — dependent-unblock write-skew).** Two parents of an AND-gated child running `complete()` concurrently at READ COMMITTED each evaluate the dependent-unblock NOT-EXISTS predicate (`complete()`'s final statement, `db/repository.py:834-847`) against a snapshot that excludes the other's uncommitted `succeeded`; both skip the child **without locking it** (the child row is never updated on the skip path, so there is no row conflict and no EvalPlanQual recheck), stranding it `blocked` permanently — the only other `blocked→queued` writer is enqueue-time C20. **Recorded explicitly: REPEATABLE READ / SERIALIZABLE-lite bumps do NOT close write-skew — the fix requires locking the child or its dependency rows (`FOR UPDATE`), consistent with this bundle's chosen row-lock mechanism.** This vector joins the C20-concurrent enqueue interleave and the #6 near-expiry reaper wedge in this ADR's scope.
**Addendum 2026-07-27 — IMPLEMENTED in two sessions; the mechanism differs from this ADR's text in one measured way, and the guarantee is narrower than "the CAS is now in the schema".** **Session C1 (row-locking)** closed all three in-scope vectors — the #6 near-expiry reaper wedge, `complete()`'s dependent-unblock write-skew, and the C20 enqueue TOCTOU — each with a hammer proven RED against the pre-fix code. ⚠ **The lock strength is `FOR NO KEY UPDATE`, never the bare `FOR UPDATE` this ADR names:** measured against a live postgres:18, `FOR UPDATE` is the only level that conflicts with the implicit RI `FOR KEY SHARE` that `INSERT INTO job_dependencies` takes on the very same `jobs` rows, and using it opened a real jobs↔jobs deadlock (REPRODUCED as `DeadlockDetected`, with a clean pre-fix control). The same pass added an ascending pre-lock in `_cascade_cancel`, so every jobs-row acquirer is now ascending by `job_id` and the whole locking pass is cycle-free; `claim()`'s `SKIP LOCKED` hot path is untouched. **Session C2 (migration `0003`)** converted the jobs/bundles state CAS and the `capture_events.model_id` bind into in-schema triggers, whose legal edge sets are the shipped writers enumerated rather than a designed ladder — no stricter, no looser. **What that does NOT give:** the triggers forbid any single UPDATE that skips a rung and make `succeeded`/`cancelled`/`dead_letter` absorbing, but they do not stop a role holding `GRANT UPDATE (state, …)` from WALKING the ladder statement by statement (`queued → claimed → succeeded` is two legal hops), because every column a stronger predicate could test is itself granted to that role and forgeable in the same transaction. Closing THAT is the worker/registrar role split — revoke `UPDATE (state)` and route the CAS through `SECURITY DEFINER` functions — together with the two arms deliberately left out of `0003`: `jobs.claim_token`/`claim_seq` fencing and `bundles.manifest_sha256` assign-once. The residual is pinned as a positive red-team probe so it cannot be silently mis-remembered as closed.

### ADR-0047 — `record_divergence` is keep-first on conflict
**Status:** Accepted — no code change (ADR-accept #3) · **Source:** phase5 matrix #3; panel #1 C14.
**Decision.** `record_divergence` keeps the FIRST measurement on a `(replicate_link, method_version)` conflict (`ON CONFLICT DO NOTHING` + re-SELECT), never last-wins. No code change; this records the rationale and the probe pins the contract (a second call with DIVERGENT metrics returns the first id and the first values persist).
**Reasons.** The two inputs are IMMUTABLE, durably-retained captures and the divergence method is VERSION-PINNED (its `code_sha` is registry/runtime-parity-guarded, ADR-0011). A re-measurement is therefore fully recomputable and a value disagreement is structurally impossible WITHOUT a prior upstream guard failure — nothing here is perishable. The genuinely perishable signal (a real divergence VERDICT) is separately protected by persist-before-raise in `replicate_and_measure`.
**Consequences.** Keep-first is safe ONLY while the upstream `code_sha` parity guard holds; that guard is the backstop the keep-first contract leans on, and it is asserted (`test_registered_divergence_method_code_sha_parity`).

### ADR-0048 — `actor`/`campaign` key-drift accepted now; owner-scoped keys deferred
**Status:** Layer (a) BUILT 2026-07-14 (drift-guard landed); layer (b) owner-scoped keys still deferred (ADR-accept #5) · **Source:** phase5 matrix #5; panel #1 C-actor.
**Decision.** `get_or_create_actor` / `get_or_create_campaign` return the existing row by key with NO comparison — a campaign re-created under the same `campaign_key` with a different `actor_id` silently keeps the old owner. ACCEPTED as-is under single-user credentials; the Phase-5 probe DOCUMENTS the current behavior (it does not assert a fix). The obligation is registered in TWO layers in the Deferred-Obligation Register: (a) the trivial drift-guard code fix (raise-on-drift + `ON CONFLICT`, matching the sibling registries), and (b) the deeper owner-scoped-key namespacing design.
**Reasons.** The real bite is NOT single-user careless reuse (low today) but TWO users colliding on a shared human-readable key, which silently reassigns ownership/lineage. That bite only exists once multi-user credentials OR the importer land — which is also exactly when the fix is cheap to land with its proper key-namespacing design rather than a hasty guard now.
**Consequences.** Trigger = multi-user creds OR the importer. Until then the single-user behavior is pinned by a probe so a regression (or the arrival of the trigger) is visible.
**Addendum 2026-07-14 — layer (a) is BUILT (the importer trigger is arriving).** The recorded trigger ("multi-user creds OR the importer") is arriving: the importer is the next build, and `runs.actor_id` / `runs.campaign_id` are both NOT NULL, so every promoted run resolves through these two functions. Layer (a) — the drift-guard code fix — has LANDED: `get_or_create_actor` / `get_or_create_campaign` now use the sibling registries' bare `ON CONFLICT DO NOTHING` → re-SELECT → raise-on-drift shape (which also closes their concurrent-first-creation race). Actor identity is (`actor_key`, `kind`): one key bound to two kinds raises. `display_name` is deliberately NOT compared — it is a mutable label, the column is NOT NULL and coerced to `actor_key` on INSERT (so there is no "unspecified" state to compare against), and the registrar holds no UPDATE on `actors`, so raising on a label would be unrecoverable in-role; the existing label is kept (no last-writer-wins). Campaign ownership (`actor_id`) IS identity-bearing and now raises instead of silently keeping the old owner. The Phase-5 probe that formerly DOCUMENTED the bug now asserts the RAISE. **Layer (b) — the owner-scoped-key namespacing design — remains DEFERRED**; this addendum discharges the code half only.

### ADR-0049 — Taint-query retrofit obligation withdrawn (supersedes ADR-0025)
**Status:** Accepted · **Source:** owner ruling 2026-07-12.
**Decision.** The ADR-0025 retrofit OBLIGATION — "restriction is retroactively computable (one migration + one taint query)" as a committed path — is WITHDRAWN. The CAPABILITY is not: taint stays computable over what the built lane records (prompt identity inside `fingerprints.semantic_config` + the verbatim `capture_events.request_text`, per the 2026-07-02 conformance audit, plus the typed-FK lineage that exists independently of `lineage_edges` — `run_inputs` stimulus rows, `prompt_sets.derived_from_run_id`, NOT-NULL `capture_events.run_id`), and the `lineage_edges` DDL COMMENT ("Enables ADR-0025 taint retrofit") deliberately stays — with its frozen-DDL mirror in `tests/reference/phase3-ddl.sql` — as a true capability statement (editing the COMMENT would itself be a schema change). The taint-query reserve on ADR-0043's edges is now NON-OBLIGATORY: no compliance promise rides on edge completeness. Supersession, not rewrite: ADR numbers are permanent, ADR-0003/ADR-0043 and the DDL COMMENT cross-reference ADR-0025's text, and the withdrawal is a new decision carrying its own residual and revival trigger.
**Reasons.** Risk-honesty, not "the institution covers it": the retrofit promise was the largest dangling risk left from the original design — an unenforced compliance guarantee whose coverage nothing requires and whose failure is silent. Recorded edges are integrity-protected (INSERT-only grants, UNIQUE) but COVERAGE is unenforced: src/dst are free-text and un-FK'd by design (ADR-0043), every producer is a Stage-2 stub, nothing reads the table, and no mechanism requires an edge to exist — so the retroactive query is FALSE-NEGATIVE-BIASED (anything with a lineage gap reads "clean"), the worst shape for a compliance control. The existing, real security for most if not all data of this kind is what we already have: storage in a non-publicly-accessible database behind role-based credentials (ADR-0007) — an operative control whose real perimeter includes every copy (desktop mirror, Azure `db-backups`, restore-drill clones, the future blob lake). We stop promising a capability no mechanism enforces; the access boundary is the operative control.
**Consequences.** What survives, explicitly: the soft rule (exam-derived raw text is never posted publicly); ADR-0028's exam-text-never-to-any-third-party-API ban; content-hash identity for prompt sets; and ADR-0043 in full — edge-writing discipline is DECOUPLED from the withdrawn obligation (edges keep being written for curation, annotation, and derived-set provenance; ADR-0043's "derived sets get content-hash identity + lineage edges to source set and generating run" consequence is unaffected). The accepted residual, named honestly: post-incident triage of exam-content exposure from any copy would have needed the taint query and now has no mechanism; and since retroactive taint is only as good as contemporaneously written edges, the withdrawal is effectively ONE-WAY. Revival trigger (ADR-0048 idiom): the first Stage-B publication/export surface (ADR-0034 pin-at-publication) OR multi-user credentials — re-evaluate BEFORE crossing either. The Deferred-Obligation Register was checked 2026-07-12 and carries no taint-premised entry.
**Addendum 2026-07-13 (owner follow-up ruling) — the mechanism door is shut, not only the promise.** PERMANENTLY CLOSED, independent of timing: a voluntary-coverage retroactive blocklist as a compliance control — a query over optional edges whose gaps read as clean, relied on as complete. The revival trigger's "re-evaluate" is never to be read as "now run the originally planned taint query"; the SHAPE is the defect. What explicitly survives: partial taint queries as post-incident FORENSICS (incompleteness stated, output never treated as complete), and future controls CONSUMING lineage/provenance data — provenance is the substrate of any taint system. The two-layer standard going forward: taint-relevant edges are ordinary ADR-0043 provenance (no special epistemic status; only as good as contemporaneous writes); any future confidentiality CONTROL enters at the house gate standard — choke-point enforced, coverage by mechanism not discipline, fail-closed (a gap reads dirty, never clean) — or not at all. Candidate shapes already identified: stamp-at-ingress in the importer write path (the `promotions` FK substrate); a fail-closed allowlist proof at any publication/export surface. Revival trigger WIDENED to three prongs (owner-ruled 2026-07-13): the first Stage-B publication/export surface (ADR-0034 pin-at-publication) OR multi-user credentials OR the importer landing (bulk exam ingress via `external_records`/`promotions`; ADR-0048's own trigger idiom).

---

## Traceability index

| Source | ADRs |
|---|---|
| phase2 V1 / checkpoint chassis | 0001, 0002, 0003, 0042 |
| 4 signed deviation ADRs | 0008, 0009, 0010, 0011 |
| 13 conditions | 0012–0024 |
| phase0 bindings | 0004, 0005, 0006, 0007, 0025, 0026, 0039, 0040, 0043 |
| escalations E1–E16 | 0019(E15), 0024(E14), 0027(E1), 0028(E2), 0029(E3), 0030(E5), 0031(E7), 0032(E8), 0033(E9), 0034(E10), 0035(E11), 0036(E12), 0037(E13), 0038(E16); E4(g3.xl)/E6(determinism default) are Phase-4 runtime items, see capture contract §6 |
| AUTHOR-DISCRETION → RESOLVED 2026-06-16 | 0041 (native PG — ACCEPTED), 0044 (no pgvector — ACCEPTED) |

**Open `[research]` items carried into Phase 4 implementation (not settled from memory):** vllm-lens↔vllm tested version pair; VLLM_BATCH_INVARIANT logprob-bitwise empirical test (E6 gate); zstd ratios on real captures; WSL2-ext4 vs native cold-start; SAELens-6 ↔ TransformerLens-3.x runtime compat; TransformerBridge MLP hook pre/post; Azure AI Foundry logprob/seed inventory; g3.xl grant state (E4). These are flagged, not resolved.
