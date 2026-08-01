# RUNBOOK — P-8: DSN wiring for the split topology (worker / reaper / control plane)

**EXECUTION: guided-required.** Contains a **credential** sequence and a step whose failure mode is
**quiet** (§2), which is exactly the class §C says must not be solo.

**DRAFTED, NOT EXECUTED.** Runs after `runbook-p7-per-worker-logins.md`. ⚠ **B-4/B-4b stay UNDEPLOYED**: the
worker loops deploy at first-worker bring-up together with **B-6 (SAS)** and **B-9 (the self-seed fence)**,
not here. This runbook only settles *which DSN each process gets*.

---

## §0 — THE THREE LANES, AND WHY THEY ARE THREE

C3 split one privilege set into three, so there are now three DSNs and mixing them up has three different
failure modes:

| process | role | reaches | failure if mis-wired |
|---|---|---|---|
| remote worker | `worker_<node>` ∈ **`neuro_writer`** | the 5 DEFINER entry points; output-table INSERTs | loud: `permission denied` on the first claim |
| **reaper (B-4b)** | a **control-plane** login (`neuro_orch` ∈ `neuro_admin`) | `reap_expired`'s direct `jobs` writes | ⚠ **QUIET — see §2** |
| registrar / capture / enqueue | `neuro_orch` ∈ **`neuro_admin`**, loopback | registry INSERTs, `enqueue`'s dep lock | loud |

⚠ **`enqueue` is an ADMIN verb, not a registrar verb — MEASURED, and it contradicts the obvious reading of
`grants.sql`.** `SELECT … FOR NO KEY UPDATE` **requires UPDATE privilege**, and `neuro_registrar` holds SELECT
+ INSERT on `jobs` but **no UPDATE**, so `_initial_state_for_deps`'s C20 dep lock returns *permission denied
for table jobs* as the registrar. It works today only because the orchestrator is `neuro_orch ∈ neuro_admin`.
Do not "tidy" enqueue onto a registrar DSN.

---

## §1 — THE WORKER DSN

Per node, from the P-7 login. Same construction idiom as the boot DSN — the secret never reaches argv or
history:
```bash
read -rsp "worker password: " PW; echo
export NEURO_DATABASE_URL="postgresql+psycopg://worker_<node>:$(PW="$PW" python3 -c 'import os,urllib.parse;print(urllib.parse.quote(os.environ["PW"],safe=""))')@<host>:5432/neuro"
unset PW
```
Smoke-test it against the split, not against connectivity:
```bash
uv run python -c "
from sqlalchemy import create_engine, text; import os
e=create_engine(os.environ['NEURO_DATABASE_URL'])
with e.connect() as c:
    print('claim_x', c.execute(text(\"SELECT has_function_privilege(current_user,'neuro.claim_job(bigint,text,text,integer)','EXECUTE')\")).scalar_one())
    print('jobs_upd', c.execute(text(\"SELECT has_table_privilege(current_user,'neuro.jobs','UPDATE')\")).scalar_one())
"
```
**Expect `claim_x True` / `jobs_upd False`.** `jobs_upd True` means the C3 grants were never re-provisioned.
⚠ **Never `echo` the DSN once it holds a password**; report `${#NEURO_DATABASE_URL}`.
⚠ Long-lived worker processes go under `tmux`, started from a shell that already exported the DSN, so an SSH
drop does not kill an unresumable run — and killing the session is also the credential scrub.

---

## §2 — ★ THE REAPER DSN. THE ONE STEP WITH A QUIET FAILURE MODE.

**`reap_expired` is the ONE queue verb that deliberately did NOT move into a SECURITY DEFINER function** — it
is VM-side and trusted — and it writes `jobs.state`, `claim_token` and `claim_seq` **directly**. C3 revoked
all of those from `neuro_writer`.

⚠⚠ **A reaper wired to the WORKER DSN (or to `/etc/neuro/env`'s `NEURO_DATABASE_URL`, which is the
`neuro_timer` WRITER) gets `permission denied for table jobs` on EVERY sweep — and `ReaperLoop`
records-and-continues past a sweep error BY DESIGN (C1a).** So the loop spins, `last_error` is set, and
**nothing is ever reaped**: every crashed worker's job stays `claimed` with an expired lease, forever, and the
queue silently stops recovering. Nothing alarms on this today.

**Wire it to a control-plane login and PROVE it, positively:**
```bash
export NEURO_DATABASE_URL="postgresql+psycopg://neuro_orch:...@127.0.0.1:5432/neuro"
uv run python -c "
from sqlalchemy import create_engine, text; import os
e=create_engine(os.environ['NEURO_DATABASE_URL'])
with e.connect() as c:
    print('jobs_upd', c.execute(text(\"SELECT has_table_privilege(current_user,'neuro.jobs','UPDATE')\")).scalar_one())
"
```
**Expect `True`** — the reaper needs the raw grant precisely because it is trusted.

**Then a POSITIVE liveness proof, not an absence of errors.** A sweep that reaps nothing is indistinguishable
from a sweep that is permission-denied if you only read the exit code:
```bash
uv run python -c "
from neuromancer_llm.db.repository import Repository
from neuromancer_llm.db.session import make_verified_engine
from neuromancer_llm.workers.runtime import reap
r=Repository(make_verified_engine(expected_lane='canonical'), expected_lane='canonical')
print('reaped', reap(r))
"
```
`reaped 0` on a quiet queue is the expected answer — but it PROVES the statement executed, because a
privilege denial would raise here rather than being swallowed (this calls `reap` directly, not through
`ReaperLoop`). **Run this check as part of bring-up, before arming any timer.**

⚠ **REGISTERED FOLLOW-ON (not built):** a reaper preflight that fails LOUD on a privilege denial instead of
records-and-continue. Changing C1a's swallow-and-continue contract is a banked design decision and was not
this unit's call — so for now this manual check is the mechanism.

---

## §3 — THE CONTROL-PLANE DSN

Unchanged from today: `neuro_orch ∈ neuro_admin`, **loopback only**, owner-held off-VM (the A2-17 posture:
`NEURO_ADMIN_DATABASE_URL` was removed from `/etc/neuro/env` and stays removed). It runs the registry mints,
the capture path, `enqueue`, and `cancel_cascade`.
⚠ If the password is unavailable, the **§A·48 break-glass** is the peer-auth superuser — and **rotation IS
recovery**: `\password` needs no old password.
⚠ `psql` cannot parse the SQLAlchemy `postgresql+psycopg://` scheme — it silently falls back to peer auth and
prints a misleading `role "<osuser>" does not exist`. Test these DSNs through `uv run neuro …`, not raw
`psql`. (A plain libpq connstring, `psql -h 127.0.0.1 -d neuro -U <role>`, is fine.)

---

## §4 — WHAT IS DELIBERATELY NOT HERE

* **No timer/unit installation.** B-4/B-4b deploy at first-worker bring-up with B-6 + B-9.
* **No `/etc/neuro/env` edit.** It holds the `neuro_timer` WRITER DSN and the §A·45 quarantined Azure key;
  the new DSNs are set explicitly in the process environment that needs them, not added to a file every child
  process inherits.
* **No capture-path change.** The registrar/writer decomposition (P-6) is a code seam, not a DSN one; the
  local in-process path keeps composing both halves under the single control-plane DSN.
