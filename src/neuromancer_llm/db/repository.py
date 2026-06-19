"""THE one repository interface — Postgres-only (ADR-0039 Reconsidered 2026-06-17; no SQLite backend).

This is the single home for the queue's SQL (ADR-0039): SKIP-LOCKED claim, monotonic claim_seq +
claim_token fencing, every mutation CAS-guarded by rowcount, runtime-owned leases, dependency gating
(non-claimable 'blocked' state, C2), and recursive cascade-cancellation. The golden-snapshot harness
(Q15 KEEP) exercises this against a real Postgres fixture.

Stage 1 implements the queue + minimal seed helpers (what the golden harness needs). The registration
side of the repository (capture_events / bundles / artifacts) is exercised via bundles/registrar.py;
broader query surface lands with the workflows that consume it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, text

# Lease timing (ADR-0039): 120s lease / 40s renew / 60s reaper, all server-side now(). The renewal
# thread + reaper loop (the *policy*) are owned by workers/runtime.py; the lease INTERVAL is applied
# here because this is where the lease SQL lives.
LEASE_SECONDS = 120
RENEW_SECONDS = 40
REAPER_SECONDS = 60

# A job is claimable only when 'queued'. 'blocked' (unmet deps) and all in-flight/terminal states are
# excluded by construction — the claim predicate never selects them (C2).
CLAIMABLE_STATE = "queued"


@dataclass(frozen=True)
class Claim:
    job_id: int
    claim_token: uuid.UUID
    claim_seq: int


class Repository:
    def __init__(
        self, engine: Engine, *, expected_lane: str, expected_uuid: uuid.UUID | str | None = None
    ) -> None:
        # R1 (fail-closed write choke point): verify the target's identity ONCE at construction
        # (ADR-0006). No write path exists on an unverified target — a Repository cannot be built
        # against an unprovisioned or wrong-lane DB. Canonical-lane callers also pass expected_uuid.
        from .session import verify_engine

        self.engine = verify_engine(engine, expected_lane=expected_lane, expected_uuid=expected_uuid)

    # --- seed helpers (minimal; full registry write paths are registrar-side) -------------------
    def create_actor(self, actor_key: str, *, kind: str = "agent", display_name: str | None = None) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.actors (actor_key, kind, display_name) "
                    "VALUES (:k, :kind, :dn) RETURNING actor_id"
                ),
                {"k": actor_key, "kind": kind, "dn": display_name or actor_key},
            ).scalar_one()

    def create_campaign(self, campaign_key: str, actor_id: int) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.campaigns (campaign_key, actor_id) VALUES (:k, :a) RETURNING campaign_id"
                ),
                {"k": campaign_key, "a": actor_id},
            ).scalar_one()

    def create_run(
        self,
        run_key: str,
        *,
        campaign_id: int,
        work_slug: str,
        variant_digest: str,
        actor_id: int,
        origin: str = "test",
        run_kind: str = "experiment",
    ) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.runs (run_key, campaign_id, work_slug, variant_digest, run_kind, actor_id, origin) "
                    "VALUES (:rk, :c, :ws, :vd, :kind, :a, :o) RETURNING run_id"
                ),
                {
                    "rk": run_key,
                    "c": campaign_id,
                    "ws": work_slug,
                    "vd": variant_digest,
                    "kind": run_kind,
                    "a": actor_id,
                    "o": origin,
                },
            ).scalar_one()

    # --- enqueue (composer sets the component columns; deps -> 'blocked' at enqueue) -------------
    def enqueue(
        self,
        *,
        run_id: int,
        job_key: str,
        job_role: str | None = None,
        shard_key: str | None = None,
        queue: str = "default",
        gpu_class: str | None = None,
        vram_needed_mb: int | None = None,
        depends_on: Sequence[int] = (),
    ) -> int:
        # A job with unmet deps is enqueued 'blocked' (composer/dispatch invariant, NOT a trigger; note D2).
        state = "blocked" if depends_on else "queued"
        with self.engine.begin() as conn:
            job_id = conn.execute(
                text(
                    "INSERT INTO neuro.jobs (job_key, run_id, job_role, shard_key, queue, gpu_class, vram_needed_mb, state) "
                    "VALUES (:k, :r, :jr, :sk, :q, :gc, :v, :st) RETURNING job_id"
                ),
                {
                    "k": job_key,
                    "r": run_id,
                    "jr": job_role,
                    "sk": shard_key,
                    "q": queue,
                    "gc": gpu_class,
                    "v": vram_needed_mb,
                    "st": state,
                },
            ).scalar_one()
            for dep in depends_on:
                conn.execute(
                    text("INSERT INTO neuro.job_dependencies (job_id, depends_on) VALUES (:j, :d)"),
                    {"j": job_id, "d": dep},
                )
            return job_id

    def state_of(self, job_id: int) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT state FROM neuro.jobs WHERE job_id = :j"), {"j": job_id}
            ).scalar_one()

    def get_job(self, job_id: int) -> dict:
        with self.engine.connect() as conn:
            return dict(
                conn.execute(text("SELECT * FROM neuro.jobs WHERE job_id = :j"), {"j": job_id})
                .mappings()
                .one()
            )

    # --- claim: SKIP LOCKED + fencing + lease (one transaction) ---------------------------------
    def claim(self, *, actor_id: int, queue: str = "default", gpu_class: str | None = None) -> Claim | None:
        token = uuid.uuid4()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    UPDATE neuro.jobs
                       SET state = 'claimed', claim_token = :tok, claim_seq = claim_seq + 1,
                           claimed_by = :actor, updated_at = now()
                     WHERE job_id = (
                         SELECT job_id FROM neuro.jobs
                          WHERE state = 'queued' AND queue = :queue
                            AND (CAST(:gpu AS text) IS NULL OR gpu_class IS NOT DISTINCT FROM :gpu)
                          ORDER BY job_id
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1
                     )
                    RETURNING job_id, claim_token, claim_seq
                    """
                    ),
                    {"tok": token, "actor": actor_id, "queue": queue, "gpu": gpu_class},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            conn.execute(
                text(
                    "INSERT INTO neuro.work_leases (job_id, claim_token, leased_by, expires_at) "
                    "VALUES (:job, :tok, :actor, now() + make_interval(secs => :lease))"
                ),
                {"job": row["job_id"], "tok": row["claim_token"], "actor": actor_id, "lease": LEASE_SECONDS},
            )
            return Claim(job_id=row["job_id"], claim_token=row["claim_token"], claim_seq=row["claim_seq"])

    # --- CAS-guarded, ownership-checked mutations -----------------------------------------------
    def heartbeat(self, *, job_id: int, claim_token: uuid.UUID) -> bool:
        # R4: the join on jobs requires the job to still be in-flight, so a CANCELLED job's heartbeat
        # returns False (the worker learns it has been preempted). UPDATE..FROM locks only work_leases
        # (jobs is read, not locked) — no jobs/leases lock-order conflict with the reaper.
        with self.engine.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE neuro.work_leases l SET last_heartbeat = now(), "
                    "expires_at = now() + make_interval(secs => :lease) "
                    "FROM neuro.jobs j "
                    "WHERE l.job_id = :job AND l.claim_token = :tok AND l.released_at IS NULL "
                    "AND j.job_id = l.job_id AND j.state IN ('claimed', 'running')"
                ),
                {"job": job_id, "tok": claim_token, "lease": LEASE_SECONDS},
            )
            return res.rowcount == 1

    def checkpoint(self, *, job_id: int, claim_token: uuid.UUID, checkpoint_ref: str) -> bool:
        with self.engine.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE neuro.jobs SET state = 'running', checkpoint_ref = :ref, updated_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND state IN ('claimed', 'running')"
                ),
                {"job": job_id, "tok": claim_token, "ref": checkpoint_ref},
            )
            return res.rowcount == 1

    def complete(self, *, job_id: int, claim_token: uuid.UUID) -> bool:
        """Succeed a job (CAS + ownership). On success, flip ready dependents 'blocked' -> 'queued'."""
        with self.engine.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE neuro.jobs SET state = 'succeeded', updated_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND state IN ('claimed', 'running')"
                ),
                {"job": job_id, "tok": claim_token},
            )
            if res.rowcount != 1:
                return False  # wrong token / not in-flight / already stolen-and-fenced
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET released_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND released_at IS NULL"
                ),
                {"job": job_id, "tok": claim_token},
            )
            conn.execute(
                text(
                    """
                    UPDATE neuro.jobs j SET state = 'queued', updated_at = now()
                     WHERE j.state = 'blocked'
                       AND j.job_id IN (SELECT job_id FROM neuro.job_dependencies WHERE depends_on = :job)
                       AND NOT EXISTS (
                           SELECT 1 FROM neuro.job_dependencies d
                             JOIN neuro.jobs dep ON dep.job_id = d.depends_on
                            WHERE d.job_id = j.job_id AND dep.state <> 'succeeded')
                    """
                ),
                {"job": job_id},
            )
            return True

    def fail_permanent(self, *, job_id: int, claim_token: uuid.UUID, detail: str) -> bool:
        """Dead-letter a permanent failure (CAS + ownership), then cascade-cancel its dependents.

        DEFER (Stage 2): there is no TRANSIENT-`failed` producer yet. When the Stage-2 failure path lands,
        define a transient failure's effect on dependents (retry vs cascade) and have the reaper stamp
        error_class='lease_expired' on expiry. Note only — not built now."""
        with self.engine.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE neuro.jobs SET state = 'dead_letter', error_class = 'permanent', "
                    "error_detail = :d, attempt_count = attempt_count + 1, updated_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND state IN ('claimed', 'running')"
                ),
                {"job": job_id, "tok": claim_token, "d": detail},
            )
            if res.rowcount != 1:
                return False
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET released_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND released_at IS NULL"
                ),
                {"job": job_id, "tok": claim_token},
            )
            self._cascade_cancel(conn, job_id)
            return True

    def cancel_cascade(self, job_id: int) -> int:
        with self.engine.begin() as conn:
            return self._cascade_cancel(conn, job_id, include_self=True)

    @staticmethod
    def _cascade_cancel(conn, job_id: int, *, include_self: bool = False) -> int:
        # Recursive cascade over the dependency edge set: cancel every transitive dependent (and,
        # if include_self, the job itself) that is not already in a terminal state.
        # CAST(:job AS bigint), not :job::bigint — SQLAlchemy text() mis-parses a named param adjacent
        # to the :: cast operator (the param goes unbound). CAST(...) keeps the colon unambiguous.
        seed = (
            "CAST(:job AS bigint)"
            if include_self
            else ("job_id FROM neuro.job_dependencies WHERE depends_on = :job")
        )
        # jobs FIRST (consistent jobs-before-leases lock order, R5).
        sql = (
            "WITH RECURSIVE deps AS ("
            + (f"  SELECT {seed} AS job_id" if include_self else f"  SELECT {seed}")
            + "  UNION "
            "  SELECT d.job_id FROM neuro.job_dependencies d JOIN deps ON d.depends_on = deps.job_id"
            ") "
            "UPDATE neuro.jobs SET state = 'cancelled', updated_at = now() "
            "WHERE job_id IN (SELECT job_id FROM deps) "
            "  AND state NOT IN ('succeeded', 'cancelled', 'dead_letter') "
            "RETURNING job_id"
        )
        cancelled = [r[0] for r in conn.execute(text(sql), {"job": job_id}).all()]
        # R4: release any active lease the cancelled (possibly in-flight) jobs held — otherwise the
        # cancelled worker heartbeats forever and the lease never frees. Leases AFTER jobs.
        if cancelled:
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET released_at = now() "
                    "WHERE job_id = ANY(:ids) AND released_at IS NULL"
                ),
                {"ids": cancelled},
            )
        return len(cancelled)

    # --- reaper: requeue expired leases, fencing the original claimant --------------------------
    def reap_expired(self) -> int:
        """Requeue jobs whose lease expired. Clearing claim_token + bumping claim_seq fences the
        original (now-dead) claimant: its stale token no longer matches, so its complete() CAS fails.

        R5 (deadlock-free): jobs-before-leases lock order — drive the requeue off a `jobs` UPDATE whose
        EXISTS subquery finds the expired, unreleased lease, THEN release those leases. complete() /
        fail_permanent() / _cascade_cancel() all lock jobs before leases too, so there is no lock cycle."""
        with self.engine.begin() as conn:
            requeued = (
                conn.execute(
                    text(
                        "UPDATE neuro.jobs j SET state = 'queued', claim_token = NULL, claimed_by = NULL, "
                        "claim_seq = claim_seq + 1, expiry_count = expiry_count + 1, updated_at = now() "
                        "WHERE j.state IN ('claimed', 'running') AND EXISTS ("
                        "  SELECT 1 FROM neuro.work_leases l "
                        "  WHERE l.job_id = j.job_id AND l.released_at IS NULL AND l.expires_at < now()) "
                        "RETURNING j.job_id"
                    )
                )
                .scalars()
                .all()
            )
            if requeued:
                conn.execute(
                    text(
                        "UPDATE neuro.work_leases SET released_at = now() "
                        "WHERE job_id = ANY(:ids) AND released_at IS NULL AND expires_at < now()"
                    ),
                    {"ids": list(requeued)},
                )
            return len(requeued)

    def expire_lease_now(self, job_id: int) -> None:
        """Test hook: force a lease to be already-expired so the reaper picks it up deterministically
        (the golden harness drives the state machine without real-time waits)."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET expires_at = now() - make_interval(secs => 1) "
                    "WHERE job_id = :job AND released_at IS NULL"
                ),
                {"job": job_id},
            )
