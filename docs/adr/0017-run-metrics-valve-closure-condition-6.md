### ADR-0017 — run_metrics valve closure (condition 6)
**Status:** Accepted. **Decision.** `run_metrics.metric_key` is an FK to a registered `metric_keys` vocabulary; `CHECK (octet_length(value_json) <= 8192)`. **Consequences.** Closes the "metadata_json reborn" valve — run_metrics cannot become an unbounded JSON dumping ground. Per-run scalars only (no per-shard width; B posture).
