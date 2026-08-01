"""the worker/registrar role split: SECURITY DEFINER queue functions + the two column-immutability arms (ADR-0046 C3).

THE THIRD SCHEMA CHANGE SINCE 0001 (owner-nodded 2026-08-01). It CLOSES the ADR-0046 bundle by removing the
residual `0003` deliberately left open and pinned as a positive probe: a role holding `GRANT UPDATE (state, …)
ON jobs` can WALK the state ladder one statement at a time (`queued -> claimed -> succeeded` is two
individually-legal hops) and mark a job done with no token, no lease and no work. `0003`'s own docstring says
why no trigger predicate can close it — every column such a predicate could test is itself granted to the
writer and forgeable in the same transaction — and names the closure: the ROLE SPLIT.

  THE MECHANISM: revoke the walking kit from `neuro_writer` (db/sql/grants.sql, a PROVISIONING file) and route
  the CAS + lease through SECURITY DEFINER functions that do both ATOMICALLY. The SQL MOVES here; the
  Repository methods become CALLERS. A parallel Python copy of the same statements would be the drift bug this
  house exists to kill, so there is exactly one implementation of each statement and it is the one below.

★ THE CLOSURE IS ROLE-SCOPED, AND EVERY ARTIFACT IN THIS UNIT SAYS SO. `neuro_admin` retains raw power BY
NATURE, so as admin the ladder stays walkable — `test_rt_ladder_is_walkable_statement_by_statement` keeps
asserting exactly that, re-scoped and re-documented rather than deleted. And even for `neuro_writer` the
closure is narrower than "a worker cannot mark a job succeeded with no work": `claim_job` followed by
`complete_job` reaches that same outcome in two GRANTED calls. What the split removes is the SILENT path — a
claim now bumps `claim_seq` and stamps `claimed_by`, so the act is ATTRIBUTED and AUDITABLE instead of being a
bare UPDATE by nobody. That surviving path is pinned as its own POSITIVE probe
(`test_rt_writer_can_still_claim_and_complete_without_work`) for the same reason 0003 pinned its residual: so
the guarantee can never be silently over-claimed by a later reader.

WHAT LANDS
  1. neuro.claim_job              SECURITY DEFINER  — the SKIP LOCKED claim + the lease INSERT, atomically
  2. neuro.renew_lease            SECURITY DEFINER  — the #6a-guarded heartbeat
  3. neuro.checkpoint_job         SECURITY DEFINER  — the A4 resume pointer
  4. neuro.complete_job           SECURITY DEFINER  — CAS + lease release + the wedge-2 dependent unblock
  5. neuro.fail_job_permanent     SECURITY DEFINER  — CAS + lease release + the cascade
  6. neuro.unblock_ready_dependents  SECURITY INVOKER (internal; the writer gets no EXECUTE)
  7. neuro.cascade_cancel_jobs       SECURITY INVOKER (internal; also called by Repository.cancel_cascade)
  8. neuro.assert_job_claim_fencing  + TRIGGER jobs_claim_fencing
     TRIGGER bundles_manifest_assign_once -> REUSES the parameterized neuro.assert_assign_once from 0001
     CREATE OR REPLACE of neuro.assert_bundle_insert_state (0003's) to also bar an INSERT-time manifest
Adds NO table, NO enum, NO column. `tables == 45` / `enums == 13` are unchanged and asserted.

★ THE MEASUREMENTS THIS MIGRATION IS BUILT ON (postgres 18.4, first-hand, 2026-08-01 — this repo does not take
a documented behaviour's word for a security boundary; C1 measured lock strength, C2 measured guard shape):
  * SECURITY DEFINER does NOT bypass triggers: an illegal `claimed -> blocked` inside a DEFINER function still
    raises 0003's `illegal job_state transition`. The ladders still bind under the owner.
  * A VOLATILE plpgsql function takes a FRESH SNAPSHOT PER STATEMENT (two reads across a concurrent commit
    inside one call returned 0 then 1); the SAME function marked STABLE returned 0 then 0. ⇒ the VOLATILE
    marking below is LOAD-BEARING — marking these STABLE silently restores the wedge-2 write-skew — and it is
    pinned by `provolatile` rather than left to a comment.
  * `SET search_path = pg_catalog, pg_temp` resolves everything these bodies use with `neuro` OFF the path:
    the enum literals (`state = 'queued'` against neuro.job_state), now(), make_interval, gen_random_uuid, and
    the uuid/bigint/bytea types. pg_temp is named LAST **explicitly** — omitted, it is searched FIRST and a
    temp object can shadow a catalog one.
  * `FOUND` is NOT `rowcount == 1`: it is true for ANY n >= 1 (measured 0/false and 1/true). Every CAS below
    therefore uses `GET DIAGNOSTICS ... ROW_COUNT` and compares to 1, exactly as the Python `res.rowcount == 1`
    did — `FOUND` would silently accept a multi-row hit.
  * `ALTER FUNCTION ... OWNER TO neuro_admin` (done in provisioning) PRESERVES the `REVOKE ... FROM PUBLIC`
    below and any GRANT EXECUTE: the ACL keeps its entries and only rewrites the grantor.

⚠ THE OWNER IS SET IN PROVISIONING, NOT HERE, AND THAT IS DELIBERATE. A migration must not reference the
provisioned roles (db/sql/grants.sql's own Finding-4 header: the schema is standalone-executable on an empty DB
with no roles). So this file does the role-INDEPENDENT half — `REVOKE ALL ... FROM PUBLIC`, which names only
the always-present pseudo-role — and `grants.sql` does `ALTER FUNCTION ... OWNER TO neuro_admin` plus the
`GRANT EXECUTE ... TO neuro_writer`. Between `alembic upgrade` and `neuro db roles` these functions are owned
by whoever ran the migration (`neuro_boot`, a SUPERUSER on canonical); the REVOKE FROM PUBLIC is what makes
that window safe, and the canonical runbook orders the two steps and verifies the owner afterwards.
⚠ MEASURED consequence for that ordering: `grants.sql` is applied as ONE statement batch
(db/provision.py::apply_grants), and a `GRANT EXECUTE` on a function that does not exist yet raises
`UndefinedFunction` and aborts the ENTIRE grants contract. `alembic upgrade` MUST precede `neuro db roles`.

Parity note: tests/reference/phase3-ddl.sql is the frozen 0001 schema and test_migration_ddl_parity upgrades
only to 0001_initial_schema, so this migration is INVISIBLE to the parity gate and the frozen reference DDL is
deliberately left UNTOUCHED. Consequence, now doubled from 0003's: anything built FROM that frozen file —
notably the A2-9 restore-drill fixture `scratch_db` (tests/conftest.py) — runs WITHOUT these functions AND
without the two new triggers, BY DESIGN.

Rollback: `alembic downgrade 0003_state_transition_triggers` drops the two triggers, then the eight functions,
then restores `assert_bundle_insert_state` to its 0003 body. It touches no table, no column and no row.
⚠ DATA-lossless is not AVAILABILITY-free, and C2's measured lesson STILL GOVERNS: `DROP TRIGGER` takes an
AccessExclusiveLock that a plain SELECT blocks, so a single open read transaction invisible to the apply will
block this downgrade. (MEASURED 2026-08-01 that `DROP FUNCTION` does NOT block on a table reader — it locks
pg_proc, not the table — so it is the two DROP TRIGGERs that carry the risk, not the eight DROP FUNCTIONs.)
Run it with a bounded `lock_timeout` and drained readers; see the canonical runbook.

Revision ID: 0004_worker_registrar_role_split
Revises: 0003_state_transition_triggers
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_worker_registrar_role_split"
down_revision: str | None = "0003_state_transition_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The five callable DEFINER functions + the two INVOKER internals, by EXACT signature. Used for the
# REVOKE loop below and, verbatim, for the downgrade's DROPs — a `DROP FUNCTION IF EXISTS` with a WRONG
# signature is a SILENT no-op, and test_rt_migrations asserts ZERO surviving neuro.* functions after a
# full downgrade, so one typo here is a live gate failure rather than a cosmetic one.
_CALLABLE_SIGNATURES: tuple[str, ...] = (
    "neuro.claim_job(bigint, text, text, integer)",
    "neuro.renew_lease(bigint, uuid, integer)",
    "neuro.checkpoint_job(bigint, uuid, text)",
    "neuro.complete_job(bigint, uuid)",
    "neuro.fail_job_permanent(bigint, uuid, text)",
    "neuro.unblock_ready_dependents(bigint)",
    "neuro.cascade_cancel_jobs(bigint, boolean)",
)


def upgrade() -> None:
    # NOTE: the SQL below is intentionally flush-left, matching 0001's assert_assign_once and 0003's idiom
    # (pg_get_functiondef preserves body whitespace, so a stored body is easiest to read back when it is
    # not re-indented here).

    # ---------------------------------------------------------------------------------------------
    # 6. unblock_ready_dependents — the wedge-2 fix, moved VERBATIM (SECURITY INVOKER: it is only ever
    #    reached from inside complete_job, which is already running as the owner).
    #
    # BOTH PROPERTIES REMAIN LOAD-BEARING AND ARE UNCHANGED BY THE MOVE:
    #   1. The lock is taken on the CANDIDATE set, never the READY set — the locking SELECT carries NO
    #      readiness predicate. Move the NOT-EXISTS into it and the losing parent selects zero rows, locks
    #      NOTHING, and the write-skew survives with a FOR NO KEY UPDATE sitting right there looking like
    #      a fix.
    #   2. The lock and the unblock stay SEPARATE statements. Fusing them into one data-modifying CTE puts
    #      both under ONE snapshot and the skew returns. ⇒ THIS IS WHY THE FUNCTION MUST STAY VOLATILE:
    #      measured, a VOLATILE plpgsql function takes a fresh snapshot per statement and a STABLE one does
    #      not, so `ALTER FUNCTION ... STABLE` here would re-open ADR-0046 §6 with the source unchanged.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.unblock_ready_dependents(p_job_id bigint) RETURNS void AS $$
BEGIN
    PERFORM j.job_id FROM neuro.jobs j
      WHERE j.state = 'blocked'
        AND j.job_id IN (SELECT job_id FROM neuro.job_dependencies WHERE depends_on = p_job_id)
      ORDER BY j.job_id FOR NO KEY UPDATE;

    UPDATE neuro.jobs j SET state = 'queued', updated_at = now()
     WHERE j.state = 'blocked'
       AND j.job_id IN (SELECT job_id FROM neuro.job_dependencies WHERE depends_on = p_job_id)
       AND NOT EXISTS (
           SELECT 1 FROM neuro.job_dependencies d
             JOIN neuro.jobs dep ON dep.job_id = d.depends_on
            WHERE d.job_id = j.job_id AND dep.state <> 'succeeded');
END;
$$ LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # 7. cascade_cancel_jobs — the recursive cascade, moved.
    #
    # ⚠ THE SEED IS A UNION SUBQUERY, NOT A COLLAPSED PREDICATE, AND THE DIFFERENCE IS BEHAVIOURAL. The
    # Python built two different SQL strings by concatenation: include_self seeded {p_job_id}, otherwise it
    # seeded {direct dependents of p_job_id}. The tempting collapse — seed always from {p_job_id} and filter
    # `job_id <> p_job_id` at the end — is NOT equivalent on a CYCLIC dependency edge set: with a cycle back
    # to the seed, the shipped include_self=false branch REACHES and cancels p_job_id through the cycle,
    # while the filtered form would always exclude it. Nothing in the schema forbids a cycle
    # (job_dependencies has no such constraint), so the two seeds are preserved exactly, as ONE
    # non-recursive term built by UNION ALL inside a subquery.
    #
    # The ascending pre-lock is the ADR-0046 C1 fix and is preserved: an UPDATE cannot carry an ORDER BY, so
    # the shipped single statement acquired its row locks in PLAN order — the one jobs-row acquirer with no
    # deterministic order — which closed a real jobs<->jobs cycle against enqueue's ascending dep lock,
    # REPRODUCED as a DeadlockDetected in the C1 pre-build vet. Locking ascending first makes every jobs-row
    # acquirer in the system ascending, so no cycle can form.
    #
    # The locked set is aggregated ORDER BY job_id from the locking statement itself, so the array handed to
    # the UPDATE preserves the acquisition order rather than re-deriving it.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.cascade_cancel_jobs(p_job_id bigint, p_include_self boolean)
RETURNS integer AS $$
DECLARE
    v_locked    bigint[];
    v_cancelled bigint[];
BEGIN
    WITH RECURSIVE deps AS (
        SELECT s.job_id FROM (
            SELECT p_job_id AS job_id WHERE p_include_self
            UNION ALL
            SELECT jd.job_id FROM neuro.job_dependencies jd
             WHERE jd.depends_on = p_job_id AND NOT p_include_self
        ) s
        UNION
        SELECT d.job_id FROM neuro.job_dependencies d JOIN deps ON d.depends_on = deps.job_id
    )
    SELECT coalesce(array_agg(t.job_id ORDER BY t.job_id), '{}'::bigint[]) INTO v_locked
      FROM (SELECT j.job_id FROM neuro.jobs j
             WHERE j.job_id IN (SELECT job_id FROM deps)
               AND j.state NOT IN ('succeeded', 'cancelled', 'dead_letter')
             ORDER BY j.job_id FOR NO KEY UPDATE) t;

    IF cardinality(v_locked) = 0 THEN
        RETURN 0;
    END IF;

    -- The state predicate is re-asserted as a belt: the rows are already locked, so it can only be a
    -- no-op here — but a locked-set UPDATE with no qual would silently widen if the lock ever moved.
    WITH upd AS (
        UPDATE neuro.jobs SET state = 'cancelled', updated_at = now()
         WHERE job_id = ANY(v_locked)
           AND state NOT IN ('succeeded', 'cancelled', 'dead_letter')
        RETURNING job_id
    )
    SELECT coalesce(array_agg(job_id), '{}'::bigint[]) INTO v_cancelled FROM upd;

    -- R4: release any active lease the cancelled (possibly in-flight) jobs held — otherwise the cancelled
    -- worker heartbeats forever and the lease never frees. Leases AFTER jobs.
    IF cardinality(v_cancelled) > 0 THEN
        UPDATE neuro.work_leases SET released_at = now()
         WHERE job_id = ANY(v_cancelled) AND released_at IS NULL;
    END IF;

    RETURN cardinality(v_cancelled);
END;
$$ LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # 1. claim_job — the hot path. `FOR UPDATE SKIP LOCKED` is preserved EXACTLY: §A·56 puts it explicitly
    #    out of scope for the ADR-0046 locking pass, and it is the one place bare FOR UPDATE is correct.
    #
    # ⚠ OUT PARAMETERS ARE `out_`-PREFIXED ON PURPOSE. plpgsql resolves ambiguous names against OUT
    #    parameters as well as columns, and this body writes the very columns it returns; the prefix removes
    #    the question entirely instead of relying on every future editor to keep every reference qualified.
    # ⚠ ONE DELIBERATE DIVERGENCE FROM A TOKEN-FOR-TOKEN TRANSCRIPTION, and it is a hardening: the claim
    #    token is now minted by `gen_random_uuid()` INSIDE the function instead of being generated in Python
    #    and passed in. The token is the capability this whole unit hangs on, so the DB mints it and the
    #    caller cannot choose it. `uuid.uuid4()` therefore disappears from Repository.claim.
    # ⚠ p_lease_seconds IS A PARAMETER, NOT A LITERAL. Hard-coding the TTL here would make LEASE_SECONDS a
    #    SECOND source of truth and leave ADR-0039's DERIVED renewal cadence (resolve_renew_interval, C1a)
    #    governing a number that no longer sets the lease — the exact drift this house kills on sight.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.claim_job(
    p_actor_id      bigint,
    p_queue         text,
    p_gpu_class     text,
    p_lease_seconds integer
) RETURNS TABLE (out_job_id bigint, out_claim_token uuid, out_claim_seq bigint) AS $$
DECLARE
    v_job   bigint;
    v_token uuid;
    v_seq   bigint;
BEGIN
    UPDATE neuro.jobs
       SET state = 'claimed', claim_token = gen_random_uuid(), claim_seq = neuro.jobs.claim_seq + 1,
           claimed_by = p_actor_id, updated_at = now()
     WHERE neuro.jobs.job_id = (
         SELECT j.job_id FROM neuro.jobs j
          WHERE j.state = 'queued' AND j.queue = p_queue
            AND (CAST(p_gpu_class AS text) IS NULL OR j.gpu_class IS NOT DISTINCT FROM p_gpu_class)
          ORDER BY j.job_id
          FOR UPDATE SKIP LOCKED
          LIMIT 1
     )
    RETURNING neuro.jobs.job_id, neuro.jobs.claim_token, neuro.jobs.claim_seq
      INTO v_job, v_token, v_seq;

    IF v_job IS NULL THEN
        RETURN;                              -- no claimable job: zero rows, the `Claim | None` contract
    END IF;

    INSERT INTO neuro.work_leases (job_id, claim_token, leased_by, expires_at)
    VALUES (v_job, v_token, p_actor_id, now() + make_interval(secs => p_lease_seconds));

    out_job_id := v_job; out_claim_token := v_token; out_claim_seq := v_seq;
    RETURN NEXT;
END;
$$ LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # 2. renew_lease — the heartbeat statement, moved verbatim.
    #    FIX #6 (a): `l.expires_at >= now()` — a worker can NEVER renew an ALREADY-EXPIRED lease. An expired
    #    lease belongs to the reaper; renewing it was the wedge trigger. R4: the join on jobs requires the
    #    job to still be in-flight, so a CANCELLED job's heartbeat returns false (the worker learns it has
    #    been preempted). UPDATE..FROM locks only work_leases (jobs is read, not locked) — no jobs/leases
    #    lock-order conflict with the reaper.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.renew_lease(
    p_job_id bigint, p_claim_token uuid, p_lease_seconds integer
) RETURNS boolean AS $$
DECLARE
    n integer;
BEGIN
    UPDATE neuro.work_leases l SET last_heartbeat = now(),
           expires_at = now() + make_interval(secs => p_lease_seconds)
      FROM neuro.jobs j
     WHERE l.job_id = p_job_id AND l.claim_token = p_claim_token AND l.released_at IS NULL
       AND l.expires_at >= now()
       AND j.job_id = l.job_id AND j.state IN ('claimed', 'running');
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n = 1;
END;
$$ LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # 3. checkpoint_job — the A4 resume-after-preemption pointer.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.checkpoint_job(
    p_job_id bigint, p_claim_token uuid, p_checkpoint_ref text
) RETURNS boolean AS $$
DECLARE
    n integer;
BEGIN
    UPDATE neuro.jobs SET state = 'running', checkpoint_ref = p_checkpoint_ref, updated_at = now()
     WHERE job_id = p_job_id AND claim_token = p_claim_token AND state IN ('claimed', 'running');
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n = 1;
END;
$$ LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # 4. complete_job — CAS + ownership, then the lease release and the dependent unblock.
    #    The EARLY RETURN is preserved exactly: on a CAS miss (wrong token / not in-flight / already
    #    stolen-and-fenced) the function returns false having touched NOTHING else — it must not release a
    #    lease or unblock a dependent it did not earn. `n = 1`, never FOUND (measured: FOUND is true for any
    #    n >= 1, so a multi-row hit would read as success).
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.complete_job(p_job_id bigint, p_claim_token uuid) RETURNS boolean AS $$
DECLARE
    n integer;
BEGIN
    UPDATE neuro.jobs SET state = 'succeeded', updated_at = now()
     WHERE job_id = p_job_id AND claim_token = p_claim_token AND state IN ('claimed', 'running');
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> 1 THEN
        RETURN false;
    END IF;

    UPDATE neuro.work_leases SET released_at = now()
     WHERE job_id = p_job_id AND claim_token = p_claim_token AND released_at IS NULL;

    PERFORM neuro.unblock_ready_dependents(p_job_id);
    RETURN true;
END;
$$ LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # 5. fail_job_permanent — dead-letter a permanent failure, then cascade-cancel its dependents.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.fail_job_permanent(
    p_job_id bigint, p_claim_token uuid, p_detail text
) RETURNS boolean AS $$
DECLARE
    n integer;
BEGIN
    UPDATE neuro.jobs SET state = 'dead_letter', error_class = 'permanent',
           error_detail = p_detail, attempt_count = attempt_count + 1, updated_at = now()
     WHERE job_id = p_job_id AND claim_token = p_claim_token AND state IN ('claimed', 'running');
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> 1 THEN
        RETURN false;
    END IF;

    UPDATE neuro.work_leases SET released_at = now()
     WHERE job_id = p_job_id AND claim_token = p_claim_token AND released_at IS NULL;

    PERFORM neuro.cascade_cancel_jobs(p_job_id, false);
    RETURN true;
END;
$$ LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # 8a. THE FENCING ARM. A BELT UNDER THE REVOCATION, not the closure — grants are provisioning-mutable
    #     while triggers are schema, so this is the 0003 defense-in-depth pattern applied to the fence.
    #
    # THE CONTRACT COMES FROM THE CODE, NOT FROM A DESIGN. The only two writers of these columns are
    # claim (`claim_token = <new>, claim_seq = claim_seq + 1`) and the reaper's requeue
    # (`claim_token = NULL, claim_seq = claim_seq + 1`). Both advance the fence by exactly one, so the
    # predicate is exactly that and nothing more.
    #
    # WHAT IT GUARANTEES: a write touching claim_token or claim_seq can never leave the fence FROZEN or move
    # it BACKWARDS ⇒ claim_seq is a trustworthy monotonic epoch and a token change is never silent.
    # WHAT IT DOES NOT — stated, because it is the honest half:
    #   * it does NOT stop a role that still holds UPDATE(claim_token, claim_seq) from STEALING a live claim:
    #     a steal that also bumps the fence is indistinguishable from a legal re-claim. That closure is the
    #     REVOCATION in grants.sql.
    #   * it permits `claim_token = NULL, claim_seq = claim_seq + 1` on a still-'claimed' job, which strands
    #     the three ownership CASes (they key on claim_token) while renew_lease keeps succeeding (it keys on
    #     the lease row). That is an AVAILABILITY wedge BOUNDED BY THE REAPER, not a permanent strand: once
    #     renewal stops the lease lapses and reap_expired requeues the job in-band. Named here rather than
    #     discovered later.
    #
    # `IS NOT TRUE`, never `IF NOT` — 0003's measured lesson. A NULL claim_seq must be refused BY THE GUARD
    # with a message naming the transition, not by the column's NOT NULL with a message naming nothing.
    # `OF claim_token, claim_seq` is load-bearing: it keeps the trigger off every state / counter /
    # checkpoint UPDATE that never touches the fence.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.assert_job_claim_fencing() RETURNS trigger AS $$
BEGIN
    IF (NEW.claim_seq = OLD.claim_seq + 1) IS NOT TRUE THEN
        RAISE EXCEPTION 'claim fence violation on %.job_id=%: claim_seq % -> % (a write touching '
                        'claim_token/claim_seq must advance the fence by exactly one; ADR-0046 C3)',
            TG_TABLE_NAME, OLD.job_id, OLD.claim_seq, NEW.claim_seq;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp;
""")
    op.execute(
        """
        CREATE TRIGGER jobs_claim_fencing
            BEFORE UPDATE OF claim_token, claim_seq ON neuro.jobs
            FOR EACH ROW EXECUTE FUNCTION neuro.assert_job_claim_fencing();
        """
    )

    # ---------------------------------------------------------------------------------------------
    # 8b. THE manifest_sha256 ASSIGN-ONCE ARM. REUSES 0001's parameterized neuro.assert_assign_once — one
    #     implementation per concept; no new function. MEASURED that it works on a BYTEA column:
    #     to_jsonb(OLD)->>'manifest_sha256' renders '\\xaabb', NULL->value is permitted, a same-value rewrite
    #     is permitted, and value->different RAISES.
    #
    # ZERO APP IMPACT, MEASURED: manifest_sha256 has exactly ONE writer in the whole repo
    # (bundles/registrar.py::_seal), it is set at seal from NULL, and _seal EARLY-RETURNS on an idempotent
    # re-seal — so no same-value rewrite even occurs. And, unlike 0003's bundles_state_transition, this
    # trigger does NOT fire on _ensure_bundle's `ON CONFLICT ... DO UPDATE SET run_id` resume arm (MEASURED):
    # `UPDATE OF manifest_sha256` keys on the SET column list, which there is {run_id}. The W6/W7 recovery
    # path 0003 had to design around is untouched here.
    #
    # ⚠ THIS SUPERSEDES the surviving manifest_sha256 half of ADR-0010's 2026-06-16 Closed decision, on its
    # OWN grounds (the ADR addendum in unit C3b argues it): "hash-verifiable on read" does not stop a writer
    # RE-KEYING the hash before any read happens, and the direct-writer topology is what makes that reachable.
    op.execute(
        """
        CREATE TRIGGER bundles_manifest_assign_once
            BEFORE UPDATE OF manifest_sha256 ON neuro.bundles
            FOR EACH ROW EXECUTE FUNCTION neuro.assert_assign_once('manifest_sha256');
        """
    )

    # ---------------------------------------------------------------------------------------------
    # 8c. ★ THE INSERT-SIDE HALF OF THE ASSIGN-ONCE ARM — 0003's LESSON, REPEATING, AND CAUGHT BY MEASUREMENT.
    #
    # An UPDATE-only assign-once arm is WALKABLE AROUND, exactly as 0003's UPDATE-only state ladder was.
    # MEASURED as the real neuro_writer, which holds full-row INSERT on bundles:
    #     INSERT INTO neuro.bundles (run_id, backend_id, state, manifest_sha256)
    #     VALUES (..., 'unsealed', '\\xdead')          -- LANDS UNOPPOSED before this change
    # 0003's bundles_state_insert constrains only `state`, so 'unsealed' passes and the forged digest rides
    # in. The row then WEDGES its identity: _seal reads a non-NULL manifest_sha256 that does not match, and
    # raises SeamIntegrityError forever.
    #
    # So 0003's INSERT arm is REPLACED (not supplemented — one guard, one concept) to assert BOTH birth
    # conditions. No new object; the function count is unchanged and the downgrade restores the 0003 body.
    # No stricter than the app: the ONLY bundles INSERT in the entire repo sets neither column and takes the
    # schema defaults ('unsealed', NULL).
    # ⚠ THE MESSAGE KEEPS 0003's EXACT LEADING PHRASE — `illegal bundle_state at INSERT` — ON PURPOSE.
    # Eight shipped 0003 probes assert on it (test_rt_state_triggers.py: the five non-birth states, the two
    # reserved members, and the NULL-state arm). The GUARD'S IDENTITY has not changed, only its second
    # conjunct, so churning eight green red-team assertions to suit a reworded string would be damage, not
    # progress. The message GAINS the manifest term rather than being replaced.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.assert_bundle_insert_state() RETURNS trigger AS $$
