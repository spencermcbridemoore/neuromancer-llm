# RUNBOOK — apply migration `0004` + re-provision grants to CANONICAL (ADR-0046 session C3)

**EXECUTION: guided-required** (one command at a time). *Mandatory* under §C's rule: this procedure contains a
**one-way door** (a schema change on the database of record), outputs needing **INTERPRETATION** rather than
comparison, a **credential** sequence, and **first-time territory** (the first grants RE-PROVISION ever run on
canonical, and the first migration that REVOKES a privilege).

**DRAFTED, NOT EXECUTED.** Nothing here has been run. HEAD at drafting: the C3a commit.

> ⚠ **THIS RUNBOOK IS TRACKED IN `ops/`, NOT `scratch/`.** Every previous runbook lived in gitignored
> `scratch/` and was machine-local; log:259 recorded that as a real loss (the durable record became a memory
> box). `ops/` is the repo's established home for tracked operator artifacts (`provision-canonical.sh`, the 12
> systemd units, `e6_run.py`), and it is outside pyright's `include=["src"]`.

---

## §0 — PREFLIGHT

**0a. ★ CHECK THE VM CHECKOUT SHA. THIS IS A NUMBERED STEP, NOT AN ASSUMPTION.**
The `0003` apply (log:259) found the VM sitting at `5d6226c` — the Unit-D sha from ten days earlier — where
`migrations/versions/` held only `0001`+`0002`, so `alembic upgrade head` **could not have seen `0003` at
all**. The canonical checkout advances only when a unit needs it to, so it is *routinely* behind.

```bash
cd /home/ubuntu/neuromancer-llm && git rev-parse --short HEAD && git status --porcelain
```
Expect: a sha that is probably NOT the banked HEAD, and a clean tree. Then advance:
```bash
git fetch origin && git merge-base --is-ancestor $(git rev-parse HEAD) <BANKED_SHA>; echo $?
```
**Expect `0`** — forward-only proven. Use `$?` on its own line, never `&&`, so nothing short-circuits.
```bash
git checkout <BANKED_SHA> && git status --porcelain && ls migrations/versions/
```
Expect a clean tree and **four** revisions (`_remediation/` holds only a `.gitkeep` — it is not a revision).
```bash
uv sync
```
Expect a **no-op**; if it is not, stop and diff `pyproject.toml`/`uv.lock` across the range.

**0b. Confirm the pending revision is visible.** See §1c — `alembic current` printing `0003_…` **bare** (no
`(head)` suffix) is the oracle that alembic can SEE `0004`.

**0c. FRESH FULL BACKUP ON BOTH REPOS — the net.**
```bash
sudo systemctl start neuro-backup.service
```
`Type=oneshot`, so it BLOCKS ~35 s and then reads `inactive (dead)`. That is success, not a hang.
```bash
sudo -u postgres pgbackrest --stanza=neuro info | tail -30
```
⚠ **Expect TWO labels seconds apart** (`…F` for repo1 and another for repo2): the unit runs the two repos as
SEPARATE `ExecStart` lines, so **each mints its own label**. A `tail -8` shows only the later one and reads as
if repo1 were missing — widen the window.
⚠ **A single repo2 `HostConnectError` / DNS error can be TRANSIENT.** log:259 hit one that did not reproduce.
Re-measure before concluding an outage: re-run `info`; `curl -o /dev/null -w '%{http_code}' https://<blob-host>/`
returning a real **400** proves DNS+TCP+TLS end to end. And while repo2 is erroring the listing shows only
`repo1:` sizes — **that absence is a symptom, not proof repo2 lacks the backups.**
Confirm the desktop arm took it too: the `ExecStartPost` probe logging `recorded ok` is positive evidence all
three durability arms are live.

**0d. ⚠ DO NOT SOURCE `/etc/neuro/env`.** Its `NEURO_DATABASE_URL` is the **`neuro_timer` WRITER** DSN, which
cannot run a migration, and sourcing drags the §A·45 quarantined `AZURE_STORAGE_CONNECTION_STRING` into your
shell and every child, for no benefit. List its keys safely if you need to (**names only, never values**):
```bash
sudo grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' /etc/neuro/env | sed 's/=$//'
```

