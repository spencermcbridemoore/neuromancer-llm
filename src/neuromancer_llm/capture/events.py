"""capture_events writer — 8 KB inline + independent request/response spill (ADR-0003/0022, A2). STAGE 2.

Writes the immutable capture row (verbatim wire bytes as TEXT, actor/origin stamp per RULING 1) on both
paths: GPU bundle lane (register at seal) and API lane (incremental per shard rotation, ADR-0021).
Flows through bundles/registrar.py (built + kill-tested in Stage 1).
"""

from __future__ import annotations
