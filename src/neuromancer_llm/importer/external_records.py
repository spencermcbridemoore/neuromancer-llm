"""Layer 1: faithful external-records mirror — byte-preserved payload_text + JSONB sidecar, idempotent
on (source_system, source_table, source_pk). study-query-llm keeps running untouched. STAGE 2.
"""

from __future__ import annotations
