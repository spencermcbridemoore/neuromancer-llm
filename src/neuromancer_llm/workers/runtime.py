"""The one worker runtime — lease/heartbeat/reaper policy owner (ADR-0039); checkpoint-first.

The runtime owns the lease/reaper *policy*: the pinned constants + the fail-closed renewal-cadence
resolution point (`resolve_renew_interval`, homed with the lease SQL in `db/repository.py`), the
on-demand `reap()` sweep, and the two loops that drive them — **B-4 `LeaseRenewer`** (periodic
heartbeat against a held claim) and **B-4b `ReaperLoop`** (periodic `reap_expired`). Heartbeats are
runtime-owned, not caller-owned.

STILL DEFERRED, deliberately: the **job-execution run loop** that claims and dispatches captured work
(B-5 / P-6). Neither loop here claims, checkpoints, completes or dispatches anything — `LeaseRenewer`
is handed a claim someone else already took.

★ TOPOLOGY (§A·63, ARCC bring-up 2026-07-19) — a DEPLOYMENT constraint, enforced by no code here:
`ReaperLoop` runs on the **always-on VM**, never solely on an ephemeral/preemptible node. SLURM's
`PreemptMode=CANCEL` + `GraceTime=0` mean a reaper co-located with the workers it reaps for dies in the
same kill that created the expired leases, and nothing requeues them.
"""

from __future__ import annotations

import threading

from ..db.repository import (
    LEASE_RENEW_MIN_ATTEMPTS,
    LEASE_SECONDS,
    REAPER_SECONDS,
    RENEW_SECONDS,
    Repository,
    resolve_renew_interval,
)

__all__ = [
    "LEASE_SECONDS",
    "RENEW_SECONDS",
    "REAPER_SECONDS",
    "LEASE_RENEW_MIN_ATTEMPTS",
    "resolve_renew_interval",
    "reap",
    "LeaseRenewer",
    "ReaperLoop",
]


def reap(repo: Repository) -> int:
    """Run one reaper sweep: requeue jobs whose lease expired (fencing the dead claimant). Returns the
    number of jobs requeued. The runtime calls this on the REAPER_SECONDS cadence."""
    return repo.reap_expired()


