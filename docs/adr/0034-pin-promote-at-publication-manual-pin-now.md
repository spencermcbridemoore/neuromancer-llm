### ADR-0034 — Pin = promote-at-publication + manual `pin now`
**Status:** Accepted · **Source:** phase2 E10.
**Decision.** Default: promote tensors to cloud at **publication** time; the recompute recipe covers the loss window. Plus an explicit `neuro pin` CLI that uploads immediately when bytes are deemed irreplaceable. Budget stays lazy by default; quota guard sized for the lazy default.
**Consequences.** A desktop failure before publication/pin loses dense bytes → falls back to the recorded recompute recipe (artifact row survives with checksum+shape+recipe).
