### ADR-0048 — `actor`/`campaign` key-drift accepted now; owner-scoped keys deferred
**Status:** Accepted now (single-user) — two-layer obligation deferred (ADR-accept #5) · **Source:** phase5 matrix #5; panel #1 C-actor.
**Decision.** `get_or_create_actor` / `get_or_create_campaign` return the existing row by key with NO comparison — a campaign re-created under the same `campaign_key` with a different `actor_id` silently keeps the old owner. ACCEPTED as-is under single-user credentials; the Phase-5 probe DOCUMENTS the current behavior (it does not assert a fix). The obligation is registered in TWO layers in the Deferred-Obligation Register: (a) the trivial drift-guard code fix (raise-on-drift + `ON CONFLICT`, matching the sibling registries), and (b) the deeper owner-scoped-key namespacing design.
**Reasons.** The real bite is NOT single-user careless reuse (low today) but TWO users colliding on a shared human-readable key, which silently reassigns ownership/lineage. That bite only exists once multi-user credentials OR the importer land — which is also exactly when the fix is cheap to land with its proper key-namespacing design rather than a hasty guard now.
**Consequences.** Trigger = multi-user creds OR the importer. Until then the single-user behavior is pinned by a probe so a regression (or the arrival of the trigger) is visible.

---
