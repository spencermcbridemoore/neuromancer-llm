# RUNBOOK — the quarterly restore drill, FROM the desktop mirror (refreshed 2026-10)

**EXECUTION: guided-required** (one command at a time). Forced by **first-time territory**: the first restore
under pgbackrest 2.59.1; the first run of mechanisms that have never executed here — the vendor's
`--archive-mode=off`, a drill-local lock and spool path, and the peer-auth scrub; the first scrub of a real
clone carrying migrations `0003`/`0004`; and the first drill since a COMPLIANCE-locked repo3 entered the live
conf. The other three triggers, stated at their true size so the declaration is not read as narrower than it
is. **Credential sequence — none:** no password, no minted DSN, no key; §4 runs on peer authentication.
**One-way doors — no step opens one as written, but three stand one edit away, under root or a superuser:**
§3c (a clone that archived), §4c (a scrub aimed at canonical) and §5c (`rm -rf` on the volume that holds
canonical). Guards hold the first; **paste discipline is most of what holds the other two.**
**Interpretation — reduced, not removed:** §2a, §2d, §3a, §3b and §4b are computed lines compared against
literals; §0f, §0g, §2b, §4a, §4f and §5e are readings compared by eye. The drill's one prior execution was
guided and needed live untangling (log:163).
*The mode is re-examined against all four triggers by whichever session next edits this file, and checked by
its vet.*

**STATUS: AUTHORED, NOT RUN.** No command below has been executed as part of this runbook, on any host.
Authored at the commit that first adds this file — `git log --diff-filter=A --format=%H -- ops/runbook-restore-drill.md`
prints it. *(A runbook cannot name the sha of the commit that contains it — the 2026-08-31 §G box, finding (a).)*
**Nothing in this file discharges the quarterly drill; only an execution, verified and banked by a session, does.**

Authority: the owner's ruling of 2026-10-02 (log:297, Q5) — *"a refreshed restore-drill runbook"* as a tracked
file under `ops/`; *"This rules the restore drill's durable home as ops/."* The procedure it refreshes is
`scratch/restore-drill-runbook-2026-07-13.md` (gitignored, left in place), executed once, 2026-07-13 UTC,
all gates green (log:163 — a physical line; the state of record's §F row cites the same entry as `log:162`,
a cite banked before the 2026-07-20 frontmatter shift).

**The drill's SOURCE is ruled and does not change here:** it restores **FROM THE DESKTOP MIRROR** (the phase-0
§6 #8 ruling; log:163), never from repo1 directly, never from Azure, never from repo3. The state of record's
§A·72 demotes the desktop mirror only when the repo3 promotion lands, and it has not.

**How to read the expectations.**
- **MEASURED** — observed first-hand, with its cite (`log:N` is physical line N of the engagement log; a path
  is tracked source in this repo; *desktop probe* is this unit's own measurement on the desktop, 2026-10-06,
  against a throwaway `postgres:18` container — never the VM, never canonical). A reading cited to log:163 was
  taken at pgbackrest **2.58.0** on a clone that predated migration `0003`.
- **FORECAST** — derived, never observed on this system. A FORECAST that fails is a FINDING about **this
  runbook** before it is one about the system. Stop either way.
- **A literal is its reading AS OF its cite.** This file is run every quarter. A mismatch that the state of
  record explains — a later migration, a new durability row, a package upgrade — is a finding about a stale
  RUNBOOK, not about the system. Stop all the same, and say which it is.

---

## WHAT THIS DRILL PROVES, AND WHAT IT MUST NEVER DO

**It proves** that the copy on the desktop NVMe — the one copy no cloud deletion reaches — restores into a
running PostgreSQL that carries the real data, and that the identity pair holds end to end: a verbatim clone
READS as canonical until it is scrubbed, and after the scrub the repo-pinned uuid REFUSES it
(`src/neuromancer_llm/db/restore.py`, `src/neuromancer_llm/db/canonical_instance.py`).

**It must never do three things. Each has its guards; know them before you start.**

