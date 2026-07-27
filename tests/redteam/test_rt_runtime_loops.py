"""Red-team: the B-4 renewal thread + the B-4b reaper-cadence loop (workers/runtime.py).

Unit C1a of the ADR-0046 bundle: the two loops `workers/runtime.py`'s own contract promised but never
shipped, plus the fail-closed renewal-cadence pin they run on. These loops exist to be REAL concurrent
targets for the C1b locking hammers (`test_rt_locking.py`) - the plan records that building the
locking fix without them would leave the race probes with no concurrent target and therefore no
provable RED-on-revert.

⚠ NO SLEEPS PACE ANYTHING HERE: waits are bounded polls that raise LOUD on timeout, and every loop a
test starts is stopped and joined inside that test - a surviving daemon thread holds an open
transaction that would block the NEXT test's `_truncate_all` TRUNCATE on ACCESS EXCLUSIVE.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import threading
import time

import pytest

from neuromancer_llm.db import repository as repo_mod
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.db.repository import resolve_renew_interval
from neuromancer_llm.workers.runtime import LeaseRenewer, ReaperLoop

pytestmark = pytest.mark.pg

_WAIT = 15.0  # generous per-step bound so a stuck loop surfaces as a LOUD failure, never a hang


def _wait_for(predicate, *, what: str, timeout: float = _WAIT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _call_names(node) -> set[str]:
    return {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
    }


# --- the loops themselves --------------------------------------------------------------------------
def test_rt_lease_renewer_exits_on_fencing(seeded):
    """B-4: a renewal thread whose job has been taken away must EXIT, not spin forever against a job it
    no longer owns. Cancelling releases the lease, so the next heartbeat returns False -> fenced."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#fenced", job_role="capture")
    claim = repo.claim(actor_id=w)
    assert claim is not None
    assert repo.cancel_cascade(job) == 1  # preempted: lease released, job cancelled

    renewer = LeaseRenewer(repo, job_id=job, claim_token=claim.claim_token, interval=1)
    try:
        renewer.start()
        thread = renewer._thread  # noqa: SLF001 - captured BEFORE stop(), see below
        assert thread is not None
        _wait_for(lambda: renewer.fenced, what="the renewer to observe fencing")
        # ⚠ THE TERMINATION ASSERTION MUST BE MADE BEFORE stop() IS CALLED. `renewer._thread is None`
        # after stop() is unconditionally true (stop() clears it on the success path), so asserting it
        # there proved nothing at all - it passed for a loop that only exited because we asked it to.
        # Joining the CAPTURED thread with stop() never signalled is the real proof that the loop ended
        # ON ITS OWN, on the fencing exit. Caught by the post-build vet.
        thread.join(timeout=_WAIT)
        assert not thread.is_alive(), "the renewal loop must EXIT on fencing, not merely stop ticking"
    finally:
        renewer.stop()
    assert renewer.fenced is True
    assert renewer.renewals == 0


def test_rt_reaper_loop_uses_the_shipped_cadence(seeded):
    """B-4b's DEFAULT cadence must be the pinned REAPER_SECONDS. Every hammer constructs the loop with a
    short explicit interval so it is observable inside a bounded test, which means nothing else in the
    suite ever executes the default - so without this the shipped cadence is asserted NOWHERE and a
    mutation changing it reddens nothing. Construct-only; never started."""
    assert ReaperLoop(seeded["repo"]).interval == repo_mod.REAPER_SECONDS == 60


def test_rt_a_stopped_loop_can_be_restarted(seeded):
    """`stop()` clears `_thread`, so `start()` is reachable again afterwards. Without clearing the stop
    event, a restarted loop would tick exactly ONCE and die SILENTLY - a foot-gun with no symptom.

    ⚠ THE ASSERTION MUST DEMAND MORE THAN ONE TICK. `_run` is act-first, so a restarted loop with a set
    stop event still performs one full sweep before exiting - so `sweeps >= 2` (or any 'did it sweep at
    all' check) passes for the broken loop too. Only requiring the loop to KEEP ticking distinguishes
    them. My first version of this test made exactly that mistake, and the mutation matrix caught it
    only once it started asserting WHICH test reddened rather than how many did."""
    loop = ReaperLoop(seeded["repo"], interval=0)
    loop.start()
    loop.stop()
    before = loop.sweeps
    try:
        loop.start()
        _wait_for(lambda: loop.sweeps >= before + 3, what="the RESTARTED loop to KEEP sweeping")
    finally:
        loop.stop()


