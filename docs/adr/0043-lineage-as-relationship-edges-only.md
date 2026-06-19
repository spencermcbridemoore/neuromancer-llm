### ADR-0043 — Lineage as relationship-edges-only
**Status:** Accepted · **Source:** phase0 Q15 KEEP; phase1 d1.
**Decision.** `lineage_edges` holds **relationship edges only** (src/dst typed-entity references + edge_kind); identities are evicted to typed tables. Edges survive for curation, annotation, derived-set provenance (paraphrase→source links), and the taint-query reserve (ADR-0025).
**Consequences.** No identity data hides in a generic graph. Generative-inference derived sets (phase0 Q1) get content-hash identity + lineage edges to source set and generating run.