BEGIN
    IF (NEW.state = 'unsealed' AND NEW.manifest_sha256 IS NULL) IS NOT TRUE THEN
        RAISE EXCEPTION 'illegal bundle_state at INSERT on %: state=% manifest_sha256=% (a bundle is born '
                        '''unsealed'' with NO manifest; it is sealed by the registrar, ADR-0046 P-4/C3)',
            TG_TABLE_NAME, NEW.state, NEW.manifest_sha256;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp;
""")

    # ---------------------------------------------------------------------------------------------
    # D. Default-deny on the new callables. PUBLIC is a pseudo-role that always exists, so this does NOT
    #    make the migration role-dependent. The positive EXECUTE grant (to neuro_writer, and only for the
    #    five DEFINER entry points) lives in db/sql/grants.sql, with the ownership change.
    #    NOT applied to the two trigger functions: a trigger function cannot be invoked directly from SQL,
    #    and 0001/0003 set no such revoke on theirs — matching precedent rather than inventing a rule.
    for sig in _CALLABLE_SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")


def downgrade() -> None:
    # ⚠ ORDER IS REQUIRED, not cosmetic (0003's measured lesson): a trigger holds a pg_depend edge on its
    # function, so dropping functions first raises "cannot drop function ... because other objects depend on
    # it" and aborts the whole downgrade. Triggers first, then functions — and deliberately NOT
    # `DROP FUNCTION ... CASCADE`, which would silently eat a future dependent instead of failing loudly.
    op.execute("DROP TRIGGER IF EXISTS jobs_claim_fencing ON neuro.jobs")
    op.execute("DROP TRIGGER IF EXISTS bundles_manifest_assign_once ON neuro.bundles")
    op.execute("DROP FUNCTION IF EXISTS neuro.assert_job_claim_fencing()")
    for sig in _CALLABLE_SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")

    # RESTORE 0003's bundles INSERT guard verbatim. This is why the downgrade is not a pure DROP list: 8c
    # REPLACED an existing function rather than adding one, so un-replacing it is part of going back.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.assert_bundle_insert_state() RETURNS trigger AS $$
BEGIN
    IF (NEW.state = 'unsealed') IS NOT TRUE THEN
        RAISE EXCEPTION 'illegal bundle_state at INSERT on %: % (a bundle is born ''unsealed''; ADR-0046 P-4)',
            TG_TABLE_NAME, NEW.state;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""")
