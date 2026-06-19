### ADR-0025 — No restricted-flag day one; taint-query retrofit path
**Status:** Accepted · **Source:** phase0 Q3.
**Decision.** No `restricted` flag in the day-one schema; access control is roles/credentials. Because every export/payload/derived artifact carries lineage to its prompt set, restriction is retroactively computable (one migration + one taint query). Soft rule: exam-derived raw text is never posted publicly. Content-hash identity for prompt sets stays regardless.
**Consequences.** This ADR *is* the recorded retrofit path. Lineage completeness (ADR-0043's `lineage_edges`) is what makes the taint query possible later.
