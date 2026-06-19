### ADR-0002 — Cardinality law as schema law
**Status:** Accepted (from pole A) · **Source:** checkpoint "From A".
**Decision.** The finest grain Postgres ever stores is `capture_events` (one row per model interaction — API call or GPU forward batch). No table is permitted to grow O(tokens) or O(features). Any per-token/per-feature data is a lake artifact registered by a manifest row. This is enforced socially by review and structurally by the absence of any per-token table in the DDL.
**Consequences.** `capture_events` is the cardinality ceiling; sizing math (capture contract §7) is bounded by interactions, not tokens. The derived-satellite exception (ADR-0011) is the *only* sanctioned path to per-response PG rows, and only on demand.