class _PeriodicLoop:
    """Shared machinery for the two runtime loops: a daemon thread that runs `_tick()` on a fixed
    cadence until stopped.

    ★ ACT-FIRST, THEN WAIT (`work(); if stop.wait(interval): break`). Wait-first would mean the first
    heartbeat lands one full RENEW_SECONDS after the claim and the first sweep one full REAPER_SECONDS
    after start — so a VM restart would leave every already-expired lease un-reaped for a minute, and a
    worker would go a third of its lease with no renewal on record. Acting first is both the correct
    production posture and what makes these loops observable inside a bounded test.

    ★ ADR-0039 HARD-KILL HONESTY: there is deliberately **no** atexit hook, no signal handler and no
    graceful-shutdown path anywhere in this module (checkable: `src/` contains no `atexit`/`signal`
    import at all). The substrate kills with no signal and `GraceTime=0` — vast.ai and SLURM
    `PreemptMode=CANCEL` alike — so on a real preemption *nothing of ours runs*. The lease simply
    expires and `ReaperLoop` requeues the job. That IS the contract; a shutdown hook here would be a
    checkable falsehood about a substrate that never grants grace. `stop()` exists for the orderly
    in-process case (and for tests), not as a preemption story.

    The thread is a daemon so a stuck loop can never wedge interpreter exit — but a caller that leaks
    one leaks an open transaction, so prefer the context-manager form, which always stops and joins."""

    _NAME = "loop"

    def __init__(self, *, interval: int) -> None:
        self.interval = interval
        self.ticks = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tick(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _run(self) -> None:
        while True:
            try:
                self._tick()
            except _LoopExit:
                return
            if self._stop.wait(timeout=self.interval):
                return

    def start(self) -> _PeriodicLoop:
        if self._thread is not None:
            raise RuntimeError(f"{self._NAME} already started")
        self._stop.clear()  # a restarted loop must not inherit a set stop event and die after one tick
        self._thread = threading.Thread(target=self._run, name=self._NAME, daemon=True)
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 10.0) -> None:
        """Signal the loop and join it. Raises if the thread outlives the join — a bounded join that
        merely RETURNS converts a hang into a silent leak, and a leaked loop holds an open transaction
        that blocks the next `TRUNCATE` with no deadlock detection to surface it.

        `_thread` is cleared only AFTER a successful join, so a stop() that RAISES leaves this object
        still owning the live thread and `start()`'s already-started guard keeps working — otherwise a
        failed stop could be followed by a second concurrent loop on the same object."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise RuntimeError(
                f"{self._NAME} did not stop within {timeout}s — it is still holding whatever it was "
                f"blocked on (last_error={self.last_error!r})"
            )
        self._thread = None

    def __enter__(self) -> _PeriodicLoop:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


class _LoopExit(Exception):
    """Internal: a tick asking its loop to terminate (never raised out of the runtime)."""


class LeaseRenewer(_PeriodicLoop):
    """B-4: the runtime-owned renewal thread — periodic `heartbeat` against an ALREADY-HELD claim.

    The interval is `resolve_renew_interval()`, i.e. derived from `LEASE_SECONDS` and validated
    fail-closed, never a caller-supplied number: a renewal cadence that does not fit
    `LEASE_RENEW_MIN_ATTEMPTS` attempts inside one lease TTL hands a live worker's job to the reaper.

    ★ IT STOPS ON FENCING. `heartbeat` returning False means this worker no longer owns the job, and
    every one of the four ways it can return False is TERMINAL for this claim token (verified against
    `Repository.heartbeat`): no matching lease row (wrong/stale token); the lease released (by
    complete / fail_permanent / cascade-cancel / the reaper); the lease already expired (FIX #6a — and
    `expires_at` is only ever advanced by a heartbeat that was permitted, so an expired lease never
    becomes un-expired); or the job leaving ('claimed','running') — whose only route back is `claim`,
    which mints a FRESH token. So the loop exits and records `fenced=True`; it never spins against a
    job it has lost. ⚠ A DB/network fault RAISES rather than returning False, so it is a different
    axis: it is recorded in `last_error` and the loop keeps trying, because a transient blip must not
    silently abandon a job the worker still owns.

    ⚠ REGISTERED FOR STAGE 2, not built: when the transient-`failed` producer lands (see
    `Repository.fail_permanent`'s own DEFER note), a `failed` job may hold a live lease under a worker
    that still owns it — at that point a False from the job-state arm is no longer terminal and this
    exit rule must be revisited."""

    _NAME = "lease-renewer"

    def __init__(self, repo: Repository, *, job_id: int, claim_token, interval: int | None = None):
        super().__init__(interval=resolve_renew_interval() if interval is None else interval)
        self._repo = repo
        self.job_id = job_id
        self.claim_token = claim_token
        self.renewals = 0
        self.fenced = False

    def _tick(self) -> None:
        try:
            ok = self._repo.heartbeat(job_id=self.job_id, claim_token=self.claim_token)
        except Exception as exc:  # transient DB/network fault: record and retry (see the class docstring)
            self.last_error = repr(exc)
            return
        self.ticks += 1
        if ok:
            self.renewals += 1
            return
        self.fenced = True
        raise _LoopExit


class ReaperLoop(_PeriodicLoop):
    """B-4b: the periodic `reap_expired` sweep, on the `REAPER_SECONDS` cadence.

    A sweep error is recorded in `last_error` and the loop CONTINUES: a reaper that dies on one
    transient DB error leaves every expired lease un-reaped, which is strictly worse than a logged
    error. ⚠ The corollary for callers and tests: `requeued == 0` is ambiguous on its own — assert
    `sweeps >= 1 and last_error is None` before reading anything into a quiet reaper.

    ★ Deployment topology (§A·63) is in the module docstring: always-on VM, never solely an ephemeral
    node. Nothing here enforces it — it is a deploy-time constraint, stated so it is not forgotten."""

    _NAME = "reaper-loop"

    def __init__(self, repo: Repository, *, interval: int = REAPER_SECONDS):
        super().__init__(interval=interval)
        self._repo = repo
        self.sweeps = 0
        self.requeued = 0

    def _tick(self) -> None:
        try:
            n = reap(self._repo)
        except Exception as exc:  # never let one bad sweep end the reaper (see the class docstring)
            self.last_error = repr(exc)
            return
        self.ticks += 1
        self.sweeps += 1
        self.requeued += n
