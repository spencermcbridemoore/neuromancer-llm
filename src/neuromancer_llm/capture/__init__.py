"""The capture contract (phase3-capture-contract.md). STAGE 2 — the 'logprob capture done right' slice.

Every model interaction produces an immutable capture_events row with the TRUE wire payload (ADR-0003),
large bodies spilled to blob (ADR-0022). The Stage-1 scaffold proves the seam (bundles/) and queue
(workers/) this lane flows through; the capture logic itself lands in Stage 2.
"""

from __future__ import annotations
