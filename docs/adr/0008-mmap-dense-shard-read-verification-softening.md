### ADR-0008 — mmap dense-shard read-verification softening
**Status:** Signed-deviation (ADR-1) · **Source:** phase2 ADR-1 "Sign as written".
**Decision.** Dense safetensors shards are verified by **post-transfer hash + first-read-per-host + monthly mirror audit**, NOT hash-on-every-open. Per-open hashing would forfeit mmap random access on the dense lane.
**Consequences.** Residual = corruption between audits on an already-verified host; bounded by monthly cadence and the lane being TTL/recomputable. This is a sanctioned deviation from the "sha256 verified on read" binding, scoped to the dense mmap lane only. No tightening (e.g. weekly) was taken.
