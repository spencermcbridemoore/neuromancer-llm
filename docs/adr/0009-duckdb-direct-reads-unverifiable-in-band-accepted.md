### ADR-0009 — DuckDB direct reads unverifiable in-band; accepted
**Status:** Signed-deviation (ADR-2) · **Source:** phase2 ADR-2 "Sign".
**Decision.** Direct DuckDB-over-https ranged reads of lake parquet **cannot** be sha256-verified in-band; this is accepted. Residual is bounded by the mirror audit (ADR-0014) + quarterly cloud spot-sample. Docs state the residual honestly.
**Consequences.** The direct-DuckDB product surface is preserved. **The offered parquet-page-checksum tightening was NOT taken** (C5) — lake writers are not required to enable page checksums. (If the owner later wants it, it is an additive writer-config change, not a schema change.)
