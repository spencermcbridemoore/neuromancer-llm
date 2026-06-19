"""Run/job key grammar (ADR-0037): {campaign_key}/{work_slug}/{variant_digest}[/inv-{uuid8}].

work_slug carries human-readable domain coordinates by convention; variant_digest is the short
uniqueness suffix. The optional /inv-{uuid8} segment maps to the real column runs.invocation_id (uuid,
NULL for the canonical run); re-invocations (force-new-run, ADR-0005) coexist because
runs_experiment_variant_uq is NULLS NOT DISTINCT over (campaign, slug, digest, invocation_id).

Components are ALSO stored as real columns — the key is NEVER parsed to recover structure (KEEP rule).
On a run_key UNIQUE collision the composer regenerates fail-loud (note D1); it never silently reuses.
"""

from __future__ import annotations

import uuid


def new_invocation_id() -> uuid.UUID:
    """Mint an explicit re-invocation id (the force-new-run path). The canonical run uses None."""
    return uuid.uuid4()


def invocation_segment(invocation_id: uuid.UUID | None) -> str:
    return f"/inv-{invocation_id.hex[:8]}" if invocation_id is not None else ""


def compose_run_key(
    campaign_key: str, work_slug: str, variant_digest: str, *, invocation_id: uuid.UUID | None = None
) -> str:
    return f"{campaign_key}/{work_slug}/{variant_digest}{invocation_segment(invocation_id)}"


def compose_job_key(run_key: str, job_role: str, *, shard_key: str | None = None) -> str:
    """A job's display key derives from its run + role (+ shard). Components are stored as real columns
    (jobs.job_role / jobs.shard_key); this string is for logs/paths, never parsed."""
    suffix = f"/{shard_key}" if shard_key is not None else ""
    return f"{run_key}#{job_role}{suffix}"


class RunKeyCollision(RuntimeError):
    """Raised when a composed run_key collides on the UNIQUE — the composer regenerates fail-loud
    (note D1) rather than silently reusing an existing run."""
