"""Red-team: the one queue / lease / heartbeat / reaper (L2, L3) + FIX #6 + C20.

The owner's DECISION OF RECORD (2026-06-23): the queue mutation paths KEEP the quiet boolean — claim/
complete/checkpoint/heartbeat/fail_permanent do NOT raise on a fenced/stolen op (correct CAS-queue
design). So these probes assert (a) the fenced-rejection bool contract, (b) the counters increment, and
(c) the FIX #6 / C20 behaviours; they never demand a typed exception from the queue.

  * FIX #6 — heartbeat refuses to renew an ALREADY-EXPIRED lease + the reaper requeue/release is atomic,
    so a dead worker's heartbeat can no longer wedge a job (queued + active lease -> un-claimable).
  * C20 — enqueue computes the initial state from the deps' REAL state (all succeeded -> queued; a
    terminal-failed dep -> cancelled), not "has deps => blocked forever".
  * L2 / L3 GAP — shard idempotency, counter increments, the active-lease partial unique, lease release.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from neuromancer_llm.workers.claim import claim_one
from neuromancer_llm.workers.runtime import reap

pytestmark = pytest.mark.pg


# --- FIX #6: reaper-vs-heartbeat wedge -------------------------------------------------------------
def test_rt_heartbeat_refuses_expired_lease(seeded):
    """FIX #6 (a): a worker cannot RENEW an already-expired lease — the reaper owns expired leases. Renewing
    one was the wedge trigger (heartbeat renews while the reaper requeues -> queued job + active lease)."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#hb", job_role="capture")
    claim = repo.claim(actor_id=w)
    assert claim is not None
    assert repo.heartbeat(job_id=job, claim_token=claim.claim_token) is True  # in-flight: ok
    repo.expire_lease_now(job)
    assert repo.heartbeat(job_id=job, claim_token=claim.claim_token) is False  # expired: refused (FIX #6)


def test_rt_dead_worker_heartbeat_then_reap_no_wedge(seeded):
    """FIX #6 end-to-end: even if a dead worker heartbeats its expired lease, the reaper still requeues the
    job and it is re-claimable (no wedge: no leftover active lease violating work_leases_active_uq)."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#wedge", job_role="capture")
    claim1 = repo.claim(actor_id=w)
    assert claim1 is not None
    repo.expire_lease_now(job)
    repo.heartbeat(job_id=job, claim_token=claim1.claim_token)  # the dead worker's heartbeat (False now)
    assert reap(repo) == 1  # the reaper requeues it (the expired lease was NOT rescued by the heartbeat)
    assert repo.state_of(job) == "queued"
    claim2 = repo.claim(actor_id=w)  # re-claimable: no wedge
    assert claim2 is not None and claim2.job_id == job
    assert repo.complete(job_id=job, claim_token=claim2.claim_token) is True
    with repo.engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM neuro.work_leases WHERE job_id = :j AND released_at IS NULL"),
            {"j": job},
        ).scalar_one()
    assert active == 0


# --- C20: enqueue-after-success / enqueue-after-failure --------------------------------------------
def test_rt_enqueue_after_dep_succeeded_is_queued(seeded):
    """C20: a job enqueued AFTER its dependency already SUCCEEDED is 'queued' (claimable), not stuck
    'blocked' forever (the unblock only fired inside complete(), which already ran)."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    a = repo.enqueue(run_id=run_id, job_key="k#a", job_role="a")
    claim_a = repo.claim(actor_id=w)
    assert repo.complete(job_id=a, claim_token=claim_a.claim_token) is True
    b = repo.enqueue(run_id=run_id, job_key="k#b", job_role="b", depends_on=[a])
    assert repo.state_of(b) == "queued"  # C20: initial state from the dep's REAL (succeeded) state
    claim_b = claim_one(repo, actor_id=w)
    assert claim_b is not None and claim_b.job_id == b


