# RUNBOOK — repo3: a third pgbackrest repository on Backblaze B2 (NOTIFY-ONLY)

**EXECUTION: guided-required** (one command at a time, owner runs every command).
Guided-required is forced three times over: a **literal one-way door** (a COMPLIANCE-mode object lock nobody
can lift), a **credential mint**, and **first-time territory** (no repo3 has ever existed here).

**STATUS: DRAFTED — NOT EXECUTED.** Nothing in this runbook has been run. Every expected value below is a
FORECAST, not a measurement, and any divergence from one is a FINDING — stop, do not improvise past it.
*(When this is executed, that banner changes and §-by-§ expectations become measured facts. The corpus-import
runbook shipped for a day asserting both at once; do not repeat that.)*

Ruling: **§A·72** (owner, 2026-08-27). Code unit: HEAD at drafting `baf021a` + this unit.
Owner nods carried (2026-08-28): the gate-basis pin in **both** directions; the WAL coupling
**accepted with pgbackrest ≥ 2.59.0 as a hard precondition**.

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
runbook, so nobody reads §7's gate-open proof as more than it is.

---

## §0 — MEASURE FIRST (nothing is changed in this section)

Run each command and **read the output**; do not proceed past a divergence.

**§0a — where am I.** Every command below runs on the canonical VM unless it says DESKTOP.
```
echo "host=$(hostname)"          # expect: neuro-canonical-pg
```
*(The host echo is not ceremony: in a guided session with an open SSH session and local shells, a VM command
run locally returns a perfectly formed, perfectly meaningless answer.)*

