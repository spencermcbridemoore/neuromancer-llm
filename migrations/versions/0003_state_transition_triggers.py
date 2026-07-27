"""jobs/bundles state-transition CAS + the capture_events.model_id bind, as in-schema triggers (ADR-0046 P-4).

The SECOND schema change since 0001 (owner-nodded 2026-07-27). Converts three software guards that today live only
in Python into in-schema triggers, so an untrusted `neuro_writer` connecting DIRECTLY — the topology the ADR-0046
bundle + the worker/registrar role split exist to enable — cannot bypass them with a bare UPDATE/INSERT:

  * `jobs_state_transition`        BEFORE UPDATE OF state ON jobs          — the job_state ladder
  * `bundles_state_transition`     BEFORE UPDATE OF state ON bundles       — the bundle_state ladder
  * `bundles_state_insert`         BEFORE INSERT ON bundles                — inserts are 'unsealed' or nothing
  * `capture_events_model_bind`    BEFORE INSERT ON capture_events         — C10(b), the run-fingerprint model bind

THE LEGAL EDGE SETS ARE THE SHIPPED WRITERS, ENUMERATED — no stricter, no looser (P-4's critic fix). Every edge below
EXCEPT ONE is the WHERE predicate of a real statement in db/repository.py or bundles/registrar.py. The exception is
`tombstoned <- registered`, which has NO producer in `src/` at all: it is admitted on the FROZEN SCHEMA's own
declaration (`tests/reference/phase3-ddl.sql`: `tombstoned_at timestamptz NULL, -- tombstone BEFORE blob removal
(capture contract §4)`) — a dedicated column that exists for nothing else, plus a normative schema comment naming
the position in the lifecycle. That is the ONE place this migration is knowingly looser than the shipped code, and
it is why the states that have NO such backing (`staged`/`verified`, enum-only; jobs `failed` as a TARGET,
enum-only) are refused instead. Stated here rather than left for a reader to discover:
  jobs   claimed  <- queued                                        (Repository.claim)
         running  <- claimed, running                              (checkpoint; running->running IS a real self-edge)
         succeeded<- claimed, running                              (complete)
         dead_letter <- claimed, running                           (fail_permanent)
         queued   <- blocked, claimed, running                     (_unblock_ready_dependents; reap_expired)
         cancelled<- blocked, queued, claimed, running, failed     (_cascade_cancel's NOT IN predicate, verbatim)
  bundles sealed     <- unsealed                                   (_seal)
          registered <- sealed                                     (register)
          tombstoned <- registered                                 (capture contract §4; see the tombstone note)

WHAT THIS DOES AND DOES NOT GUARANTEE — stated precisely, because the honest scope is narrower than "the CAS is now
in the schema". IT DOES: make `succeeded`/`cancelled`/`dead_letter` ABSORBING (no out-edge, so no resurrection),
make `blocked`/`failed` unreachable as UPDATE targets, freeze the bundle lifecycle forward-only with `tombstoned`
as a sink, pin every bundles INSERT to 'unsealed', and refuse — AT INSERT — a capture that contradicts its run's
fingerprint model. ⚠ That last one is INSERT-time only: it is NOT re-checked when a run's fingerprint is later
ADOPTED, and `neuro_writer` holds `GRANT UPDATE (fingerprint_id) ON runs`, so an insert-then-adopt pair still lands
a contradiction (the shipped Python check has the identical hole; it is the registered "adhoc auto-mint +
fingerprint-adoption" obligation, and it is IRREVERSIBLE once adopted, because runs_fingerprint_assign_once then
refuses any correction of fingerprint_id).
IT DOES NOT: prevent a writer holding `GRANT UPDATE (state, ...)` from WALKING the ladder one statement at a
time — `queued -> claimed -> succeeded` is two individually-legal hops and marks a job done with no token, no lease
and no work. That closure is NOT a trigger predicate (every column such a predicate could test — claim_token,
claim_seq, claimed_by, and work_leases INSERT — is itself granted to the writer, so it is forgeable in the same
transaction, and rejecting the app's own claim()/complete() pair would violate P-4's no-stricter arm). It belongs
to the worker/registrar ROLE SPLIT: revoke UPDATE(state) from neuro_writer and route claim/complete/fail through
SECURITY DEFINER functions that do the CAS + lease atomically. The residual is pinned as a POSITIVE red-team probe
(tests/redteam/test_rt_state_triggers.py) so it can never be silently mis-remembered as closed.

SCOPE, DELIBERATE: `jobs.claim_token`/`claim_seq` fencing and `bundles.manifest_sha256` assign-once are NOT in this
migration. P-4's actionable node is "state CAS + the model_id bind"; both are registered follow-ons landing with
the role split.

Parity note: tests/reference/phase3-ddl.sql is the frozen 0001 schema and test_migration_ddl_parity upgrades only to
0001_initial_schema, so this migration is INVISIBLE to the parity gate and the frozen reference DDL is deliberately
left UNTOUCHED. Consequence to know: anything built FROM that frozen file — notably the A2-9 restore-drill fixture
`scratch_db` (tests/conftest.py) — runs WITHOUT these four guards BY DESIGN. The suite's own session schema is
built by `alembic upgrade head` (tests/conftest.py), so there they are live.

Rollback: `alembic downgrade 0002_external_records_stamp` drops the four triggers and the four functions and
touches no table, no column and no row — data-lossless by construction. ⚠ DATA-lossless is not AVAILABILITY-free,
and the rollback is the MORE disruptive direction: MEASURED on postgres:18.4, `DROP TRIGGER` takes an
**AccessExclusiveLock** (which conflicts with plain SELECT) while `CREATE TRIGGER` takes only ShareRowExclusive
(writers only) — so a single open READ transaction that is invisible to the apply will block the rollback, and
because all four DROPs share one alembic transaction a contended `capture_events` DROP freezes reads on `jobs` and
`bundles` too. Run it with a bounded `lock_timeout` and drained readers; see the canonical runbook. ⚠ This
SUPERSEDES the stale lever in scratch/go-remote-staged-plan-2026-06-29.md **§3** (the Rollback bullet under "The
concurrency prerequisite bundle" — NOT §7, which is a status section): "downgrade migration 0002 dropping the
triggers" now points at the importer's D1 stamp, applied to canonical 2026-07-17, and running it strips the
confidentiality / derived_by_predecessor grades off 360 live rows.

Revision ID: 0003_state_transition_triggers
Revises: 0002_external_records_stamp
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_state_transition_triggers"
down_revision: str | None = "0002_external_records_stamp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOTE: the SQL below is intentionally flush-left, matching 0001's assert_assign_once idiom (pg_get_functiondef
    # preserves body whitespace, so a stored body is easiest to read back when it is not re-indented here).
    #
    # ⚠ `IF (...) IS NOT TRUE`, never `IF NOT (...)`. MEASURED on postgres:18.4: with `IF NOT (...)`, a `SET
    # state = NULL` makes every disjunct NULL, the OR-chain NULL, and `IF NOT NULL` does NOT take the THEN branch —
    # the guard PERMITS and only the column's NOT NULL catches it, with an error naming no transition. Worse, the
    # same statement DOES raise when OLD.state is terminal (NULL AND FALSE collapses to FALSE), so the guard's
    # behaviour would be order-dependent on the row's current state. `IS NOT TRUE` treats NULL as a rejection and
    # makes the guard's stated contract true. It reddens nothing: no app statement writes a NULL state.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.assert_job_state_transition() RETURNS trigger AS $$
BEGIN
    IF (
           (NEW.state = 'claimed'     AND OLD.state = 'queued')
        OR (NEW.state = 'running'     AND OLD.state IN ('claimed', 'running'))
        OR (NEW.state = 'succeeded'   AND OLD.state IN ('claimed', 'running'))
        OR (NEW.state = 'dead_letter' AND OLD.state IN ('claimed', 'running'))
        OR (NEW.state = 'queued'      AND OLD.state IN ('blocked', 'claimed', 'running'))
        OR (NEW.state = 'cancelled'   AND OLD.state IN ('blocked', 'queued', 'claimed', 'running', 'failed'))
    ) IS NOT TRUE THEN
        RAISE EXCEPTION 'illegal job_state transition on %.job_id=%: % -> % (ADR-0046 P-4 state ladder)',
            TG_TABLE_NAME, OLD.job_id, OLD.state, NEW.state;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""")
    # `OF state` is LOAD-BEARING, not an optimisation: it keeps the trigger off every lease/counter/checkpoint_ref
    # UPDATE that never touches `state`, and it mirrors runs_fingerprint_assign_once. It is NOT interchangeable
    # with a same-value early-out or a WHEN (OLD.state IS DISTINCT FROM NEW.state) clause — those legalize EVERY
    # self-edge (succeeded->succeeded, cancelled->cancelled, ...), which is looser than the app, and they would
    # make the running->running probe vacuously green (passing because the trigger never fired).
    op.execute(
        """
        CREATE TRIGGER jobs_state_transition
            BEFORE UPDATE OF state ON neuro.jobs
            FOR EACH ROW EXECUTE FUNCTION neuro.assert_job_state_transition();
        """
    )

    # ⚠ tombstoned <- registered ONLY, and this is the migration's ONE deliberate no-looser deviation (see the
    # module docstring). NOTHING in `src/` writes `state='tombstoned'` — the tombstone writer is unbuilt; the only
    # producer in the whole repo is a red-team test. What admits the edge is the FROZEN SCHEMA: a dedicated
    # `bundles.tombstoned_at` column whose comment names the lifecycle position ("tombstone BEFORE blob removal,
    # capture contract §4"), which gc.py's header restates as "Deletion of REGISTERED data is the mirror".
    # `sealed -> tombstoned` is deliberately FORBIDDEN:
    # a sealed bundle is precisely the one a resumed worker RE-REGISTERS (ADR-0010's GC exemption is what makes
    # W6/W7 recoverable), and tombstoned is terminal, so blessing that edge would hand a writer a one-statement,
    # schema-sanctioned way to permanently strand an in-flight bundle. Same fail-closed reading applies to the
    # ADR-0018 reserved members: `staged`/`verified` are reserved VALUES with no defined edges and no code path
    # (gc.py names them in prose only; its executable COLLECTIBLE_STATES is ("unsealed",)), so entry is refused
    # rather than invented. Re-arming the relay extends this ladder in its own migration.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.assert_bundle_state_transition() RETURNS trigger AS $$
