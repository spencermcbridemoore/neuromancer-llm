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
reachability: cloud-repo recency across every repo in the pinned GATE BASIS (backup_driver
`_repo_freshness_from_info` -- so a stalled repo2/Azure cadence fires THIS alert; ⚠ since 2026-08-28 the
basis is `provisioning_invariants.GATE_BASIS_REPOS`, NOT "every configured repo", so a configured-but-
unpinned repo3 does NOT), `pgbackrest verify`, the sftp push, delta-verify, the
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
names the push. ~~⚠ A gate-origin flip records NO probe_reports row, and `neuro probe report` does not render
`system_health.detail` -- so step 0 can come back empty. Registered follow-on, not closed here.~~
**→ CLOSED 2026-08-28 by the repo3 unit:** `neuro probe report` now renders `detail` on every system_health
row (and takes `--key`), so the two states that write no probe_reports row -- a gate-origin drift/staleness
flip, and a freshly-seeded born-blocked row -- are both visible to step 0. The step 0 wording in both triages
depends on that, so do not remove it without rewording them.

WHAT AN ARM SUPPLIES AND WHAT IT INHERITS. `ESCALATION_ARMS` is keyed by the same `system_health` health_key
constants as `governance/durability.py::DURABILITY_ROWS`, and a keyset test pins the containment, so a NEW
durability arm that wants an escalation alert is a PURE APPEND here and cannot ship with hand-written copy that
has quietly drifted from its siblings. That mirrors `governance/probe_registry.py::PROBE_RUNNERS`, whose keyset
test pins the same relation for producers.

⚠ THE CONTAINMENT IS `<=`, NOT `==`, DELIBERATELY. `wal_lag` is a provisioned durability row with NO escalation
arm (its interim policy is a boolean archiver check, not a staleness block), so equality would force a
fabricated arm for it. Every escalating arm must be a provisioned row; not every provisioned row escalates.

⚠ WHAT THIS MODULE DOES NOT DEDUPLICATE, STATED SO NOBODY READS MORE INTO IT. This module extracts the COPY,
not the ARM. ~~The two arms' evaluator BODIES are 15 of 17 lines byte-identical (the SQL, the four-branch
chain, `days`) ... a third arm still copies an evaluator, a CLI verb and a systemd pair.~~ **→ HALF CLOSED
2026-08-28 by the repo3 unit** (⚠ and the struck figure was wrong: counted, the bodies were 20 lines each
with 3 differing — see `block_escalation.py`)**:** the duplicated EVALUATOR is now extracted to
`governance/block_escalation.py`, on the `sftp_transport.py` precedent (a third copy is where the house rule
says extract). What a new arm still copies is a three-line delegate, a CLI verb and a systemd pair; the
evaluator and the message are both shared. Collapsing the CLI verbs remains OPEN and is the registered
`neuro probe triage` follow-on -- a different surface.

⚠ THE MESSAGES ARE LONG (the backup arm ~2.5 KB from ~0.35 KB; the repo3 arm, added 2026-08-28, is the
longest at ~3.2 KB), AND THAT IS A DELIBERATE TRADE RATHER THAN AN OVERSIGHT. The
alternative to a long procedure is a short wrong remedy, which is what this unit exists to remove; the front
of the message still carries what an operator sees first (what is blocked, for how long, last good), and step
0 ends the triage for most causes. REGISTERED FOLLOW-ON, deliberately not built here because the charter
scopes this unit to COPY and not machinery: move the procedure behind a `neuro probe triage` verb and let the
alert point at it, which would cut the push to a few hundred bytes without losing a step.

⚠ THE ALERT SURFACE IS SEVEN SITES, NOT TWO (was six before the repo3 arm). `notify(` is called from
`cli/probe.py` (escalate, lake-escalate, **repo3-escalate**, disk) and from
`governance/health.py::_flip_and_notify` (three gate callsites). The health ones alert on the SAME
`backup_freshness` row but each names a cause it actually MEASURED (drift / staleness) and offers no remedy,
so none is an instance of this defect and none is changed here. Enumerated so the next arm's author starts
from seven rather than rediscovering four.

