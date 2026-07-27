# Architecture Decision Records (generated index)

Split from `docs/adr/_source/phase3-adrs.md` by `neuro docs build`. ADR numbers are permanent.

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](0001-adopt-the-b-chassis-hybrid.md) | Adopt the B-chassis hybrid | Accepted |
| [ADR-0002](0002-cardinality-law-as-schema-law.md) | Cardinality law as schema law | Accepted (from pole A) |
| [ADR-0003](0003-byte-exact-text-wire-bodies-never-jsonb.md) | Byte-exact TEXT wire bodies, never JSONB | Accepted (from pole A) |
| [ADR-0004](0004-three-layer-determinism-model-replaces-the-enum.md) | Three-layer determinism model replaces the enum | Accepted |
| [ADR-0005](0005-identity-fingerprint-semantics.md) | Identity & fingerprint semantics | Accepted |
| [ADR-0006](0006-lanes-v2-positive-identity-unknown-fails-closed.md) | Lanes v2: positive identity, UNKNOWN fails closed | Accepted |
| [ADR-0007](0007-least-privilege-roles.md) | Least-privilege roles | Accepted |
| [ADR-0008](0008-mmap-dense-shard-read-verification-softening.md) | mmap dense-shard read-verification softening | Signed-deviation (ADR-1) |
| [ADR-0009](0009-duckdb-direct-reads-unverifiable-in-band-accepted.md) | DuckDB direct reads unverifiable in-band; accepted | Signed-deviation (ADR-2) |
| [ADR-0010](0010-sealed-bundle-gc-exemption-venue-purge-window.md) | Sealed-bundle GC exemption + venue purge-window | Signed-deviation (ADR-3) |
| [ADR-0011](0011-promotion-on-demand-satellite-doctrine.md) | Promotion-on-demand satellite doctrine | Signed-deviation (ADR-4) |
| [ADR-0012](0012-registration-hashing-of-cloud-bound-shards-condition-1.md) | Registration hashing of cloud-bound shards (condition 1) | Accepted |
| [ADR-0013](0013-sas-credential-lifecycle-condition-2.md) | SAS credential lifecycle (condition 2) | Accepted |
| [ADR-0014](0014-audit-against-the-desktop-mirror-condition-3.md) | Audit against the desktop mirror (condition 3) | Accepted |
| [ADR-0015](0015-desktop-half-health-surfacing-condition-4.md) | Desktop-half health surfacing (condition 4) | Accepted |
| [ADR-0016](0016-hook-vocabulary-pinning-condition-5.md) | Hook-vocabulary pinning (condition 5) | Accepted |
| [ADR-0017](0017-run-metrics-valve-closure-condition-6.md) | run_metrics valve closure (condition 6) | Accepted |
| [ADR-0018](0018-arcc-relay-gating-condition-7.md) | ARCC relay gating (condition 7) | Reserved-seam |
| [ADR-0019](0019-probe-alert-channel-is-a-blocking-precondition-condition-8-e15.md) | Probe alert channel is a blocking precondition (condition 8 + E15) | Accepted |
| [ADR-0020](0020-durability-staleness-gate-condition-9.md) | Durability-staleness gate (condition 9) | Accepted |
| [ADR-0021](0021-api-lane-incremental-registration-condition-10.md) | API-lane incremental registration (condition 10) | Accepted |
| [ADR-0022](0022-prompt-spill-path-condition-11.md) | Prompt spill path (condition 11) | Accepted |
| [ADR-0023](0023-mcq-stimulus-family-stays-in-pg-condition-12.md) | MCQ stimulus family stays in PG (condition 12) | Accepted |
| [ADR-0024](0024-ci-branch-protection-precondition-condition-13-e14.md) | CI / branch-protection precondition (condition 13 + E14) | Accepted |
| [ADR-0025](0025-no-restricted-flag-day-one-taint-query-retrofit-path.md) | No restricted-flag day one; taint-query retrofit path | Superseded by ADR-0049 (2026-07-12) |
| [ADR-0026](0026-tracker-emit-default-off-post-finalize-emitter-seam.md) | Tracker-emit default OFF; post-finalize emitter seam | Reserved-seam |
| [ADR-0027](0027-ndif-documented-seam-only.md) | NDIF documented seam only | Reserved-seam |
| [ADR-0028](0028-neuronpedia-deferred-to-workflow-4.md) | Neuronpedia deferred to workflow 4 | Reserved-seam |
| [ADR-0029](0029-modal-deferred-to-phase-4.md) | Modal deferred to Phase 4 | Reserved-seam |
| [ADR-0030](0030-quantization-no-policy-bar-fingerprint-recorded.md) | Quantization: no policy bar; fingerprint-recorded | Accepted |
| [ADR-0031](0031-qwen-scope-deferred-to-an-empty-registry-row.md) | Qwen-Scope deferred to an empty registry row | Reserved-seam |
| [ADR-0032](0032-sae-training-schema-yes-code-later.md) | SAE training: schema-yes / code-later | Accepted (schema) / Reserved-seam (code) |
| [ADR-0033](0033-dense-lane-500-gb-single-layer-default-for-8-9b.md) | Dense-lane ≤500 GB; single-layer default for 8–9B | Accepted |
| [ADR-0034](0034-pin-promote-at-publication-manual-pin-now.md) | Pin = promote-at-publication + manual `pin now` | Accepted |
| [ADR-0035](0035-4090-host-wsl2-hf-cache-on-ext4.md) | 4090 host: WSL2, HF cache on ext4 | Accepted |
| [ADR-0036](0036-ad-hoc-capture-auto-mint-label-later.md) | Ad-hoc capture: auto-mint + label-later | Accepted |
| [ADR-0037](0037-display-keys-hybrid-slug-digest.md) | Display keys: hybrid slug + digest | Accepted |
| [ADR-0038](0038-cli-name-is-neuro.md) | CLI name is `neuro` | Accepted |
| [ADR-0039](0039-one-queue-skip-locked-claim-runtime-owned-leases.md) | One queue: SKIP LOCKED claim, runtime-owned leases | Accepted |
| [ADR-0040](0040-storage-backend-registry-azurite-only-ci.md) | Storage backend registry; Azurite-only CI | Accepted |
| [ADR-0041](0041-author-discretion-native-pgdg-postgres-on-the-vm.md) | `AUTHOR-DISCRETION`: native PGDG Postgres on the VM | Author-discretion — **RESOLVED 2026-06-16, owner ACCEPTED** (no longer an open DoF) |
| [ADR-0042](0042-vm-resize-deferral-cinder-volume-instead.md) | VM resize deferral; Cinder volume instead | Accepted (from pole A, resize NOT adopted) |
| [ADR-0043](0043-lineage-as-relationship-edges-only.md) | Lineage as relationship-edges-only | Accepted |
| [ADR-0044](0044-author-discretion-no-pgvector-day-one.md) | `AUTHOR-DISCRETION`: no pgvector day-one | Author-discretion — **RESOLVED 2026-06-16, owner ACCEPTED** (no longer an open DoF) |
| [ADR-0045](0045-content-addressed-wire-spill-seam-shard-storage-keys.md) | Content-addressed wire-spill + seam shard storage keys | Accepted — Phase 5 red-team correction (FIX #1 + #7) |
| [ADR-0046](0046-read-committed-isolation-targeted-row-locking-for-contended-paths.md) | `READ COMMITTED` isolation + targeted row-locking for contended paths | Accepted — targeted row-locking IMPLEMENTED (sessions C1/C2, 2026-07-27); the worker/registrar role split and its two column-immutability arms remain open |
| [ADR-0047](0047-record-divergence-is-keep-first-on-conflict.md) | `record_divergence` is keep-first on conflict | Accepted — no code change (ADR-accept #3) |
| [ADR-0048](0048-actor-campaign-key-drift-accepted-now-owner-scoped-keys-deferred.md) | `actor`/`campaign` key-drift accepted now; owner-scoped keys deferred | Layer (a) BUILT 2026-07-14 (drift-guard landed); layer (b) owner-scoped keys still deferred (ADR-accept #5) |
| [ADR-0049](0049-taint-query-retrofit-obligation-withdrawn-supersedes-adr-0025.md) | Taint-query retrofit obligation withdrawn (supersedes ADR-0025) | Accepted |