BEGIN
    IF (
           (NEW.state = 'sealed'     AND OLD.state = 'unsealed')
        OR (NEW.state = 'registered' AND OLD.state = 'sealed')
        OR (NEW.state = 'tombstoned' AND OLD.state = 'registered')
    ) IS NOT TRUE THEN
        RAISE EXCEPTION 'illegal bundle_state transition on %.bundle_id=%: % -> % (ADR-0046 P-4 state ladder)',
            TG_TABLE_NAME, OLD.bundle_id, OLD.state, NEW.state;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""")
    # `OF state` is load-bearing here for a SECOND, bundles-specific reason: BundleRegistrar._ensure_bundle runs
    # `INSERT ... ON CONFLICT (bundle_uuid) DO UPDATE SET run_id = neuro.bundles.run_id` on EVERY register call,
    # including an idempotent resume of a 'sealed' or 'registered' bundle. A bare BEFORE UPDATE would fire on that
    # conflict arm with OLD.state = NEW.state and reject the resume — breaking the W6/W7 crash-recovery path.
    # MEASURED: `UPDATE OF state` keys on the SET column list, which there is {run_id}, so it does not fire.
    op.execute(
        """
        CREATE TRIGGER bundles_state_transition
            BEFORE UPDATE OF state ON neuro.bundles
            FOR EACH ROW EXECUTE FUNCTION neuro.assert_bundle_state_transition();
        """
    )

    # THE INSERT ARM. `neuro_writer` holds full-row INSERT on bundles (db/sql/grants.sql, the writer INSERT list),
    # so an UPDATE-only ladder is walkable around: MEASURED on postgres:18.4 as neuro_writer, a direct
    # `INSERT INTO neuro.bundles (..., state) VALUES (..., 'registered')` — and equally 'sealed'/'staged'/
    # 'verified'/'tombstoned' — lands unopposed, producing a row that is permanently GC-exempt (COLLECTIBLE_STATES
    # is ('unsealed',)), unreclaimable in-role (the writer holds no DELETE), and that wedges its (run, dataset,
    # partition) out of registration forever, because _ensure_bundle's ON CONFLICT adopts the forged row and _seal
    # then matches zero rows and raises. No stricter than the app: the ONLY bundles INSERT in the entire repo
    # writes the literal 'unsealed'.
    #
    # ⚠ A SEPARATE FUNCTION, and BEFORE INSERT ONLY. Two measured traps.
    # (1) Reusing assert_bundle_state_transition here is unusable in BOTH directions, and which way it fails
    #     depends on the guard shape — which is exactly why it is written down. In a BEFORE INSERT trigger OLD is
    #     a NULL *record* (not an error, measured), so every conjunct is NULL and the OR-chain is NULL. With this
    #     file's mandated `IS NOT TRUE`, `NULL IS NOT TRUE` is TRUE, so it RAISES on ALL FOUR insert states —
    #     rejecting the one LEGAL insert ('unsealed') as loudly as the damaging ones. With the `IF NOT (...)` form
    #     this file refuses above, the same body PERMITS `INSERT ... state='registered'` outright. So: fail-closed
    #     on everything here, fail-OPEN on the damaging insert there. Both measured on postgres:18.4 against this
    #     function's verbatim body. Hence a separate function that never dereferences OLD.
    # (2) Widening this to BEFORE INSERT OR UPDATE re-arms the _ensure_bundle conflict-arm breakage described
    #     above.
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
    op.execute(
        """
        CREATE TRIGGER bundles_state_insert
            BEFORE INSERT ON neuro.bundles
            FOR EACH ROW EXECUTE FUNCTION neuro.assert_bundle_insert_state();
        """
    )

    # C10(b), the INSERT-side obligation the Deferred-Obligation Register names by that word: bind
    # capture_events.model_id to the run's fingerprint model. `SELECT ... INTO` with no matching row leaves
    # bound_model NULL and does not raise (no STRICT), which IS the ADR-0036 carve-out: an adhoc run has
    # runs.fingerprint_id NULL, the join yields nothing, and any model_id is accepted — exactly what the shipped
    # write-path check in capture/events.py does. A nonexistent run_id also yields NULL, so the FK raises next
    # (BEFORE INSERT triggers fire ahead of RI) rather than this guard swallowing it.
    #
    # ⚠ ONE DISCLOSED DIVERGENCE FROM THE PYTHON CHECK, snapshot timing: the app reads the bind near the top of
    # its transaction and INSERTs later, while this trigger re-evaluates at the INSERT statement's own READ
    # COMMITTED snapshot. If a run's fingerprint is ADOPTED (NULL->value, permitted once by
    # runs_fingerprint_assign_once) in that window, the trigger can reject a capture whose Python pre-check saw a
    # NULL fingerprint. That window is the only place this migration touches the separately-registered "adhoc
    # auto-mint + fingerprint-adoption" obligation; it is stated, not closed, here.
    op.execute("""\