⚠ THERE ARE NOW TWO TRIAGES, SCOPED BY FAILING LEG. `OFF_CLOUD_MIRROR_TRIAGE` is the shared desktop-sftp
procedure (backup + lake); `OBJECT_STORE_REPO_TRIAGE` is the HTTPS object-store procedure (repo3). A new arm
picks the one matching its leg or writes a third -- `require_triage` forces the choice to be explicit, and
the test suite pins the arm-to-triage mapping by IDENTITY so an arm cannot drift onto the wrong leg quietly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db.lanes import ConfigurationError
from .freshness import BACKUP_FRESHNESS_KEY
from .lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
from .repo3_freshness import REPO3_FRESHNESS_KEY

if TYPE_CHECKING:
    import datetime as _dt


#: The discriminating procedure for an arm whose off-cloud leg is the shared sftp push
#: (`governance/sftp_transport.py` -- the ONE off-cloud transport). Every step is here because a REAL incident
#: needed it, and each names the DISCRIMINATOR rather than only the command:
#:
#:  (0) is FIRST because the answer is already recorded in TWO places one verb away, and steps 1-3 are
#:      scoped to one of the ~13 things that set this row. Without it the procedure looks exhaustive while
#:      covering one family -- the same defect as the hardcoded remedy, one layer out. ⚠ BOTH reads are named
#:      deliberately: the row's own `detail=` is the ONLY trace of the two states that write no probe_reports
#:      row (a gate-origin drift/staleness flip; a never-probed seed), while `--key` is what makes the
#:      probe_reports half reachable past the 15-minute WAL-archiver probe. Both were added by the repo3 unit
#:      (2026-08-28) and this wording depends on them -- the module docstring says so, and the first draft of
#:      that closure updated the docstring and the repo3 triage while leaving THIS one asserting the residual
#:      was still open. A closure applied to the new surfaces and not the old ones is not a closure.
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
    "(0) READ THE RECORDED REASON FIRST: run `neuro probe report`. Every durability row prints a `detail=` "
    "field, and THIS row's line is the first thing to read, because two states write NO probe_reports row at "
    "all and their reason exists ONLY there: a gate-origin flip (stale_after drift or staleness), and a "
    "freshly-seeded row that has never been probed. Then re-run it as `neuro probe report --key <the "
    "health_key at the start of that line>` for the driver's own step-labelled reason (recency: / pgbackrest "
    "verify / sftp / source get / delta-verify / manifest write). The flag is not optional in practice: the "
    "WAL-archiver probe writes a probe_reports row every 15 minutes, which crowds the default listing back "
    "to about the last two hours, while this arm's reason is DAYS old by construction. Steps 1-3 apply ONLY "
    "if that reason names the off-cloud PUSH; a recency or verify reason is a CLOUD-repo problem. "
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


