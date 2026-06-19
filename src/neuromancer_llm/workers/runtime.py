"""The one worker runtime — lease/heartbeat/reaper policy owner (ADR-0039); checkpoint-first.

Stage 1 provides the lease/reaper *policy* (constants + reaper sweep), exercised by the golden harness.
The renewal thread and the job-execution run loop (which dispatches captured work) land in Stage 2 —
the runtime owns them so heartbeats are runtime-owned, not caller-owned.
"""

from __future__ import annotations

from ..db.repository import LEASE_SECONDS, REAPER_SECONDS, RENEW_SECONDS, Repository

__all__ = ["LEASE_SECONDS", "RENEW_SECONDS", "REAPER_SECONDS", "reap"]


def reap(repo: Repository) -> int:
    """Run one reaper sweep: requeue jobs whose lease expired (fencing the dead claimant). Returns the
    number of jobs requeued. The runtime calls this on the REAPER_SECONDS cadence."""
    return repo.reap_expired()