| The drill must never… | Why it could | What holds it shut |
|---|---|---|
| **let the clone ARCHIVE** | MEASURED (log:163, finding 2): the clone's `postgresql.auto.conf` carries canonical's `archive_mode='on'` and a bare `archive_command`. DERIVED, never observed: a started clone that archived would hand its new-timeline WAL to the **real** `/etc` conf, which now names repo1, Azure **and B2** — and B2 holds a 14-day COMPLIANCE lock (MEASURED, log:285), so nobody could delete what landed there. *(By pgbackrest's source at 2.59.1 an `archive-push` run from a data directory that is not the conf's `pg1-path` is refused before it reaches a repository. That is also unmeasured, and no guard here is relaxed on the strength of it.)* | **(1)** `--archive-mode=off` on the restore (§2c, FORECAST); **(2)** the computed gate before start (§2d); **(3)** the `-o "-c archive_mode=off"` command-line override on the start, which outranks both conf files (§3c, MEASURED log:163); **(4)** `archive_mode=off` and `archiver=0,0` as the first reading after start (§4a). |
| **scrub CANONICAL** | The scrub rewrites `database_identity`. A verbatim clone is indistinguishable from canonical by identity, so the only guard is the connection TARGET (`db/restore.py`'s own docstring, which also says the guard does not resolve unix sockets). | **(1)** `--confirm-scratch`; **(2)** the target guard, which refuses when `--scratch-url` and `NEURO_DATABASE_URL` name the same host+port+database (MEASURED, desktop probe: it also refuses a scratch URL whose port is forgotten or typed `5432`); **(3)** §4b, a read-only proof that the scratch URL lands on the clone, taken from the **same shell variable** §4c then passes. ⚠ The guard compares two strings. **It cannot catch the two URLs being swapped, or a line that was edited** — paste §4c, never retype it. |
| **write or delete anything outside `/pgdata/restore-drill`** | `/pgdata` also holds canonical's data directory and repo1. And by pgbackrest's source at 2.59.1, `restore` clears `<spool-path>/archive/<stanza>` and takes a lock under `<lock-path>` — by default `/var/spool/pgbackrest` and `/tmp/pgbackrest`, shared by name with the live stanza `neuro` (FORECAST; the live conf's own spool path is elsewhere — log:285). | The drill conf pins `lock-path` and `spool-path` **inside** the drill directory (§2a, FORECAST). The teardown removes one literal path (§5c): paste it; never retype it; never use a variable in it. The one thing the drill puts outside the directory is the clone's socket, `/var/run/postgresql/.s.PGSQL.5433`, which leaves with the clone. |

---

## THE FIVE DR FINDINGS OF 2026-07-13 — carried forward

*Each was observed or source-verified then (log:163 and the 2026-07-13 runbook).*

1. **The Debian-split configs are NOT in the backup.** `postgresql.conf` and `pg_hba.conf` live in
   `/etc/postgresql/18/main/`, outside `pg1-path`. A restore alone does not rebuild them; a real DR rebuild is
   `ops/provision-canonical.sh` plus the runbooks. The drill supplies minimal ones (§3a, §3b) and does **not**
   exercise that rebuild.
2. **`postgresql.auto.conf` carries `archive_mode='on'` and a BARE `archive_command` into every restore**
   (canonical set them with ALTER SYSTEM). The `pg_ctl -o` command-line override is **MANDATORY** in any restore
   of this cluster. *Changed since:* the blast radius — see the table above.
3. **The recovery parameter floor.** The recovering instance needs `max_connections` at least the primary's
   (100); the first start on 2026-07-13 failed loud at 20. §0e re-reads the primary's value.
4. **`restore_command` embeds a COMMAND-LINE `--config`,** so recovery's archive-gets stay on the drill's
   mirror — source-verified at pgbackrest **2.58.0**. An environment-variable `PGBACKREST_CONFIG` would NOT be
   embedded: always pass `--config` as an argument. *Changed since:* the installed version is 2.59.1. The
   2.59.1 source was read by this file's post-build vet and says the same; it was read, not run, so §2d
   observes it (FORECAST at 2.59.1).
5. **`sftp get -r` into a MISSING local directory silently flattens one level** (OpenSSH 9.6,
   source-verified). §1a pre-creates the target; §1c checks the result.

## WHAT HAS CHANGED UNDER THE OLD TEXT SINCE 2026-07-13

| Change | Cite | How this file handles it |
|---|---|---|
| pgbackrest is **2.59.1**, not 2.58.0; 2.59.0 restricts running as root (*"only the restore command may be run as root by default"*) | log:285; the 2026-08-31 §G box, finding (i) | Every pgbackrest call here runs as `postgres`. None runs as root. Finding 4 is re-observed, not assumed. |
| The old text **sourced `/etc/neuro/env`** and read `NEURO_ADMIN_DATABASE_URL` from it | log:259 (do not source it: it holds the quarantined Azure key and the writer DSN); log:285 (it reaches systemd units only); log:271 (its `neuro_orch` line had been commented out, not removed, and was scrubbed to a placeholder) | Nothing is sourced. No variable from that file is used. No DSN is minted. |
| **`neuro_orch` was rotated** 2026-08-27, which bears on the old preflight's credential-lag note | log:271 | Moot by design: no step authenticates with a password. *(The clone's role passwords are still as-of the mirrored backup — which no longer matters to any step.)* |
| **Nine** timers are armed, not four; `system_health` holds **four** rows, not two | log:285 | §0g lists the timers; §4a expects the clone's row count to equal canonical's own. |
| Migrations **`0003` and `0004`** are applied: the clone carries seven triggers, the queue functions and the role split | log:259, log:279 | Read from source: the scrub is one `UPDATE` of `neuro.database_identity`. MEASURED (desktop probe, alembic head `0004`, grants applied): that table carries **no** trigger, and the scrub and both verifies behave exactly as §4 states. Not yet measured on a real clone. |
| The tailnet is **default-deny**; the VM→desktop sftp path depends on ROW 1 and on NordVPN being off | log:288; log:266 | §0h probes the path before anything is created. Never loosen the policy to make a probe pass (the owner's rule, log:288). |
| The VM checkout is **routinely behind** the banked HEAD | log:259, log:279, log:285 (found behind each time) | §0b measures it and records which sha's code the drill runs. The drill does not need it advanced — do not advance it for this. |
| The live conf now names **three** repositories | log:285 | The drill's own conf names only the mirror (§2a). See the hazard table. |
| The desktop chroot also holds `lake/` (the B-7 mirror) | log:225 | The pull names `backup` and `archive` only. |
| **NEW IN THIS FILE, not in the 2026-07-13 procedure** — each therefore FORECAST where the old step was measured | — | `--archive-mode=off` on the restore (§2c) · `lock-path` and `spool-path` in the drill conf (§2a) · `sftp -b -` for the listing and the pull (§0h, §1b) · the three small files written by `printf … \| tee` with a sha256 check, where the old text used heredocs (§2a, §3a, §3b; the six conf lines kept from then are unchanged) · the inspect grep anchored to line starts, plus a names-only listing and a computed gate (§2d) · the scrub and both verifies on peer authentication through password-free URLs (§4b–§4f) · `neuro` by absolute path · one labelled reading block in place of the old per-query gates — the old `probe_reports` count survives in it as a recorded line, not a gate. |

---

## §0 — PREFLIGHT (nothing is created or changed in this section)

Every command in this runbook runs **on the canonical VM**, in a **bash shell as `ubuntu`**, from the checkout
directory, with **no environment variable required**. Reach the VM the way you normally do (Tailscale SSH as
`ubuntu`). **One command per block, in order; read each output before the next.**
A trailing `; echo "exit=$?"` prints that command's own exit status and is part of its block.

**§0a — the working directory for everything below, and the host.**
*VM · bash as `ubuntu` · no env*
```bash
cd /home/ubuntu/neuromancer-llm
```
*VM · bash as `ubuntu` · cwd: the checkout · no env*
```bash
echo "host=$(hostname) pwd=$PWD"
```
Expect `host=neuro-canonical-pg pwd=/home/ubuntu/neuromancer-llm` — the path is **MEASURED** (log:279); the
hostname is **FORECAST** (`ops/runbook-repo3-b2.md` §0a carries the same expectation; the reading was never
banked). A desktop hostname means the wrong window: stop.

**§0b — the checkout sha: which code will this drill run?**
*VM · bash as `ubuntu` · cwd: the checkout · no env*
```bash
git rev-parse HEAD
```
Expect a 40-character sha — **FORECAST as to which.** **Record it.** It should be the sha the state of
record's Canonical-VM row names as this checkout's last advance, or a later one; it has been found **behind**
the stamp's HEAD at every apply that looked (MEASURED: log:259, log:279, log:285), and that is the normal
state, not a finding. **Do not advance the checkout for this drill.** The three `neuro` calls in §4 run the
code at *this* sha — the banking session compares it against the stamp.
*VM · bash as `ubuntu` · cwd: the checkout · no env*
```bash
git status --porcelain
```
Expect **no output** — **FORECAST** (the checkout read clean at log:285). Any line is a FINDING: stop.

**§0c — the verb exists at that sha.**
*VM · bash as `ubuntu` · cwd: the checkout · no env*
```bash
/home/ubuntu/neuromancer-llm/.venv/bin/neuro db restore-drill --help; echo "exit=$?"
```
Expect usage text naming `--scratch-url` and `--confirm-scratch`, then `exit=0` — **FORECAST as a terminal
reading**; the two options are MEASURED from source (`src/neuromancer_llm/cli/db.py`), and the venv path is
the one the installed units invoke (`ops/neuro-backup.service`). ⚠ `neuro` is called by its absolute path
throughout: it is not on `ubuntu`'s PATH (MEASURED, log:287).

**§0d — the pgbackrest version.**
*VM · bash as `ubuntu` · no env*
```bash
pgbackrest version
```
Expect `pgBackRest 2.59.1` — the version is **MEASURED as of log:285**; the line's wording is FORECAST. Any
other version is a FINDING: stop — §2a's two added options, §2c's option and §2d's gate were written against
2.59.1, and the package is PGDG-managed, so a later release is the likely cause.

**§0e — canonical's baseline. This one block is run four times in this runbook, on two ports.**
*VM · bash as `ubuntu` (psql runs as the OS user `postgres`, admitted by PEER authentication over the unix
socket — no password is involved) · targets CANONICAL, port 5432 · read-only*
```bash
sudo -u postgres psql -p 5432 -P pager=off -d neuro -Atc "select v from (values (1, 'identity=' || (select lane || ',' || instance_uuid || ',' || coalesce(cloned_from::text, 'NULL') from neuro.database_identity)), (2, 'is_pinned_instance=' || (select instance_uuid = 'c7a1b953-485a-4127-a2e6-a18f7423742a' from neuro.database_identity)), (3, 'alembic=' || (select version_num from neuro.alembic_version)), (4, 'system_health_rows=' || (select count(*) from neuro.system_health)), (5, 'probe_reports_rows=' || (select count(*) from neuro.probe_reports)), (6, 'triggers=' || (select count(*) from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'neuro' and not t.tgisinternal)), (7, 'archive_mode=' || current_setting('archive_mode')), (8, 'port=' || current_setting('port')), (9, 'data_directory=' || current_setting('data_directory')), (10, 'in_recovery=' || pg_is_in_recovery()), (11, 'archiver=' || (select archived_count || ',' || failed_count from pg_stat_archiver)), (12, 'floor=' || current_setting('max_connections') || ',' || current_setting('max_worker_processes') || ',' || current_setting('max_wal_senders') || ',' || current_setting('max_prepared_transactions') || ',' || current_setting('max_locks_per_transaction'))) t(o, v) order by o"
```
Expect twelve lines. The output FORMAT is **MEASURED** (desktop probe: this exact SQL on PostgreSQL 18).
The VALUES on canonical:

| Line | Expect | Status |
|---|---|---|
| `identity=` | `canonical,c7a1b953-485a-4127-a2e6-a18f7423742a,NULL` | MEASURED (log:259: lane and uuid; log:279: `cloned_from` NULL) |
| `is_pinned_instance=` | `true` | MEASURED (the pin is `db/canonical_instance.py`'s constant) |
| `alembic=` | `0004_worker_registrar_role_split` | MEASURED as of log:279 |
| `system_health_rows=` | `4` | MEASURED as of log:285 |
| `probe_reports_rows=` | a number — **record it**; it grows through the day | not a gate |
| `triggers=` | `7` | MEASURED as of log:279 |
| `archive_mode=` | `on` | MEASURED (log:163) |
| `port=` | `5432` | FORECAST |
| `data_directory=` | a path under `/pgdata` that is **not** `/pgdata/restore-drill/data` — record it | FORECAST (`ops/provision-canonical.sh` seats it at `/pgdata/18/main`) |
| `in_recovery=` | `false` | FORECAST |
| `archiver=` | two numbers — **record them**; §4f compares | the second was 59 at log:286; not a gate here |
| `floor=` | `100,8,10,0,64` | `100` MEASURED (log:163); the other four FORECAST (PostgreSQL's defaults) |

**A `floor=` field ABOVE its expected value is a FINDING — stop before §1:** the drill's minimal conf (§3a)
would abort recovery on the parameter floor (finding 3). Any other mismatch on a MEASURED line: stop.

**§0f — room on the volume.**
*VM · bash as `ubuntu` · no env*
```bash
df -h /pgdata
```
Expect `Avail` **above 20G** — MEASURED 2026-08-31 at 139G free on a 147G volume, 1% used (log:285). Less
than 20G is a FINDING: stop.

**§0g — no backup in flight, and none about to start.**
*VM · bash as `ubuntu` · no env*
```bash
systemctl is-active neuro-backup.service
```
Expect `inactive` — **FORECAST** (the unit is `Type=oneshot`, MEASURED at log:279, so it rests `inactive`
between cycles). `activating` means a backup is running **now**: wait for it to finish and re-run this block —
that is this file's instruction, not an improvisation. `failed` means the last cycle failed, which is when the
mirror is most likely stale or torn: a FINDING — stop.
*VM · bash as `ubuntu` · no env*
```bash
systemctl list-timers --no-pager --full 'neuro-*'
```
**Record the whole table.** Expect `neuro-backup.timer`'s NEXT to be **more than one hour away** —
**FORECAST** (the cadence is 2 days, `ops/neuro-backup.timer`). The end of every backup cycle rewrites the
mirror, and a pull that overlaps a push can copy a torn tree. If NEXT is nearer, wait until that cycle has run
and re-run §0g. **And carry the time forward:** if §1b is still running when that NEXT is fifteen minutes
away, interrupt it with Ctrl-C and go to §7. For corroboration only: nine timers were armed at log:285; the
count is not derivable from `ops/` (at least one installed timer has never been tracked), so §5e compares
against **this** table, not against nine.

**§0h — the path to the mirror.** On the **desktop**, before this step: NordVPN is **disconnected** and the
machine is awake. While NordVPN is connected, the VM cannot reach the desktop on any port and every Tailscale
status surface still reads healthy (MEASURED, log:266).
*VM · bash as `ubuntu` · no env*
```bash
timeout 5 bash -c 'cat </dev/null >/dev/tcp/100.77.118.14/22'; echo "exit=$?"
```
Expect `exit=0` — **MEASURED** as a reading (log:266, log:271): `0` is open; `124` is *no reply came back
within 5 s*. **`124` names no cause.** Four are on record: NordVPN connected on the desktop (log:266); the
desktop withheld from the VM's netmap by the tailnet policy (log:271); a listed peer without the port's grant,
under default-deny (log:288); a plain outage. `exit=1` with *Connection refused* means the host answered and
nothing listens on that port — MEASURED for the desktop's closed ports (log:288); for the desktop's sshd being
down it is FORECAST (log:215 and log:225 are the sshd-down incidents; neither was probed this way). On
anything but `exit=0`: stop; do not diagnose inside the drill; **never loosen the tailnet policy to make this
pass.**
*VM · bash as `ubuntu` (sftp runs as `postgres`: the `neuro-desktop` alias and its key belong to that user —
MEASURED, log:225) · no env*
```bash
printf 'ls -la\n' | sudo -u postgres sftp -b - -o BatchMode=yes neuro-desktop; echo "exit=$?"
```
Expect a listing that includes the directories **`backup`** and **`archive`**, then `exit=0` — **FORECAST**
as a reading. Those two names are MEASURED (log:163 pulled them); `lake` and `.neuro-mirror-manifest.json`
beside them are expected and are not used here (log:225; `src/neuromancer_llm/governance/backup_driver.py`).
Two forms here are not the banked ones, and are relied on knowingly: `sudo -u postgres` is the 2026-07-13
runbook's form for the pull (log:225 banks `sudo -iu postgres` for manual checks; ssh finds the alias through
the user's passwd home either way); and `-b -` (batch commands on standard input, abort on the first failure)
follows the shipped mirror, which runs `sftp -b <file>` on every cycle
(`src/neuromancer_llm/governance/sftp_transport.py`) — the 2026-07-13 pull did not use `-b`.

**§0i — nothing is left over from an earlier drill.**
*VM · bash as `ubuntu` · no env*
```bash
sudo test -e /pgdata/restore-drill; echo "exit=$?"
```
Expect `exit=1` (absent) — **FORECAST**. `exit=0` means an earlier drill was not torn down: a FINDING, stop.
*VM · bash as `ubuntu` (psql as `postgres`, peer auth) · targets port 5433 · read-only*
```bash
sudo -u postgres psql -p 5433 -d postgres -Atc "select 1"; echo "exit=$?"
```
Expect a connection error naming `.s.PGSQL.5433`, then `exit=2` — **FORECAST** (psql's documented status for a
failed connection). A printed `1` means something already listens on 5433: a FINDING, stop.

---

## §1 — PULL THE MIRROR BACK (the transport's restore direction)

**§1a — pre-create the targets (finding 5), and the drill's own lock and spool directories.**
*VM · bash as `ubuntu` · no env*
```bash
sudo install -d -m 700 -o postgres -g postgres /pgdata/restore-drill /pgdata/restore-drill/mirror /pgdata/restore-drill/data /pgdata/restore-drill/lock /pgdata/restore-drill/spool
```
Expect no output — the first three directories are the MEASURED 2026-07-13 form (log:163); `lock` and `spool`
are new here and **FORECAST**.

**§1b — the pull.**
*VM · bash as `ubuntu` (sftp as `postgres`) · no env*
```bash
printf 'get -r backup /pgdata/restore-drill/mirror\nget -r archive /pgdata/restore-drill/mirror\n' | sudo -u postgres sftp -b - -o BatchMode=yes neuro-desktop; echo "exit=$?"
```
Expect each command echoed as `sftp> get -r …`, a `Retrieving …` line per remote directory under it, and then
`exit=0` — **FORECAST** (read from OpenSSH 9.6's source: batch mode is quiet, so there is **no** per-file
line and no progress meter). The two `get -r` commands are the MEASURED form (log:163). The duration is
unmeasured: on 2026-07-13 the mirror held five backup sets; repo1 held nineteen at log:285, so this pull is
several times larger. **Silence between directory lines is not a hang** — but mind the NEXT time from §0g. A
non-zero exit is a FINDING: stop, go to §7.

**§1c — the pull landed un-flattened (finding 5).**
*VM · bash as `ubuntu` · no env*
```bash
sudo test -f /pgdata/restore-drill/mirror/backup/neuro/backup.info; echo "exit=$?"
```
Expect `exit=0` — **FORECAST**.
*VM · bash as `ubuntu` · no env*
```bash
sudo test -f /pgdata/restore-drill/mirror/archive/neuro/archive.info; echo "exit=$?"
```
Expect `exit=0` — **FORECAST**. Both paths follow from the repository layout the shipped driver reads
(`backup/<stanza>/backup.info` — `src/neuromancer_llm/governance/backup_driver.py`). `exit=1` on either means
the tree flattened or the pull was partial: stop, go to §7.
*VM · bash as `ubuntu` · no env*
```bash
sudo du -sh /pgdata/restore-drill/mirror
```
**Record the size.** No gate.

---

## §2 — A DRILL-SCOPED pgbackrest CONFIG, THEN THE RESTORE

⚠ **Never the `/etc` conf.** Its `pg1-path` is live canonical and it names all three real repositories. Every
pgbackrest call in this runbook carries `--config=/pgdata/restore-drill/pgbackrest-drill.conf` **as a
command-line argument** (finding 4) and runs **as `postgres`**.

**§2a — write the drill conf.** It holds no secret.
*VM · bash as `ubuntu` (the file is written as `postgres`) · no env*
```bash
printf '%s\n' "[global]" "repo1-path=/pgdata/restore-drill/mirror" "lock-path=/pgdata/restore-drill/lock" "spool-path=/pgdata/restore-drill/spool" "log-level-console=info" "log-level-file=off" "[neuro]" "pg1-path=/pgdata/restore-drill/data" | sudo -u postgres tee /pgdata/restore-drill/pgbackrest-drill.conf >/dev/null
```
Expect no output — **FORECAST** (the write mechanism is new). Six of the eight lines are the MEASURED
2026-07-13 conf (log:163); `lock-path` and `spool-path` are new and FORECAST — they keep the drill's restore
lock and its spool clean-up inside the drill directory instead of the defaults it would otherwise share by
name with the live stanza (the hazard table's third row).
*VM · bash as `ubuntu` · no env*
```bash
sudo sha256sum /pgdata/restore-drill/pgbackrest-drill.conf
```
Expect `f6f19504ff38b72e1ab3f28a4295d5e4451ffd56e5d674d8b342a708fe8352ec  /pgdata/restore-drill/pgbackrest-drill.conf`
— the digest is computed at authoring over exactly those eight lines; as a reading on the VM it is
**FORECAST**. A different digest means the paste garbled: stop, §7.

**§2b — the mirror reads as a repository.**
*VM · bash as `ubuntu` (pgbackrest as `postgres`) · reads the MIRROR ONLY*
```bash
sudo -u postgres pgbackrest --config=/pgdata/restore-drill/pgbackrest-drill.conf --stanza=neuro info
```
Expect `status: ok` and a list of `full backup:` entries — MEASURED 2026-07-13 at 2.58.0 (log:163: the sets
were faithfully present); with the two added conf lines, at 2.59.1, **FORECAST**. **Record the newest label.**
Anything but `status: ok` is a FINDING: stop, §7.

**§2c — the restore, with archiving disabled in the restored cluster.**
*VM · bash as `ubuntu` (pgbackrest as `postgres`) · writes under `/pgdata/restore-drill` only (`data`, and
its own `lock` and `spool`)*
```bash
sudo -u postgres pgbackrest --config=/pgdata/restore-drill/pgbackrest-drill.conf --stanza=neuro --archive-mode=off restore; echo "exit=$?"
```
Expect INFO lines ending in `restore command end: completed successfully`, then `exit=0`. That the mirror
restores is **MEASURED** at 2.58.0 without the option (log:163: 1523 files, 8.5 s). **`--archive-mode=off` is
FORECAST** — it has never been run here. The vendor's text for it: *"This option allows archiving to be
preserved or disabled on a restored cluster. This is useful when the cluster must be promoted to do some work
but is not intended to become the new primary. In this case it is not a good idea to push WAL from the
cluster into the repository."* — and its `off` mode: *"disable archiving by setting archive_mode=off."*
(the restore section's Archive Mode Option at <https://pgbackrest.org/configuration.html>; the same text in
<https://github.com/pgbackrest/pgbackrest/blob/release/2.59.1/src/build/help/help.xml>, read 2026-10-06.)
An error naming an option, or any non-zero exit, is a FINDING: stop, §7. **Do not retry without the option.**

**§2d — ★ THE GATE BEFORE START. Do not run §3c unless the third block prints PASS.**
*VM · bash as `ubuntu` (elevates to read the restored file) · read-only · prints parameter NAMES only*
```bash
sudo grep -o -E '^[[:space:]]*[a-z_][a-z0-9_.]*' /pgdata/restore-drill/data/postgresql.auto.conf
```
Evidence, pasted whole: the NAME of every setting the clone inherits from canonical's ALTER SYSTEM history —
every one of which outranks the minimal `postgresql.conf` of §3a. Expect `archive_mode` (twice),
`archive_command` and `restore_command` among them — **FORECAST**; the full list has never been banked.
**Any of these names is a FINDING — stop before §3c:** `archive_cleanup_command`, `recovery_end_command`
(the clone would run a command), or `hba_file`, `ident_file`, `config_file`, `data_directory`,
`unix_socket_directories`, `external_pid_file` (the clone would use a file outside the drill directory).
Any other name: record it and go on.
*VM · bash as `ubuntu` (elevates to read the restored file) · read-only*
```bash
sudo grep -n -E '^[[:space:]]*(archive_mode|archive_command|restore_command)' /pgdata/restore-drill/data/postgresql.auto.conf
```
Evidence, pasted whole. Expect the bare `archive_command` and `archive_mode = 'on'` that canonical's ALTER
SYSTEM left (MEASURED, log:163), a **later** `archive_mode` line reading off, and a `restore_command` carrying
`--config=/pgdata/restore-drill/pgbackrest-drill.conf`. The last two are **FORECAST** at 2.59.1.
*VM · bash as `ubuntu` (python as `postgres`, which owns the restored file) · read-only*
```bash
sudo -u postgres python3 -c "import re; t = open('/pgdata/restore-drill/data/postgresql.auto.conf').read(); am = re.findall(r'(?m)^[ \t]*archive_mode\s*=\s*(\S+)', t); rc = re.findall(r'(?m)^[ \t]*restore_command\s*=.*', t); ok = bool(am) and am[-1].strip(chr(39)) == 'off' and bool(rc) and '--config=/pgdata/restore-drill/pgbackrest-drill.conf' in rc[-1]; print('archive_mode_settings=%s' % am); print('restore_command_lines=%d' % len(rc)); print('PRESTART_GATE=' + ('PASS' if ok else 'FAIL'))"
```
Expect the last line to read exactly:
```
PRESTART_GATE=PASS
```
It is PASS only when the **last** `archive_mode` setting in the file is off (within one file the last entry
wins) **and** the last `restore_command` carries the drill's `--config`. The script is MEASURED on synthetic
files (desktop probe); its verdict on a real restore is **FORECAST**. **`PRESTART_GATE=FAIL` is a hard stop:
do not start the cluster. Go to §7.**

---

## §3 — MINIMAL CONFIGS (finding 1) AND THE GUARDED START (findings 2, 3)

**§3a — `postgresql.conf`.**
*VM · bash as `ubuntu` (written as `postgres`) · no env*
```bash
printf '%s\n' "port = 5433" "listen_addresses = '127.0.0.1'" "unix_socket_directories = '/var/run/postgresql'" "max_connections = 100" "archive_mode = off" | sudo -u postgres tee /pgdata/restore-drill/data/postgresql.conf >/dev/null
```
*VM · bash as `ubuntu` · no env*
```bash
sudo sha256sum /pgdata/restore-drill/data/postgresql.conf
```
Expect `a395dd038ec6097c8a79d97e4e96b38c036baf93cf8c1cb647503beab0a37833  /pgdata/restore-drill/data/postgresql.conf`
— computed at authoring; as a reading on the VM, **FORECAST**. The five lines are the MEASURED 2026-07-13
file (log:163; `max_connections = 100` is finding 3's repair).

**§3b — `pg_hba.conf` and an empty `pg_ident.conf`.**
*VM · bash as `ubuntu` (written as `postgres`) · no env*
```bash
printf '%s\n' "local   all   postgres                  peer" "host    all   all        127.0.0.1/32   scram-sha-256" | sudo -u postgres tee /pgdata/restore-drill/data/pg_hba.conf >/dev/null
```
*VM · bash as `ubuntu` · no env*
```bash
sudo sha256sum /pgdata/restore-drill/data/pg_hba.conf
```
Expect `54821c3547def7e04fceaf6c8819bc92a1f1602500538bdbf44d7dace4eef6ba  /pgdata/restore-drill/data/pg_hba.conf`
— computed at authoring; as a reading on the VM, **FORECAST**. The two lines are the MEASURED 2026-07-13
file (log:163).
*VM · bash as `ubuntu` (as `postgres`) · no env*
```bash
sudo -u postgres touch /pgdata/restore-drill/data/pg_ident.conf
```
The `local … postgres peer` line is what admits every gate in §4 without a password. A digest mismatch in
§3a or §3b: stop, §7.

**§3c — ★ THE START. A ONE-WAY DOOR STANDS BESIDE THIS COMMAND.**
Before pressing Enter, **read the line and find `-c archive_mode=off` in it.** Run it **once**.
*VM · bash as `ubuntu` (pg_ctl as `postgres`) · starts a SECOND postmaster on port 5433*
```bash
sudo -u postgres /usr/lib/postgresql/18/bin/pg_ctl -D /pgdata/restore-drill/data -l /pgdata/restore-drill/startup.log -o "-c archive_mode=off -c port=5433 -c listen_addresses=127.0.0.1" -w -t 120 start; echo "exit=$?"
```
Expect `server started`, then `exit=0` — the MEASURED 2026-07-13 line (log:163). On any other result, go to
§7: it stops the clone, captures its log, and only then removes anything. The one known loud failure is the
parameter floor (finding 3). ⚠ **Never run the start line a second time** — 2026-07-13's one confusion was a
double start (log:163). To see whether it is up, ask instead:
*VM · bash as `ubuntu` (as `postgres`) · read-only*
```bash
sudo -u postgres /usr/lib/postgresql/18/bin/pg_ctl -D /pgdata/restore-drill/data status
```
A record, not a gate — **FORECAST:** `pg_ctl: server is running (PID: …)` followed by the command line the
postmaster was started with, which carries `archive_mode=off`.

---

## §4 — THE GATES, THE PROOF, THE SCRUB, THE NEGATIVES

**§4a — ★ FIRST READING OF THE CLONE.** The §0e block, on port **5433**.
*VM · bash as `ubuntu` (psql as `postgres`, peer auth) · targets the CLONE, port 5433 · read-only*
```bash
sudo -u postgres psql -p 5433 -P pager=off -d neuro -Atc "select v from (values (1, 'identity=' || (select lane || ',' || instance_uuid || ',' || coalesce(cloned_from::text, 'NULL') from neuro.database_identity)), (2, 'is_pinned_instance=' || (select instance_uuid = 'c7a1b953-485a-4127-a2e6-a18f7423742a' from neuro.database_identity)), (3, 'alembic=' || (select version_num from neuro.alembic_version)), (4, 'system_health_rows=' || (select count(*) from neuro.system_health)), (5, 'probe_reports_rows=' || (select count(*) from neuro.probe_reports)), (6, 'triggers=' || (select count(*) from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'neuro' and not t.tgisinternal)), (7, 'archive_mode=' || current_setting('archive_mode')), (8, 'port=' || current_setting('port')), (9, 'data_directory=' || current_setting('data_directory')), (10, 'in_recovery=' || pg_is_in_recovery()), (11, 'archiver=' || (select archived_count || ',' || failed_count from pg_stat_archiver)), (12, 'floor=' || current_setting('max_connections') || ',' || current_setting('max_worker_processes') || ',' || current_setting('max_wal_senders') || ',' || current_setting('max_prepared_transactions') || ',' || current_setting('max_locks_per_transaction'))) t(o, v) order by o"
```
| Line | Expect on the CLONE | Status |
|---|---|---|
| `archive_mode=` | **`off`** | MEASURED (log:163, with the `-o` override) |
| `in_recovery=` | `false` | MEASURED (log:163: promoted) |
| `archiver=` | **`0,0`** — the clone has archived nothing and tried nothing | FORECAST |
| `port=` | `5433` | FORECAST |
| `data_directory=` | `/pgdata/restore-drill/data` | FORECAST |
| `identity=` | `canonical,c7a1b953-485a-4127-a2e6-a18f7423742a,NULL` — **the verbatim clone READS AS CANONICAL** | MEASURED (log:163) |
| `is_pinned_instance=` | `true` | MEASURED (log:163) |
| `alembic=`, `system_health_rows=`, `triggers=` | **the same three values §0e printed for canonical** | FORECAST (the clone is canonical as of its last mirrored backup) |
| `probe_reports_rows=` | a positive number, no larger than canonical's in §0e — **record it** | not a gate (the old drill read 5 here, log:163) |
| `floor=` | `100,8,10,0,64` — the drill's own minimal conf | FORECAST; not a gate on the clone |

⚠ **`archive_mode=` anything but `off`, or `archiver=` anything but `0,0`: go to §5a NOW** — stop the clone
first, report second. `in_recovery=true` means recovery is still replaying: wait thirty seconds and re-run
this block, up to five times (this file's instruction); still `true` after that is a FINDING — stop, §7.

**§4b — ★ PROVE THE SCRATCH URL LANDS ON THE CLONE — through the driver the scrub will use, read-only.**
The scratch URL is set **once**, here, and §4b, §4c and §4e all read it from this shell — so the URL that is
proved is the URL that is used. It holds no secret.
*VM · bash as `ubuntu` · sets the shell variable `SCRATCH_URL` in THIS shell*
```bash
SCRATCH_URL='postgresql+psycopg://:5433/neuro?host=/var/run/postgresql'
```
*VM · bash as `ubuntu` · reads `SCRATCH_URL` from this shell*
```bash
echo "SCRATCH_URL=${SCRATCH_URL}"
```
Expect `SCRATCH_URL=postgresql+psycopg://:5433/neuro?host=/var/run/postgresql` — MEASURED as a form (desktop
probe: it parses with port 5433 in its authority and the socket directory as its host). If you open a new
shell before §4e, re-run these two blocks; every later step fails loudly on an empty value.
*VM · bash as `ubuntu` (the venv's python as `postgres`, peer auth over the unix socket) · passes `SCRATCH_URL`
to that one command · read-only*
```bash
sudo -u postgres env SCRATCH_URL="$SCRATCH_URL" /home/ubuntu/neuromancer-llm/.venv/bin/python -c "import os, sqlalchemy as sa; c = sa.create_engine(os.environ['SCRATCH_URL']).connect(); print(c.exec_driver_sql('show port').scalar(), c.exec_driver_sql('show data_directory').scalar(), c.exec_driver_sql('select current_user').scalar())"
```
Expect exactly:
```
5433 /pgdata/restore-drill/data postgres
```
**FORECAST.** What is MEASURED (desktop probe): the driver derives `host=/var/run/postgresql port=5433` from
this URL, and the scrub's target guard reads it as distinct from canonical. That a peer-auth login then
succeeds is the charter's design question, **measured as far as a desktop can and no further**; the same
socket form on the default port ran as `postgres` through `neuro` at log:225. **Any other output, or an
error, is a hard stop: do not run §4c.** Stop the clone (§5a) and report.

**§4c — ★ THE SCRUB. A ONE-WAY DOOR STANDS BESIDE THIS COMMAND. Paste this line; do not retype or edit it.**
*VM · bash as `ubuntu` (`neuro` as `postgres`) · `NEURO_DATABASE_URL` is set for this ONE command — it names
canonical so the guard knows what to refuse, and it is never dialled · reads `SCRATCH_URL` from this shell ·
WRITES to the clone*
```bash
sudo -u postgres env NEURO_DATABASE_URL='postgresql+psycopg:///neuro?host=/var/run/postgresql' /home/ubuntu/neuromancer-llm/.venv/bin/neuro db restore-drill --scratch-url "$SCRATCH_URL" --confirm-scratch; echo "exit=$?"
```
Expect one line of this form, then `exit=0`:
```
restore-drill: scrubbed clone -> non-canonical (instance_uuid c7a1b953-485a-4127-a2e6-a18f7423742a -> <a new uuid>; cloned_from recorded)
```
The wording is **MEASURED** (desktop probe, alembic head `0004`; log:163 for the 2026-07-13 scrub). Through
peer auth on the real clone it is **FORECAST**. ⚠ **Neither URL carries a secret — that is the only reason the
`env VAR=…` form is safe here.** With a DSN that holds a password this form puts the password in `ps` and in
history (log:287); never copy this line's shape for one. A refusal (`error (fail closed): …`, `exit=1`) means
a guard fired: stop and paste it.

**§4d — the clone after the scrub.** The §0e block again, port **5433**.
*VM · bash as `ubuntu` (psql as `postgres`, peer auth) · targets the CLONE, port 5433 · read-only*
```bash
sudo -u postgres psql -p 5433 -P pager=off -d neuro -Atc "select v from (values (1, 'identity=' || (select lane || ',' || instance_uuid || ',' || coalesce(cloned_from::text, 'NULL') from neuro.database_identity)), (2, 'is_pinned_instance=' || (select instance_uuid = 'c7a1b953-485a-4127-a2e6-a18f7423742a' from neuro.database_identity)), (3, 'alembic=' || (select version_num from neuro.alembic_version)), (4, 'system_health_rows=' || (select count(*) from neuro.system_health)), (5, 'probe_reports_rows=' || (select count(*) from neuro.probe_reports)), (6, 'triggers=' || (select count(*) from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'neuro' and not t.tgisinternal)), (7, 'archive_mode=' || current_setting('archive_mode')), (8, 'port=' || current_setting('port')), (9, 'data_directory=' || current_setting('data_directory')), (10, 'in_recovery=' || pg_is_in_recovery()), (11, 'archiver=' || (select archived_count || ',' || failed_count from pg_stat_archiver)), (12, 'floor=' || current_setting('max_connections') || ',' || current_setting('max_worker_processes') || ',' || current_setting('max_wal_senders') || ',' || current_setting('max_prepared_transactions') || ',' || current_setting('max_locks_per_transaction'))) t(o, v) order by o"
```
Expect `is_pinned_instance=false`, and `identity=canonical,<the new uuid §4c printed>,c7a1b953-485a-4127-a2e6-a18f7423742a`
— the lane unchanged, a fresh uuid, the original recorded in `cloned_from`. MEASURED in shape (desktop probe;
log:163); on the real clone **FORECAST**. `archive_mode=off` and `archiver=0,0` must still hold.
⚠ **`is_pinned_instance=true` here, after §4c printed its success line, means the scrub landed somewhere
else.** Stop. Run the §4f block and paste it. **Repair nothing and tear nothing down:** if canonical's
identity row was rewritten, every canonical-lane writer now fails closed until an admin restores it, and that
repair is a session's work with the evidence intact.

**§4e — the pin REFUSES the scrubbed clone.**
*VM · bash as `ubuntu` (`neuro` as `postgres`) · `NEURO_DATABASE_URL` set for this one command to the CLONE,
from `SCRATCH_URL` in this shell · read-only*
```bash
sudo -u postgres env NEURO_DATABASE_URL="$SCRATCH_URL" /home/ubuntu/neuromancer-llm/.venv/bin/neuro db verify --lane canonical; echo "exit=$?"
```
Expect this line, then `exit=1`:
```
error (fail closed): instance_uuid mismatch: connected DB is <the new uuid> but c7a1b953-485a-4127-a2e6-a18f7423742a was pinned (fail closed).
```
MEASURED wording (desktop probe; log:163); on the real clone **FORECAST**. **Here `exit=1` WITH THAT MESSAGE
is the PASS** — `exit=1` with any other message is not (an empty `SCRATCH_URL` gives one). `verified: …` with
`exit=0` would mean the clone still passes for canonical: a FINDING — stop the clone (§5a) and report.

**§4f — canonical is untouched.** The §0e block, back on port **5432**.
*VM · bash as `ubuntu` (psql as `postgres`, peer auth) · targets CANONICAL, port 5432 · read-only*
```bash
sudo -u postgres psql -p 5432 -P pager=off -d neuro -Atc "select v from (values (1, 'identity=' || (select lane || ',' || instance_uuid || ',' || coalesce(cloned_from::text, 'NULL') from neuro.database_identity)), (2, 'is_pinned_instance=' || (select instance_uuid = 'c7a1b953-485a-4127-a2e6-a18f7423742a' from neuro.database_identity)), (3, 'alembic=' || (select version_num from neuro.alembic_version)), (4, 'system_health_rows=' || (select count(*) from neuro.system_health)), (5, 'probe_reports_rows=' || (select count(*) from neuro.probe_reports)), (6, 'triggers=' || (select count(*) from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'neuro' and not t.tgisinternal)), (7, 'archive_mode=' || current_setting('archive_mode')), (8, 'port=' || current_setting('port')), (9, 'data_directory=' || current_setting('data_directory')), (10, 'in_recovery=' || pg_is_in_recovery()), (11, 'archiver=' || (select archived_count || ',' || failed_count from pg_stat_archiver)), (12, 'floor=' || current_setting('max_connections') || ',' || current_setting('max_worker_processes') || ',' || current_setting('max_wal_senders') || ',' || current_setting('max_prepared_transactions') || ',' || current_setting('max_locks_per_transaction'))) t(o, v) order by o"
```
Expect **every line equal to §0e's** — **FORECAST** — with two allowances: `probe_reports_rows=` may have
grown, and `archiver=`'s FIRST number may have grown (canonical keeps probing and archiving its own WAL); its
SECOND — the failure count — must not have. `identity=` still ends `,NULL` and `is_pinned_instance=true`.
*VM · bash as `ubuntu` (`neuro` as `postgres`) · `NEURO_DATABASE_URL` set for this one command to CANONICAL ·
read-only (two SELECTs of the identity row)*
```bash
sudo -u postgres env NEURO_DATABASE_URL='postgresql+psycopg:///neuro?host=/var/run/postgresql' /home/ubuntu/neuromancer-llm/.venv/bin/neuro db verify --lane canonical; echo "exit=$?"
```
Expect `verified: lane=canonical instance_uuid=c7a1b953-485a-4127-a2e6-a18f7423742a`, then `exit=0` —
MEASURED wording (desktop probe; log:163: canonical verified green after the drill).

**§4g — the clone's own log, before it is stopped (evidence, every run; no gate).**
*VM · bash as `ubuntu` (elevates to read the log) · read-only*
```bash
sudo cat /pgdata/restore-drill/startup.log
```
Paste it whole. It is the direct record of what recovery fetched and from where, and §5c deletes it.

---

## §5 — TEARDOWN

**§5a — stop the clone.**
*VM · bash as `ubuntu` (pg_ctl as `postgres`) · stops the postmaster on port 5433 ONLY*
```bash
sudo -u postgres /usr/lib/postgresql/18/bin/pg_ctl -D /pgdata/restore-drill/data -w stop; echo "exit=$?"
```
Expect `server stopped`, then `exit=0` — the MEASURED 2026-07-13 form (log:163).

**§5b — it is gone.**
*VM · bash as `ubuntu` (psql as `postgres`) · targets port 5433 · read-only*
```bash
sudo -u postgres psql -p 5433 -d postgres -Atc "select 1"; echo "exit=$?"
```
Expect the connection error and `exit=2`, as in §0i — **FORECAST**. **If it prints `1`, something still
listens on 5433: do NOT run §5c.** Stop and report.

**§5c — remove the drill's directory. A ONE-WAY DOOR STANDS BESIDE THIS COMMAND. Paste it; do not retype it.**
*VM · bash as `ubuntu` (elevates) · DELETES `/pgdata/restore-drill` and nothing else*
```bash
sudo rm -rf /pgdata/restore-drill
```
*VM · bash as `ubuntu` · no env*
```bash
sudo test -e /pgdata/restore-drill; echo "exit=$?"
```
Expect `exit=1` — **FORECAST**.

**§5d — canonical is still serving.**
*VM · bash as `ubuntu` (psql as `postgres`, peer auth) · targets CANONICAL, port 5432 · read-only*
```bash
sudo -u postgres psql -p 5432 -P pager=off -d neuro -Atc "select 'canonical_up=' || current_setting('port')"
```
Expect `canonical_up=5432` — **FORECAST** (the format is MEASURED, desktop probe).

**§5e — the timers are as §0g found them.**
*VM · bash as `ubuntu` · no env*
```bash
systemctl list-timers --no-pager --full 'neuro-*'
```
Expect the same timer names as §0g's table — **FORECAST**. Nothing in this drill arms, stops or edits a unit.

No credential was typed, minted or exported at any step, so there is **no history scrub to run.**

---

## §6 — ACCEPTANCE (all of)

1. §2b: the **mirror** read as a repository, `status: ok`, sets listed.
2. §2d: `PRESTART_GATE=PASS` — archiving off in the restored file; `restore_command` on the drill's conf.
3. §4a: `archive_mode=off`, `in_recovery=false`, **`archiver=0,0`**, and the verbatim clone READ as canonical
   (`is_pinned_instance=true`) carrying canonical's own `alembic`, row and trigger counts.
4. §4b: the scratch URL landed on the clone — `5433 /pgdata/restore-drill/data postgres`.
5. §4c–§4d: the scrub ran; a fresh uuid; `cloned_from` holds the original; `is_pinned_instance=false`.
6. §4e: the pin **refused** the scrubbed clone, `exit=1`, with the mismatch message.
7. §4f: canonical unchanged — identity, `cloned_from` NULL, archiver failure count — and verified green.
8. §5: the clone stopped, the directory gone, canonical serving, the timers as found.

## §7 — IF IT STOPS PARTWAY

**1. Stop the clone first — always, at once: §5a, then §5b.** What §5a prints depends on how far the drill got
(**FORECAST**, read from PostgreSQL 18's `pg_ctl` source): before §2c completed, `… is not a database cluster
directory`; after §2c but before a successful §3c, `Is server running?`; both with `exit=1`, and both
expected in that case. **A RUNNING clone is never something to walk away from.**

**2. Then capture, before removing anything.** A **stopped** clone on disk harms nothing, and the directory is
the only local evidence. Run §4g (the clone's log, if a start was attempted) and the first two blocks of §2d
(the restored file's names and its three settings, if a restore ran), and paste them with everything above.

**3. Only then §5c and §5d** — unless §4d sent you here, in which case it has already told you to remove
nothing.

A stopped drill is a FINDING to bank, not a failure to hide.

## §NOT DONE HERE (each with its ground)

- **No repo3 restore leg.** The source is ruled (the desktop mirror); §A·72 changes that only when the repo3
  promotion lands. A B2 restore would also exercise a download cap and a credentialed read this drill avoids.
- **No repo2 / Azure leg, and no restore from repo1 in place.** Same ruling.
- **No DR rebuild.** Finding 1 stands: the `/etc` configs are not in the backup, and this drill supplies
  minimal ones instead of rebuilding from `ops/provision-canonical.sh`.
- **No judgement of the mirror's currency.** §2b records the newest label the mirror holds; whether that is
  the newest backup canonical has taken is the `backup_freshness` arm's job, not this drill's.
- **No lake restore.** `lake/` on the same chroot is the B-7 audit mirror; it is not pulled.
- **The clone still listens on `127.0.0.1:5433` with a scram line in its `pg_hba.conf`,** though no step uses
  either now. They are the measured 2026-07-13 start line and file, kept as measured; the clone is reachable
  from this host's loopback only, the same boundary canonical itself sits behind.

## WHAT TO PASTE BACK FOR THE BANKING SESSION

The **whole terminal transcript, unedited, from §0a to §5e** — every command as typed and everything it
printed, including errors. Do not summarise and do not trim. Add, in your own words:

1. the **date and time** of the run (the VM prints UTC; say which you are quoting);
2. the **checkout sha** §0b printed — the banking session compares it against the stamp's HEAD and checks, on
   the desktop, that `src/neuromancer_llm/db` and `src/neuromancer_llm/cli/db.py` do not differ between the two;
3. the **newest label** §2b listed, the **size** §1c recorded and roughly **how long §1b took**;
4. each of the eight acceptance lines, met or not;
5. for every expectation that did not match, whether this file had marked it **MEASURED** or **FORECAST**.