#: The discriminating procedure for an arm whose failing leg is an OBJECT-STORE repository reached over
#: HTTPS (pgbackrest `repo-type=s3`/`azure`/`gcs`) rather than the shared sftp push. repo3 (Backblaze B2 via
#: its S3-compatible endpoint, §A·72) is its first consumer.
#:
#: ⚠ IT IS SCOPED BY FAILING LEG, NOT BY ARM, AND THAT IS DELIBERATE. `OFF_CLOUD_MIRROR_TRIAGE` above is the
#: desktop-sftp procedure; not one of its four steps applies to a bucket. Handing this arm that procedure
#: would be the 2026-08-28 alert-copy defect reproduced one arm over, which is exactly what `require_triage`
#: can make EXPLICIT and cannot make CORRECT. Scoping by leg also states the honest thing: a future repo4 on
#: another object store SHOULD share this text, and a second arm on the sftp leg should share that one.
#:
#: WHY EACH STEP IS HERE, and what makes it discriminating rather than merely a command:
#:  (0) is FIRST because the probe records a STEP-LABELLED reason and steps 1-4 are scoped by which label it
#:      carries. ⚠ It also names the ONE state where step 0 comes back with nothing useful -- the born
#:      fail-closed seed, which writes no probe_reports row at all -- because a procedure that is silent
#:      about its own empty case sends the operator to B2 over a provisioning step.
#:      ⚠⚠ AND `recency:` IS ITSELF A DISJUNCTION OF TWO, WHICH THIS COPY GOT WRONG ONCE. A first draft read
#:      "a `recency:` reason means repo3 HAS a full backup but an old one" -- but `repo3_probe` emits that
#:      same label for "has NO full backup", so the copy asserted a third copy EXISTS in the one state
#:      where it does not. That is this module's own law -- a line keyed on a disjunction may not assert
#:      which disjunct fired -- violated by this module one level down: the LABEL was treated as atomic
#:      when it is a family. The copy now names both members and how to tell them apart, and a probe pins
#:      the mapping mechanically rather than trusting the prose.
#:      ⚠ THE `--key` FLAG IS LOAD-BEARING, NOT DECORATION: the WAL archiver probe writes a probe_reports
#:      row every 15 minutes, so at `neuro probe report`'s default limit of 10 an operator sees roughly the
#:      last two hours, all of it wal_lag, and never reaches a repo3 reason that is days old by construction.
#:  (1) exists because this arm's own notify-only mechanism hides it: the repo3 systemd lines carry the
#:      ignore-failure `-` prefix (the §A·72 requirement), so `neuro-backup.service` reports SUCCESS while
#:      repo3 fails. An operator who reads a green unit as a green repo3 stops looking.
#:  (2) orders SPECIFIC BEFORE GENERAL and asserts on the error TEXT, because the families collide on the
#:      status code alone: a compliance-lock refusal and a key-scope fault are both 403. Naming a
#:      discriminator that does not discriminate is the defect this module exists to prevent, so the
#:      credential family is stated as the RESIDUAL and is explicitly said not to narrow further.
#:  (3) is the failure this alert would otherwise mis-route entirely: a spend cap rejects WRITES while READS
#:      keep working, so every health-looking read succeeds while every backup fails.
#:  (4) re-establishes what the PROVEN repos did, so a repo3 alert is not read as a general backup failure.
#:
#: ⚠ NO EXPIRING FACT IS HARDCODED: no bucket name, no endpoint, no region, no price, no version, no
#: retention number. The conf that holds them is root-only and holds the account keys, so the copy names the
#: provider console and pgbackrest's own output as the homes of those values -- the redaction contract that
#: governs `provisioning_invariants.py`, carried into operator copy.
#: ⚠ PURE ASCII, for the same three renderers as the sibling triage (a cp1252 console, typer.secho, a phone).
OBJECT_STORE_REPO_TRIAGE = (
    "CAUSE NOT ESTABLISHED -- this row folds several checks into one boolean and this alert names none of "
    "them. ACTION -- TRIAGE IN ORDER: "
    "(0) READ THE RECORDED REASON FIRST: `neuro probe report --key repo3_freshness` prints the probe's own "
    "step-labelled reason. A `recency:` reason names WHICH of two states, and they are not the same "
    "problem: one containing 'NO full backup' means repo3 has NEVER carried one -- the conf edit landed but "
    "no repo3 backup has ever succeeded, so there is no third copy yet; one naming a backup label and an "
    "age means repo3 HAS a full backup but an old one, so the repo3 backup step has been failing since. An "
    "`info-read:` reason means pgbackrest could not report at all, which is a config, credential or "
    "reachability fault and NOT a cadence one. If instead the detail still reads "
    "'seeded; awaiting first repo3 backup', this row has never been probed successfully at all -- that is "
    "the born fail-closed provisioning state, not an outage, and the fix is to run neuro-backup.service "
    "once, not to touch the bucket. "
    "(1) A GREEN neuro-backup.service DOES NOT MEAN repo3 SUCCEEDED -- the repo3 lines carry systemd's "
    "ignore-failure prefix, so the unit reports success while repo3 fails. Read pgbackrest's own words: "
    "sudo journalctl -u neuro-backup.service --since '-8 days' | grep -iE 'repo3|P00 +ERROR' "
    "(2) SEPARATE THE FAMILIES BY THE ERROR TEXT, SPECIFIC FIRST, because they collide on the status code "
    "alone. A 403 whose text NAMES object lock or retention is the compliance lock refusing a delete that "
    "`expire` attempted: that is a retention-SIZING fault, not an outage and not a credential. A 401, or a "
    "403 that does NOT name object lock or retention, is a credential or key-scope fault -- and an "
    "application key deleted or rotated in the console reads exactly like a wrong secret, so the message "
    "alone does not narrow this further. A 400 naming the endpoint or host, or a DNS resolution failure, is "
    "an endpoint or region fault. A connection timeout carrying NO HTTP status at all is reachability. "
    "(3) CHECK THE SPENDING CAP -- it is the one failure this alert would otherwise mis-route, because an "
    "account at its cap rejects WRITES while READS keep succeeding, so `pgbackrest info` can look entirely "
    "healthy while every repo3 backup fails. The cap and the current usage live in the provider console. "
    "(4) ESTABLISH WHAT THE PROVEN REPOS DID, so this is not read as a general backup failure: "
    "sudo -u postgres pgbackrest --stanza=neuro info -- each repository mints its OWN backup label seconds "
    "apart, so widen the window rather than reading the last entry."
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
        # ⚠ CORRECTED 2026-08-28 BY THE repo3 UNIT, AND THE CORRECTION WAS FORCED BY MEASUREMENT, NOT TASTE.
        # This clause used to read "cloud-repo recency across EVERY configured pgbackrest repo". That was
        # true while the driver derived its repo set from the pgbackrest info JSON; it became FALSE the
        # moment the gate basis became a pin (provisioning_invariants.GATE_BASIS_REPOS), because a
        # configured-but-unpinned repo3 is read and REPORTED without setting this row. An operator reading
        # the old sentence would believe every configured repo's recency was folded into this boolean and
        # would never go look for the separate repo3 row -- the render-honesty defect this module exists to
        # end, committed by this module about itself. The clause still does NOT enumerate the repos (that
        # inventory lives in the root-only conf beside the account keys); it names the BASIS instead, which
        # is a repo constant a reader can actually go and read.
        consequence=(
            "The independent desktop-NVMe copy (ADR-0014, the restore-drill source) is NOT confirmed "
            "updating. NOTE: this row folds SEVERAL checks into one boolean -- cloud-repo recency across "
            "every repo in the pinned GATE BASIS, repo1 integrity, and the off-cloud push -- so it does NOT "
            "tell you which failed, and a cloud-repo cadence stall lands here too. A configured repo "
            "OUTSIDE the basis is reported in this row's detail but does NOT set it."
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
    REPO3_FRESHNESS_KEY: BlockAlertArm(
        headline="IMMUTABLE THIRD COPY (pgbackrest repo3) BLOCKED",
        no_confirmed_label="repo3 backup",
        # ⚠ THE NOTIFY-ONLY SENTENCE IS NOT THE LAKE ARM'S, AND THE DIFFERENCE IS THE WHOLE POINT. For the
        # lake row, "not consulted by the gate" and "cannot block your write" coincide -- no capture-path
        # consumer of that row exists. For repo3 they DIVERGE, and the divergence was MEASURED: pgbackrest's
        # archive-push writes WAL to EVERY configured repository (no per-repo toggle, no `--repo` on that
        # command), so a failing repo3 fails the whole push, PostgreSQL's archiver records it, and §A·36
        # flips `wal_lag` -- which IS in health.GATE_CONSULTED_KEYS. An operator whose writes are being
        # refused, holding an alert that said only "notify-only", would go looking somewhere else during the
        # one incident this arm's own risk story is about. So the divergence is stated as a POSITIVE
        # sentence that names the row to read instead, rather than left as an omission.
        consequence=(
            "The third, deletion-resistant copy (pgbackrest repo3, object-lock protected) is NOT confirmed "
            "updating. NOTE: this row folds SEVERAL checks into one boolean -- whether pgbackrest can "
            "report at all, and whether repo3 carries a full backup younger than the pinned bound -- so it "
            "does NOT tell you which failed. NOTIFY-ONLY: this repo3 row is not consulted by the ADR-0020 "
            "durability gate, so a block here does not by itself refuse a canonical write. SEPARATELY, AND "
            "THIS IS NOT THIS ROW: pgbackrest pushes WAL to EVERY configured repository, so a repo3 that is "
            "failing archive-push blocks the archiver and CAN close the gate through the wal_lag row. If "
            "canonical writes are being refused right now, read wal_lag, not this row."
        ),
        triage=OBJECT_STORE_REPO_TRIAGE,
        # ⚠ SHARED WITH THE BACKUP ARM, DELIBERATELY AND TRUTHFULLY. repo3's backup and its probe are extra
        # `ExecStart=-` / `ExecStartPost=-` lines on the SAME unit (the §A·72 shape), so this genuinely IS
        # the unit to re-run. A distinct name here would be a checkable falsehood, and a separate unit would
        # race the main backup for pgbackrest's per-stanza lock. The arm-distinctness test is therefore
        # keyed on (headline, no_confirmed_label) -- which stay unique -- with the shared rerun_unit pinned
        # as an explicit, justified allowance rather than silently dropped from the check.
        rerun_unit="neuro-backup.service",
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