def test_rt_enqueue_partial_deps_still_blocked(seeded):
    """C20 must NOT over-eagerly queue: a job with one succeeded + one not-yet-succeeded dependency stays
    'blocked' (the AND-gate). Guards the fix against a least-privilege->no-privilege style regression."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    a = repo.enqueue(run_id=run_id, job_key="k#pa", job_role="pa")
    claim_a = repo.claim(actor_id=w)
    assert repo.complete(job_id=a, claim_token=claim_a.claim_token) is True
    b = repo.enqueue(run_id=run_id, job_key="k#pb", job_role="pb")  # queued, NOT succeeded
    c = repo.enqueue(run_id=run_id, job_key="k#pc", job_role="pc", depends_on=[a, b])
    assert repo.state_of(c) == "blocked"  # one dep unfinished -> blocked


def test_rt_enqueue_after_dep_failed_is_cancelled(seeded):
    """C20: a job enqueued against a TERMINAL-FAILED dependency can never run — it is cancelled, never left
    'blocked' forever (the unblock predicate requires all deps 'succeeded', which a dead-lettered dep never
    becomes)."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    a = repo.enqueue(run_id=run_id, job_key="k#fa", job_role="fa")
    claim_a = repo.claim(actor_id=w)
    assert repo.fail_permanent(job_id=a, claim_token=claim_a.claim_token, detail="boom") is True
    assert repo.state_of(a) == "dead_letter"
    b = repo.enqueue(run_id=run_id, job_key="k#fb", job_role="fb", depends_on=[a])
    assert repo.state_of(b) == "cancelled"  # C20: a terminal-failed dep -> the dependent is cancelled


# --- L2 GAP: shard idempotency + counter increments ------------------------------------------------
def test_rt_shard_double_enqueue_is_integrity_error(seeded):
    """L2 GAP: enqueuing the SAME (run_id, job_role, shard_key) twice is a double-work vector — the
    jobs_shard_idempotency_uq (NULLS NOT DISTINCT) rejects it with an IntegrityError."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    repo.enqueue(run_id=run_id, job_key="k#s1", job_role="capture", shard_key="shard-3")
    with pytest.raises(IntegrityError):
        repo.enqueue(run_id=run_id, job_key="k#s2", job_role="capture", shard_key="shard-3")


def test_rt_queue_counters_increment(seeded):
    """L2 GAP (anti-blind-detector): each queue event bumps its counter — a counter stuck at zero makes the
    (deferred) health probe a blind symptom-detector. reap -> expiry_count+1; fail_permanent -> attempt_count+1."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#cnt", job_role="capture")
    repo.claim(actor_id=w)
    repo.expire_lease_now(job)
    assert reap(repo) == 1
    assert repo.get_job(job)["expiry_count"] == 1  # expiry counter bumped on requeue
    repo.cancel_cascade(job)  # drain the requeued job so the next claim targets the attempt-counter job

    job2 = repo.enqueue(run_id=run_id, job_key="k#cnt2", job_role="capture2")
    c2 = repo.claim(actor_id=w)
    assert c2 is not None and c2.job_id == job2
    assert repo.fail_permanent(job_id=job2, claim_token=c2.claim_token, detail="x") is True
    assert repo.get_job(job2)["attempt_count"] == 1  # attempt counter bumped on the permanent failure


def test_rt_fenced_complete_is_quiet_false_state_unchanged(seeded):
    """L2 (owner decision of record): a fenced/stolen op returns the QUIET boolean (never raises) AND leaves
    state unchanged — a real caller acts on the False (it does not ignore it)."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#fence", job_role="capture")
    repo.claim(actor_id=w)
    assert repo.complete(job_id=job, claim_token=uuid.uuid4()) is False  # bogus token -> quiet False
    assert repo.state_of(job) == "claimed"  # state unchanged (the write did not happen)


# --- L3 GAP: the active-lease partial unique + lease release ---------------------------------------
def test_rt_double_active_lease_is_integrity_error(seeded):
    """L3 GAP: work_leases_active_uq (partial unique on the unreleased lease) is the structural backstop
    against double-occupancy — a second active lease for one job_id is rejected, only asserted indirectly
    until now."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#lease", job_role="capture")
    claim = repo.claim(actor_id=w)
    with pytest.raises(IntegrityError), repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.work_leases (job_id, claim_token, leased_by, expires_at) "
                "VALUES (:j, :t, :a, now() + make_interval(secs => 60))"
            ),
            {"j": job, "t": uuid.uuid4(), "a": w},
        )
    assert claim is not None  # the original lease is the only active one


def test_rt_fail_permanent_releases_lease(seeded):
    """L3 GAP: a dead-lettered job's lease is actually released (the cascade test checked STATE only)."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#dl", job_role="capture")
    claim = repo.claim(actor_id=w)
    assert repo.fail_permanent(job_id=job, claim_token=claim.claim_token, detail="x") is True
    with repo.engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM neuro.work_leases WHERE job_id = :j AND released_at IS NULL"),
            {"j": job},
        ).scalar_one()
    assert active == 0  # the lease was released in the same txn
