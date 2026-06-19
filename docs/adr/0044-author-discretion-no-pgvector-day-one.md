### ADR-0044 — `AUTHOR-DISCRETION`: no pgvector day-one
**Status:** Author-discretion — **RESOLVED 2026-06-16, owner ACCEPTED** (no longer an open DoF) · **Source:** digest DoF "pgvector use"; b (no justified day-one use).
**Decision.** No pgvector extension day-one. Embeddings are **lake artifacts** (parquet derived-feature lane); similarity search is a DuckDB/numpy concern on export, not a PG index. If an in-DB ANN need materializes, it is a later additive migration.
**Reasons.** Embeddings are bulk → they obey the cardinality law (ADR-0002) and live in the lake, not PG. Adding pgvector now is a speculative registry (NEVER-AGAIN). The predecessor's pgvector use was not load-bearing for the chosen export-discipline surface.
**Owner ruling (2026-06-16):** ACCEPTED — no pgvector day-one is the decision; embeddings stay lake artifacts; this axis is closed.

---
