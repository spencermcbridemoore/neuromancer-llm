"""The repo3 (Backblaze B2) base-backup freshness contract — the third, deletion-resistant copy's signal.

§A·72 (owner ruling, 2026-08-27): a THIRD pgbackrest repository on Backblaze B2 via its S3-compatible
endpoint, versioning ON, COMPLIANCE-mode object lock sized UNDER the pgbackrest retention window, WAL
included, provider access logs ON. It lands **NOTIFY-ONLY FIRST** — the §A·64 / B-7 pattern — and PROMOTION
into the gate basis is a LATER owner ruling, never a default.

A LEAF module (the freshness.py / wal_freshness.py / lake_freshness.py idiom): the health_key, the staleness
bound, and its fail-closed resolvers, and NOTHING heavy — so governance/durability.py can import the key and
bound for the provisioning row without pulling in the pgbackrest reader. The PRODUCER is
governance/repo3_probe.py; the notify-only ESCALATION is governance/repo3_escalation.py.

★ NOTIFY-ONLY, AND THE SCOPE OF THAT WORD IS NARROWER THAN IT LOOKS — READ THIS BEFORE QUOTING IT.
This key is deliberately kept OUT of health.GATE_CONSULTED_KEYS, exactly like `lake_mirror_freshness`, so a
blocked `repo3_freshness` row ALARMS and never blocks a canonical write. That guarantee is real and it is
about THIS ROW.

⚠ IT IS NOT A GUARANTEE THAT A FAILING repo3 CANNOT BLOCK A WRITE, and the difference is measured, not
theoretical (2026-08-28). pgbackrest's `archive-push` writes WAL to EVERY configured repository — there is no
per-repo archive toggle and no `--repo` option on that command, and the standalone-repository feature
request that would supply one (pgbackrest #1053) is still open — so a repo3
that is failing fails the whole `archive-push`, PostgreSQL's archiver records the failure, and §A·36 ("BLOCK
on ANY archiver error", with its self-healing current-failure rule) flips `wal_lag` — which IS in
GATE_CONSULTED_KEYS. The write refusal then arrives through the `wal_lag` row, not this one.
⇒ The operator-facing copy in governance/alert_triage.py states BOTH halves on purpose, and points a reader
whose writes are being refused at `wal_lag` rather than here. A copy that said only "notify-only" would be
the render-honesty defect (a fact keyed on X asserting Y when X and Y diverge) at the one moment it costs
most. The owner accepted this coupling 2026-08-28 on the grounds already ruled for repo2 at A2-7 §7 — a
decoupled, WAL-less third copy trades a bounded, monitored coupling for a restore-integrity risk.

⚠ WHY THE KEY NAMES A COORDINATE AND NOT A PROPERTY. `repo3_freshness` was chosen over `locked_repo_freshness`
/ `immutable_copy_freshness` deliberately: the probe VERIFIES the coordinate it is named for — it reads
`database.repo-key == 3` straight out of the info JSON — whereas a property name would assert immutability
this code cannot check, because the compliance lock lives in a B2 console setting no code here reads. A
module must not ship a claim about itself it cannot check. The cost is a real one and is stated rather than
hidden: a pgbackrest repo RENUMBER would make the key stale, and the deploy runbook carries a step forbidding
one without a key migration. (`offsite_repo_freshness` was rejected on a different ground — repo2 is offsite
too, so it would not discriminate.)

Doctrine (the freshness.py / canonical_instance.py / price_pin.py pin idiom): bounds are COMMITTED REPO
CONSTANTS resolved through fail-closed resolvers, never a DB column.
"""

from __future__ import annotations

import datetime as _dt

from .provisioning_invariants import resolve_base_backup_interval, resolve_provisioning_margin

# The shared health_key (one constant imported by the provisioning row, the producer, the escalation and the
# alert registry). It is a NATURAL KEY in neuro.system_health once seeded — expensive to rename.
REPO3_FRESHNESS_KEY = "repo3_freshness"

# The pgbackrest repository index this arm measures. It is the SAME integer the conf declares as `repo3-*`
# and that `pgbackrest info` reports as `database.repo-key`, so the probe can confirm the coordinate it is
# named for rather than trusting it.
REPO3_REPO_KEY = 3


def resolve_repo3_stale_after() -> _dt.timedelta:
    """The staleness bound for the repo3 signal, DERIVED BY REFERENCE — never a third pinned number.

    The repo3 backup and its probe are driven by `neuro-backup.service`, whose cadence is
    BASE_BACKUP_INTERVAL; one cadence plus the missed-cycle margin is exactly the shape
    LAKE_MIRROR_STALE_AFTER uses ("one cadence + ~2 missed-cycle margin"), and it is the SAME quantity the
    backup driver's own recency bound computes. Expressing it as the sum keeps one implementation of that
    concept and inherits BOTH pins' fail-closed resolvers, so there is no new constant to forget."""
    return resolve_base_backup_interval() + resolve_provisioning_margin()


def resolve_repo3_block_escalate_after() -> _dt.timedelta:
    """The persistent-block ESCALATION onset — how long `repo3_freshness` may read 'blocked' before
    governance/repo3_escalation.py re-alerts a human. Onset = the staleness bound BY REFERENCE (the
    lake_freshness idiom: one implementation per concept). Fail-closed through resolve_repo3_stale_after.

    ⚠ AN HONEST LIMIT, STATED SO IT IS NOT DISCOVERED IN AN INCIDENT: the alert is not fast. The backup
    cadence is 2 days and this onset is 4 days, so a repo3 that stops succeeding can take up to about
    2 + 4 days to raise the daily escalation — and because the repo3 ExecStart/ExecStartPost lines carry
    systemd's ignore-failure `-` prefix (the §A·72 requirement that keeps an unproven arm from blocking the
    gating freshness bump), repo3 also gets NO per-cycle `OnFailure=` ping of its own. That is the price of
    notify-only-by-construction, it was accepted deliberately, and it is why the escalation exists at all."""
    return resolve_repo3_stale_after()
