### ADR-0032 — SAE training: schema-yes / code-later
**Status:** Accepted (schema) / Reserved-seam (code) · **Source:** phase2 E8.
**Decision.** Phase 3 DDL carries `sae_training_runs` provenance (trainer config, dataset identity, token count, library version, resulting local `sae_release`) + the local-release asset case (no HF repo). **Zero trainer code** until a training run is justified.
**Consequences.** DDL-only reservation — distinct from the code-prebuild pattern the panel dinged (ARCC, ADR-0018). Locally-trained releases have no `hf_repo`; `assets` accommodates the null.
