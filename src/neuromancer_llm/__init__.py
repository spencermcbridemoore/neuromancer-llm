"""neuromancer-llm — LLM experimentation platform with mechanistic-interpretability capture.

Thin Postgres control plane (identity core + queue + per-run scalars + manifest rows) over a
parquet lake + safetensors TTL dense lane. NO UI; the product is capture + provenance + export
discipline. Postgres-only (ADR-0039 Reconsidered 2026-06-17). See engagement/phase3/ for the
binding design of record.
"""

from __future__ import annotations

__version__ = "0.0.1"
