"""The repo3 (Backblaze B2) recency PRODUCER — the notify-only third-copy signal (§A·72, 2026-08-28).

The parallel of governance/probes.py (backup_freshness) and governance/lake_mirror.py
(lake_mirror_freshness), and deliberately the SMALLEST of the three: it runs ONE read —
`pgbackrest info --output=json` — and asks one question, "does repo3 carry a FULL backup younger than the
pinned bound?". No verify, no transfer, no credential of its own.

WHY IT IS A SEPARATE PROBE RATHER THAN A BRANCH OF THE BACKUP DRIVER. The backup driver's step 0 is the
GATING recency check and its basis is now the pin (provisioning_invariants.GATE_BASIS_REPOS), which
deliberately excludes repo3 — that exclusion is what makes repo3 notify-only. Asking repo3's question inside
the gating check would re-couple exactly what the pin decoupled. Two questions, two callers, ONE parser:
both use backup_driver.newest_full_per_repo, so the info JSON is read one way.

FAIL-CLOSED SHAPE (the probes.py contract, carried verbatim): the row must already be seeded; the outcome is
PERSISTED BEFORE the raise (bump + a probe_reports audit row through the shared _record_probe_report), so an
operator following step (0) of the alert's triage has a recorded reason to read. Every failure detail is
STEP-LABELLED — `recency:` or `info-read:` — because the triage discriminates on that label and a reason that
does not say which step failed cannot discriminate anything.

⚠ THE FIRST STATE THIS ROW IS EVER IN RECORDS NO REASON AT ALL. `neuro db durability seed` inserts it born
blocked at the epoch with detail "seeded; awaiting first repo3 backup" and writes NO probe_reports row —
row presence is the provisioning proof, not a probe result. So between the seed and the first probe run,
step (0) of the triage comes back EMPTY. That is a real hole in the triage's first step, it is named in the
alert copy rather than left for an operator to hit, and the deploy runbook orders the beats so the first
probe runs before the escalation timer is ever armed.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from .backup_driver import INFO_TIMEOUT_S, InfoUnreadableError, newest_full_per_repo
from .probes import _record_probe_report
from .repo3_freshness import REPO3_FRESHNESS_KEY, REPO3_REPO_KEY, resolve_repo3_stale_after
from .sftp_transport import CommandRunner, run_subprocess

if TYPE_CHECKING:
    from sqlalchemy import Engine


class Repo3ProbeError(RuntimeError):
    """The repo3 recency probe failed or could not be recorded — fail LOUD. Raised AFTER the failure is
    persisted to system_health + probe_reports (persist-before-raise).

    ⚠ The systemd unit runs this behind an ignore-failure `ExecStartPost=-` prefix, so this raise does NOT
    fail `neuro-backup.service` — that is the §A·72 requirement (an unproven arm must not block the gating
    freshness bump), and it is exactly why the daily escalation exists to carry the news instead."""


@dataclass(frozen=True)
class Repo3Outcome:
    """The driver's verdict: `ok` iff repo3 carries a FULL backup younger than the pinned bound. `detail` is
    the step-labelled human summary recorded into system_health.detail and the probe report."""

    ok: bool
    detail: str


# The injectable seam: () -> Repo3Outcome. It takes no destination — repo3 has no off-cloud leg and no
# operator-supplied coordinate, which is why the registry runner can default-construct it.
Repo3Driver = Callable[[], Repo3Outcome]


def make_repo3_recency_driver(
    *,
    stanza: str = "neuro",
    repo: int = REPO3_REPO_KEY,
    runner: CommandRunner = run_subprocess,
    now: _dt.datetime | None = None,
) -> Repo3Driver:
    """Build the repo3 recency driver. Every coordinate is a COMMITTED default (the stanza of record, the
    pinned repo index, the real subprocess runner) — the same shape make_pgbackrest_mirror_driver already
    uses for `runner`. `now` is threaded for deterministic tests."""

    def driver() -> Repo3Outcome:
        moment = now or _dt.datetime.now(_dt.UTC)
        bound = resolve_repo3_stale_after()  # fail closed on an unpinned cadence/margin, before the read
        info = runner(["pgbackrest", f"--stanza={stanza}", "info", "--output=json"], timeout_s=INFO_TIMEOUT_S)
        if info.returncode != 0:
            # ⚠ stderr is TRUNCATED and never widened: this text reaches system_health.detail, probe_reports
            # and (through the alert) a phone. pgbackrest error output can name conf options, and the conf
            # holds the account-wide cloud keys — the provisioning_invariants.py redaction contract.
            return Repo3Outcome(
                ok=False,
                detail=(
                    f"info-read: pgbackrest info failed (rc={info.returncode}): {info.stderr.strip()[:200]}"
                ),
            )
        try:
            repo_keys, newest_full = newest_full_per_repo(info.stdout, stanza=stanza)
        except InfoUnreadableError as exc:
            return Repo3Outcome(
                ok=False, detail=f"info-read: could not read pgbackrest info json ({exc}) — fail closed"
            )
        if repo not in set(repo_keys):
            # The coordinate the key is NAMED for, confirmed rather than trusted. This is the state between
            # a seed and the conf edit, and it is also what a repo RENUMBER looks like.
            return Repo3Outcome(
                ok=False,
                detail=(
                    f"info-read: repo{repo} is NOT configured in pgbackrest (configured: "
                    f"{sorted(repo_keys)}) — fail closed. Either the conf edit has not landed yet, or the "
                    "repositories were renumbered and this arm's health_key no longer names the right one."
                ),
            )
        if repo not in newest_full:
            return Repo3Outcome(ok=False, detail=f"recency: repo{repo} has NO full backup (fail closed)")
        stop, label = newest_full[repo]
        age = moment - _dt.datetime.fromtimestamp(stop, tz=_dt.UTC)
        if age > bound:
            return Repo3Outcome(
                ok=False,
                detail=(
                    f"recency: repo{repo}'s newest full backup {label} is {age.days}d old > the pinned "
                    f"{bound.days}d (BASE_BACKUP_INTERVAL + margin) — the repo3 backup step has stalled "
                    "(fail closed)"
                ),
            )
        return Repo3Outcome(ok=True, detail=f"repo{repo}:{label}")

    return driver


def _repo3_row_present(engine: Engine) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM neuro.system_health WHERE health_key = :k"),
                {"k": REPO3_FRESHNESS_KEY},
            ).first()
            is not None
        )


def _bump_repo3(engine: Engine, *, ok: bool, detail: str) -> None:
    """Bump the signal as neuro_writer (grants.sql:48 UPDATE(status,detail,measured_at); no stale_after) —
    the probes.py::_bump_freshness contract, per-arm. On success advance measured_at=now(); on failure leave
    it, so staleness accrues from the last CONFIRMED-good repo3 backup. Raises on rowcount 0: a bump on an
    unseeded/vanished row is fail-loud, never a silent no-op.

    ⚠ THE BLOCKED BRANCH MUST NOT TOUCH `measured_at`, AND THE REASON IS THIS ARM'S ONLY ALERT. The daily
    escalation asks `(now() - measured_at) > onset` in SQL. If a failed probe advanced measured_at, a repo3
    failing on EVERY 2-day cycle would reset that clock every run, the 4-day onset could never be crossed,
    and `neuro probe repo3-escalate` would return None forever -- silently, with the row still reading
    'blocked' and every existing test still green. repo3 has no per-cycle OnFailure ping of its own (the
    ignore-failure `-` prefix), so that is the whole alert gone. The lake arm pins this property for the
    same reason; so does this one now."""
    if ok:
        sql = "UPDATE neuro.system_health SET status='ok', measured_at=now(), detail=:d WHERE health_key=:k"
    else:
        sql = "UPDATE neuro.system_health SET status='blocked', detail=:d WHERE health_key=:k"
    with engine.begin() as conn:
        result = conn.execute(text(sql), {"d": detail, "k": REPO3_FRESHNESS_KEY})
    if result.rowcount == 0:
        raise Repo3ProbeError(
            "repo3_freshness row absent at bump time (rowcount 0) — refusing a silent no-op. Seed it "
            "(`neuro db durability seed`, registrar/admin) before running the probe."
        )


def run_repo3_probe(
    engine: Engine, *, repo3_driver: Repo3Driver, actor_id: int | None = None
) -> Repo3Outcome:
    """Read repo3's base-backup recency and record it into the notify-only signal. `engine` must be a
    VERIFIED engine. Order mirrors run_backup_probe exactly:

      1. the row MUST already be seeded — abort loudly BEFORE the driver runs, so a real read is never
         orphaned with nowhere to record it (the writer cannot self-seed);
      2. run the injected read;
      3. record the outcome (bump + probe_reports), THEN raise on failure (persist-before-raise).
    """
    if not _repo3_row_present(engine):
        raise Repo3ProbeError(
            "system_health['repo3_freshness'] is not seeded — run `neuro db durability seed` "
            "(registrar/admin) before this probe. Refusing to read a signal that cannot be recorded."
        )
    try:
        outcome = repo3_driver()
    except Exception as exc:  # noqa: BLE001 — record the failure, then re-raise loud (never a silent pass)
        # ⚠ NO STEP LABEL HERE, DELIBERATELY. `recency:` and `info-read:` are claims about WHICH step
        # failed, and the triage discriminates on them — but a driver that RAISED may have died before any
        # read happened (an unpinned cadence resolver raises first of all). Stamping `info-read:` on it
        # would assert a step that never ran: a line keyed on a disjunction naming which disjunct fired.
        _bump_repo3(engine, ok=False, detail=f"driver raised (step not established): {exc!r}")
        _record_probe_report(
            engine,
            probe_key=REPO3_FRESHNESS_KEY,
            status="blocked",
            # ⚠ THE SAME TEXT AS system_health.detail, deliberately. Step (0) sends the operator to
            # `neuro probe report`, which renders BOTH the row detail and the probe_reports row; if the two
            # disagreed about which step failed, the step that exists to END the triage would instead start
            # an argument with itself.
            report_text=f"driver raised (step not established): {exc!r}",
            actor_id=actor_id,
        )
        raise Repo3ProbeError("repo3 recency probe failed: the driver raised") from exc

    _bump_repo3(engine, ok=outcome.ok, detail=outcome.detail)
    _record_probe_report(
        engine,
        probe_key=REPO3_FRESHNESS_KEY,
        status="ok" if outcome.ok else "blocked",
        report_text=outcome.detail,
        actor_id=actor_id,
    )
    if not outcome.ok:
        raise Repo3ProbeError(f"repo3 recency probe recorded BLOCKED: {outcome.detail}")
    return outcome