def test_rt_reaper_loop_actually_reaps(seeded):
    """B-4b positive control: the loop must really sweep. Paired with the negative wedge assertion
    above, which a reaper that never runs would satisfy vacuously."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#loopreap", job_role="capture")
    assert repo.claim(actor_id=w) is not None
    repo.expire_lease_now(job)

    loop = ReaperLoop(repo, interval=1)
    try:
        loop.start()  # ACT-FIRST: the first sweep is immediate, not one interval away
        _wait_for(lambda: loop.requeued >= 1, what="the reaper loop to requeue the expired job")
    finally:
        loop.stop()
    assert loop.sweeps >= 1 and loop.last_error is None
    assert loop.requeued == 1
    assert repo.state_of(job) == "queued"
    assert repo.get_job(job)["expiry_count"] == 1


def test_rt_reaper_loop_survives_a_bad_sweep(seeded):
    """A reaper that dies on one transient error leaves every expired lease un-reaped - strictly worse
    than a logged error. It must record and continue."""
    repo = seeded["repo"]
    calls = {"n": 0}

    def _flaky() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return 0

    loop = ReaperLoop(repo, interval=1)
    loop._repo = type("R", (), {"reap_expired": staticmethod(_flaky)})()  # noqa: SLF001
    try:
        loop.start()
        _wait_for(lambda: loop.sweeps >= 1, what="the reaper to recover from a bad sweep")
    finally:
        loop.stop()
    assert loop.last_error is not None and "transient" in loop.last_error
    assert loop.sweeps >= 1, "the loop must keep sweeping after an error"


# --- B-4: the renewal-cadence pin -----------------------------------------------------------------
# --- B-4: the renewal-cadence pin ------------------------------------------------------------------
def test_rt_renew_seconds_is_derived_from_the_lease(monkeypatch):
    """The shipped ADR-0039 cadence is UNCHANGED (no churn) and is now DERIVED, not an independent
    literal that a later LEASE_SECONDS edit would leave behind."""
    assert repo_mod.RENEW_SECONDS == 40
    assert repo_mod.LEASE_SECONDS == 120
    assert repo_mod.RENEW_SECONDS == repo_mod.LEASE_SECONDS // repo_mod.LEASE_RENEW_MIN_ATTEMPTS


def test_rt_renew_seconds_derivation_is_structural():
    """A SOURCE-SHAPE belt (labelled as such): re-hardcoding `RENEW_SECONDS = 40` preserves every value
    every other pin can observe, so only the module-level assignment's SHAPE can catch it."""
    tree = ast.parse(inspect.getsource(repo_mod))
    assign = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "RENEW_SECONDS" for t in n.targets)
    )
    assert isinstance(assign.value, ast.BinOp) and isinstance(assign.value.op, ast.FloorDiv), (
        "RENEW_SECONDS must be DERIVED from LEASE_SECONDS, never re-hardcoded as a literal"
    )
    assert isinstance(assign.value.left, ast.Name) and assign.value.left.id == "LEASE_SECONDS"
    assert isinstance(assign.value.right, ast.Name)
    assert assign.value.right.id == "LEASE_RENEW_MIN_ATTEMPTS"


def test_rt_resolve_renew_interval_accepts_the_shipped_cadence():
    assert resolve_renew_interval() == 40


@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("RENEW_SECONDS", 60),  # 60*3 > 120: one lost heartbeat expires a live worker's lease
        ("RENEW_SECONDS", 0),
        ("RENEW_SECONDS", -1),
        ("LEASE_SECONDS", 100),  # a lease shortened without re-deriving the cadence
    ],
)
def test_rt_resolve_renew_interval_fails_closed(monkeypatch, attr, value):
    """FAIL CLOSED ON THE DOCUMENTED PROPERTY. A merely `RENEW < LEASE` check would admit 60/120, under
    which a SINGLE lost heartbeat expires a live worker's lease - handing a running job to the reaper
    and manufacturing exactly the near-expiry contention this session closes."""
    monkeypatch.setattr(repo_mod, attr, value)
    with pytest.raises(ConfigurationError):
        resolve_renew_interval()


