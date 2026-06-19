"""Golden-snapshot + concurrency harness for the one queue (Q15 KEEP, ADR-0039), on real Postgres.

Covers the full state machine: plan -> claim (SKIP LOCKED) -> checkpoint -> STEAL-ATTEMPT ->
complete / CAS-fail -> reaper requeue + claim_token fencing -> dependency gating -> cascade-cancel.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from neuromancer_llm.workers.claim import claim_one
from neuromancer_llm.workers.runtime import reap

pytestmark = pytest.mark.pg


def test_claim_single_winner_and_steal_attempt(seeded):
    """Two workers race for one job: exactly one wins (SKIP LOCKED); the loser gets nothing and
    cannot steal it (CAS rejects a complete with a bogus token)."""
    repo, run_id, w1 = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    w2 = repo.create_actor("worker:test2", kind="scheduled_worker")
    job = repo.enqueue(run_id=run_id, job_key="k#capture", job_role="capture")

    barrier = threading.Barrier(2)

    def attempt(actor_id):
        barrier.wait()  # maximize contention on the single queued row
        return claim_one(repo, actor_id=actor_id)

    with ThreadPoolExecutor(max_workers=2) as ex:
        r1 = ex.submit(attempt, w1)
        r2 = ex.submit(attempt, w2)
        results = [r1.result(), r2.result()]

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, "exactly one worker may win the job (no double-claim)"
    assert len(losers) == 1, "the losing worker must get nothing"
    assert repo.state_of(job) == "claimed"

    # steal attempt: complete with a bogus token -> CAS reject, state unchanged
    assert repo.complete(job_id=job, claim_token=uuid.uuid4()) is False
    assert repo.state_of(job) == "claimed"

    # the legitimate winner completes
    assert repo.complete(job_id=job, claim_token=winners[0].claim_token) is True
    assert repo.state_of(job) == "succeeded"


def test_checkpoint_then_complete(seeded):
    """checkpoint-first: a claimed job records a resume pointer and goes 'running' before completing."""
    repo, run_id, w1 = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#sharded", job_role="capture", shard_key="shard-3")
    claim = repo.claim(actor_id=w1)
    assert claim is not None and claim.job_id == job

    assert repo.checkpoint(job_id=job, claim_token=claim.claim_token, checkpoint_ref="offset=4096") is True
    row = repo.get_job(job)
    assert row["state"] == "running"
    assert row["checkpoint_ref"] == "offset=4096"

    # a wrong-token checkpoint is rejected (CAS + ownership)
    assert repo.checkpoint(job_id=job, claim_token=uuid.uuid4(), checkpoint_ref="evil") is False
    assert repo.complete(job_id=job, claim_token=claim.claim_token) is True


def test_dependency_gating(seeded):
    """A blocked job is never claimable until its last dependency succeeds (C2)."""
    repo, run_id, w1 = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    a = repo.enqueue(run_id=run_id, job_key="k#a", job_role="a")
    b = repo.enqueue(run_id=run_id, job_key="k#b", job_role="b", depends_on=[a])
    assert repo.state_of(a) == "queued"
    assert repo.state_of(b) == "blocked"

    claim_a = repo.claim(actor_id=w1)
    assert claim_a is not None and claim_a.job_id == a, "must claim A, never the blocked B"
    assert repo.claim(actor_id=w1) is None, "B is blocked; nothing else is claimable"

    assert repo.complete(job_id=a, claim_token=claim_a.claim_token) is True
    assert repo.state_of(b) == "queued", "B unblocks on its last dependency succeeding"

    claim_b = repo.claim(actor_id=w1)
    assert claim_b is not None and claim_b.job_id == b


def test_reaper_requeue_and_token_fencing(seeded):
    """An expired lease is requeued by the reaper; the dead claimant is fenced (its stale token fails)."""
    repo, run_id, w1 = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#long", job_role="capture")
    claim1 = repo.claim(actor_id=w1)
    assert claim1 is not None

    repo.expire_lease_now(job)  # deterministic: force the lease already-expired
    assert reap(repo) == 1
    assert repo.state_of(job) == "queued"
    assert repo.get_job(job)["expiry_count"] == 1

    # the original worker is fenced — its now-stale token no longer matches (token cleared on requeue)
    assert repo.complete(job_id=job, claim_token=claim1.claim_token) is False

    # a fresh claim gets a NEW token and a bumped claim_seq, and can complete
    claim2 = repo.claim(actor_id=w1)
    assert claim2 is not None
    assert claim2.claim_token != claim1.claim_token
    assert claim2.claim_seq > claim1.claim_seq
    assert repo.complete(job_id=job, claim_token=claim2.claim_token) is True


def test_cascade_cancel_on_permanent_failure(seeded):
    """A permanent failure dead-letters the job and cascade-cancels all transitive dependents."""
    repo, run_id, w1 = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    a = repo.enqueue(run_id=run_id, job_key="k#a", job_role="a")
    b = repo.enqueue(run_id=run_id, job_key="k#b", job_role="b", depends_on=[a])
    c = repo.enqueue(run_id=run_id, job_key="k#c", job_role="c", depends_on=[b])

    claim_a = repo.claim(actor_id=w1)
    assert claim_a is not None and claim_a.job_id == a
    assert repo.fail_permanent(job_id=a, claim_token=claim_a.claim_token, detail="boom") is True

    assert repo.state_of(a) == "dead_letter"
    assert repo.state_of(b) == "cancelled", "direct dependent cancelled"
    assert repo.state_of(c) == "cancelled", "transitive dependent cancelled"


def test_cancel_releases_lease_and_blocks_heartbeat(seeded):
    """R4: cancelling an in-flight job releases its lease, and the worker's next heartbeat returns False."""
    repo, run_id, w1 = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#claimed", job_role="capture")
    claim = repo.claim(actor_id=w1)
    assert claim is not None
    assert repo.heartbeat(job_id=job, claim_token=claim.claim_token) is True  # in-flight: heartbeat ok

    assert repo.cancel_cascade(job) == 1
    assert repo.state_of(job) == "cancelled"

    # the lease was released in the same txn, and a heartbeat now fails (the worker learns it was preempted)
    assert repo.heartbeat(job_id=job, claim_token=claim.claim_token) is False
    with repo.engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM neuro.work_leases WHERE job_id = :j AND released_at IS NULL"),
            {"j": job},
        ).scalar_one()
    assert active == 0


def test_and_gate_two_dependencies(seeded):
    """A job with TWO parents unblocks only when BOTH succeed (AND-gate, not OR)."""
    repo, run_id, w1 = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    a = repo.enqueue(run_id=run_id, job_key="k#pa", job_role="pa")
    b = repo.enqueue(run_id=run_id, job_key="k#pb", job_role="pb")
    child = repo.enqueue(run_id=run_id, job_key="k#child", job_role="child", depends_on=[a, b])
    assert repo.state_of(child) == "blocked"

    claim_a = repo.claim(actor_id=w1)  # claims A (lowest queued id)
    assert claim_a is not None and claim_a.job_id == a
    assert repo.complete(job_id=a, claim_token=claim_a.claim_token) is True
    assert repo.state_of(child) == "blocked", "one parent done is not enough"

    claim_b = repo.claim(actor_id=w1)
    assert claim_b is not None and claim_b.job_id == b
    assert repo.complete(job_id=b, claim_token=claim_b.claim_token) is True
    assert repo.state_of(child) == "queued", "both parents done -> unblocked"
