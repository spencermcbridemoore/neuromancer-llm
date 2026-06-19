### ADR-0026 — Tracker-emit default OFF; post-finalize emitter seam
**Status:** Reserved-seam · **Source:** phase0 Q12; checkpoint.
**Decision.** No MLflow/W&B emission by default. A post-finalize emitter seam is reserved (run summaries can be emitted as a *viewing* layer later). Heavy-tracker/thin-Postgres is ruled out permanently.
**Consequences.** The Postgres provenance core is the only source of truth; trackers, if ever enabled, are downstream and non-authoritative.
