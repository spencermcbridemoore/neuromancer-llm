### ADR-0004 — Three-layer determinism model replaces the enum
**Status:** Accepted · **Source:** phase0 Q10; phase1 d2.
**Decision.** Determinism is three independent things, never one enum:
1. **DECLARED mode** — `deterministic_algo | greedy | seeded_sampling | unseeded_sampling`, *derived from the captured wire payload* (what was requested AND what the provider honored), participates in the semantic config and therefore the fingerprint.
2. **EXPECTED reproducibility level** — `bitwise | tolerance | distributional | none`, a maintained heuristic rule table keyed (declared_mode × substrate); **never touches identity**; overridable per-run via `runs.expected_level_override` (A6).
3. **MEASURED reproducibility** — replicate runs linked to the original via `replicate_links`, storing divergence metrics (max abs/rel diff, argmax-flip, `answer_letter_flip_rate`, near-tie nat-margin buckets). Answer flips are first-class MCQ-position-bias data.
**Consequences.** Three DDL homes: `fingerprints` (declared in the hashed semantic config), `expected_reproducibility_rules` (heuristic table), `divergence_measurements` + `replicate_links` (measured). The bitwise-vs-tolerance *default* (E6; detailed in capture contract §6) is a runtime serving-config default, not schema — the schema seats both branches.
