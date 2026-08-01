# RUNBOOK — P-7: per-worker `neuro_writer` LOGIN roles + the tailnet pg_hba widening

**EXECUTION: guided-required.** Contains a **credential mint** sequence, a **network-exposure change**, and
**first-time territory** (canonical has only ever been written FROM the VM over loopback).

**DRAFTED, NOT EXECUTED.** This runbook is a PREREQUISITE of the first untrusted remote worker, alongside
**B-6 (SAS)** and **B-9 (the self-seed fence)**. Do not execute it before those are ready — it is the step
that makes canonical reachable from off-box, and there is no reason to widen exposure ahead of the thing that
needs it.

> ⚠ **THIS RUNBOOK SUPERSEDES A STANDING PROHIBITION, DELIBERATELY AND ON THE RECORD.**
> `ops/provision-canonical.sh:378` says **"Do NOT add tailnet/pg_hba rows in Stage A."** That instruction is
> correct FOR STAGE A and is **superseded by this runbook when it is executed**, because the pg_hba widening
> is precisely the Stage-B step it was deferring. Executing this means Stage B has started. Note it in the
> bank so the prohibition is not later read as having been violated silently.

---

## §0 — WHY PER-WORKER LOGINS, NOT A SHARED ONE

ADR-0007 has said **"per-human and per-worker logins; no shared password"** since 2026-06-16; it has simply
never been provisioned, because until C3 there were no remote workers. Three things now depend on it:

1. **Revocability.** A compromised node must be cuttable without rotating every other worker.
2. **Attribution.** `claimed_by` is an `actors` FK the CALLER supplies, so today it attributes an *asserted*
   actor, not an authenticated one.
3. **★ THE C3 RESIDUAL THIS RUNBOOK EXISTS TO CLOSE.** `GRANT SELECT ON ALL TABLES` lets any `neuro_writer`
   READ another worker's `jobs.claim_token` and then call `complete_job`/`fail_job_permanent` against it. That
   is **not a C3 regression** — before C3 the writer could mark any job succeeded with no token at all — but
   C3 promoted the token from a concurrency fence to an **authorization value**, and a value the role can read
   cannot bear that load. **The fix is identity, not secrecy:** once each worker logs in as itself, the
   DEFINER functions can authorize on `current_user` instead of on a shared secret.
   ⚠ MEASURED alternative, and why it is NOT the recommendation: column-scoping `SELECT` on `jobs` to exclude
   `claim_token` DOES work — and it **breaks `SELECT *`**, which `Repository.get_job` is. It hides the token
   without making the caller identifiable, i.e. it treats a symptom.

---

## §1 — MINT ONE LOGIN PER WORKER (repeat per node)

`neuro_writer` is a **GROUP** role (NOLOGIN); the login roles are members. This mirrors the existing VM map:
`neuro_orch → neuro_admin`, `neuro_timer → neuro_writer`.

```bash
sudo -u postgres psql -d neuro
```
```sql
CREATE ROLE worker_<node> LOGIN IN ROLE neuro_writer;
\password worker_<node>
```
⚠ **`\password`, never `ALTER ROLE … PASSWORD '<literal>'`.** `\password` does not echo, only the meta-command
(not the secret) reaches `~/.psql_history`, and with `password_encryption=scram-sha-256` psql hashes
**client-side**, so the plaintext never crosses the wire or reaches a log (`log_statement=none` on canonical).

Verify over loopback TCP — **`local` is `peer` for ALL roles, so a password can never be tested over the unix
socket**:
```bash
psql -h 127.0.0.1 -d neuro -U worker_<node> -c "SELECT current_user, current_setting('is_superuser')"
```
Then confirm the inherited surface is exactly C3's:
```sql
SELECT has_function_privilege('worker_<node>','neuro.claim_job(bigint,text,text,integer)','EXECUTE') AS claim_x,
       has_table_privilege('worker_<node>','neuro.jobs','UPDATE')                                    AS jobs_upd,
       has_table_privilege('worker_<node>','neuro.work_leases','INSERT')                             AS leases_ins;
```
**Expect `t, f, f`.** If `jobs_upd` is `t`, the C3 grants were never re-provisioned — stop and run
`runbook-0004-canonical-apply.md` §4.

⚠ **The registrar stays LOOPBACK-ONLY.** Do not create a remote `neuro_registrar` login. The whole grants-C3
boundary is that GPU/remote workers connect as `neuro_writer`, never `neuro_registrar`; a remote registrar
login would hand an untrusted node the registry INSERTs the split exists to withhold.
⚠ **Also do NOT point the reaper at one of these.** `reap_expired` writes `jobs.state`/`claim_token` directly
and is revoked for the writer class — see `runbook-p8-dsn-wiring.md` §2.

---

## §2 — THE pg_hba WIDENING (the actual exposure change)

Measured 2026-07-20: `pg_hba_file_rules` has `local` = peer for ALL roles and `host` = scram on
**127.0.0.1 + ::1 ONLY** ⇒ a desktop→VM connection is REJECTED today.

**Prefer the narrowest thing that works, in this order:**

1. **An SSH `-L` tunnel — NO pg_hba change at all.** Measured: Tailscale SSH DOES carry `-L` forwarding (the
   commonly-cited tailscale#6575/#5091 are stale), and a tunnel makes PG see `127.0.0.1`, so **the existing
   loopback scram rule admits it with no edit and no restart.** For a small number of long-lived workers this
   is strictly better than widening pg_hba and should be the default.
2. **A tailnet CIDR rule**, only if the worker fleet makes tunnels impractical:
```bash
sudo cp /etc/postgresql/18/main/pg_hba.conf /etc/postgresql/18/main/pg_hba.conf.bak.$(date +%F)
# append ONE rule, scoped to the tailnet and to the worker role class:
# host  neuro  +neuro_writer  100.64.0.0/10  scram-sha-256
sudo systemctl reload postgresql
sudo -u postgres psql -c "SELECT * FROM pg_hba_file_rules WHERE error IS NOT NULL"   -- expect ZERO rows
```
⚠ **`listen_addresses` is a postmaster GUC a RELOAD CANNOT change.** If PG is still `localhost`-only, a
pg_hba rule alone changes nothing and you will debug the wrong layer. Check first:
```bash
sudo -u postgres psql -tAc "show listen_addresses"
```
Changing it requires a **restart**, which is a separate decision with its own quiesce — and it is another
reason to prefer the tunnel.
⚠ Verify the firewall separately; a pg_hba rule with a closed port is a silent no-op in the other direction.

---

## §3 — REVOCATION DRILL (do this BEFORE trusting the fleet)

A credential you have never revoked is a credential you do not know you can revoke.
```sql
ALTER ROLE worker_<node> NOLOGIN;   -- immediate: blocks NEW connections
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='worker_<node>';
```
Then confirm the job it held is recovered by the normal path: its lease lapses and the reaper requeues it.
⚠ That is the ONLY recovery path, and it depends on the reaper actually running — which is P-8.
