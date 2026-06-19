### ADR-0030 — Quantization: no policy bar; fingerprint-recorded
**Status:** Accepted · **Source:** phase2 E5.
**Decision.** No publication policy bar on quantization. Any quant level is publishable so long as the fingerprint records it (ADR-0005) and **pooling never crosses quant boundaries silently**. Reviewers judge case by case.
**Consequences.** Cross-quant comparison is itself an experiment, never an accident — enforced by the fingerprint participating in run identity. Maximizes usable rented-GPU work.
