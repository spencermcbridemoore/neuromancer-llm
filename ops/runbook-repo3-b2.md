# RUNBOOK — repo3: a third pgbackrest repository on Backblaze B2 (NOTIFY-ONLY)

**EXECUTION: guided-required** (one command at a time, owner runs every command).
Guided-required is forced three times over: a **literal one-way door** (a COMPLIANCE-mode object lock nobody
can lift), a **credential mint**, and **first-time territory** (no repo3 has ever existed here).

**STATUS: DRAFTED — NOT EXECUTED.** No step in this runbook has been run against B2 or the conf, and no
`§`-expectation below has been confirmed by executing this procedure.
*(When this is executed, that banner changes and §-by-§ expectations become measured facts. The corpus-import
runbook shipped for a day asserting both at once; do not repeat that.)*
⚠ **But read "FORECAST" precisely, because the two kinds are not equally reliable.** Some expectations here
are now **derived from first-hand measurement of something else** — the installed pgbackrest version
(`2.58.0`, measured on canonical at log:279), the exact output strings of `probe report` / `verify-config` /
`repo3-escalate` (read from the shipped source at `d5528cb`), and the epoch-seeded age (computed). Those are
strong. Others remain genuine guesses about a system nobody has driven yet. **Every divergence is still a
FINDING — stop, do not improvise past it** — but a divergence from a *measured* forecast is the louder one.

Ruling: **§A·72** (owner, 2026-08-27). Code unit: HEAD at drafting `baf021a` + this unit.
Owner nods carried (2026-08-28): the gate-basis pin in **both** directions; the WAL coupling
**accepted with pgbackrest ≥ 2.59.0 as a hard precondition**.
Owner ruling carried (2026-08-30): the **repo3/B2 budget line = $2/mo of NEW money**, with §A·39's $10 Azure
envelope and its split **UNMOVED** ⇒ total storage envelope $10 Azure + $2 B2 (§2e carries the grounds).

---

## ⚠⚠ READ FIRST — TWO THINGS THAT ARE NOT OBVIOUS AND WILL BITE