CREATE OR REPLACE FUNCTION neuro.assert_capture_model_bind() RETURNS trigger AS $$
DECLARE
    bound_model bigint;
BEGIN
    SELECT f.model_id INTO bound_model
      FROM neuro.runs r
      JOIN neuro.fingerprints f ON f.fingerprint_id = r.fingerprint_id
     WHERE r.run_id = NEW.run_id;
    IF bound_model IS NOT NULL AND bound_model IS DISTINCT FROM NEW.model_id THEN
        RAISE EXCEPTION 'capture_events.model_id=% contradicts run %''s fingerprint model % (C10b bind, ADR-0046 P-4)',
            NEW.model_id, NEW.run_id, bound_model;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""")
    op.execute(
        """
        CREATE TRIGGER capture_events_model_bind
            BEFORE INSERT ON neuro.capture_events
            FOR EACH ROW EXECUTE FUNCTION neuro.assert_capture_model_bind();
        """
    )


def downgrade() -> None:
    # ⚠ ORDER IS REQUIRED, not cosmetic: a trigger holds a pg_depend edge on its function, so dropping the
    # functions first raises "cannot drop function ... because other objects depend on it" (MEASURED on
    # postgres:18.4) and aborts the whole downgrade. Triggers first, then functions — and deliberately NOT
    # `DROP FUNCTION ... CASCADE`, which would silently eat any future dependent instead of failing loudly.
    op.execute("DROP TRIGGER IF EXISTS jobs_state_transition ON neuro.jobs")
    op.execute("DROP TRIGGER IF EXISTS bundles_state_transition ON neuro.bundles")
    op.execute("DROP TRIGGER IF EXISTS bundles_state_insert ON neuro.bundles")
    op.execute("DROP TRIGGER IF EXISTS capture_events_model_bind ON neuro.capture_events")
    op.execute("DROP FUNCTION IF EXISTS neuro.assert_job_state_transition()")
    op.execute("DROP FUNCTION IF EXISTS neuro.assert_bundle_state_transition()")
    op.execute("DROP FUNCTION IF EXISTS neuro.assert_bundle_insert_state()")
    op.execute("DROP FUNCTION IF EXISTS neuro.assert_capture_model_bind()")
