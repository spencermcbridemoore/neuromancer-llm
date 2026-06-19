### ADR-0022 — Prompt spill path (condition 11)
**Status:** Accepted. **Decision.** Any hand-authored prompt **>8 KB** takes the artifact-FK spill path (blob + artifact row), regardless of origin. **Consequences.** Prompts obey the same 8 KB inline cap as wire bodies (ADR-0003); large stimuli never bloat the canonical row.
