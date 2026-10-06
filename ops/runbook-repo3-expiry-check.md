# RUNBOOK — the repo3 expiry check: is pgbackrest's `expire` removing aged backups from repo3? (READ-ONLY)

**EXECUTION: solo-OK** (owner executes; transcript + bank-after). Against §C's four triggers:
**no one-way door** — every command is a read (§READ-ONLY lists what is never run); **no credential sequence**
— pgbackrest reads its own conf, and nothing here prints, types or moves a key; **no output needs
interpretation** — the verdict is three computed lines compared against literals, and anything else is a
stop. **First-time territory is the judgement call, stated plainly:** the instruments have run on this host
(`--repo=3 info`, log:286; reads of this unit's journal, log:271), but nobody has yet taken the readings this
runbook gates on — **every gating value below is FORECAST.** What carries `solo-OK` is that the procedure is
read-only and that every way a forecast can miss is loud — a named non-PASS line or a Python error (each case
run on synthetic input at authoring) — so a wrong forecast costs one stopped, harmless sitting. Solo
execution keeps the stop-on-divergence duty: **any output that differs from the stated expectation is a
FINDING — stop, record it, never improvise past it.**

**STATUS: AUTHORED, NOT RUN.** No command below has been executed as part of this runbook, on any host.
Authored at the commit that first adds this file — `git log --diff-filter=A --format=%H -- ops/runbook-repo3-expiry-check.md`
prints it. *(A runbook cannot name the sha of the commit that contains it — the 2026-08-31 §G box, finding (a).)*
**Nothing in this file discharges the `deleteFiles` check-later; only the owner's run, verified and banked by a
session, does.**

Authority: the owner's ruling of 2026-10-02 (log:297, Q5) — *"a read-only check that repo3 expiry is deleting
aged backups"*, a tracked file under `ops/`, run by the owner at the VM on the owner's own schedule. The
check-later it serves was registered 2026-09-01 (log:286; the state of record's §A·72 and its §F row); a run
of this file, once verified and banked, is what discharges it.

**How to read the expectations.** Every expected output is marked one of two ways:
- **MEASURED** — observed first-hand on this system, with its cite (`log:N` is physical line N of the
  engagement log; a file path is tracked source in this repo).
- **FORECAST** — derived, never observed here. A FORECAST that fails is still a FINDING, but it is a finding
  about **this runbook** before it is one about repo3. Say which it was when you paste the transcript back.

---

## THE QUESTION, AND THE RULE THE ANSWER IS READ AGAINST

**The question:** has pgbackrest's `expire` removed aged full backups from repo3, or is repo3 only growing?

Why it is asked (all MEASURED, log:286): repo3's key was cut down to four capabilities on 2026-09-01, and
`deleteFiles` — the one expiry needs — had never been exercised, because repo3's retention is 30 days and its
first backup was then one day old. If deletes are refused, **backups keep succeeding while expiry silently
fails**: the repo3 lines of `neuro-backup.service` carry the ignore-failure `-` prefix
(`ops/neuro-backup.service`), so a failing repo3 step cannot fail the unit, and repo3 has no per-cycle alert of
its own (`src/neuromancer_llm/governance/repo3_freshness.py`).

**The vendor's rule for time-based retention**, at the installed version (2.59.1 — MEASURED, log:285), from
pgbackrest's own option reference, `repo-retention-full-type`:

> *"If set to time then full backups older than repo-retention-full will be removed from the repository if
> there is at least one other backup that is equal to or greater than the repo-retention-full setting. For
> example, if repo-retention-full is 30 (days) and there are 2 full backups: one 25 days old and one 35 days
> old, no full backups will be expired because expiring the 35 day old backup would leave only the 25 day old
> backup, which would violate the 30 day retention policy of having at least one backup 30 days old before an
> older one can be expired."*

Sources, read 2026-10-06 (the same paragraph at both; §1c prints the installed binary's own copy):
- <https://pgbackrest.org/configuration.html#section-repository/option-repo-retention-full-type>
- <https://github.com/pgbackrest/pgbackrest/blob/release/2.59.1/src/build/help/help.xml>

**How the pass condition follows — a SHAPE, computed at run time, with no date in it.** Call repo3's retention
**RF3** days (`repo3-retention-full`; the symbol is `ops/runbook-repo3-b2.md` §2a's). At each run, `expire`
takes the cutoff *now − RF3 days*, finds the **newest** full whose stop time is older than the cutoff, **keeps
that one**, and removes every full older than it (`expireTimeBasedBackup`, read at
<https://github.com/pgbackrest/pgbackrest/blob/release/2.59.1/src/command/expire/expire.c>). `expire` runs by
itself, in the same process, at the end of every successful `backup` (`expire-auto`, default `y`). So,
measured against the stop time of the **newest** repo3 full — which `expire` ran just after:

- **at most ONE repo3 full may be RF3 days old or older.** That one is the anchor the rule keeps.
- **if the SECOND-oldest repo3 full is also that old, aged fulls are not leaving repo3's catalog.** §2 computes
  exactly this.

**What that shape can and cannot see — read this before trusting either layer alone** (all from the same
source file, FORECAST: none of it has been observed on this host). `expire` works in three moves: it deletes
each expiring backup's two manifest objects; then it saves the catalog (`backup.info`); then it removes each
expired backup's directory, printing `remove expired backup <label>` *before* it tries. So:
- if B2 refuses deletes outright, the **first** move fails, the catalog is never rewritten, and the aged fulls
  stay listed — **§2 sees that**;
- if a delete is refused only at the **third** move, the catalog has already been rewritten and the listing
  looks healthy — **§2 is blind to that; only §3 sees it**, as an `ERROR` and an expire that did not end
  *completed successfully*, on that cycle and every later one;
- the line `remove expired backup` is therefore a **request**. What shows B2 **accepted** it is the same
  process then ending *completed successfully* with no `ERROR` — which is how §3 counts.

Two facts fix what "aged" means on the first run (MEASURED, log:285): the first repo3 full ever taken is
`20260831-201628F`, and a second, `20260831-201945F`, followed it the same evening. Once the newest repo3 full
is RF3 days younger than the second of those, the first can no longer be listed beside a healthy shape. Its
absence stays true on every later run, so it is not an expectation that goes stale.

⚠ **Two clocks, two events — do not merge them.** MEASURED (log:285, re-read through the B2 API at log:286):
the bucket is versioned, holds a COMPLIANCE lock of 14 days, and its lifecycle rule deletes a version 15 days
after it is **hidden**. FORECAST from those settings: "pgbackrest expired it" and "B2 has freed the bytes" are
different events, and bytes can stay in B2 for weeks after pgbackrest stops listing a backup. **This runbook
observes the first event only.**

---

## WHAT EACH STEP OBSERVES — AND WHAT A GREEN THERE DOES NOT PROVE

| Step | Layer it observes | A green reading proves | It does NOT prove |
|---|---|---|---|
| §2 | **pgbackrest's own listing** of repo3 (`info`, which reads repo3's catalog `backup.info`) | aged fulls have left pgbackrest's catalog: `expire` ran and got as far as saving it | that any backup directory was removed from B2 — the listing never looks at them, and a delete refused late leaves this layer green |
| §3 | **the backup unit's journal** (`neuro-backup.service`: what pgbackrest printed as it ran) | on every repo3 cycle in the window `expire` ended *completed successfully* with no `ERROR`, and at least one such clean cycle asked for a removal — i.e. B2 **accepted** the removals pgbackrest asked for | that B2 has freed the bytes (the two clocks above), or **which** key capability the removal used — this runbook does not establish that `deleteFiles` specifically was exercised |
| — | **the B2 side** (bucket size, versions, lifecycle) | *not observed here — see §NOT OBSERVED* | — |

---

## READ-ONLY BY CONSTRUCTION

Nothing below runs `expire`, `backup`, `stanza-*`, `check` or `verify`; edits or rewrites any file; writes to
`system_health` or any table; opens a database connection at all; starts or stops a unit; or calls B2 with
anything but pgbackrest's own `info` read. The only reads of `/etc/pgbackrest/pgbackrest.conf` are two
patterns **anchored to the option name `repo3-retention-full`**, which cannot match a key line.

⚠ **Paste every block; never retype one.** As printed, no command here can show a secret. Retyped, two can:
the §1b patterns are what keep the conf's key lines out of the output, and a shortened pattern or a dropped
`-n` would print them. **If you find yourself about to type anything not printed in this file, stop.**

⚠ **A result that looks like failed expiry is a FINDING, not a task.** Stop, record it, paste it back.
**Do NOT run `pgbackrest expire` by hand** — not to "see what it says", not with `--dry-run` improvised on the
spot. A manual expire against a repository in the WAL path is its own decision and is not made here.

---

## §0 — WHERE AM I, AND IS A BACKUP RUNNING

Every command in this runbook runs **on the canonical VM**, in a **bash shell as `ubuntu`**, with **no
environment variable required** and from **any working directory**. Reach it the way you normally do
(Tailscale SSH as `ubuntu` from the desktop). One command per block; run them in order.

**§0a — the host.**
*VM · bash as `ubuntu` · no env*
```bash
echo "host=$(hostname)"
```
Expect `host=neuro-canonical-pg` — **FORECAST** (`ops/runbook-repo3-b2.md` §0a carries the same expectation and
the 2026-08-31 deploy recorded no divergence from it; the reading itself was never banked). A desktop
hostname here means you are in the wrong window: stop.

**§0b — no backup cycle is in flight.**
*VM · bash as `ubuntu` · no env*
```bash
systemctl is-active neuro-backup.service
```
Expect `inactive` — **FORECAST** (the unit is `Type=oneshot`, MEASURED at log:279, so it rests `inactive`
between cycles). `activating` means a cycle is running **now**: a listing or a journal read taken mid-cycle
gives a false FINDING, so wait for it to finish and re-run this block — that is this file's instruction, not
an improvisation. `failed` means the last cycle failed: a FINDING — record it, then **still run §1 to §3b**
(they are reads, and they are the evidence), and paste everything.

---

## §1 — THE VERSION, THE RETENTION, THE RULE AS INSTALLED

**§1a — the pgbackrest version. The rule quoted above was read at 2.59.1.**
*VM · bash as `ubuntu` · no env*
```bash
pgbackrest version
```
Expect `pgBackRest 2.59.1` — the version is **MEASURED as of log:285** (upgraded 2.58.0 → 2.59.1 from PGDG,
`/usr/bin/pgbackrest`); the line's exact wording is **FORECAST**. **Any other version is a FINDING — stop.**
It is most likely a finding about a stale RUNBOOK (the package is PGDG-managed and newer releases exist), not
about the system: the rule and the strings §3 matches must be re-read at that version before a verdict from
this file can be trusted.

**§1b — RF3, re-read live. Never assume it.**
*VM · bash as `ubuntu` (the command itself elevates to root to read the `600 postgres:postgres` conf) · no env*
```bash
sudo grep -n -E '^repo3-retention-full(-type)?=' /etc/pgbackrest/pgbackrest.conf
```
Expect exactly two lines, in this order, each prefixed by its line number:
```
<n>:repo3-retention-full-type=time
<n>:repo3-retention-full=30
```
**MEASURED** for both values (log:285: *"RF3=30 days, matching … retention-full=30 -type=time"*, all eleven
`repo3-*` names inside `[global]`); their order follows the block `ops/runbook-repo3-b2.md` §3a seated. The
line numbers are **FORECAST** and are not a gate. ⚠ `time` is load-bearing: pgbackrest's default is `count`,
under which the number means *backups*, not *days*, and the rule above does not apply. **A missing line, a
third line, `count`, or a number other than 30 is a FINDING — stop.**

Then capture the number for §2:
*VM · bash as `ubuntu` (elevates to read the conf) · sets the shell variable `RF3` in THIS shell*
```bash
RF3=$(sudo sed -n 's/^repo3-retention-full=//p' /etc/pgbackrest/pgbackrest.conf)
```
*VM · bash as `ubuntu` · reads `RF3` from this shell*
```bash
echo "RF3=${RF3}"
```
Expect `RF3=30` — **MEASURED** (the same cite). ⚠ §2 reads `$RF3` from this shell. If you open a new shell
before §2, re-run these two blocks first; §2 fails loudly, not quietly, on an empty value.

**§1c — the rule, as the installed binary states it (a record, not a gate).**
*VM · bash as `ubuntu` (pgbackrest runs as `postgres`: `help <command>` loads the conf, which only `postgres`
can read) · no env*
```bash
sudo -u postgres pgbackrest help expire repo-retention-full-type
```
**FORECAST:** prints the option's description, whose `time` paragraph is the text quoted above. **No gate
hangs on this step.** If it prints an error instead, record the error and continue to §2 — that is this
file's instruction, not an improvisation. Paste whatever it prints.

---

## §2 — LAYER ONE: pgbackrest'S OWN LISTING OF repo3

*VM · bash as `ubuntu` (pgbackrest runs as `postgres`, which can read the conf; the script runs as `ubuntu`) ·
needs `RF3` from §1b in this shell*
```bash
sudo -u postgres pgbackrest --stanza=neuro --repo=3 info --output=json | python3 -c '
import json, sys, datetime as d
rf3 = int(sys.argv[1])
st = [s for s in json.load(sys.stdin) if s.get("name") == "neuro"][0]
print("stanza_status=%s" % json.dumps(st.get("status")))
print("repo_status=%s" % json.dumps(st.get("repo")))
f = sorted(
    (float(b["timestamp"]["stop"]), str(b["label"]))
    for b in st.get("backup", [])
    if b.get("type") == "full" and int(b["database"]["repo-key"]) == 3
)
assert len(f) >= 2, "fewer than two repo3 fulls listed - STOP, paste this"
new = f[-1][0]
now = d.datetime.now(d.timezone.utc).timestamp()
utc = lambda t: d.datetime.fromtimestamp(t, d.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
print("repo3_fulls=%d rf3_days=%d" % (len(f), rf3))
for t, l in f:
    print("  %s stop=%s age_at_newest=%.2fd" % (l, utc(t), (new - t) / 86400))
print("newest_full_age_now=%.2fd" % ((now - new) / 86400))
first = "20260831-201628F" in [l for _, l in f]
print("first_repo3_full_on_record_listed=%s" % first)
if (now - new) / 86400 > 4:
    print("VERDICT=STALE newest repo3 full is over 4 days old: expiry cannot be judged")
elif (new - f[1][0]) / 86400 >= rf3:
    print("VERDICT=FINDING the second-oldest repo3 full is past RF3: aged fulls are NOT leaving")
else:
    print("VERDICT=OK at most one repo3 full is past RF3")
' "$RF3"
```
⚠ Paste the block whole, from `sudo` to the closing `"$RF3"`, and **check before pressing Enter that the last
line reads `' "$RF3"`**. Long pastes on this host have echoed with a garbled tail while the bytes that ran
were correct (log:286 — three of five, judged from the output, never from the echo). If the echo looks
garbled, press Ctrl-C and paste again; **a second garble is a FINDING — stop.** A script that did run is
judged by its output: it either prints the lines below or dies with a Python error (FORECAST).

What it does: asks pgbackrest for repo3's catalog as JSON — the form the shipped probes parse on every cycle
(`src/neuromancer_llm/governance/backup_driver.py::newest_full_per_repo`; `--repo` is valid on `info` at
2.59.1, MEASURED log:286) — prints pgbackrest's own status for the stanza and the repository, then every repo3
full with its age, then the lines to compare.

**Expect, and compare these two lines exactly:**
```
first_repo3_full_on_record_listed=False
VERDICT=OK at most one repo3 full is past RF3
```
plus `newest_full_age_now=` reading **under 4** (the backup cadence is 2 days — `ops/neuro-backup.timer`,
MEASURED ~2-daily at log:288). All three are **FORECAST**.

For orientation only, **not a gate** (FORECAST): with RF3 = 30 and a 2-day cadence expect on the order of 15
to 20 fulls, the oldest between 30 and about 36 days older than the newest. The upper end is not a round
number because the VM was shelved 2026-09-08 → 2026-09-11 and missed two cycles (MEASURED, log:288).

**Any of these is a FINDING. Record it — and then still run §3 and §3b before you stop:** they are reads, and
they are the layer that explains a §2 finding. Do not run `expire`.
- `VERDICT=FINDING …` — aged fulls are still in repo3's catalog: `expire` is not running, or is failing
  before it saves the catalog.
- `VERDICT=STALE …` — repo3 backups have stopped; that is a different fault (`repo3_freshness`), and expiry
  cannot be judged from a stale listing.
- `first_repo3_full_on_record_listed=True` beside `VERDICT=OK` — that pair is possible only while the newest
  repo3 full is less than RF3 days younger than `20260831-201945F`, and that window closed before this file
  was written. If you see the pair, something in this file's reasoning is wrong.
- a Python traceback, an `AssertionError`, or no `VERDICT=` line at all — the read itself failed (network,
  a key refused, a JSON shape this file did not expect). The two `…_status=` lines, if they printed, carry
  pgbackrest's own reason. Paste it whole.

---

## §3 — LAYER TWO: THE BACKUP UNIT'S JOURNAL

*VM · bash as `ubuntu` (elevates to read the journal) · no env*
```bash
sudo journalctl --no-pager -u neuro-backup.service --since "15 days ago" -o json | python3 -c '
import json, re, sys, datetime as d
runs = {}
for ln in sys.stdin:
    r = json.loads(ln)
    m, p = r.get("MESSAGE"), r.get("_PID")
    if not isinstance(m, str) or p is None:
        continue
    key = (r.get("_SYSTEMD_INVOCATION_ID"), p)
    if key not in runs:
        runs[key] = dict(t=int(r["__REALTIME_TIMESTAMP"]), r3=0, bk=0, eb=0, ee=0, rm=0, er=0)
    k = runs[key]
    if "backup command begin" in m and "--repo=3" in m: k["r3"] = 1
    if "backup command end: completed successfully" in m: k["bk"] = 1
    if "expire command begin" in m: k["eb"] = 1
    if "expire command end: completed successfully" in m: k["ee"] = 1
    if re.search("remove expired backup [0-9]{8}-[0-9]{6}F", m): k["rm"] += 1
    if "ERROR" in m: k["er"] += 1
r3 = sorted((k for k in runs.values() if k["r3"]), key=lambda k: k["t"])
bad = removed = 0
for k in r3:
    ok = k["bk"] and k["eb"] and k["ee"] and not k["er"]
    bad += 0 if ok else 1
    removed += k["rm"] if ok else 0
    ts = d.datetime.fromtimestamp(k["t"] / 1e6, d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = (ts, k["bk"], k["eb"], k["ee"], k["rm"], k["er"])
    print("%s backup_ok=%d expire_began=%d expire_ok=%d removed=%d errors=%d" % row)
print("repo3_cycles=%d clean_removed_total=%d bad_cycles=%d" % (len(r3), removed, bad))
v = "FINDING" if bad or not r3 else ("OK" if removed else "NO-REMOVAL-IN-WINDOW")
print("VERDICT_JOURNAL=" + v)
'
```
⚠ Paste it whole; **check the last line is a lone `'`** before pressing Enter. The §2 note on a garbled echo
applies here too.

What it does: reads the last 15 days of the unit's journal as JSON, groups the lines by process, keeps the
processes whose `backup command begin` line names `--repo=3`, and reports for each whether the backup
finished, whether `expire` then began and finished *completed successfully*, how many backups it asked to
remove, and how many `ERROR` lines it printed. A cycle is **clean** when all four hold; only clean cycles'
removals are counted. The unit runs one pgbackrest process per repository (`ops/neuro-backup.service`; each
repo mints its own label — MEASURED, log:259, log:285), and `expire` runs inside the same process as its
`backup` (pgbackrest `src/main.c` at the 2.59.1 tag).

**Expect, and compare the last line exactly:**
```
VERDICT_JOURNAL=OK
```
with one line per repo3 cycle above it, each reading `backup_ok=1 expire_began=1 expire_ok=1 … errors=0`. All
**FORECAST**. That this journal carries pgbackrest's `INFO` lines is MEASURED (log:279 read
`backup command begin 2.58.0` from it; the journal is persistent — log:271). But of the seven strings this
script matches, **six were read from pgbackrest's source at the 2.59.1 tag and never from this host's
journal** — the two `backup command` strings, the two `expire command` strings, `remove expired backup`
(`src/command/command.c`, `src/command/expire/expire.c`) and the `--repo=3` option inside the begin line. Only
`ERROR` has been seen in this journal (log:271).

**Any other last line is a FINDING — stop, run §3b, paste, and do not run `expire`:**
- `VERDICT_JOURNAL=FINDING` with `bad_cycles` above 0 — on some repo3 cycle the backup or its expire did not
  finish clean, or printed an `ERROR`. A refused delete is expected to surface exactly here (**FORECAST**:
  pgbackrest's S3 driver raises on a refused request — `src/storage/s3/storage.c` at the 2.59.1 tag — and the
  `-` prefix then hides the failure from systemd, which is why nothing has alerted).
- `VERDICT_JOURNAL=FINDING` with `repo3_cycles=0` — the script found no repo3 cycle in 15 days. Either there
  was none, or the journal's wording is not what this file forecast. That is a finding about **this runbook's
  instrument**, not yet about expiry.
- `VERDICT_JOURNAL=NO-REMOVAL-IN-WINDOW` — every cycle was clean and none asked for a removal. **Not a PASS.**
  On a 2-day cadence a 15-day window should hold several removals (FORECAST), so read this first as a finding
  about the instrument; the raw journal settles it.

**§3b — the raw journal of the same window (evidence, pasted whole; no gate).**
*VM · bash as `ubuntu` (elevates to read the journal) · no env*
```bash
sudo journalctl --no-pager -u neuro-backup.service --since "15 days ago" -o short-iso
```
Run this **whatever §2 and §3 printed**, and paste all of it — do not trim it. It is the unfiltered text §3
summarised, over the same window, and it is what lets the banking session check this file's six forecast
strings against what pgbackrest really prints. It is long: several hundred lines. pgbackrest's own echo shows
credential options as `<redacted>` (MEASURED, log:286); the `neuro probe` lines the same journal carries are
not covered by that measurement — **read the paste before you send it.**

---

## §4 — THE VERDICT

**PASS — at the two layers this runbook observes — means all three of:**
1. §2 printed `VERDICT=OK at most one repo3 full is past RF3`;
2. §2 printed `first_repo3_full_on_record_listed=False`;
3. §3 printed `VERDICT_JOURNAL=OK`.

State `clean_removed_total` beside the verdict. A PASS says: **aged fulls are leaving repo3's catalog, and on
every repo3 cycle of the last 15 days pgbackrest's expire ran clean, at least once asking B2 to remove a
backup and ending without error.** Re-read the table above for what that does not say.

**Anything else is a FINDING.** Stop. Do not run `expire`, do not edit the conf, do not touch the key or the
bucket. Record what you saw and paste it back; the diagnosis is a session's work with the evidence in front
of it. For scale, not urgency: the forecast consequence of unbounded growth is the storage cap eventually
refusing **writes**, which would close the canonical gate through `wal_lag` (the state of record's §A·72) —
and the measured footprint was ~39 MB against a cap sized in hundreds of gigabytes (log:286). There is time to
diagnose properly.

---

## §NOT OBSERVED BY THIS RUNBOOK — the B2 side, left out on purpose

Whether B2 has actually **freed the bytes** — the bucket's size, its hidden versions, its lifecycle reaper —
is not observed here, and no step for it is given. Seeing it needs either the provider console or an
S3-API call made with repo3's key, and reading a credential out of the conf is a credential sequence under
§C. **If that observation is wanted it is its own procedure, `EXECUTION: guided-required`** (a credential or
a provider console; outputs that need interpretation against two clocks; and a console nobody has driven for
this question — the 2026-08-31 §G box, finding (d), is what an undriven console section costs). It is not
needed to answer this runbook's question.

---

## WHAT TO PASTE BACK FOR THE BANKING SESSION

The **whole terminal transcript, unedited, from §0a to §3b** — every command as typed and everything it
printed, including any error. Do not summarise it and do not trim the long outputs: §2's per-backup lines and
§3b's raw journal are the evidence, and a session verifies them against this file before anything is banked.
Add three things in your own words:

1. the **date and time** you ran it (the VM prints UTC; say which you are quoting);
2. the **verdict** as §4 defines it — PASS, or FINDING with the line that made it one;
3. for each expectation that did not match, whether this file had marked it **MEASURED** or **FORECAST**.