---

## §1 — THE BOOT-SUPERUSER DSN (reuse this idiom verbatim)

**1a. Get the lane FROM THE DATABASE, not from memory.**
```bash
sudo -u postgres psql -d neuro -c "SELECT * FROM neuro.database_identity"
```
Expect `lane=canonical` and `instance_uuid c7a1b953-485a-4127-a2e6-a18f7423742a` (the repo pin).

**1b. Build the DSN so the secret never reaches argv, `ps`, or history.**
```bash
read -rsp "neuro_boot password: " PW; echo
export NEURO_MIGRATION_EXPECTED_LANE=canonical
export NEURO_DATABASE_URL="postgresql+psycopg://neuro_boot:$(PW="$PW" python3 -c 'import os,urllib.parse;print(urllib.parse.quote(os.environ["PW"],safe=""))')@127.0.0.1:5432/neuro"
unset PW
```
The percent-encode kills the URL-metacharacter silent-auth-failure trap **by construction**, so you never have
to ask whether the password contains `@ : / ? # %`.
⚠ **Never `echo $NEURO_DATABASE_URL`.** Report `${#NEURO_DATABASE_URL}` instead.

**1c. Verify auth + lane guard + PG-major in ONE read-only call.**
```bash
cd /home/ubuntu/neuromancer-llm && uv run alembic current
```
**Expect `0003_state_transition_triggers` BARE.** A `(head)` suffix would mean alembic cannot see `0004` —
stop, because the upgrade would then be a silent no-op.

---

## §2 — QUIESCE

**2a. ⚠ THE CHECK MUST EXCLUDE ITSELF.** `pg_stat_activity WHERE xact_start IS NOT NULL` includes the
observing query's own transaction, so *"require zero rows"* is **unsatisfiable** as written — log:259 caught
that live, and an operator who "passes" it by ignoring the one row has silently disabled the gate.
```bash
sudo -u postgres psql -d neuro -c "SELECT pid, usename, state, xact_start, left(query,60) FROM pg_stat_activity WHERE datname='neuro' AND xact_start IS NOT NULL AND pid <> pg_backend_pid()"
```
Expect **zero rows**. Stronger check worth running alongside — list **ALL** connections to `neuro`, since a
live capture MUST hold one, so an empty list is POSITIVE evidence of quiesce rather than an inference:
```bash
sudo -u postgres psql -d neuro -c "SELECT pid, usename, application_name, state FROM pg_stat_activity WHERE datname='neuro' AND pid <> pg_backend_pid()"
```
**2b. Timers.** Of the **8** armed on canonical, only `neuro-archiver-probe` (15-min) fires inside a short
window, and it writes `system_health` only ⇒ it cannot contend for `jobs`/`bundles`/`capture_events`.
⚠ **There is NO worker/reaper timer** — B-4/B-4b are built but undeployed — so any instruction to "stop the
worker timers" is **vacuous here**. Do not go looking for them.

---

## §3 — APPLY

```bash
uv run alembic upgrade head; echo "upgrade_exit=$?"
```
Expect `upgrade_exit=0`.

⚠ **LOCK NOTE, measured rather than assumed.** `CREATE FUNCTION` does not conflict with table readers, and
**MEASURED 2026-08-01: `DROP FUNCTION` does not block on an open read transaction either** (it locks
`pg_proc`, not the table). The two `CREATE TRIGGER`s take `ShareRowExclusive` (writers only). So the FORWARD
direction is low-risk. **The ROLLBACK is the more disruptive direction** — see §6.

---

## §4 — ★ RE-PROVISION THE GRANTS. THIS STEP IS NOT OPTIONAL AND NOT A FORMALITY.

The migration alone leaves the functions **owned by `neuro_boot` (a SUPERUSER)** and leaves the walking kit
still granted to `neuro_writer`. **`grants.sql` is where the revocations and the ownership change live**, and
a GRANT-only file cannot un-grant — the explicit `REVOKE`s added at C3 are what make a re-provision effective.