**§0b — the VM checkout is ROUTINELY BEHIND the banked HEAD.** Measure it; do not assume.
```
cd /home/ubuntu/neuromancer-llm && git rev-parse --short HEAD
```
If it is not this unit's sha, advance **forward-only, proven**:
```
git fetch origin
git merge-base --is-ancestor <old-sha> <new-sha>; echo $?     # expect 0 — read $?, never chain with &&
git checkout <new-sha>
git status --porcelain                                        # expect empty
uv sync                                                       # expect a no-op; if it is not, STOP
```

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
```
⚠ Widen the window rather than reading the last entry: **each repo mints its OWN backup label seconds apart**
(measured: `20260801-010505F` repo1 + `20260801-010509F` repo2), so a narrow `tail` reads as if repo1 were
missing.

**§0f — the conf's SECTION MAP, because §3 depends on it.**
```
sudo grep -n '^\[' /etc/pgbackrest/pgbackrest.conf
```
Record the line numbers of `[global]` and `[neuro]`. §3 inserts **before** `[neuro]`, and this is the
measurement that makes that possible. ⚠ Two tracked records disagree about where the repo2 key currently
sits (the a2-7 execution note says it was moved before `[neuro]`; `tests/test_provisioning_invariants.py`'s
fixture places it inside `[neuro]`). **The file is the authority — read it, and record which record was
right.**

**§0g — sequencing.** The **`0004` canonical apply is FIRST in the execution line** (§A13). This deploy runs
after it. Confirm:
```
uv run alembic current                 # expect 0004_worker_registrar_role_split (head)
```

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

**§2a — the retention arithmetic, worked here so the console steps have real inputs.**
Let **R3** = repo3's pgbackrest `repo3-retention-full` (days) and **L** = the compliance lock retention (days).
- `verify-config` assertion 2 takes **min() over ALL repos**, and the floor is
  `BACKUP_STALE_AFTER(8d) + BASE_BACKUP_INTERVAL(2d) + PROVISIONING_MARGIN(2d)` = **12 days**.
  ⇒ **R3 ≥ 12.** Setting **R3 = 30** matches repos 1 and 2 and leaves `min()` where it is; anything in
  [12, 29] passes but makes repo3 the repo named in any future floor violation.
- The ruling: **L strictly under R3**, so pgbackrest's own `expire` is never fighting the lock. ⇒ **L ≤ R3−1.**
- L bounded BELOW by detect-plus-respond: the repo3 arm's own onset is 4 days and the daily escalation adds
  a day, so **L comfortably above ~7** is the point of having a lock at all.
- B2's documented range is **1–3000 days**.
- **RECORD HERE AT EXECUTION: R3 = ____ , L = ____ .**

**§2b — the bucket.** Create a **NEW** bucket with **Object Lock enabled at creation**. Do not retrofit: B2
documents default-bucket-retention as a create-time setting, and a bucket without it silently accepts
unlocked objects.
- versioning **ON**
- default retention mode **COMPLIANCE**, **L** days (from §2a)
- ⚠⚠ **THIS IS THE ONE-WAY DOOR.** Compliance retention *"cannot be removed by any user"* — only **extended**.
  Not by the account owner, not by support. A mis-sized L means paying for locked garbage until it lapses.
  **Re-read §2a's L before clicking.**

**§2c — the lifecycle rule.** ⚠ **TWO SEPARATE INEQUALITIES; do not collapse them.**
- `L ≤ R3−1` (§2a) governs whether pgbackrest's **`expire`** delete succeeds.
- **`daysFromHidingToDeleting ≥ L`** governs the **lifecycle reaper**, and it is independent of R3 — the
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
- Size the floor from measurement, not from memory: take repo1's current on-disk size from §0e's
  `pgbackrest info`, and **read B2's current published rate at execution time** — never a rate hardcoded
  here; a price is an expiring fact, and the vendor page is the out-of-band refresh (the §A·40 lane-(b)
  idiom). Add the version tail from §2c and the access-log bucket from §2d.
- ⚠ **§A·39 fixes R3 at $10/mo split $6 / $1.50 / $2 / $0.50 — "owner-adjustable DATA, never edited without a
  ruling."** §A·72 authorizes *an* R3 line for repo3 but sets no number, and does not say whether the $10
  total moves. **That is an owner figure this runbook must not invent.**
- **RECORD: monthly cap = $____ ; owner ruling on the R3 total: ____________.**

**§2f — RECORD THE ENDPOINT COORDINATES.** The bucket's region determines both the endpoint host and the
`repo3-s3-region` value, and §3a carries a `<region>` placeholder in two options. Read them off the bucket's
own page in the console (B2's S3 endpoint has the form `s3.<region>.backblazeb2.com`) and write them down
here before §3, so the conf is not authored from memory.
- **RECORD: region = ____________ ; endpoint host = s3.____________.backblazeb2.com .**

**§2g — the application key.** Mint a key **scoped to the one bucket** (never an account-wide key). Copy it
once; §3 seats it without it touching argv, history, or this transcript.

---

## §3 — THE CONF EDIT (root-only; the credential never rides a command line)

⚠⚠ **DO NOT USE `tee -a`.** It appends to **end-of-file**, which lands **inside `[neuro]`** — the a2-7
execution recorded exactly that as a latent placement bug and worked around it with an insert-before. The
`read -rs` half of that idiom is the proven part; the append half is not.

**§3a — seat the non-secret block, BEFORE `[neuro]`** (use §0f's line map). Values from §2:
```ini
repo3-type=s3
repo3-path=/neuro
repo3-s3-bucket=<the bucket from §2b>
repo3-s3-endpoint=s3.<region>.backblazeb2.com
repo3-s3-region=<region>
repo3-s3-uri-style=path
repo3-retention-full-type=time
repo3-retention-full=<R3 from §2a>
repo3-bundle=y
```
⚠ `repo3-retention-full-type=time` is **not optional**: pgbackrest defaults to `count`, which would silently
turn R3 from *days* into *backups*. Assertion 1 catches it in §5, but do not rely on the checker to author
the file.

**§3b — seat the secret, without it reaching argv or history.** Paste `read -rs` **alone**, then the key,
then the consumer; insert **before `[neuro]`**, never appended:
```
read -rs B2KEY
```
```text
<paste the application key alone on this line>
```
Then insert `repo3-s3-key-secret=$B2KEY` (and `repo3-s3-key=<keyID>`) before the `[neuro]` line with a
root-side insert, and:
```
unset B2KEY
sudo chmod 600 /etc/pgbackrest/pgbackrest.conf
sudo chown postgres:postgres /etc/pgbackrest/pgbackrest.conf
```

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
are in place). The escalation timer is started in §7g, deliberately **after** the first green probe, so the
born-blocked row cannot fire an alert that means nothing.
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
Expect, **in the order the command prints them**: `verify-config OK: repos [1, 2, 3]; retention repo1=30d,
repo2=30d, repo3=<R3>d`, then `backup timer cadence checked: OnUnitActiveSec == BASE_BACKUP_INTERVAL`, then
`archiver-probe timer cadence checked: OnUnitActiveSec == ARCHIVER_PROBE_INTERVAL`.

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
Expect **9** timers (the 8 previously measured + this one). If the count differs, that is a FINDING.

**§7b** — one full natural cycle:
```
sudo systemctl start neuro-backup.service ; echo "exit=$?"
```
Expect exit 0 (`Type=oneshot` blocks until every step finishes), then `pgbackrest info` showing **three**
labels seconds apart, and `neuro probe report` showing all four rows `ok`.

**§7c** — bank: the state doc **edited in place** (preamble + all three edit-stamp fields + a §G box) and the
log appended with its line count **re-measured**, never assumed `+1`.

---

## §ROLLBACK — every irreversible step, with its undo AND its non-undo

| Step | Undo | Non-undo |
|---|---|---|
| §3 conf edit | **The A2-7 §7 lever**: remove the `repo3-*` lines from the conf, `sudo -u postgres pgbackrest --stanza=neuro check`, reload. Archive-push returns to repos 1+2. **This is also the emergency lever if repo3 wedges archiving.** | — |
| §2f key mint | Revoke the application key in the console. | — |
| §2b bucket + **compliance lock** | **NONE.** Objects already written are locked for **L** days and cannot be deleted by anyone, including the account owner. The bucket is abandoned in place and the spend line stands **until the lock lapses — L days from the last write.** | ⚠ This is the one-way door; it is why §2a is a recorded decision. |
| §5a `durability seed` | **NO un-seed verb exists.** The row is permanent. | Aborting after §5a leaves a permanently blocked `repo3_freshness` row that a daily timer will alert on. If aborting here, **also disable `neuro-repo3-escalate.timer`** or the alert fires forever about an arm that no longer exists. |
| §4 unit install | `systemctl disable --now`, restore the previous `neuro-backup.service` from §1's `/tmp/live-neuro-backup.service` — ⚠ **strip the
`systemctl cat` header lines first** (`# /etc/systemd/system/...`, and one per drop-in): that capture is a
concatenated LISTING, not a unit file, and installing it verbatim would merge any drop-in into the base unit.
Then `daemon-reload`. | — |

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
