"""Red-team: the ADR-0046 row-locking pass (P-1/P-2/P-3) — the three wedges and their hammers.

Each wedge gets a hammer that is RED against pre-fix code and GREEN after, plus the sibling vector and
the structural pins no behavioural probe can reach:

  * WEDGE 1 - the #6 NEAR-EXPIRY REAPER wedge (`reap_expired` vs a renewing `heartbeat`), driven by the
    REAL `LeaseRenewer` + `ReaperLoop` (unit C1a, `test_rt_runtime_loops.py`) - which is precisely why
    the loops had to land first: without a concurrent renewal loop this fix has no target and no
    provable RED-on-revert.
  * WEDGE 2 - the `complete()` DEPENDENT-UNBLOCK WRITE-SKEW (ADR-0046 s6). Hand-driven interleave, two
    sessions, explicit latches (precedent E-13: a 5-of-6 panel was once wrong about this territory; one
    reviewer with a hand-driven interleave was right).
  * WEDGE 3 - the C20 ENQUEUE TOCTOU (the unlocked dep-state SELECT), plus its `cancel_cascade`
    SIBLING, which the enqueue lock makes DETERMINISTIC rather than racy if left unclosed.

★ THE `now()` FACT THE WHOLE FILE TURNS ON: PostgreSQL `now()` is `transaction_timestamp()`, fixed for
a whole transaction, NOT per statement. The near-expiry residual is reachable ONLY when
`now_heartbeat <= expires_at < now_reaper`, so `expire_lease_now()` (which backdates a full second)
CANNOT stage it: any heartbeat transaction opened afterwards is refused by FIX #6a, takes no row lock,
and the "latch" would hold nothing while the hammer passed against UNFIXED code. `_stage_near_expiry`
pins the heartbeat's transaction timestamp FIRST and only then backdates the lease by a microsecond.

⚠ NO SLEEPS PACE ANYTHING HERE. Every wait is an event latch or a bounded poll on observable server
state (`pg_blocking_pids`), and every poll raises LOUD on timeout rather than continuing.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import threading
import time

import pytest
from sqlalchemy import text

from neuromancer_llm.db import repository as repo_mod
from neuromancer_llm.db.repository import Repository
from neuromancer_llm.workers.runtime import LeaseRenewer, ReaperLoop, reap

pytestmark = pytest.mark.pg

_WAIT = 15.0  # generous per-step bound so a wedge surfaces as a LOUD failure, never a hang


# --- bounded, observable waits (never a paced sleep) ----------------------------------------------
def _backend_pid(conn) -> int:
    return conn.execute(text("SELECT pg_backend_pid()")).scalar_one()


def _bound_connection(conn) -> None:
    """Server-side bounds on a connection this test holds across a latch. A stranded lock wait then
    ABORTS loudly instead of blocking the NEXT test's `_truncate_all` TRUNCATE (which needs ACCESS
    EXCLUSIVE and would wait unbounded, with no deadlock detection to surface it). SET LOCAL, so the
    setting reverts when the transaction ends and never leaks back into the pool."""
    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    conn.execute(text("SET LOCAL statement_timeout = '20s'"))


def _wait_until_blocked_by(engine, blocker_pid: int, *, timeout: float = _WAIT) -> int:
    """Poll until SOME backend is lock-blocked BY `blocker_pid`, and return its pid.

    Pinned on `pg_blocking_pids`, deliberately NOT on a `query ILIKE` grep: the shipped
    `_wait_until_blocked` matches query TEXT, which cannot reliably match a `SELECT ... FOR NO KEY
    UPDATE` issued from a helper, and a predicate that silently never matches degrades the whole
    interleave into a race. Raises on timeout - a hammer that proceeds without its interleave having
    happened is a fabricated green."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            pid = conn.execute(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    " WHERE :blocker = ANY(pg_blocking_pids(pid)) AND pid <> pg_backend_pid() LIMIT 1"
                ),
                {"blocker": blocker_pid},
            ).scalar_one_or_none()
        if pid is not None:
            return pid
        time.sleep(0.02)
    raise AssertionError(f"nothing ever blocked on backend {blocker_pid} within {timeout}s")


