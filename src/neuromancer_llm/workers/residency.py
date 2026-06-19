"""Residency: a hash-addressed SET of model + asset members budgeted against VRAM (Finding 1). STAGE 2.

Resolves jobs.residency_set_id (residency_sets / residency_set_members). At most one base model per set
(DB-enforced, B6); the adapters are the asset members. run_inputs no longer carries residency.
"""

from __future__ import annotations
