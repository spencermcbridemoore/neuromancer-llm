"""Determinism: declared / expected / measured (ADR-0004) + the E6 bring-up test harness. STAGE 2.

DECLARED mode is derived from the captured wire payload (participates in the fingerprint); EXPECTED is
the heuristic table (never identity, overridable per-run); MEASURED is replicate divergence. The E6 gate
(N=50 logprob-array exact-match under VLLM_BATCH_INVARIANT=1 on the 4090) is a mandatory, decision-bearing
Phase-4 bring-up step — it decides the bitwise-vs-tolerance canonical default. NOT settled from memory.
"""

from __future__ import annotations
