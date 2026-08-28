"""Operator-facing copy for the persistent-block escalation arms — the ONE home for the triage procedure
and the ONE message shape (alert-copy repair, 2026-08-28; the log:271 registered finding).

WHY THIS MODULE EXISTS. Both escalation arms (governance/escalation.py, governance/lake_escalation.py) each
carried their own copy of one hardcoded remedy -- "check the desktop sshd endpoint (Get-Service sshd)" -- and
across THREE recorded multi-day blocks that remedy was right ONCE:

  * 2026-07-13..17 (~4d): the desktop OpenSSH service was UNREGISTERED.               remedy CORRECT
  * 2026-07-24..30 (~6d): a VPN held the desktop default route, so SYN-ACKs left      remedy WRONG
    via the VPN and every port timed out while Tailscale read healthy.                (sshd was healthy)
  * 2026-08-14..27 (13d): a tailnet policy edit tagged the VM `tag:research`, for     remedy WRONG
    which no grant existed either way, so the peer left the netmap.                   (sshd was healthy)

THE REPAIR IS NOT A BETTER GUESS -- IT IS TO STOP GUESSING. A confidently wrong action is worse than none,
because it routes the operator to the one component that is not broken. That is the render-honesty rule (the
forbidden Y is not only a VERDICT but a CAUSE) applied to an alert. So the copy states the consequence, says
plainly that the cause is NOT established, and hands over a DISCRIMINATING PROCEDURE.

*** THE SIGNAL IS A DISJUNCTION, AND THE TRIAGE IS NOT. *** The vet's sharpest finding, and the reason step 0
exists. `status='blocked'` is set by ~13 distinct producers, only three or four of which are desktop
reachability: cloud-repo recency across EVERY configured pgbackrest repo (backup_driver `_repo_freshness_from_info`
-- so a stalled repo2/Azure cadence fires THIS alert), `pgbackrest verify`, the sftp push, delta-verify, the
manifest write, a driver raise -- plus, on the lake arm, a source-backend read that hangs against the 300 s
Azure bound (lake_mirror, whose own comment says a hang "-> status 'blocked' -> the daily escalation fires"),
and, on both, two gate-origin flips (`stale_after` drift and staleness) the probe never touches. An earlier
draft of this repair asserted that the cloud repos were "a SEPARATE signal this alert does not read"; that was
MEASURED FALSE against backup_driver's step 0, and shipping it would have replaced one wrong cause-claim with
another. A line keyed on a disjunction may not assert which disjunct fired.

*** AND THE DRIVERS ALREADY RECORD THE ANSWER. *** Every probe-origin block writes a step-labelled reason to
`probe_reports` (`recency:` / `sftp` / `source get` / `delta-verify` / `manifest write`), rendered TODAY by the
shipped `neuro probe report`. Saying "cause not established" while a recorded reason sits one verb away would
be its own dishonesty, so step 0 sends the operator there first and scopes steps 1-3 to the case the reason
names the push. ⚠ A gate-origin flip records NO probe_reports row, and `neuro probe report` does not render
`system_health.detail` -- so step 0 can come back empty. Registered follow-on, not closed here.

WHAT AN ARM SUPPLIES AND WHAT IT INHERITS. `ESCALATION_ARMS` is keyed by the same `system_health` health_key
constants as `governance/durability.py::DURABILITY_ROWS`, and a keyset test pins the containment, so a NEW
durability arm that wants an escalation alert is a PURE APPEND here and cannot ship with hand-written copy that
has quietly drifted from its siblings. That mirrors `governance/probe_registry.py::PROBE_RUNNERS`, whose keyset
test pins the same relation for producers.

⚠ THE CONTAINMENT IS `<=`, NOT `==`, DELIBERATELY. `wal_lag` is a provisioned durability row with NO escalation
arm (its interim policy is a boolean archiver check, not a staleness block), so equality would force a
fabricated arm for it. Every escalating arm must be a provisioned row; not every provisioned row escalates.

⚠ WHAT THIS MODULE DOES NOT DEDUPLICATE, STATED SO NOBODY READS MORE INTO IT. The two arms' evaluator BODIES
are 15 of 17 lines byte-identical (the SQL, the four-branch chain, `days`), and their two CLI verbs likewise.
This module extracts the COPY, not the ARM: a third arm still copies an evaluator, a CLI verb and a systemd
pair; only the message stops being hand-written. Registered as a follow-on, not closed here.

⚠ THE MESSAGE IS ~2.5 KB, UP FROM ~0.35 KB, AND THAT IS A DELIBERATE TRADE RATHER THAN AN OVERSIGHT. The
alternative to a long procedure is a short wrong remedy, which is what this unit exists to remove; the front
of the message still carries what an operator sees first (what is blocked, for how long, last good), and step
0 ends the triage for most causes. REGISTERED FOLLOW-ON, deliberately not built here because the charter
scopes this unit to COPY and not machinery: move the procedure behind a `neuro probe triage` verb and let the
alert point at it, which would cut the push to a few hundred bytes without losing a step.

⚠ THE ALERT SURFACE IS SIX SITES, NOT TWO. `notify(` is called from `cli/probe.py` (escalate, lake-escalate,
disk) and from `governance/health.py::_flip_and_notify` (three gate callsites). The health ones alert on the
SAME `backup_freshness` row but each names a cause it actually MEASURED (drift / staleness) and offers no
remedy, so none is an instance of this defect and none is changed here. Enumerated so the next arm's author
starts from six rather than rediscovering four.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db.lanes import ConfigurationError
from .freshness import BACKUP_FRESHNESS_KEY
from .lake_freshness import LAKE_MIRROR_FRESHNESS_KEY

if TYPE_CHECKING:
    import datetime as _dt


#: The discriminating procedure for an arm whose off-cloud leg is the shared sftp push
#: (`governance/sftp_transport.py` -- the ONE off-cloud transport). Every step is here because a REAL incident
#: needed it, and each names the DISCRIMINATOR rather than only the command:
#:
#:  (0) is FIRST because the drivers already record a step-labelled reason and steps 1-3 are scoped to one of
#:      the ~13 things that set this row. Without it the procedure looks exhaustive while covering one family
#:      -- the same defect as the hardcoded remedy, one layer out.
#:  (1) splits "the VM never reached us" from "it reached us and failed". ⚠ Only the CONTRAPOSITIVE is sound:
#:      no event ⇒ it never got here. An event does NOT entail "the network is fine" -- a desktop that sleeps
#:      mid-stream, or a stall inside the 3600 s sftp bound, authenticates and then dies. The copy says
#:      DOWNSTREAM OF AUTHENTICATION, never "not the network".
#:  (2) ABSENT-vs-listed-as-`offline` is the whole finding of the 13-day block: a down peer is still LISTED.
#:      The admin-console cross-read is what converted that from inference to measurement, so it is named.
#:      "Measure before you repair" is here because `tailscale up` re-authenticates and ERASES the reason.
#:  (3) names the SIGNATURE and refuses to name a cause: timeout-vs-refused separates the ROUTING layer from a
#:      listening host, and at least three causes produce 124 identically. An earlier draft named one vendor
#:      product as the cause of a signature the record says three causes share -- the defect, reproduced.
#:
#: ⚠ NO TAILNET ADDRESS IS HARDCODED. An address is an expiring fact whose home is `tailscale status`, which
#: step 2 runs ON EACH END and which prints the other end's address for step 3.
#:
#: ⚠ TWO DELIBERATE WORDING CONSTRAINTS ON THE RENDERED STRING — both pinned by tests, so do not "tidy" them:
#:  * IT IS PURE ASCII. This box's stdout is cp1252 and a non-ASCII `print()` raises there; the message is
#:    also echoed by the CLI and pushed to a phone. Nothing in the alert needs a glyph, so it uses none.
#:    (Docstrings and comments here are source-only and never printed, so they keep their em-dashes.)
#:  * IT DOES NOT CONTAIN THE TOKEN `sshd`. Step 3's contrast originally read "...and sshd is not listening",
#:    which is true but collides with the probe asserting the OLD REMEDY is gone. "nothing is listening on
#:    :22" says the same thing about the observable, and keeps that probe a blunt, unmissable one.
OFF_CLOUD_MIRROR_TRIAGE = (
    "CAUSE NOT ESTABLISHED -- past blocks have had different causes, so this alert names none. "
    "ACTION -- TRIAGE IN ORDER: "
    "(0) READ THE RECORDED REASON FIRST: `neuro probe report` prints the driver's own step-labelled reason "
    "(recency: / pgbackrest verify / sftp / source get / delta-verify / manifest write). Steps 1-3 apply ONLY "
    "if that reason names the off-cloud PUSH; a recency or verify reason is a CLOUD-repo problem and a "
    "gate-origin flip (stale_after drift/staleness) records no reason at all. "
    "(1) On the DESKTOP: Get-WinEvent -LogName 'OpenSSH/Operational' -MaxEvents 400 | Where-Object "
    "{ $_.Message -match 'neuromirror' } -- NO event in the run window (backup ~:31-:36, lake ~:00-:04) means "
    "the VM never reached us. An 'Accepted publickey for neuromirror' means it DID, and the fault is "
    "DOWNSTREAM of authentication (a desktop that slept, or a mid-transfer stall) -- not that the network is "
    "fine. Ignore 172.26.192.1 '[preauth]' lines: WSL2 NAT, not the VM. Use the connection TIMES, not day "
    "counts. "
    "(2) Run `tailscale status` ON EACH END -- it also prints the other end's 100.x address for step 3. A peer "
    "ABSENT from the list is an ACL/netmap exclusion, NOT a down host: a down peer is still LISTED, as "
    "'offline'. Confirm at login.tailscale.com/admin/machines -- 'Connected' there while absent locally IS the "
    "exclusion -- then read admin/logs for the policy edit. MEASURE BEFORE YOU REPAIR: `tailscale up` erases "
    "the reason it left. NOTE: an exclusion ALSO blocks Tailscale SSH, so being unable to ssh the VM is "
    "confirmation, not a second fault. "
    "(3) Listed on both ends? From either end: timeout 5 bash -c 'cat </dev/null >/dev/tcp/<peer-100.x>/22'; "
    "echo $? -- 124 (timeout) puts the fault at the ROUTING layer, where 'refused' would mean you reached the "
    "host and nothing is listening on :22. At least three causes produce 124 identically (a netmap exclusion, "
    "a VPN holding the desktop default route, a plain outage), so it narrows the LAYER, not the cause."
)


@dataclass(frozen=True)
class BlockAlertArm:
    """The per-arm copy an escalation supplies; everything else it inherits from `compose_block_alert`.

    `consequence` states what the operator LOSES while the block stands, and nothing about WHY."""

    headline: str
    no_confirmed_label: str
    consequence: str
    triage: str
    rerun_unit: str


#: The arm registry. Keyed by the SAME health_key constants as `durability.py::DURABILITY_ROWS` so a new
#: escalating arm is a pure append; the keyset test pins `frozenset(ESCALATION_ARMS) <= DURABILITY_KEYS`.
ESCALATION_ARMS: dict[str, BlockAlertArm] = {
    BACKUP_FRESHNESS_KEY: BlockAlertArm(
        headline="OFF-CLOUD BACKUP MIRROR BLOCKED",
        # ⚠ "no CONFIRMED off-cloud backup for ~Nd", not "blocked ~Nd". `measured_at` advances ONLY on a
        # verified success, so the age is time-since-last-confirmed-good, an UPPER BOUND on the block. The
        # difference is not academic: a seeded-but-never-probed row is born blocked at the epoch and would
        # render "blocked ~20,700d".
        no_confirmed_label="off-cloud backup",
        # ⚠ "NOT confirmed updating", not the former "DEGRADED to cloud-only". The evaluator reads ONE row and
        # can establish that no success was recorded -- never that the cloud copies are fine. "You now have
        # only the cloud copies" is a claim about what REMAINS, and it is not merely unsupported: a stalled
        # cloud repo is itself one of the triggers, so the old copy could be flatly false exactly when it
        # mattered most. It also stops naming the repos -- a third is ruled and coming, and this path cannot
        # enumerate them anyway (that inventory lives in the pgbackrest conf, root-only because it holds the
        # account-wide Azure key).
        consequence=(
            "The independent desktop-NVMe copy (ADR-0014, the restore-drill source) is NOT confirmed "
            "updating. NOTE: this row folds SEVERAL checks into one boolean -- cloud-repo recency across "
            "EVERY configured pgbackrest repo, repo1 integrity, and the off-cloud push -- so it does NOT "
            "tell you which failed, and a cloud-repo cadence stall lands here too."
        ),
        triage=OFF_CLOUD_MIRROR_TRIAGE,
        rerun_unit="neuro-backup.service",
    ),
    LAKE_MIRROR_FRESHNESS_KEY: BlockAlertArm(
        headline="BLOB-LAKE MIRROR BLOCKED",
        no_confirmed_label="lake mirror",
        # ⚠ The former copy called this "the HARD GATE before the first non-recomputable capture" -- one clause
        # from the truth that it does not gate, and a reader in a prior session concluded from a blocked lake
        # row that the ADR-0020 gate had closed. Both halves corrected: the key is deliberately absent from
        # `health.GATE_CONSULTED_KEYS`, and the write-time preflight that WOULD gate is registered-and-unbuilt
        # (no capture-path consumer of this row exists in src/). ⚠ The claim is scoped to the GATE rather than
        # to "blocking a write", because the registered preflight would block writes with both existing pins
        # still green.
        consequence=(
            "The independent desktop-NVMe copy of the artifact lake (ADR-0014, the audit mirror) is NOT "
            "confirmed updating. NOTE: this row folds SEVERAL checks into one boolean -- the source read "
            "(which is a CLOUD fetch), the off-cloud push, delta-verify and the manifest write -- so it does "
            "NOT tell you which failed. NOTIFY-ONLY: this signal is not consulted by the ADR-0020 "
            "durability gate."
        ),
        triage=OFF_CLOUD_MIRROR_TRIAGE,
        rerun_unit="neuro-lake-mirror.service",
    ),
}


def require_triage(triage: str) -> str:
    """Refuse an absent/blank triage (the D1 no-default-clean-member idiom; `importer/ingress.py`).

    ⚠ READ THE SCOPE, because this is ONE leg of a three-leg precedent and only one leg is here. It makes a
    new arm's triage choice EXPLICIT; it cannot make it CORRECT. Nothing stops a future arm on a DIFFERENT
    off-cloud leg passing `OFF_CLOUD_MIRROR_TRIAGE` -- the only triage this module exports -- and thereby
    shipping the exact defect this unit repaired, one arm over. That residual is DISCIPLINE, and the registry
    docstring is where a new arm meets it. Recorded rather than overclaimed.

    Raises ConfigurationError, which both CLI verbs already handle, so no shipped exit code moves."""
    if not triage or not triage.strip():
        raise ConfigurationError(
            "escalation copy requires a triage procedure and none was supplied -- refusing to emit an alert "
            "that states a consequence with no way to act on it (fail closed). An arm whose off-cloud leg is "
            "NOT the shared sftp push needs its OWN triage; do not reuse OFF_CLOUD_MIRROR_TRIAGE by default."
        )
    return triage


def compose_block_alert(*, arm: BlockAlertArm, days: int, measured_at: _dt.datetime) -> str:
    """Render one persistent-block alert. PURE -- no I/O, no DB, no clock.

    The shape is fixed here so every arm reads the same way: what is blocked, how long since it was last
    CONFIRMED good (with the timestamp, both up front where an operator sees them), what that costs, that the
    cause is unknown, and how to tell the causes apart. The age and the last-good timestamp are the two
    elements the record says worked, so they are preserved -- but relabelled, since `measured_at` advances only
    on success and the old wording read the age as the block's duration."""
    return (
        f"neuromancer {arm.headline} -- no confirmed {arm.no_confirmed_label} for ~{days}d "
        f"(last good: {measured_at}). {arm.consequence} {require_triage(arm.triage)} "
        f"THEN re-run {arm.rerun_unit}."
    )