def _wait_done_or_blocked(engine, done: threading.Event, blocker_pid: int, *, timeout: float = _WAIT):
    """Wait until a worker thread either FINISHES or becomes lock-blocked by `blocker_pid`.

    Both outcomes are legitimate and which one occurs is exactly what distinguishes pre-fix from
    post-fix, so the hammer must handle either without a timing assumption. Returns True if it blocked."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done.is_set():
            return False
        with engine.connect() as conn:
            n = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    " WHERE :blocker = ANY(pg_blocking_pids(pid)) AND pid <> pg_backend_pid()"
                ),
                {"blocker": blocker_pid},
            ).scalar_one()
        if n >= 1:
            return True
        time.sleep(0.02)
    raise AssertionError(f"the worker neither finished nor blocked on backend {blocker_pid} in {timeout}s")


def _wait_for(predicate, *, what: str, timeout: float = _WAIT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _active_leases(engine, job_id: int) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM neuro.work_leases WHERE job_id = :j AND released_at IS NULL"),
            {"j": job_id},
        ).scalar_one()


def _assert_no_wedge(engine, job_id: int) -> None:
    """THE WEDGE SIGNATURE: a 'queued' job that still owns an unreleased lease. The next claim() then
    hits work_leases_active_uq (phase3-ddl.sql:371) and the job is un-claimable forever."""
    with engine.connect() as conn:
        wedged = conn.execute(
            text(
                "SELECT count(*) FROM neuro.jobs j JOIN neuro.work_leases l ON l.job_id = j.job_id "
                " WHERE j.job_id = :j AND j.state = 'queued' AND l.released_at IS NULL"
            ),
            {"j": job_id},
        ).scalar_one()
    assert wedged == 0, "WEDGE: job is 'queued' while an unreleased lease still exists for it"


# --- WEDGE 1: the near-expiry reaper wedge, driven by the REAL B-4/B-4b loops ----------------------
class _LatchedRepo:
    """Wraps a real Repository and latches the FIRST heartbeat INSIDE its own transaction, so the
    renewal's row lock is HELD OPEN while the reaper sweeps (the shipped `_GatedBackend` idiom,
    test_rt_concurrency.py:88).

    ⚑ The statement executed is `Repository._renew_lease` - the PRODUCTION SQL, on a connection this
    wrapper owns. Nothing here re-implements it: a hammer that tests its own copy of the statement it
    is proving is a fabricated green (precedent 14). All this class owns is the transaction envelope
    and the latches. Every heartbeat after the first delegates straight through."""

    def __init__(self, repo: Repository):
        self._repo = repo
        self.engine = repo.engine
        self.txn_open = threading.Event()
        self.staged = threading.Event()
        self.lock_held = threading.Event()
        self.release = threading.Event()
        self.hb_pid: int | None = None
        self.hb_now = None
        self.renewed: bool | None = None
        self._latched = False

    def heartbeat(self, *, job_id: int, claim_token) -> bool:
        if self._latched:
            return self._repo.heartbeat(job_id=job_id, claim_token=claim_token)
        self._latched = True
        conn = self.engine.connect()
        try:
            # PHASE 1 - PIN now_hb. psycopg sends BEGIN lazily, so the transaction timestamp is fixed by
            # this FIRST execute, not by connect(). It must be pinned while the lease is still LIVE, or
            # FIX #6a refuses the renewal below and the whole interleave is vacuous.
            _bound_connection(conn)
            self.hb_now = conn.execute(text("SELECT now()")).scalar_one()
            self.hb_pid = _backend_pid(conn)
            self.txn_open.set()
            assert self.staged.wait(timeout=_WAIT), "the hammer never staged near-expiry"
            # PHASE 2 - the PRODUCTION renewal statement, on the latched connection.
            self.renewed = Repository._renew_lease(conn, job_id=job_id, claim_token=claim_token)  # noqa: SLF001
            # HARD precondition: a False here means FIX #6a refused the renewal, so NO row lock was
            # taken and the "latch" holds nothing. That is exactly how this hammer goes vacuously green
            # against UNFIXED code, so it must fail LOUD rather than proceed to an assertion.
            assert self.renewed, (
                "the latched heartbeat renewed nothing - FIX #6a refused it, so the interleave is "
                "VACUOUS (no lock is held and the reaper will never contend)"
            )
            self.lock_held.set()
            assert self.release.wait(timeout=_WAIT), "the hammer never released the heartbeat latch"
            conn.commit()
            return True
        finally:
            conn.rollback()
            conn.close()


def _stage_near_expiry(engine, job_id: int, hb_now) -> None:
    """Make the lease expired for any LATER transaction while still satisfying FIX #6a's
    `expires_at >= now()` for the heartbeat whose transaction timestamp is `hb_now`.

    `expire_lease_now()` cannot do this: it backdates a full second, so a heartbeat transaction opened
    afterwards is unconditionally refused and the interleave never happens. Asserts the staging landed
    in the required order rather than assuming it."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE neuro.work_leases SET expires_at = :hb + interval '1 microsecond' "
                " WHERE job_id = :j AND released_at IS NULL"
            ),
            {"hb": hb_now, "j": job_id},
        )
    with engine.connect() as conn:
        ok = conn.execute(
            text(
                "SELECT expires_at >= :hb AND expires_at < now() FROM neuro.work_leases "
                " WHERE job_id = :j AND released_at IS NULL"
            ),
            {"hb": hb_now, "j": job_id},
        ).scalar_one()
    assert ok, "near-expiry staging failed: need now_heartbeat <= expires_at < now_reaper"