**1. repo3 IS IN THE WAL PATH, UNAVOIDABLY, AND A FAILING repo3 CAN REFUSE CANONICAL WRITES.**
pgbackrest's `archive-push` writes WAL to **every** configured repository. There is no per-repo archive
toggle, no `--repo` option on `archive-push`, and the standalone-repository feature request (#1053) is still
open. So the moment `repo3-*` lines enter the conf, repo3 is in the archive path — this is not a choice the
conf can express.

Consequently, if repo3 starts failing:
- **First order (fail-CLOSED):** `archive-push` fails, PostgreSQL's archiver records the failure, and §A·36
  ("block on ANY archiver error", with its self-healing current-failure rule) flips **`wal_lag`** — which
  **is** consulted by the ADR-0020 gate. Canonical writes are refused. ⚠ Note the self-healing half: a
  *transient* failure followed by a successful segment clears itself at the next probe with no operator
  action; it is a *sustained* failure that closes the gate.
- **Second order (fail-OPEN, and silent):** WAL accumulates in the async spool; at
  `archive-push-queue-max=32GiB` pgbackrest drops **the entire queue** — for **all** repos, not just
  repo3 — reports success to PostgreSQL, and the gate REOPENS over repo1/repo2 archives that now have PITR
  holes. **No shipped signal catches this**; it is the queue-drop / WAL-continuity obligation recorded as
  still OPEN in `governance/wal_archiving.py`.

The owner accepted this on 2026-08-28 on the grounds already ruled for repo2 at **A2-7 §7 decision 3** — a
decoupled, WAL-less third copy trades a bounded, monitored coupling for a *restore-integrity* risk. The
mitigations are the ≥2.59.0 precondition in §0 and the **rollback lever in §ROLLBACK**, which is one edit.

**2. "NOTIFY-ONLY" IS A CLAIM ABOUT THE `repo3_freshness` ROW, NOT ABOUT repo3.**
That row is deliberately outside `health.GATE_CONSULTED_KEYS`, so a blocked repo3 row never blocks a write.
Point 1 is a different row (`wal_lag`) and a different mechanism. The alert copy says both; so does this
runbook, so nobody reads **§6c**'s gate-open proof as more than it is.

---

## §0 — MEASURE FIRST (nothing is changed in this section)

Run each command and **read the output**; do not proceed past a divergence.

**§0a — where am I.** Every command below runs on the canonical VM unless it says DESKTOP.
```
echo "host=$(hostname)"          # expect: neuro-canonical-pg
```
*(The host echo is not ceremony: in a guided session with an open SSH session and local shells, a VM command
run locally returns a perfectly formed, perfectly meaningless answer.)*

**§0b — the VM checkout is ROUTINELY BEHIND the banked HEAD — but is EXPECTED TO ALREADY BE CURRENT here.**
Measure it; do not assume in either direction.
```
cd /home/ubuntu/neuromancer-llm && git rev-parse --short HEAD
```
**Expect `d5528cb`** — this unit's own sha. The `0004` canonical apply advanced the checkout `34636c4` →
`d5528cb` on 2026-08-29 (log:279), so **the advance below is expected to be a NO-OP and skipping it is the
normal path, not a shortcut.** A match is the expected reading; anything else is the divergence.

Only if it does NOT match, advance **forward-only, proven**:
```
git fetch origin
git merge-base --is-ancestor <old-sha> <new-sha>; echo $?     # expect 0 — read $?, never chain with &&
git checkout <new-sha>
git status --porcelain                                        # expect empty
uv sync                                                       # expect a no-op; if it is not, STOP
```
⚠ **If you DO advance, say which code the installed units will then run** (the log:279 standing instruction).
A `git checkout` cannot touch an installed unit *file*, but `neuro-backup.service`'s `ExecStartPost` invokes
`/home/ubuntu/neuromancer-llm/.venv/bin/neuro`, so the next backup runs the **checked-out** probe code. Those
are two different claims — one about systemd, one about the venv — and only the first is obvious.

**§0c — ★ THE pgbackrest VERSION IS A HARD PRECONDITION (owner-ruled 2026-08-28).**
```
pgbackrest version
```
- **Expect ≥ 2.59.0.** The VM was last recorded at **2.58.0** (released 2026-01-19), which is on the affected
  side of pgbackrest issue **#2629** — `archive-push-queue-max` **is not enforced while a WAL file errors**.
  On that version the §0.1 valve does not fire during exactly the failure repo3 introduces, so the failure
  mode is unbounded spool growth on `/pgdata` instead of a bounded PITR gap.
- **v2.59.0 (2026-07-20) fixes it**, verbatim from the release notes: *"Fix archive-push-queue-max not
  enforced when a WAL file errors."* v2.59.1 followed on 2026-08-17.
- **If the measured version is < 2.59.0: STOP and upgrade before any conf edit.** Do not proceed on a
  measurement-and-accept basis; the owner ruled this a precondition, not an observation.

⚠⚠ **THIS BRANCH FIRES BY CONSTRUCTION — IT IS NOT A CONTINGENCY.** Canonical was **MEASURED at `2.58.0`**
on 2026-08-29 (log:279, read from the journal's own `backup command begin 2.58.0`), which is the first
first-hand confirmation of the installed version since the precondition was ruled. **Expect this command to
print 2.58.0 and expect to run §0c·UPGRADE below.** Reaching §0d without upgrading would mean the version
moved for a reason nobody recorded — itself a finding.

### §0c·UPGRADE — the pgbackrest upgrade (guided-required, like everything else here)

**Step 1 — MEASURE HOW IT WAS INSTALLED. Do this FIRST; the repo does not record it.**
`ops/provision-canonical.sh` **never installs pgbackrest** (verified first-hand at HEAD: the script installs
`postgresql-common`, runs the PGDG `apt.postgresql.org.sh`, and installs `postgresql-18` — pgbackrest appears
only in prose about a future restore). So the install route is an OPEN QUESTION and the upgrade command
depends on the answer. Do not guess it from the PGDG line in that script.
```
command -v pgbackrest
dpkg -S "$(readlink -f "$(command -v pgbackrest)")" ; echo "dpkg-S exit=$?"
dpkg -l pgbackrest | cat
apt-cache policy pgbackrest
```
Read the four together:
- `dpkg -S` naming a package ⇒ **package-managed**; `no path found matching pattern` (non-zero exit) ⇒
  **built from source or vendored** — a different, hand-rolled upgrade, so **STOP and surface it** rather
  than running `apt` over a source install.
- `apt-cache policy` is the discriminator that matters: it prints **Installed**, **Candidate**, and the
  origin table. **If Candidate < 2.59.0, apt cannot satisfy the precondition** — the PGDG repository is
  absent, pinned, or stale. `sudo apt-get update` first, re-read, and if it still will not offer ≥2.59.0,
  **STOP**: obtaining a newer pgbackrest is its own decision and is not made mid-deploy.
- Record which repository line supplies the candidate. That is the provenance the repo has never held.

**Step 2 — read the release notes between the installed version and the target**, for any migration or
stanza step. Do not assume "patch release, no action": the record already knows 2.59.0 changed archive-push
behaviour, which is the whole reason for this precondition.
```
# https://pgbackrest.org/release.html  — read every entry from the installed version up to the candidate
```
Expect **no** stanza/repo migration step for a 2.58 → 2.59 move. **If the notes name one, it is a FINDING —
stop and surface it**; a migration step discovered after the conf edit is a much worse place to be.

**Step 3 — upgrade.** No service restart is required: pgbackrest is invoked per-command (PostgreSQL's
`archive_command` and the systemd units), there is no daemon here, and in-flight invocations keep the old
inode. It IS still worth running between backup cycles rather than during one.
```
sudo apt-get update
sudo apt-get install --only-upgrade pgbackrest
pgbackrest version                      # expect >= 2.59.0 — this is the precondition, re-read it
```
⚠ **Single-host, so there is no version-skew problem to solve.** pgbackrest wants matching versions across
repo host and pg host; here every repository is driven from this one VM, so upgrading the one binary
upgrades every participant. Stated rather than left open, because it is the question a reader will ask.

**Step 4 — ★ THE POST-UPGRADE CHECK THAT IS EASY TO SKIP: a package-restored legacy config.**
```
ls -l /etc/pgbackrest.conf ; echo "exit=$?"
```
- **Expect: `No such file or directory`, exit non-zero.** That is the clean result.
- **If it EXISTS**, the package restored the legacy file-form config. Diff it against the real one, then
  **remove it**:
```
sudo diff -u /etc/pgbackrest.conf /etc/pgbackrest/pgbackrest.conf   # expect: no unique content worth keeping
sudo rm /etc/pgbackrest.conf
```
⚠ **Read the exposure CORRECTLY, because the record had it BACKWARDS until 2026-08-30.** pgbackrest reads
`/etc/pgbackrest/pgbackrest.conf` **FIRST** and falls back to `/etc/pgbackrest.conf` **only when the new path
is absent** (vendor doc: *"If no file exists in that location then the old default of /etc/pgbackrest.conf
will be checked."*). So a restored legacy file does **NOT** silently shadow the real config while the real
config is present — **the exposure is a LOUD `verify-config` assertion-6 RED on the next daily run**, not a
silent takeover. Clean it up anyway: it is a hazard-in-waiting the moment the real conf is lost or renamed,
and leaving it means a red daily probe for as long as it sits there.

**Step 5 — prove the upgrade did not disturb what already works, and take the net.**
```
pgbackrest version
sudo -u postgres pgbackrest --stanza=neuro info
sudo -u postgres pgbackrest --stanza=neuro check
uv run neuro probe verify-config \
  --timer-file /etc/systemd/system/neuro-backup.timer \
  --archiver-timer-file /etc/systemd/system/neuro-archiver-probe.timer
```
Expect: the new version; `info` green with **both** repo1 and repo2 present; `check` clean; verify-config OK
(at this point it still reports **repos [1, 2]** and `gate basis: [1, 2]; every configured repo gates` —
repo3 does not exist yet, so the NON-GATING line is correctly ABSENT here and its absence is not a defect).
Then take a fresh full on **both** proven repos as the net, before anything touches the conf:
```
sudo -u postgres pgbackrest --stanza=neuro --repo=1 --type=full backup
sudo -u postgres pgbackrest --stanza=neuro --repo=2 --type=full backup
```
⚠ **The upgrade PRECEDES any conf edit** (§3), per the ruling. Do not fold these two into one visit.

**§0d — the valve must be smaller than the disk it protects.** A cap larger than the free space is not a
valve.
```
sudo grep -n 'archive-push-queue-max\|spool-path' /etc/pgbackrest/pgbackrest.conf
df -h /pgdata /
```
⚠ `sudo` is not optional: this runbook makes that file `0600 postgres:postgres`, so a bare `grep` returns
*Permission denied* — which reads like a missing file and is not one.
Record both numbers here at execution. If free space on the spool's filesystem is below the cap, that is a
FINDING to surface before proceeding — it is pre-existing, not caused by repo3, but repo3 is what makes it
reachable.

**§0e — the baseline.** Capture it so §7 has something to compare against.
```
sudo -u postgres pgbackrest --stanza=neuro info
sudo -u postgres pgbackrest --stanza=neuro check
uv run neuro probe report --lane canonical
systemctl list-timers 'neuro-*' --all
```
⚠ Widen the window rather than reading the last entry: **each repo mints its OWN backup label seconds apart**
(measured: `20260801-010505F` repo1 + `20260801-010509F` repo2), so a narrow `tail` reads as if repo1 were
missing.

**FORECAST — `probe report` will show a MISSING repo3 row, and that is CORRECT, not drift.** The row is
seeded in §5a; before then `repo3_freshness` does not exist in `system_health`. Expect literally:
```
repo3_freshness: MISSING (run `neuro db durability seed`) measured_at=None
```
alongside `backup_freshness`, `wal_lag` and `lake_mirror_freshness` reading `status=ok`. ⚠ **Do not read the
MISSING line as a defect and do not "fix" it early** — the code already carries `repo3_freshness` in
`DURABILITY_ROWS` (it arrived with the `d5528cb` checkout), while canonical's `system_health` does not yet
carry the row. That gap is the expected pre-deploy state and is registered as such at log:279. It cannot
gate: `GATE_CONSULTED_KEYS` is `{backup_freshness, wal_lag}` and repo3 is deliberately outside it.

**RECORD THE TIMER COUNT — §7a compares against it, and the absolute number is NOT derivable from git.**
```
RECORD: neuro-* timers before this deploy, N = ____
```
⚠ Count them from the command's own output, do not infer from `ops/`: **at least one installed timer
(`neuro-verify.timer`) has never been tracked in this repo**, which is exactly why §7a asserts **N+1** rather
than a fixed number. The last measurement (log:259) found **8**; treat that as corroboration of your count,
never as a substitute for it.

**§0f — the conf's SECTION MAP, because §3 depends on it.**
```
sudo grep -n '^\[' /etc/pgbackrest/pgbackrest.conf
```
Record the line numbers of `[global]` and `[neuro]`. §3 inserts **before** `[neuro]`, and this is the
measurement that makes that possible. ⚠ Two tracked records disagree about where the repo2 key currently
sits (the a2-7 execution note says it was moved before `[neuro]`; `tests/test_provisioning_invariants.py`'s
fixture places it inside `[neuro]`). **The file is the authority — read it, and record which record was
right.**

**§0g — sequencing. ★ THE PREDECESSOR IS ALREADY DISCHARGED.** The `0004` canonical apply was the standing
FIRST item in the execution line (§A13); it was **EXECUTED on 2026-08-29 with zero divergences (log:279)**,
so this deploy is unblocked on the sequencing axis. This step is now a **confirmation that the record matches
the database**, not a gate you are waiting on. Confirm anyway:
```
uv run alembic current                 # expect: 0004_worker_registrar_role_split (head)
```
⚠ **Read the `(head)` suffix as an oracle, and note it means the OPPOSITE thing at the two ends of an apply.**
Here — *after* the apply — **`(head)` is the correct reading**: it means the applied revision is the newest
alembic knows about. A **bare** `0004_worker_registrar_role_split` with no suffix would mean alembic can see a
NEWER revision on disk, i.e. this checkout carries a migration canonical has not had applied — which would be
a FINDING and a stop, not a detail.
⚠ `migrations/env.py` requires **both** `NEURO_DATABASE_URL` and `NEURO_MIGRATION_EXPECTED_LANE`, each
fail-closed. If this command errors on an absent variable, that is the environment, not the database — set
the two explicitly and re-run. **Do not `source /etc/neuro/env` to satisfy it:** that file's
`NEURO_DATABASE_URL` is the `neuro_timer` writer DSN and sourcing it also drags the §A·45-quarantined Azure
account key into this shell and every child of it.

---

## §1 — RECONCILE `neuro-backup.service`: LIVE → `ops/`, never the reverse

`ops/neuro-backup.service` is **new to version control in this unit**. The live unit has run since
2026-07-11 and has never been tracked. The committed copy is drafted from the A2-16 record plus the repo3
additions and **is not automatically the truth.**

```
systemctl cat neuro-backup.service > /tmp/live-neuro-backup.service      # includes drop-ins
systemctl show neuro-backup.service -p ExecStart -p ExecStartPost -p TimeoutStartUSec -p User \
    -p EnvironmentFiles -p OnFailure
diff -u /home/ubuntu/neuromancer-llm/ops/neuro-backup.service /tmp/live-neuro-backup.service
```
- `systemctl cat` gives the TEXT (with drop-ins). `systemctl show` gives the **parsed** view — what actually
  runs — and renders an ignore-failure prefix as `ignore_errors=yes`. Read both.
- ⚠ **FORECAST: the diff will open with a difference that is NOT a divergence.** `systemctl cat` prints its
  own provenance header — `# /etc/systemd/system/neuro-backup.service` as the first line, plus one such line
  per drop-in — and `ops/neuro-backup.service` has no such line, so `diff -u` reports it as added on the live
  side. **That header is an artifact of the capture, not a difference between the units.** Expect it, do not
  chase it, and do not "fix" `ops/` by adding it. It is the same fact §ROLLBACK depends on: the capture is a
  concatenated LISTING, not an installable unit file.
- **Direction of authority: the LIVE unit wins** on every difference that is not one of this unit's three
  deliberate repo3 additions (the `ExecStart=-` repo3 backup line and the `ExecStartPost=-` repo3 probe line,
  plus their comments). Any other divergence is a FINDING: bring `ops/` into line with live and record it,
  do not overwrite live with `ops/`.
- ⚠ `ops/` mirrors the live unit set only PARTIALLY — several installed services are still untracked. Do not
  read "it is in ops/" as "it is current".

---

## §2 — THE PROVIDER CONSOLE (Backblaze B2). One-way doors live here.

⚠ **Every value in this section is an OWNER DECISION recorded at execution.** No number below is invented by
this runbook; each has a step that resolves it.

⚠⚠ **SYMBOL WARNING — READ BEFORE WRITING ANYTHING DOWN. `R3` MEANS TWO DIFFERENT THINGS IN THIS
ENGAGEMENT, AND BOTH GET RECORDED AT A CONSOLE IN THIS SECTION.**
- **`R3` (the RULING)** = §A·39's **$10/mo Azure storage budget envelope**. A dollar figure.
- **repo3's retention-full window** = a number of **days**. This runbook previously also called that `R3`.

Two different quantities, both owner-recorded in §2, both bare integers on a form. **This runbook now calls
the retention window `RF3`** (mnemonic: `repo3-retention-full`) and reserves `R3` for the budget ruling
alone. If you see a bare `R3` anywhere below, it is the **money**.

**§2a — the retention arithmetic, worked here so the console steps have real inputs.**
Let **RF3** = repo3's pgbackrest `repo3-retention-full` (**days** — NOT §A·39's `R3` dollars) and **L** = the
compliance lock retention (days).
- `verify-config` assertion 2 takes **min() over ALL repos**, and the floor is
  `BACKUP_STALE_AFTER(8d) + BASE_BACKUP_INTERVAL(2d) + PROVISIONING_MARGIN(2d)` = **12 days**.
  ⇒ **RF3 ≥ 12.** Setting **RF3 = 30** matches repos 1 and 2 and leaves `min()` where it is; anything in
  [12, 29] passes but makes repo3 the repo named in any future floor violation.
- The ruling: **L strictly under RF3**, so pgbackrest's own `expire` is never fighting the lock.
  ⇒ **L ≤ RF3−1.**
- L bounded BELOW by detect-plus-respond: the repo3 arm's own onset is 4 days and the daily escalation adds
  a day, so **L comfortably above ~7** is the point of having a lock at all.
- B2's documented range is **1–3000 days**.
- **RECORD HERE AT EXECUTION: RF3 = ____ days , L = ____ days .** *(Days. The dollar figure is §2e.)*

**§2b — the bucket.** Create a **NEW** bucket with **Object Lock enabled at creation**. Do not retrofit: B2
documents default-bucket-retention as a create-time setting, and a bucket without it silently accepts
unlocked objects.
- versioning **ON**
- default retention mode **COMPLIANCE**, **L** days (from §2a)
- ⚠⚠ **THIS IS THE ONE-WAY DOOR.** Compliance retention *"cannot be removed by any user"* — only **extended**.
  Not by the account owner, not by support. A mis-sized L means paying for locked garbage until it lapses.
  **Re-read §2a's L before clicking.**

**§2c — the lifecycle rule.** ⚠ **TWO SEPARATE INEQUALITIES; do not collapse them.**
- `L ≤ RF3−1` (§2a) governs whether pgbackrest's **`expire`** delete succeeds.
- **`daysFromHidingToDeleting ≥ L`** governs the **lifecycle reaper**, and it is independent of RF3 — the
  reaper runs on the **hiding** clock, not pgbackrest's retention clock. This matters because pgbackrest
  rewrites fixed-name objects (e.g. `backup/neuro/backup.info`) every cycle, so a version is *superseded*
  ~one 2-day cadence after its own upload while its lock still has L−2 days to run.
- If the rule is sized shorter than L, B2 documents the outcome plainly: *"When a file is locked, lifecycle
  settings … attempting to change or delete the file will fail."* Every cycle then produces failing deletes
  and the non-current-version tail grows — a silent spend line, not just noise.
- **RECORD: daysFromHidingToDeleting = ____ (≥ L).**

**§2d — bucket access logs ON.** Backblaze **Bucket Access Logs** are generally available and S3-compatible;
configure from the console or the S3 API. ⚠ Logs are delivered **into a bucket** — use a *different*,
**unlocked** bucket as the destination (delivery into the locked bucket would collide with the lock), and
note it as a second, small spend line.

**§2e — the spending cap.** This is a MECHANISM, not discipline, and it is the failure the repo3 alert would
otherwise mis-route: an account at its cap **rejects writes while reads keep succeeding**, so
`pgbackrest info` looks healthy while every backup fails.
**★ THE FIGURE IS RULED — $2/mo, NEW MONEY (owner, 2026-08-30).** This runbook no longer has a blank to
invent here.
- **repo3 / B2 budget line = $2/mo, NEW money.**
- **§A·39's `R3` Azure envelope is UNMOVED**: still $10/mo, still split $6 prod / $1.50 dev+stage /
  $2 db-backups / $0.50 upload-staging. Nothing in that split is re-allocated to pay for B2.
- ⇒ **total storage envelope = $10 Azure + $2 B2.**

*Grounds, banked with the figure so no future session re-litigates it:* $2 **mirrors the existing Azure
`db-backups` line** — repo3 is the same kind of thing, a pgbackrest cloud repository — and it is roughly
**100× the measured repo footprint** (~1–3 GB standing ≈ $0.01–0.02/mo at B2 rates). It is deliberately
sized so the cap **never fires absent a true runaway**, because a cap-hit **refuses WRITES**, and a repo3
that cannot be written to closes the canonical gate through the measured `wal_lag` coupling (§READ FIRST
point 1). A tight cap here would be a self-inflicted outage lever, not thrift.

**⚠ MECHANISM SPLIT — STATE IT HONESTLY, BECAUSE ONLY ONE HALF ENFORCES ANYTHING.**
- **The enforcement is the B2 CONSOLE spend cap set to $2** — the step below. That is the mechanism.
- **The $2 budget line is recorded DATA.** `storage/quota.py`'s `QuotaGuardedBackend` **does not reach
  pgbackrest's write path at all** (its own comment says so), and repo3 is deliberately absent from
  `_BUDGET_GROUPS` — not for routing reasons, but because that map's ceiling derives from a repo-pinned
  **Azure** Hot-LRS meter, so a B2 prefix would silently inherit the wrong provider's rate.
- The §2c lifecycle rule still lands independently; it bounds the version tail, not the spend.

**The step:**
- In the B2 console, set the account/bucket **spending cap to $2/mo**.
- Sanity-check the headroom rather than trusting the number: take repo1's current on-disk size from §0e's
  `pgbackrest info`, and **read B2's current published rate at execution time** — never a rate hardcoded
  here; a price is an expiring fact, and the vendor page is the out-of-band refresh (the §A·40 lane-(b)
  idiom). Add the version tail from §2c and the access-log bucket from §2d. Expect the total to land
  **far** under $2; if it does not, that is a FINDING — surface it rather than quietly raising the cap.
- **RECORD: spend cap set = $2/mo (confirmed in console: ☐) ; estimated actual monthly = $____ .**

**§2f — RECORD THE BUCKET NAME AND THE ENDPOINT COORDINATES.** The bucket's region determines both the endpoint host and the
`repo3-s3-region` value, and §3a carries a `<region>` placeholder in two options. Read them off the bucket's
own page in the console (B2's S3 endpoint has the form `s3.<region>.backblazeb2.com`) and write them down
here before §3, so the conf is not authored from memory.
- **RECORD: bucket name = ____________________** *(the §2b bucket; `repo3-s3-bucket` consumes it verbatim,
  and §3a refers to it — record it HERE so §3 is authored from this page and not from recollection).*
- **RECORD: region = ____________ ; endpoint host = s3.____________.backblazeb2.com .**

**§2g — the application key. ⚠ B2 MINTS *TWO* VALUES AND §3b CONSUMES BOTH.** Mint a key **scoped to the
one bucket** (never an account-wide key). B2 returns a **keyID** *and* an **applicationKey**; they are
different things with different handling, and a key shown once and recorded as "the key" loses half of it.
- **`keyID` — NOT secret.** It is the `repo3-s3-key` value. **RECORD: keyID = ____________________**
- **`applicationKey` — SECRET, shown exactly once.** It is the `repo3-s3-key-secret` value. **Do NOT write
  it here.** Keep it in the console/clipboard only until §3b consumes it, where it is seated without ever
  touching argv, shell history, or this transcript.
- If the applicationKey is lost before §3b, do not hunt for it — **mint a new key and revoke the old one.**

---

## §3 — THE CONF EDIT (root-only; the credential never rides a command line)

⚠⚠ **DO NOT USE `tee -a`.** It appends to **end-of-file**, which lands **inside `[neuro]`** — the a2-7
execution recorded exactly that as a latent placement bug and worked around it with an insert-before. The
`read -rs` half of that idiom is the proven part; the append half is not.

**⚠⚠ THE WINDOW: FROM §3a UNTIL §3d SUCCEEDS, repo3 IS CONFIGURED BUT UNUSABLE — AND THAT CAN CLOSE THE
CANONICAL GATE.** Name it up front so it is not discovered mid-deploy.
- The moment `repo3-*` lines land in the conf (§3a), pgbackrest considers repo3 **configured**, so
  `archive-push` starts writing WAL to it — see §READ FIRST point 1: there is no per-repo archive toggle.
- Between §3a and §3d the repository has **no credential yet** (§3b) and **no stanza yet** (§3d), so those
  pushes **FAIL**. PostgreSQL's archiver records the failure and §A·36 flips **`wal_lag`**, which **is**
  gate-consulted ⇒ **canonical writes are refused for the duration of the window.**
- ⇒ **Work §3a → §3d in one continuous sitting. Do not stop inside the window**, and do not leave it open
  over a break. If you must stop, use the §ROLLBACK lever (remove the `repo3-*` lines) rather than walking
  away — this is exactly the "stopped between §3 and §5a" case §ROLLBACK names.
- ⚠ The window closes on the **first successful archive-push after §3d**, not at the instant §3d returns.
  `wal_lag` is self-healing but **not instantaneous**: the archiver probe runs every 15 minutes, so the row
  clears at the next probe after a good segment. **§5c confirms that heal explicitly — do not assume it.**

**§3a — seat the non-secret block, BEFORE `[neuro]`** (use §0f's line map). Values from §2:
```ini
repo3-type=s3
repo3-path=/neuro
repo3-s3-bucket=<the bucket name from §2f>
repo3-s3-endpoint=s3.<region>.backblazeb2.com
repo3-s3-region=<region>
repo3-s3-uri-style=path
repo3-retention-full-type=time
repo3-retention-full=<RF3 from §2a — DAYS>
repo3-bundle=y
```
⚠ `repo3-retention-full-type=time` is **not optional**: pgbackrest defaults to `count`, which would silently
turn RF3 from *days* into *backups*. Assertion 1 catches it in §5, but do not rely on the checker to author
the file.

**Take the undo first** (this is the file every backup and every WAL push reads):
```
sudo cp -a /etc/pgbackrest/pgbackrest.conf /root/pgbackrest.conf.pre-repo3
sudo stat -c '%a %U:%G' /etc/pgbackrest/pgbackrest.conf     # RECORD: ____ ____ — §3b re-checks it
```
⚠ `cp -a` preserves mode and owner, and `/root/` is root-only — necessary because that copy contains the
§A·45-quarantined Azure account key. **Remove it once §7 confirms** (`sudo shred -u /root/pgbackrest.conf.pre-repo3`).

**THE INSERT.** One mechanism, used twice — here for the non-secret block, and again in §3b for the
credential pair. It edits **in place** (same inode ⇒ mode and owner are preserved by construction, not by a
follow-up `chown`), and it **fails closed without touching the file** if the `[neuro]` header is not found
exactly once.
```
sudo python3 - <<'PY'
import pathlib, re
conf = pathlib.Path("/etc/pgbackrest/pgbackrest.conf")
block = """\
repo3-type=s3
repo3-path=/neuro
repo3-s3-bucket=REPLACE_BUCKET
repo3-s3-endpoint=s3.REPLACE_REGION.backblazeb2.com
repo3-s3-region=REPLACE_REGION
repo3-s3-uri-style=path
repo3-retention-full-type=time
repo3-retention-full=REPLACE_RF3
repo3-bundle=y
"""
assert "REPLACE_" not in block, "fill in the §2 values before running this — refusing to seat placeholders"
text = conf.read_text(encoding="utf-8")
assert "repo3-type" not in text, "a repo3 block is already seated — fail closed, not twice"
hits = list(re.finditer(r"^\[neuro\]\s*$", text, flags=re.M))
assert len(hits) == 1, f"expected exactly one [neuro] header, found {len(hits)} — fail closed, file untouched"
i = hits[0].start()
conf.write_text(text[:i] + block + text[i:], encoding="utf-8")
print("inserted before [neuro]")
PY
```
⚠ **Edit the heredoc's `REPLACE_*` values before you paste it.** The `assert` is there so a half-filled
paste refuses rather than seating literal placeholders — a conf that parses but names a bucket called
`REPLACE_BUCKET` is the failure that looks like success. Expect `inserted before [neuro]` and nothing else.

**§3b — seat the secret, without it reaching argv or history.** Same insert as §3a, with the
`applicationKey` read straight from the terminal by the **root** process that writes it. The secret never
becomes a shell variable, never appears in `ps`, and is never written to `~/.bash_history`.

The **keyID is not secret**, so it rides `argv` deliberately; only the `applicationKey` needs the tty path.
```
sudo python3 - '<the keyID from §2g>' <<'PY'
import getpass, pathlib, re, sys
key_id = sys.argv[1].strip()
assert key_id and not key_id.startswith("<"), "pass the real keyID from §2g as the argument — fail closed"
# getpass reads /dev/tty directly, NOT stdin — stdin is this heredoc, so a plain input() would silently
# consume the script's own remaining bytes instead of prompting. That distinction is the whole trick.
secret = getpass.getpass("B2 applicationKey (not echoed): ").strip()
assert secret, "empty applicationKey — fail closed, file untouched"
conf = pathlib.Path("/etc/pgbackrest/pgbackrest.conf")
text = conf.read_text(encoding="utf-8")
assert "repo3-s3-key-secret" not in text, "a repo3 credential is already seated — fail closed, not twice"
hits = list(re.finditer(r"^\[neuro\]\s*$", text, flags=re.M))
assert len(hits) == 1, f"expected exactly one [neuro] header, found {len(hits)} — fail closed, file untouched"
i = hits[0].start()
conf.write_text(text[:i] + f"repo3-s3-key={key_id}\nrepo3-s3-key-secret={secret}\n" + text[i:], encoding="utf-8")
print(f"seated repo3 credential (keyID len={len(key_id)}, secret len={len(secret)})")
PY
```
Expect a single line reporting the two **lengths** — never the values. A `secret len` of 0 is impossible
(the assert refuses it); a surprising length means you pasted the wrong one of B2's two values, and the fix
is §ROLLBACK's key revoke plus a re-mint, not an edit.

**Confirm the file's posture is unchanged** — the insert edits in place, so this should be identical to the
figure recorded in §3a, not something the next two commands had to repair:
```
sudo stat -c '%a %U:%G' /etc/pgbackrest/pgbackrest.conf     # expect: 600 postgres:postgres, UNCHANGED
sudo chmod 600 /etc/pgbackrest/pgbackrest.conf
sudo chown postgres:postgres /etc/pgbackrest/pgbackrest.conf
```
⚠ The `chmod`/`chown` are kept as an **idempotent belt**, not as the mechanism. If `stat` above already read
`600 postgres:postgres`, they change nothing — and if it did **not**, that is a FINDING about the file's
prior state, not a step that quietly fixed it. Record which of the two you saw.

**§3c — verify placement by NAMES ONLY** (never echo a value from this file):
```
sudo grep -n '^\[' /etc/pgbackrest/pgbackrest.conf
sudo grep -on 'repo3-[a-z0-9-]*' /etc/pgbackrest/pgbackrest.conf
```
Expect every `repo3-*` name to appear at a line number **below `[global]` and above `[neuro]`**.
⚠ The character class **must include digits** — an earlier draft used `[a-z-]*`, which stops at the `3` in
`repo3-s3-bucket` and reports the truncated name `repo3-s`. A verification command that silently matches
less than it appears to is worse than none.

**§3d — `stanza-create`.** Without this, archive-push to repo3 fails from the first WAL segment.
```
sudo -u postgres pgbackrest --stanza=neuro stanza-create
sudo -u postgres pgbackrest --stanza=neuro check
```
⚠ **Bare `stanza-create`, no `--repo`.** The a2-7 execution recorded `--repo=N stanza-create` as **INVALID**
on 2.58 (*"option 'repo' not valid for command 'stanza-create'"*); the bare form operates on all repos and is
idempotent — it validates the existing ones and creates the new one.

---

## §4 — INSTALL THE UNITS

```
sudo cp ops/neuro-backup.service /etc/systemd/system/          # ONLY after §1 reconciliation
sudo cp ops/neuro-repo3-escalate.service /etc/systemd/system/
sudo cp ops/neuro-repo3-escalate.timer   /etc/systemd/system/
sudo systemctl daemon-reload
```
⚠ **`enable` WITHOUT `--now`** at this point (the A2-16 trap: `--now` starts a unit before its preconditions
are in place). The escalation timer is started in **§7a**, deliberately **after** the first green probe, so
the born-blocked row cannot fire an alert that means nothing.
```
sudo systemctl enable neuro-repo3-escalate.timer
```

**Verify the parsed unit — the text is not what runs:**
```
systemctl show neuro-backup.service -p ExecStart -p ExecStartPost
```
Expect, and check each: the repo3 `ExecStart` and the repo3 `ExecStartPost` show **`ignore_errors=yes`**; the
repo1/repo2 `ExecStart`s and the `backup_freshness` `ExecStartPost` show **`ignore_errors=no`**; and the
repo3 `ExecStartPost` is listed **FIRST**. That order is load-bearing: systemd stops at the first failing
unprefixed `ExecStartPost`, so a repo3 probe placed after the gating one would be skipped exactly when
`backup_freshness` fails.

---

## §5 — SEED THE ROW, THEN VERIFY THE CONFIG

**§5a — the durability row** (registrar/admin — the writer holds no INSERT on `system_health`).
⚠ `psql` **cannot parse** the SQLAlchemy `postgresql+psycopg://` scheme; run this through `uv run neuro`.
```
read -rsp "neuro_orch pw: " PW; echo
export NEURO_DATABASE_URL="postgresql+psycopg://neuro_orch:$(PW="$PW" python3 -c 'import os,urllib.parse;print(urllib.parse.quote(os.environ["PW"],safe=""))')@127.0.0.1:5432/neuro"
unset PW
echo "len=${#NEURO_DATABASE_URL}"      # ⚠ NEVER echo the variable itself
uv run neuro db verify --lane canonical
uv run neuro db durability seed --lane canonical
```
Expect `verify` to print `verified: lane=canonical instance_uuid=c7a1b953-…`, then seed to report **1
inserted** (the three existing rows are already present; seeding is idempotent).
⚠ If the admin password is unavailable, the **§A·48 break-glass** is the peer-auth superuser — name it as the
chosen path if used, and say why the other was not.

**§5b — verify-config must be GREEN.**
```
uv run neuro probe verify-config \
  --timer-file /etc/systemd/system/neuro-backup.timer \
  --archiver-timer-file /etc/systemd/system/neuro-archiver-probe.timer
```
Expect **FOUR** lines, **in the order the command prints them** — note the second one, which an earlier
draft of this runbook omitted entirely:
```
verify-config OK: repos [1, 2, 3]; retention repo1=30d, repo2=30d, repo3=<RF3>d
gate basis: [1, 2]; NON-GATING (reported only): [3]
backup timer cadence checked: OnUnitActiveSec == BASE_BACKUP_INTERVAL
archiver-probe timer cadence checked: OnUnitActiveSec == ARCHIVER_PROBE_INTERVAL
```
**★ THE SECOND LINE IS THE ONE THAT MATTERS HERE, AND ITS ABSENCE IS THE FINDING.** It is assertion 8's
REPORTED half, and it is the only place the daily run says out loud that repo3 is configured-but-non-gating.
- Seeing `gate basis: [1, 2]; NON-GATING (reported only): [3]` is the **expected, correct** notify-only
  state — repo3 exists in the conf and is deliberately outside the ADR-0020 basis.
- Seeing instead `gate basis: [1, 2]; every configured repo gates` means the command found **no** repo
  outside the basis — i.e. **repo3 is not in the conf pgbackrest actually parsed**. §3 did not take. STOP.
- Seeing `gate basis: [1, 2, 3]` at all would mean the pin itself moved, which is the **promotion** and is a
  later owner ruling (§NOT DONE HERE). STOP.
⚠ Before §3, this line correctly reads `every configured repo gates` (there is no non-gating repo yet) — so
the same line means different things at §0c·UPGRADE step 5 and here. Read it against where you are.

**§5c — ★ CONFIRM `wal_lag` HAS HEALED FROM THE §3 WINDOW. Do not assume it; §6 depends on it.**
Between §3a and the first successful archive-push after §3d, WAL pushes to repo3 failed, so §A·36 will have
flipped `wal_lag` to `blocked`. It self-heals — but only on the **next archiver probe after a good
segment**, and that probe runs on a **15-minute** timer.
```
uv run neuro probe report --lane canonical --key wal_lag
```
**Expect `wal_lag: status=ok`.** If it still reads `blocked`, that is **not** a failure of this deploy — it
is the window closing on its own schedule. Force a segment and wait one probe interval rather than
improvising:
```
sudo -u postgres psql -P pager=off -d neuro -c "SELECT pg_switch_wal();"
sudo systemctl start neuro-archiver-probe.service
uv run neuro probe report --lane canonical --key wal_lag        # expect: status=ok
```
⚠ **If `wal_lag` will not clear, STOP — do not proceed to §6.** A persistently blocked `wal_lag` means
archive-push is still failing to *some* repo, and §6c's whole proof is that the gate is open **for
uncontaminated reasons**. Running it against a gate that is closed for a WAL reason would not just fail —
it would fail in a way that looks like the notify-only guarantee being false when it is not. Diagnose with
`sudo -u postgres pgbackrest --stanza=neuro check` first.

---

## §6 — THE E·16 INDUCED PROOF, RUN AS AN **ORDERING**, NOT AN INDUCTION

⚠ **WHY THERE IS NO INDUCED FAILURE HERE, STATED SO THE SUBSTITUTION IS NOT MISTAKEN FOR A SHORTCUT.**
The obvious test — break repo3's credential or endpoint and watch the row flip — **cannot work**. Every such
break also breaks `archive-push` (§READ FIRST point 1), which flips `wal_lag` and closes the ADR-0020 gate.
The gate would then be closed *for a different reason* while we were trying to prove it stays open, and the
notify-only proof would be contaminated by construction. There is no read-scoped induction available.

**Instead, use the state the deploy naturally passes through**: between §5a's seed and the first repo3
backup, `repo3_freshness` is legitimately blocked (`recency: repo3 has NO full backup`) while archive-push is
**healthy** and repos 1+2 are **fresh**. That is a real blocked repo3 arm with an uncontaminated gate — which
is exactly the state the proof needs.

**§6·PRE — ★ DROP BACK TO THE WRITER DSN BEFORE ANY OF §6. THIS IS A STEP, NOT A NOTE.**
§5a exported an **ADMIN** (`neuro_orch`) DSN into this shell and nothing has taken it away. Every command in
§6 must run at **writer** grade instead — that is what the nightly `ExecStartPost` actually uses, and §6c's
proof is worthless at admin grade.
⚠ **Why this needs a step and not a reminder: `neuro_admin` ⊇ `neuro_writer`, so §6 would PASS either way.**
Nothing in the output would reveal the omission. An unnoticed admin-grade run means §6c proved the gate
opens for a role the capture path never uses — a green that certifies the wrong thing.

**Preferred: open a SECOND shell as `ubuntu` and run all of §6 there**, leaving the §5a shell untouched for
nothing further. The plain `ubuntu` shell already carries the `neuro_timer` **writer** DSN, which is
precisely the grade under test.
```
# in the NEW shell:
echo "len=${#NEURO_DATABASE_URL}"     # expect NON-ZERO — ⚠ never echo the variable itself
cd /home/ubuntu/neuromancer-llm
```
If you must stay in the §5a shell, clear the admin DSN and re-establish the writer one explicitly:
```
unset NEURO_DATABASE_URL
```
…then re-open as `ubuntu` anyway — do **not** `source /etc/neuro/env` to recover it (§0g: that file also
carries the quarantined Azure account key, which would then leak into every child of this shell).
⚠ If `len=0` in the new shell, the writer DSN is not in the environment. **STOP and resolve that first** —
do not "temporarily" fall back to the admin DSN to get §6 moving. That fallback is the exact defect this
step exists to prevent.

**§6a — confirm the uncontaminated starting state.**
```
uv run neuro probe run --key repo3_freshness --lane canonical ; echo "exit=$?"
uv run neuro probe report --lane canonical --key repo3_freshness
```
Expect exit **1**, and a reason reading `recency: repo3 has NO full backup (fail closed)`.
Then confirm the *other* arms are healthy, so the next step means something:
```
uv run neuro probe report --lane canonical
```
Expect `backup_freshness: status=ok` and `wal_lag: status=ok`.

**§6b — the alert LANDS on the device** (an untested alert is prose — §E·16).
```
uv run neuro probe repo3-escalate --lane canonical --escalate-after-hours 0
```
Expect `ESCALATED`, and **confirm on the phone** that the ntfy message arrived. Read it: it must name the
consequence, say the cause is not established, and point at `neuro probe report --key repo3_freshness`.

**⚠ FORECAST — THE AGE WILL READ AS ~57 YEARS, AND THAT IS CORRECT BY DESIGN. DO NOT TREAT IT AS A BUG.**
The message opens:
```
neuromancer IMMUTABLE THIRD COPY (pgbackrest repo3) BLOCKED -- no confirmed repo3 backup for ~20695d
(last good: 1970-01-01 00:00:00+00:00). ...
```
`durability seed` writes `measured_at = 'epoch'::timestamptz` deliberately — every durability row is **born
fail-closed at the Unix epoch** so it can never read fresh before its first real probe. The escalation
composer renders `(now() - measured_at).days`, so on a just-seeded row that is simply **days since
1970-01-01**.
⚠ **The number is NOT a constant — it grows by 1 per day.** `~20695d` is its value on **2026-08-30**; on the
day you actually run this it will be larger (≈20,7xx). **Check the SHAPE, not the digits:** a five-digit day
count paired with `last good: 1970-01-01` is the correct born-blocked reading. What would be a FINDING is a
*small* number or a *recent* `last good` — either would mean the row is not freshly seeded and something
else has already written to it.

**§6c — ★ THE PROOF THAT "NOTIFY-ONLY" IS REAL.** With `repo3_freshness` blocked, a **real**
`assert_durability_ok` consult must pass.

⚠ **AN EARLIER DRAFT OF THIS STEP USED `neuro runs show` AND WAS VACUOUS — recorded because the same mistake
is easy to make again.** `assert_durability_ok` has exactly **three** callsites in `src/`
(`bundles/registrar.py:273`, `capture/events.py:664`, `importer/ingress.py:134`) and **`db/run_report.py` is
not among them** — it is a read over an already-verified engine and touches no durability row. `runs show`
would therefore have succeeded whether or not repo3 could block a write: a proof that does not exercise the
gate proves nothing. The three real callsites all *write* to canonical, which this runbook must not do
casually, so the honest option is to call the gate directly:

```
uv run python -c "
from neuromancer_llm.db.session import make_verified_engine
from neuromancer_llm.governance.health import assert_durability_ok
assert_durability_ok(make_verified_engine(expected_lane='canonical'))
print('GATE OPEN: assert_durability_ok passed with repo3_freshness blocked')
"
```
Expect exactly that line, and exit 0. **That output IS the notify-only proof** — the same function the
capture path calls, consulted while the repo3 row reads `blocked`.

⚠ Run this as `ubuntu` with `/etc/neuro/env`'s `NEURO_DATABASE_URL` (the `neuro_timer` **writer** DSN), NOT
the admin DSN: the gate needs writer grade because a drift/stale transition would UPDATE `system_health`, and
writer is the least privilege that satisfies it. It is otherwise read-only — and §6a already established that
both gating arms are green, so no flip can fire here.

**§6d — restore to green.** The "restore" is simply the first successful repo3 backup:
```
sudo -u postgres pgbackrest --stanza=neuro --repo=3 --type=full backup
uv run neuro probe run --key repo3_freshness --lane canonical ; echo "exit=$?"
uv run neuro probe report --lane canonical --key repo3_freshness
```
Expect, in this order: the backup command's own completion output, then
`probe repo3_freshness (lane=canonical): recorded ok` with **exit=0**, then a report line reading
`repo3_freshness: status=ok measured_at=<now> detail='repo3:<label>'`.
⚠ `probe run` does NOT print the row's status — it prints `recorded ok`. The status line comes from
`probe report`, which is why both commands are here.

**§6e — and confirm the alert goes SILENT.**
```
uv run neuro probe repo3-escalate --lane canonical
```
Expect `repo3_freshness is not in a persistent-block state — no alert.` and **no ping on the device**.

---

## §7 — ARM AND BANK

**§7a** — start the escalation timer only now:
```
sudo systemctl start neuro-repo3-escalate.timer
systemctl list-timers 'neuro-*' --all
```
**Expect exactly `N + 1` timers, where `N` is the count RECORDED IN §0e** — and confirm the new one by name:
`neuro-repo3-escalate.timer` must be present and listed as active. Any other delta is a FINDING.
⚠ **This is deliberately relative and not an absolute number.** An earlier draft said "expect 9". That is
unsafe here: **at least one installed timer (`neuro-verify.timer`) has never been tracked in this repo**, so
the absolute total is not derivable from `ops/` and a future rebuild could legitimately change it. `N+1` is
checkable from this deploy's own baseline; `9` is a fact with an expiry date. *(For corroboration only: the
last measurement, log:259, found N = 8 ⇒ 9 here. If your §0e count was not 8, trust §0e and record the
difference — it means the installed timer set moved and that is worth knowing.)*

**§7b** — one full natural cycle:
```
sudo systemctl start neuro-backup.service ; echo "exit=$?"
```
Expect exit 0 — but read the next paragraph before you record that as success.

**⚠⚠ `exit=0` CANNOT DISTINGUISH A repo3 SUCCESS FROM A repo3 FAILURE. THAT IS WHAT THE `-` PREFIX IS FOR.**
`Type=oneshot` blocks until every step finishes, so exit 0 does prove the unit *ran to completion*. It
proves nothing about repo3: the repo3 `ExecStart` and the repo3 `ExecStartPost` both carry the
ignore-failure `-` prefix — that asymmetry **is** the notify-only mechanism at the systemd layer — so a
repo3 that failed outright still leaves this unit exiting 0. Reading exit 0 as "all three repos backed up"
is the fail-open this whole design deliberately accepts, and the runbook must not repeat it.

**The discriminating check is the label count.** Run it explicitly:
```
sudo -u postgres pgbackrest --stanza=neuro info
```
**Expect THREE backup labels, one per repository, minted seconds apart** — the unit runs the repos as three
separate `ExecStart` lines, so **each mints its OWN label** (measured previously: `20260801-010505F` repo1 +
`20260801-010509F` repo2, and now a repo3 label alongside them). Widen the window; do **not** `tail` it.
- **Three labels ⇒ repo3 really was written.** That, not the exit code, is the evidence.
- **Two labels + exit 0 ⇒ repo3 FAILED SILENTLY.** This is the expected shape of a repo3 failure and it is
  precisely why the row and its escalation exist. Go read the reason: `neuro probe report --lane canonical
  --key repo3_freshness` and its `detail=` field.

Then confirm the durability surface:
```
uv run neuro probe report --lane canonical
```
Expect **all four** rows `status=ok` — `backup_freshness`, `wal_lag`, `lake_mirror_freshness`, and now
`repo3_freshness` (no longer `MISSING`, the §0e forecast having been discharged by §5a's seed).

**§7c** — bank: the state doc **edited in place** (preamble + all three edit-stamp fields + a §G box) and the
log appended with its line count **re-measured**, never assumed `+1`.

---

## §ROLLBACK — every irreversible step, with its undo AND its non-undo

| Step | Undo | Non-undo |
|---|---|---|
| §3 conf edit | **The A2-7 §7 lever**: remove the `repo3-*` lines from the conf, `sudo -u postgres pgbackrest --stanza=neuro check`, reload. Archive-push returns to repos 1+2. **This is also the emergency lever if repo3 wedges archiving.** | — |
| §2g key mint | Revoke the application key in the console. | — |
| §2b bucket + **compliance lock** | **NONE.** Objects already written are locked for **L** days and cannot be deleted by anyone, including the account owner. The bucket is abandoned in place and the spend line stands **until the lock lapses — L days from the last write.** | ⚠ This is the one-way door; it is why §2a is a recorded decision. |
| §5a `durability seed` | **NO un-seed verb exists.** The row is permanent. | Aborting after §5a leaves a permanently blocked `repo3_freshness` row that a daily timer will alert on. If aborting here, **also disable `neuro-repo3-escalate.timer`** or the alert fires forever about an arm that no longer exists. |
| §4 unit install | `systemctl disable --now`, restore the previous `neuro-backup.service` from §1's `/tmp/live-neuro-backup.service` — ⚠ **strip the `systemctl cat` header lines first** (`# /etc/systemd/system/...`, and one per drop-in): that capture is a concatenated LISTING, not a unit file, and installing it verbatim would merge any drop-in into the base unit. Then `daemon-reload`. | — |

**IF THIS STOPS HALFWAY** — the ordering is chosen so the reversible steps come first:
- stopped **before §3**: nothing has changed; no action.
- stopped **between §3 and §5a**: repo3 is in the WAL path but has no row. If it is failing, use the
  §ROLLBACK lever *now* rather than leaving it — a failing repo3 refuses canonical writes through `wal_lag`.
- stopped **after §5a**: see the seed row above — disable the escalate timer before walking away.

---

## §NOT DONE HERE (each with its ground)

- **No promotion.** `GATE_CONSULTED_KEYS` is unchanged and `GATE_BASIS_REPOS` stays `{1, 2}`. Promotion is a
  **later owner ruling** (§F, "armed, not dated") and is deliberately two edits: move the pin, and drop the
  `-` prefixes in `neuro-backup.service`.
- **No desktop demotion.** That is tailnet step 2 and is gated on the promote (§A·73).
- **No `neuro probe triage` verb.** Registered follow-on, different surface.
- **No repo renumber, ever, without a key migration.** `repo3_freshness` is a natural key in
  `system_health`; renumbering the pgbackrest repos would make it name the wrong one.
