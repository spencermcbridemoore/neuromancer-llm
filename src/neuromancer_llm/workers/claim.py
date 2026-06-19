"""SKIP-LOCKED claim (Postgres only) via the repository — the single claim path (ADR-0039).

Jobs with unmet dependencies are enqueued 'blocked' (set at enqueue — a composer/dispatch invariant,
NOT a trigger; note D2) and flip to 'queued' via CAS when their last dependency succeeds. The claim
predicate selects only 'queued', so nothing is claimable before its dependencies have succeeded (C2).
"""

from __future__ import annotations

from ..db.repository import Claim, Repository


def claim_one(
    repo: Repository, *, actor_id: int, queue: str = "default", gpu_class: str | None = None
) -> Claim | None:
    """Claim a single queued job for this worker, or return None if the queue offers nothing."""
    return repo.claim(actor_id=actor_id, queue=queue, gpu_class=gpu_class)