def test_rt_lease_renewer_uses_the_resolved_cadence(seeded):
    """The guard is only real if it is LIVE on the renewal path - a resolver nobody calls is prose."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    repo.enqueue(run_id=run_id, job_key="k#cadence", job_role="capture")
    claim = repo.claim(actor_id=w)
    assert claim is not None
    renewer = LeaseRenewer(repo, job_id=claim.job_id, claim_token=claim.claim_token)
    assert renewer.interval == resolve_renew_interval() == 40
    assert "resolve_renew_interval" in _call_names(
        ast.parse(textwrap.dedent(inspect.getsource(LeaseRenewer.__init__)))
    )


# --- the loop contract -----------------------------------------------------------------------------
def test_rt_loops_act_before_they_wait(seeded):
    """ACT-FIRST, THEN WAIT - pinned BEHAVIOURALLY, not by source shape.

    Wait-first would put the first heartbeat a full RENEW_SECONDS after the claim and the first sweep a
    full REAPER_SECONDS after start, so a VM restart would leave every already-expired lease un-reaped
    for a minute. The interval here is an HOUR: only an act-first loop can possibly sweep inside this
    test's bound, so a wait-first `_run` times out LOUDLY. (`stop()` wakes the loop out of its hour-long
    wait immediately, so this costs nothing in wall-clock.)

    ⚠ A short interval CANNOT pin this - with `interval=1` a wait-first loop still sweeps at t=1s and
    the test passes. My first version made that mistake and the matrix scored it RED anyway, because it
    was counting failures rather than checking WHICH test failed."""
    repo, run_id, w = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#actfirst", job_role="capture")
    assert repo.claim(actor_id=w) is not None
    repo.expire_lease_now(job)

    loop = ReaperLoop(repo, interval=3600)
    try:
        loop.start()
        _wait_for(lambda: loop.sweeps >= 1, what="the FIRST sweep to land at once, not an interval later")
    finally:
        loop.stop()
    assert loop.requeued == 1
    assert repo.state_of(job) == "queued"


def test_rt_stop_raises_rather_than_leaking_a_live_loop(seeded):
    """A bounded join that merely RETURNS converts a hang into a SILENT leak, and a leaked loop holds an
    open transaction that blocks the next test's TRUNCATE with no deadlock detection to surface it."""
    repo = seeded["repo"]
    stuck = threading.Event()
    loop = ReaperLoop(repo, interval=1)
    loop._tick = lambda: stuck.wait(timeout=30)  # type: ignore[method-assign]  # noqa: SLF001
    try:
        loop.start()
        with pytest.raises(RuntimeError, match="did not stop"):
            loop.stop(timeout=0.2)
    finally:
        stuck.set()


def test_rt_runtime_declares_its_public_surface():
    """Precedent 15 - a module must not ship a checkable falsehood about itself, and its `__all__` must
    name what it now provides."""
    from neuromancer_llm.workers import runtime  # noqa: PLC0415

    for name in ("LeaseRenewer", "ReaperLoop", "resolve_renew_interval", "reap"):
        assert name in runtime.__all__
    doc = runtime.__doc__ or ""
    assert "job-execution run loop" in doc, "the module must name the ONE thing still deferred (C3)"
    assert "always-on VM" in doc, "the §A·63 reaper topology constraint must be stated"


def test_rt_no_shutdown_hook_anywhere_in_src():
    """ADR-0039 hard-kill honesty, made CHECKABLE: the runtime claims there is deliberately no
    atexit/signal/graceful-shutdown path, because the substrate kills with no signal (GraceTime=0). If
    one is ever added, that docstring becomes a falsehood and this reddens."""
    import pathlib  # noqa: PLC0415

    root = pathlib.Path(inspect.getfile(repo_mod)).parent.parent
    hits = [
        p.name
        for p in root.rglob("*.py")
        if re.search(
            r"^\s*import\s+(atexit|signal)\b|^\s*from\s+(atexit|signal)\b",
            p.read_text(encoding="utf-8"),
            re.M,
        )
    ]
    assert not hits, f"a shutdown hook would falsify workers/runtime.py's hard-kill docstring: {hits}"