```bash
uv run neuro db roles
```
⚠ **MEASURED: this file is applied as ONE statement batch**, and it now `GRANT EXECUTE`s on functions that
only exist at `0004`. On a pre-`0004` database it raises `UndefinedFunction` and **aborts the ENTIRE grants
contract**. That is why §3 strictly precedes §4. If you see that error, you are not at `0004` — go back.

---

## §5 — VERIFY (PRESENCE-ONLY; NO WRITES, NO PROBE ROWS, NO INDUCED NEGATIVE ON CANONICAL)

```bash
uv run alembic current                     # expect 0004_worker_registrar_role_split (head)
```
```bash
sudo -u postgres psql -d neuro -c "SELECT p.proname, p.prosecdef, p.provolatile, r.rolname AS owner, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname='neuro' AND p.proname IN ('claim_job','renew_lease','checkpoint_job','complete_job','fail_job_permanent','unblock_ready_dependents','cascade_cancel_jobs','assert_job_claim_fencing') ORDER BY 1"
```
Expect **8 rows**; `prosecdef=t` for exactly the five entry points and `f` for the two internals + the trigger
function; `provolatile=v` for **all eight** (⚠ `s` would silently re-open the ADR-0046 §6 write-skew);
`owner=neuro_admin` for all eight; `proconfig` containing `search_path=pg_catalog, pg_temp`.

```bash
sudo -u postgres psql -d neuro -c "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='neuro' AND NOT t.tgisinternal ORDER BY 1"
```
Expect **7** non-internal triggers: the 5 already present at `0003` plus `jobs_claim_fencing` and
`bundles_manifest_assign_once`.

```bash
sudo -u postgres psql -d neuro -c "SELECT has_table_privilege('neuro_writer','neuro.jobs','UPDATE') AS jobs_upd, has_column_privilege('neuro_writer','neuro.jobs','state','UPDATE') AS jobs_state, has_table_privilege('neuro_writer','neuro.work_leases','INSERT') AS leases_ins, has_column_privilege('neuro_writer','neuro.runs','fingerprint_id','UPDATE') AS fp, has_function_privilege('neuro_writer','neuro.claim_job(bigint,text,text,integer)','EXECUTE') AS claim_x, has_function_privilege('neuro_writer','neuro.cascade_cancel_jobs(bigint,boolean)','EXECUTE') AS internal_x, has_function_privilege('public','neuro.complete_job(bigint,uuid)','EXECUTE') AS public_x"
```
Expect **`f, f, f, f, t, f, f`** — the revocations took, the five entry points are reachable, the internals and
PUBLIC are not.

**Standing invariants, unmoved:**
```bash
sudo -u postgres psql -d neuro -c "SELECT (SELECT count(*) FROM pg_tables WHERE schemaname='neuro') AS raw_46, (SELECT count(*) FROM pg_tables WHERE schemaname='neuro' AND tablename<>'alembic_version') AS domain_45, (SELECT count(DISTINCT typname) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='neuro' AND typtype='e') AS enums_13"
```
Expect **46 / 45 / 13**. ⚠ A bare 46 is HEALTHY — `test_rt_migrations` excludes `alembic_version`; this trap
already cost one stop-and-investigate.

---

## §6 — ROLLBACK (only if §3 or §5 fails)

```bash
uv run alembic downgrade 0003_state_transition_triggers
```
⚠ **THE ROLLBACK IS THE MORE DISRUPTIVE DIRECTION — the opposite of the intuition, and MEASURED.**
`DROP TRIGGER` takes an **AccessExclusiveLock**, which conflicts with a plain `SELECT`, so a single open
READ-ONLY transaction that is invisible to the apply will **block** it; and because both DROPs share one
alembic transaction, a contended drop can freeze reads on tables nobody is touching. `DROP FUNCTION` does NOT
have this problem (measured). Therefore: **drain readers first (§2a), set a bounded `lock_timeout`, and do NOT
retry blind** — a blocked rollback that is retried in a loop is how a short outage becomes a long one.
The downgrade also **restores** `assert_bundle_insert_state` to its `0003` body; a plain DROP would not.

---

## §7 — AFTER

Nothing to restart: no code moved, and no worker/reaper timer exists yet to re-arm. Bank the result (the
durable record is the log entry + the state-doc §G box, not this file).
