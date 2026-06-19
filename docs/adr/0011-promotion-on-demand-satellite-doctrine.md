### ADR-0011 — Promotion-on-demand satellite doctrine
**Status:** Signed-deviation (ADR-4) · **Source:** phase2 ADR-4 "Sign".
**Decision.** Derived Postgres tables (e.g. `mcq_responses`) exist **only** via explicit promotion when a concrete experiment consumes them — never always-on. Each promoted satellite pays the **governance trio**: `method_version_id` on every derived row + a `neuro derive` re-derive CLI + a parity probe comparing the satellite to its lake source. **`mcq_responses` is NOT pre-created in the Phase 3 DDL** (C1); the promotion *machinery* is specified, the first satellite waits for demand.
**Consequences.** Keeps A's most useful pattern (fast SQL on derived MCQ data) without A's standing silent-wrongness surface. Promotion is a deliberate, audited act. Distinct from the MCQ *stimulus* family, which is always-on first-class PG (ADR-0023).

---