def test_rt_near_expiry_renewal_vs_reaper_no_wedge(seeded):
    """WEDGE 1 (#6 residual): a heartbeat renewing a not-yet-expired lease commits while the reaper is
    mid-sweep. PRE-FIX the two-CTE reaper's `requeued` CTE sets the job 'queued' from the statement
    snapshot while its `released` CTE re-evaluates the now-renewed lease under EvalPlanQual and SKIPS
    the release -> 'queued' job + active lease -> un-claimable. POST-FIX one locked read decides BOTH
    halves, so the renewed lease is excluded and the job is simply not reaped."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    engine = repo.engine
    job = repo.enqueue(run_id=run_id, job_key="k#nearexp", job_role="capture")
    claim = repo.claim(actor_id=w)
    assert claim is not None and claim.job_id == job

    latched = _LatchedRepo(repo)
    renewer = LeaseRenewer(latched, job_id=job, claim_token=claim.claim_token)  # type: ignore[arg-type]
    reaper = ReaperLoop(repo, interval=1)
    try:
        renewer.start()
        assert latched.txn_open.wait(timeout=_WAIT), "the renewal thread never opened its transaction"
        # now_hb is pinned and the lease is still live -> stage expiry BETWEEN the two clocks.
        _stage_near_expiry(engine, job, latched.hb_now)
        latched.staged.set()
        assert latched.lock_held.wait(timeout=_WAIT), "the heartbeat never took the lease row lock"

        reaper.start()  # ACT-FIRST: the first sweep is immediate
        assert latched.hb_pid is not None
        _wait_until_blocked_by(engine, latched.hb_pid)  # the reaper is genuinely contending
        latched.release.set()
        _wait_for(lambda: reaper.sweeps >= 1, what="the reaper to complete a sweep")
    finally:
        latched.release.set()  # release EVERY latch before stopping, or the loops cannot be joined
        latched.staged.set()
        reaper.stop()
        renewer.stop()

    # the loops actually ran: a negative-only assertion is satisfied by a reaper that did nothing, and
    # ReaperLoop swallows sweep errors by design.
    assert reaper.sweeps >= 1 and reaper.last_error is None, f"reaper unhealthy: {reaper.last_error}"
    assert renewer.renewals >= 1 and not renewer.fenced, "the renewal must have SUCCEEDED for this race"
    assert latched.renewed is True

    _assert_no_wedge(engine, job)
    assert reaper.requeued == 0, "a lease renewed under the reaper's nose must NOT be reaped"
    assert repo.state_of(job) == "claimed", "the worker renewed in time and still owns the job"
    assert _active_leases(engine, job) == 1
    # PRE-FIX this RAISES IntegrityError (queued job + active lease -> work_leases_active_uq); POST-FIX
    # it is a quiet None, because a live worker still owns the job.
    assert repo.claim(actor_id=w) is None


def test_rt_reaper_wins_worker_is_fenced(seeded):
    """WEDGE 1, the OTHER ordering (a CONTRACT pin, not a RED-on-revert hammer - stated honestly): when
    the reaper gets there first, the worker must LEARN it was preempted, and the queue must be left in
    the state that makes the job re-claimable.

    ⚠ THE False IS OVER-DETERMINED, and saying otherwise would be a checkable falsehood: after the sweep
    the lease is released AND the job is 'queued', so `_renew_lease`'s qual fails on `released_at IS
    NULL` and on `j.state IN ('claimed','running')` alike. This pin does not attribute the False to one
    arm; it pins the OUTCOME - fenced worker, no active lease, a fresh token available. (The `now()`
    pinning below only keeps the #6a arm from being the trivially-first failure, so the assertion is
    about preemption rather than about expiry.)"""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    engine = repo.engine
    job = repo.enqueue(run_id=run_id, job_key="k#reaperwins", job_role="capture")
    claim = repo.claim(actor_id=w)
    assert claim is not None

    conn = engine.connect()
    try:
        _bound_connection(conn)
        hb_now = conn.execute(text("SELECT now()")).scalar_one()  # pin now_hb while the lease is live
        _stage_near_expiry(engine, job, hb_now)
        assert reap(repo) == 1  # the reaper wins outright: requeue + release, atomically
        # the worker's heartbeat arrives second. #6a passes (expires_at >= now_hb); it fails on
        # released_at, i.e. it learns it was preempted.
        assert repo.heartbeat(job_id=job, claim_token=claim.claim_token) is False
    finally:
        conn.rollback()
        conn.close()

    _assert_no_wedge(engine, job)
    assert repo.state_of(job) == "queued"
    assert _active_leases(engine, job) == 0
    fresh = repo.claim(actor_id=w)
    assert fresh is not None and fresh.claim_token != claim.claim_token


# --- WEDGE 2: complete()'s dependent-unblock write-skew (ADR-0046 s6) ------------------------------
def test_rt_two_parents_complete_concurrently_child_unblocks(seeded):
    """WEDGE 2 (ADR-0046 s6): two parents of one AND-gated child complete concurrently. PRE-FIX each
    evaluates the NOT-EXISTS unblock predicate against a snapshot excluding the other's uncommitted
    'succeeded', each SKIPS the child, and the skip path touches NO row - so there is no conflict, no
    EvalPlanQual, and the child is stranded 'blocked' forever (the only other blocked->queued writer is
    enqueue-time C20). POST-FIX both parents contend for the child's row lock, so the loser re-evaluates
    under a fresh snapshot that includes the winner's commit.

    HAND-DRIVEN INTERLEAVE, two sessions, explicit latches (E-13). Both connections drive the PRODUCTION
    helper, so the hammer never tests its own copy of the SQL.

    ⚠ THE PARENTS ARE ADVANCED TO 'claimed' FIRST, and that is not decoration. `complete()` opens its own
    transaction and commits, so the hammer cannot use it — it needs both parents' 'succeeded' writes held
    UNCOMMITTED on two connections. Since migration 0003 the jobs state ladder is enforced in-schema, and
    queued->succeeded is not a rung the app can take, so the bare UPDATE must start from a state the app
    really reaches. The advance is itself a legal edge (queued->claimed, Repository.claim's own), it is
    committed before the interleave begins, and it changes nothing about what this hammer proves — it
    makes the schedule one production can actually produce, which the earlier form was not."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    engine = repo.engine
    p1 = repo.enqueue(run_id=run_id, job_key="k#ws-p1", job_role="p1")
    p2 = repo.enqueue(run_id=run_id, job_key="k#ws-p2", job_role="p2")
    child = repo.enqueue(run_id=run_id, job_key="k#ws-c", job_role="c", depends_on=[p1, p2])
    assert repo.state_of(child) == "blocked"
    with engine.begin() as conn:  # queued -> claimed (legal), committed before the interleave
        conn.execute(
            text("UPDATE neuro.jobs SET state = 'claimed' WHERE job_id = ANY(:ids)"), {"ids": [p1, p2]}
        )

    conn_a, conn_b = engine.connect(), engine.connect()
    done_b = threading.Event()
    err_b: list[BaseException] = []
    try:
        _bound_connection(conn_a)
        _bound_connection(conn_b)
        a_pid = _backend_pid(conn_a)
        succeed = text("UPDATE neuro.jobs SET state = 'succeeded', updated_at = now() WHERE job_id = :j")
        conn_a.execute(succeed, {"j": p1})  # A: parent 1 succeeded, uncommitted
        conn_b.execute(succeed, {"j": p2})  # B: parent 2 succeeded, uncommitted

        # A runs the production unblock. Post-fix it LOCKS the child (and skips the update, since p2 is
        # not yet 'succeeded' from A's snapshot); pre-fix it locks nothing at all.
        Repository._unblock_ready_dependents(conn_a, p1)  # noqa: SLF001

        def _b() -> None:
            try:
                Repository._unblock_ready_dependents(conn_b, p2)  # noqa: SLF001
            except BaseException as exc:  # surfaced below; never swallowed
                err_b.append(exc)
            finally:
                done_b.set()

        t = threading.Thread(target=_b, name="write-skew-B", daemon=True)
        t.start()
        # Post-fix B BLOCKS on A's child lock; pre-fix it returns at once. Both are handled - which one
        # happens is exactly what distinguishes the two, and neither is a timing assumption.
        _wait_done_or_blocked(engine, done_b, a_pid)
        conn_a.commit()  # A's 'succeeded' becomes visible; B (if blocked) wakes
        assert done_b.wait(timeout=_WAIT), "the second parent's unblock never returned"
        t.join(timeout=_WAIT)
        assert not err_b, f"the second parent's unblock raised: {err_b[0]!r}"
        conn_b.commit()
    finally:
        conn_a.rollback()
        conn_a.close()
        conn_b.rollback()
        conn_b.close()

    assert repo.state_of(child) == "queued", (
        "WRITE-SKEW: both parents succeeded but the child is stranded - neither parent's unblock saw "
        "the other's commit and neither locked the child"
    )
    claimed = repo.claim(actor_id=w)
    assert claimed is not None and claimed.job_id == child  # genuinely runnable, not merely relabelled


def test_rt_and_gate_still_holds_under_the_child_lock(seeded):
    """The write-skew fix must not become an OR-gate: locking the child unconditionally must NOT unblock
    it while a sibling parent is still unfinished. (Precedent 20: the fix's own criterion needs a
    falsifying fixture, not only the happy case.)"""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    p1 = repo.enqueue(run_id=run_id, job_key="k#and-p1", job_role="p1")
    p2 = repo.enqueue(run_id=run_id, job_key="k#and-p2", job_role="p2")
    child = repo.enqueue(run_id=run_id, job_key="k#and-c", job_role="c", depends_on=[p1, p2])
    c1 = repo.claim(actor_id=w)
    assert c1 is not None and c1.job_id == p1
    assert repo.complete(job_id=p1, claim_token=c1.claim_token) is True
    assert repo.state_of(child) == "blocked", "one parent done is not enough"


# --- WEDGE 3: the C20 enqueue TOCTOU, and its cancel_cascade sibling -------------------------------
def _insert_dependent(conn, *, run_id: int, job_key: str, state: str, dep: int) -> int:
    """The child INSERT half of enqueue, hand-written. ⚠ DISCLOSED: this is NOT the contended step (the
    dep read is, and that IS production code). What keeps production from drifting away underneath this
    is the AST CONTAINMENT pin below, which requires the real dep read and the real INSERTs to live in
    one `engine.begin()` block."""
    job_id = conn.execute(
        text(
            "INSERT INTO neuro.jobs (job_key, run_id, job_role, queue, state) "
            "VALUES (:k, :r, 'c', 'default', :st) RETURNING job_id"
        ),
        {"k": job_key, "r": run_id, "st": state},
    ).scalar_one()
    conn.execute(
        text("INSERT INTO neuro.job_dependencies (job_id, depends_on) VALUES (:j, :d)"),
        {"j": job_id, "d": dep},
    )
    return job_id


def _toctou(seeded_parent, *, victim, job_key: str) -> int:
    """Drive the C20 TOCTOU: read the dep state (production code, which post-fix takes the row lock),
    let `victim` run against the parent in a thread, then insert the child and commit."""
    repo, run_id = seeded_parent["repo"], seeded_parent["run_id"]
    engine = repo.engine
    conn_e = engine.connect()
    done = threading.Event()
    err: list[BaseException] = []
    try:
        _bound_connection(conn_e)
        e_pid = _backend_pid(conn_e)
        parent = seeded_parent["parent"]
        state = Repository._initial_state_for_deps(conn_e, [parent])  # noqa: SLF001
        assert state == "blocked", f"the parent must still be in flight at read time, got {state!r}"

        def _run() -> None:
            try:
                victim()
            except BaseException as exc:
                err.append(exc)
            finally:
                done.set()

        t = threading.Thread(target=_run, name="toctou-victim", daemon=True)
        t.start()
        _wait_done_or_blocked(engine, done, e_pid)
        child = _insert_dependent(conn_e, run_id=run_id, job_key=job_key, state=state, dep=parent)
        conn_e.commit()
        assert done.wait(timeout=_WAIT), "the concurrent writer never returned"
        t.join(timeout=_WAIT)
        assert not err, f"the concurrent writer raised: {err[0]!r}"
        return child
    finally:
        conn_e.rollback()
        conn_e.close()


@pytest.fixture
def seeded_parent(seeded):
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    parent = repo.enqueue(run_id=run_id, job_key="k#toctou-p", job_role="p")
    claim = repo.claim(actor_id=w)
    assert claim is not None and claim.job_id == parent
    return {**seeded, "parent": parent, "claim": claim}


def test_rt_enqueue_toctou_vs_complete(seeded_parent):
    """WEDGE 3 (C20 TOCTOU): a job enqueued against a parent that COMPLETES between the dep read and the
    child's INSERT. PRE-FIX the unlocked read decides 'blocked' while `complete`'s unblock cannot see a
    child that does not exist yet -> the child commits 'blocked' with NO remaining blocked->queued
    writer. POST-FIX the dep lock makes `complete` wait for this transaction, so its unblock sees it."""
    repo, claim, parent = seeded_parent["repo"], seeded_parent["claim"], seeded_parent["parent"]
    child = _toctou(
        seeded_parent,
        victim=lambda: repo.complete(job_id=parent, claim_token=claim.claim_token),
        job_key="k#toctou-c",
    )
    assert repo.state_of(parent) == "succeeded"
    assert repo.state_of(child) == "queued", (
        "C20 TOCTOU: the child was enqueued 'blocked' against a parent that succeeded under it, and no "
        "blocked->queued writer remains"
    )


def test_rt_enqueue_toctou_vs_cancel_cascade(seeded_parent):
    """WEDGE 3's SIBLING - the vector the enqueue lock makes DETERMINISTIC rather than racy if left
    unclosed. `cancel_cascade` is a single statement whose recursive `deps` CTE is materialised under
    ONE snapshot, and EvalPlanQual re-checks only the TARGET row's qual, never the CTE - so a child
    enqueued concurrently is invisible to it and is stranded 'blocked' against a 'cancelled' parent
    forever. The seed lock in `cancel_cascade` makes the enqueue commit first."""
    repo, parent = seeded_parent["repo"], seeded_parent["parent"]
    child = _toctou(seeded_parent, victim=lambda: repo.cancel_cascade(parent), job_key="k#toctou-cc")
    assert repo.state_of(parent) == "cancelled"
    assert repo.state_of(child) != "blocked", (
        "a dependent of a CANCELLED parent is stranded: complete() needs 'succeeded' (unreachable) and "
        "enqueue-time C20 only runs for a brand-new job"
    )
    assert repo.state_of(child) == "cancelled"


def test_rt_enqueue_toctou_vs_fail_permanent(seeded_parent):
    """The same vector against `fail_permanent`, which needs NO seed lock of its own (its cascade is
    already a SECOND statement after the dead-letter UPDATE, so it takes a fresh snapshot for free).
    Pinned so that a later refactor fusing those two statements cannot reintroduce the strand."""
    repo, claim, parent = seeded_parent["repo"], seeded_parent["claim"], seeded_parent["parent"]
    child = _toctou(
        seeded_parent,
        victim=lambda: repo.fail_permanent(job_id=parent, claim_token=claim.claim_token, detail="x"),
        job_key="k#toctou-fp",
    )
    assert repo.state_of(parent) == "dead_letter"
    assert repo.state_of(child) == "cancelled"


# --- the structural pins the behavioural hammers cannot reach -------------------------------------
def _fn_sql(fn) -> str:
    """Every string CONSTANT in a function EXCLUDING its docstring, whitespace-normalised.

    AST, not a source grep: these docstrings quote 'FOR NO KEY UPDATE' and 'ORDER BY' at length, so a
    text scan over the source would be satisfied by PROSE - the exact failure the repo's AST-scan idiom
    exists to prevent."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)).body
    doc = body[0].value if body and isinstance(body[0], ast.Expr) else None
    return " ".join(
        " ".join(
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n is not doc
        ).split()
    )


# EVERY lock this pass takes, named EXPLICITLY per site. ⚠ A single "does an ordered lock appear
# anywhere in this function?" search is NOT enough and was MEASURED to be a hole: `reap_expired` takes
# TWO locks, so dropping the jobs-side one left the leases-side one for the search to find and the pin
# went GREEN on a mutation that inverts the whole jobs-before-leases order.
_REQUIRED_LOCKS = {
    "reap_expired": (
        Repository.reap_expired,
        ["ORDER BY j.job_id FOR NO KEY UPDATE", "ORDER BY l.job_id FOR NO KEY UPDATE"],
    ),
    "_initial_state_for_deps": (
        Repository._initial_state_for_deps,  # noqa: SLF001
        ["ORDER BY job_id FOR NO KEY UPDATE"],
    ),
    "_unblock_ready_dependents": (
        Repository._unblock_ready_dependents,  # noqa: SLF001
        ["ORDER BY j.job_id FOR NO KEY UPDATE"],
    ),
    "_cascade_cancel": (
        Repository._cascade_cancel,  # noqa: SLF001
        ["ORDER BY j.job_id FOR NO KEY UPDATE"],
    ),
    "cancel_cascade": (Repository.cancel_cascade, ["FOR NO KEY UPDATE"]),
}


@pytest.mark.parametrize("name", sorted(_REQUIRED_LOCKS))
def test_rt_every_jobs_locker_takes_an_ordered_no_key_lock(name):
    """ADR-0046 lock-order + lock-strength, pinned as a SOURCE-SHAPE belt (stated honestly: the deadlock
    it prevents is planner-dependent, so a behavioural reproduction would be flaky-by-design and is
    deliberately not written).

    TWO properties, both load-bearing and both learned from a REPRODUCED `DeadlockDetected` in the
    pre-build vet:
      * ORDER BY - every jobs-row acquirer must lock ASCENDING by job_id. `_cascade_cancel`'s shipped
        single UPDATE acquired locks in PLAN order, and an UPDATE cannot carry an ORDER BY; once
        enqueue's dep read took an ascending lock, that un-ordered acquisition closed a real
        jobs<->jobs cycle.
      * NO KEY UPDATE, never FOR UPDATE - `FOR UPDATE` is the only level that conflicts with the
        implicit RI `FOR KEY SHARE` taken by `INSERT INTO neuro.job_dependencies` on these same rows,
        and it is STRONGER than what the shipped plain UPDATEs already took.

    Each site is named individually: a function-wide "is there an ordered lock anywhere?" search let a
    dropped jobs-side lock hide behind a surviving leases-side one (MEASURED as a false green)."""
    fn, required = _REQUIRED_LOCKS[name]
    sql = _fn_sql(fn)
    for clause in required:
        assert clause in sql, f"{name} must take its lock as `... {clause}`"
    assert "FOR UPDATE" not in sql.replace("FOR NO KEY UPDATE", ""), (
        f"{name} must NOT use bare FOR UPDATE - it conflicts with the FK's implicit FOR KEY SHARE"
    )
    assert sql.count("FOR NO KEY UPDATE") == len(required), (
        f"{name} has {sql.count('FOR NO KEY UPDATE')} locks but {len(required)} are pinned by name - "
        f"a new lock site must be added to _REQUIRED_LOCKS, not left unpinned"
    )


def test_rt_claim_hot_path_keeps_skip_locked_untouched():
    """§A·56: `claim()`'s `FOR UPDATE SKIP LOCKED` hot path is explicitly OUT of scope for this pass."""
    assert "FOR UPDATE SKIP LOCKED" in _fn_sql(Repository.claim)


def test_rt_no_engine_wide_serializable():
    """§A·56 forbids engine-wide SERIALIZABLE (and ADR-0046 s6 records that an isolation bump does not
    close write-skew anyway). Nothing on the queue path may set an isolation level."""
    src = inspect.getsource(repo_mod)
    assert "SERIALIZABLE" not in src.upper()
    assert "isolation_level" not in src


def _call_names(node) -> set[str]:
    return {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
    }


def _the_single_begin_block(fn):
    """The one `with self.engine.begin() as conn:` block in `fn` (asserting it IS one - a second
    transaction in these methods is itself the drift this pin exists to catch)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    blocks = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.With) and any("begin" in _call_names(item.context_expr) for item in n.items)
    ]
    assert len(blocks) == 1, f"{fn.__name__} must open EXACTLY ONE transaction, found {len(blocks)}"
    return blocks[0]


@pytest.mark.parametrize(
    ("fn", "helper"),
    [
        (Repository.enqueue, "_initial_state_for_deps"),
        (Repository.complete, "_unblock_ready_dependents"),
    ],
    ids=["enqueue", "complete"],
)
def test_rt_contended_read_shares_the_writes_transaction(fn, helper):
    """★ THE ENVELOPE PIN - the property that is actually load-bearing, and which no behavioural probe
    in this file can reach.

    What closes the C20 TOCTOU is not the `FOR NO KEY UPDATE` token; it is that the row lock is still
    HELD when the child and its edges COMMIT. Hoisting the helper into its own `engine.begin()` - a
    plausible-looking connection-hygiene refactor - releases the lock early and FULLY restores the
    TOCTOU, while a call-existence pin, the hammers (which supply their own transaction envelope) and
    the whole suite all stay green. So the pin is CONTAINMENT, not existence: the helper call must be a
    descendant of the SAME single `engine.begin()` block, and must be handed that block's own
    connection variable."""
    block = _the_single_begin_block(fn)
    assert helper in _call_names(block), f"{fn.__name__} must call {helper} INSIDE its transaction"
    var = block.items[0].optional_vars
    assert isinstance(var, ast.Name), f"{fn.__name__}'s transaction must bind a connection name"
    call = next(
        n
        for n in ast.walk(block)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == helper
    )
    assert call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == var.id, (
        f"{fn.__name__} must pass its OWN transaction's connection ({var.id}) to {helper}"
    )


def test_rt_ast_matcher_is_not_satisfied_by_prose():
    """The containment pin's own matcher, pinned: it must see a real call and NOT a docstring mention.
    An AST helper that silently matched nothing would make every pin above vacuous."""
    assert "_initial_state_for_deps" in _call_names(_the_single_begin_block(Repository.enqueue))
    assert "reap_expired" not in _call_names(_the_single_begin_block(Repository.enqueue))


def test_rt_heartbeat_delegates_to_the_renewal_statement():
    """The near-expiry hammer drives `_renew_lease` directly, so `heartbeat` must still BE that
    statement - otherwise production could drift away from the SQL the hammer proves."""
    assert "_renew_lease" in _call_names(_the_single_begin_block(Repository.heartbeat))
    assert "UPDATE" not in _fn_sql(Repository.heartbeat), "heartbeat must hold no SQL of its own"


def test_rt_unblock_is_two_statements_not_one_fused():
    """The SECOND load-bearing detail of wedge 2: the lock and the unblock must stay separate
    statements. All CTEs of one statement share ONE snapshot, so a fused data-modifying CTE would again
    evaluate the NOT-EXISTS against a pre-commit view of the sibling parent and the write-skew would
    survive. The shipped code WAS one statement, so 'collapse these two' reads as a strict improvement."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Repository._unblock_ready_dependents)))  # noqa: SLF001
    executes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
    ]
    assert len(executes) == 2, f"the child lock and the unblock must be TWO statements, found {len(executes)}"


def test_rt_unblock_lock_carries_no_readiness_predicate():
    """★ THE FIRST load-bearing detail of wedge 2: the lock is taken on the CANDIDATE set, never the
    READY set. Move the NOT-EXISTS into the locking SELECT and the losing parent selects zero rows,
    locks NOTHING, and the write-skew survives with a FOR NO KEY UPDATE sitting right there.

    ⚠ THIS PIN ONCE READ THE DOCSTRING. `ast.walk` is BREADTH-FIRST, so a function's docstring Constant
    is yielded BEFORE the SQL Constants nested inside its `conn.execute(text(...))` calls - and this
    method's docstring happens to contain 'FOR NO KEY UPDATE' while not containing 'UPDATE neuro.jobs',
    so a `next(...)` over `ast.walk` selected the PROSE and the pin could never fail, for any edit.
    Caught by the post-build vet, reproduced standalone. It now takes the FIRST `conn.execute`'s SQL
    positionally and asserts that what it got is really a SELECT - so a selector that ever drifts off
    the statement reddens instead of going quietly blind."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Repository._unblock_ready_dependents)))  # noqa: SLF001
    executes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
    ]
    lock_sql = " ".join(
        " ".join(
            n.value for n in ast.walk(executes[0]) if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ).split()
    )
    assert lock_sql.upper().startswith("SELECT"), (
        f"the pin must be reading the LOCKING STATEMENT, not prose - it got {lock_sql[:60]!r}"
    )
    assert "FOR NO KEY UPDATE" in lock_sql
    assert "NOT EXISTS" not in lock_sql.upper(), (
        "the locking SELECT must NOT carry the readiness predicate - a zero-row SELECT locks nothing"
    )


# --- the reaper's own contract under the new 4-step form ------------------------------------------
def test_rt_reaper_is_idempotent_under_concurrent_sweeps(seeded):
    """Two sweeps over the same expired lease must requeue it exactly ONCE (the second sees it already
    'queued' via EvalPlanQual and declines) - no double requeue, no double expiry_count bump."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#double", job_role="capture")
    assert repo.claim(actor_id=w) is not None
    repo.expire_lease_now(job)
    results: list[int] = []
    barrier = threading.Barrier(2)

    def _sweep() -> None:
        barrier.wait(timeout=_WAIT)
        results.append(reap(repo))

    threads = [threading.Thread(target=_sweep, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_WAIT)
        assert not t.is_alive()
    assert sorted(results) == [0, 1], f"exactly one sweep may requeue the job, got {results}"
    assert repo.get_job(job)["expiry_count"] == 1
    assert repo.state_of(job) == "queued"
    _assert_no_wedge(repo.engine, job)


def test_rt_reaper_empty_sweep_is_cheap_and_quiet(seeded):
    """The most frequent path: nothing expired. It must return 0 without touching anything (and without
    binding an empty array into `= ANY(...)`)."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#empty", job_role="capture")
    assert repo.claim(actor_id=w) is not None
    assert reap(repo) == 0
    assert repo.state_of(job) == "claimed"
    assert repo.get_job(job)["expiry_count"] == 0
    assert _active_leases(repo.engine, job) == 1


def test_rt_migration_set_is_the_three_of_record():
    """The migration set of record, pinned by IDENTITY so a stray or misnamed revision reddens.

    ⚠ This was `assert len(migrations) == 2` with the docstring "ADR-0046's locking arm carries NO schema
    change (the 0003 trigger migration is a later unit)". Session C2 IS that later unit, so that clause is
    DISCHARGED and leaving it asserting 2 would ship a checkable falsehood (precedent 15). The pin is not
    deleted and not bumped to a bare count — it is the only migration-set assertion in the tree, and an
    exact list is what keeps its value once the count stops being the point.

    The OTHER clause of the original docstring stands UNCHANGED and is load-bearing for 0003 itself:
    parity stays 0001-scoped (test_migration_ddl_parity upgrades to `0001_initial_schema`, not head), so
    0003 is invisible to it and `tests/reference/phase3-ddl.sql` stays pristine."""
    import pathlib  # noqa: PLC0415

    root = pathlib.Path(inspect.getfile(repo_mod)).parent.parent.parent.parent
    migrations = sorted(p.name for p in (root / "migrations" / "versions").glob("*.py"))
    assert migrations == [
        "0001_initial_schema.py",
        "0002_external_records_stamp.py",
        "0003_state_transition_triggers.py",
    ], f"unexpected migration set: {migrations}"
