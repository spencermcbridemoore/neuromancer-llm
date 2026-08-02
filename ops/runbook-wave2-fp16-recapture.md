# RUNBOOK — WAVE-2 fp16 RE-CAPTURE (the ESTELA order-bias campaign at fp16)

**EXECUTION: guided-required** (one command at a time; the assistant guides, the owner executes).

*Mode justification (§C runbook-execution ruling): this procedure contains **one-way doors** (6,000 rows into
production canonical, with **no delete verb** in `src/` and a **one-shot / no-resume** campaign), outputs that
need **INTERPRETATION** rather than comparison (the E6 signature count, the grid preflight note, the
position-bias numbers), and **first-time territory** (the first non-bf16 capture the platform has ever run, and
the first mint of a SECOND model identity). Any one of those forces guided-required; all four are present.*

> **STATUS: DRAFTED, NOT EXECUTED.** Tracked in `ops/` rather than gitignored `scratch/` on the §D-#7 / session-C3
> precedent — log:259 recorded scratch-only runbooks as a real loss.

---

## §0 — WHAT THIS RUNBOOK ASSUMES, AND THE THREE FACTS THAT MAKE IT SAFE

1. **The Phase-1 code is committed** (`campaign_key` is a REQUIRED caller argument; the `CAMPAIGN_KEY` module
   constant is gone). Without it the campaign CANNOT run at a new key — that is the whole reason Phase 1 exists.
2. **Migration `0004` is NOT applied to canonical, and does NOT need to be.** MEASURED: the capture path touches
   **no queue verb** — a location-scoped identifier search over `capture/**` and `bundles/**` for the queue
   tables (`jobs`, `work_leases`, `job_dependencies`), all five `0004` SECURITY DEFINER functions
   (`claim_job`/`complete_job`/`renew_lease`/`checkpoint_job`/`fail_job_permanent`), plus `reap_expired`,
   `enqueue`, `claim_token` and `heartbeat`, returns **zero matches**. Advancing the VM checkout to the banked
   HEAD is therefore safe: the new code the checkout brings is queue-only, and nothing on the capture path calls
   it. **No armed timer runs a worker either** (`workers/runtime.py` notes it; `ops/runbook-0004-canonical-apply.md`
   §113-115 repeats it), so the advance arms nothing.
3. **A NEW dtype is a NEW model identity, BY DESIGN.** `dtype_quant` folds into `model_identity_hash` →
   `semantic_config["model_identity"]` → `fingerprint_hash`. Wave-2 therefore mints the **second `model_id` in the
   platform's history** and needs its **own E6 cert**. It does NOT and must NOT reuse `model_id=1`.

⚠ **THE ONE-SHOT PROPERTY, RESTATED BECAUSE IT GOVERNS EVERY BEAT.** Two identical `/v1/completions` requests
return **different bytes** (per-request `"id"`, epoch `"created"`), so `write_capture_event`'s byte verification
raises `SeamIntegrityError` on any re-run — the sweep cannot resume. Combined with the absence of a delete verb,
**a partial sweep is clearable only by hand**, which is what Appendix A is for. Read Appendix A BEFORE beat 4.

---

## §1 — BEAT 1: LAUNCH THE fp16 SERVER (desktop, the 4090)

The pinned wave-1 recipe **plus `--dtype float16`**. Everything else is byte-identical to the recipe constant
(`capture/recipe.py`), because the substrate is otherwise unchanged.

⚠⚠ **THE ONE SILENT FAILURE DIRECTION ON THIS WHOLE PAGE — read before typing.**
- **OMITTING `--dtype` is SILENT AND WRONG.** vLLM resolves `--dtype auto` → the checkpoint dtype → **bf16** for
  Mistral-7B-v0.3. So running the wave-1 recipe *verbatim* produces a **bf16 server** that the campaign will
  happily label `fp16`, and **nothing on the capture path can detect it** (bf16↔fp16 is unidentifiable from
  log-softmax; `capture/gridcheck.py:14-16` states this and registers the residual). This is the single most
  dangerous line in the procedure.
- **Pasting the model-identity grade literally (`--dtype fp16`) is LOUD AND SAFE.** vLLM's `ModelDType` literal
  set is `auto|half|float16|bfloat16|float|float32` — `fp16` is not a member, so the server **refuses to start**.
  A refusal here is a correct outcome, not a problem to work around.

⇒ **The flag is `--dtype float16`**, and the hazard is closed by MECHANISM, not by this warning: **CHECK 1.1
below (positive dtype verification at launch) and CHECK 2.1 at beat 2 (signature cross-check against the banked
bf16 reference)**. Both are STOP-gated. Neither is optional.

```
docker run --rm -d --name neuro-vllm-estela-fp16 --gpus all -p 8001:8000 \
  -v <HF_CACHE>:/root/.cache/huggingface \
  -e HF_HUB_OFFLINE=1 -e VLLM_BATCH_INVARIANT=1 -e VLLM_USE_V2_MODEL_RUNNER=0 \
  vllm/vllm-openai:v0.23.0 \
  --model mistralai/Mistral-7B-v0.3 --revision caa1feb0e54d415e2df31207e5f4e273e33509b1 \
  --dtype float16 \
  --gpu-memory-utilization 0.65 --max-model-len 2048 --max-logprobs 128 \
  --logprobs-mode raw_logprobs --return-tokens-as-token-ids --enforce-eager \
  --no-enable-prefix-caching --seed 1234
```

### ★ CHECK 1.1 — POSITIVE dtype VERIFICATION (mechanism, not a warning line)

**Do not infer the dtype from the flag you typed. Read what the server RESOLVED.** Two exact strings, both
COMPARED not interpreted. Both must be present.

```
docker logs neuro-vllm-estela-fp16 2>&1 | grep -F "dtype=torch."
docker logs neuro-vllm-estela-fp16 2>&1 | grep -F "Casting torch.bfloat16 to torch.float16."
```

| # | Exact expected text | Source | Meaning of absence |
|---|---|---|---|
| **1.1a** | `dtype=torch.float16` — inside the line `Initializing a V1 LLM engine (v0.23.0) with config: …` | `vllm/config/vllm.py:1955` (`VllmConfig.__str__` emits `f"dtype={self.model_config.dtype}, "`), logged at `vllm/v1/engine/core.py:114` | **UNCONDITIONAL** — this line prints on every launch. If it reads `dtype=torch.bfloat16`, the server is bf16. **STOP.** |
| **1.1b** | `Casting torch.bfloat16 to torch.float16.` | `vllm/config/model.py:2083` — the `logger.warning` on the else-branch of `torch_dtype != config_dtype` | ★ **Fires ONLY when an explicit `--dtype` overrode the checkpoint's bf16.** With `--dtype` omitted, `torch_dtype == config_dtype` and **NOTHING is logged at all** — so **the ABSENCE of 1.1b IS the silent `auto`→bf16 failure mode**, which is exactly why absence is a STOP rather than a shrug. |

⛔ **If 1.1a reads `bfloat16`, or 1.1b is absent: STOP.** Do not proceed to the E6 cert, and do not
"just re-run with the flag" without tearing down the container — a running bf16 server that later gets
labelled fp16 is the one failure this whole runbook exists to prevent.

**Then the two ordinary checks:**
```
curl.exe -s http://127.0.0.1:8001/version                                   # expect 0.23.0
curl.exe -s http://127.0.0.1:8001/v1/models                                 # served model id
```
⚠ `VLLM_USE_V2_MODEL_RUNNER=0` is REQUIRED on this box (v0.23's V2 runner uses a CUDA-UVA buffer unsupported by
WSL2 passthrough — "UVA is not available"). ⚠ On PowerShell use `curl.exe`, never `curl` (an alias for
`Invoke-WebRequest`, which dies on parameter binding without contacting the server).

---

## §2 — BEAT 2: THE E6-fp16 CERT, **BEFORE ANY CAPTURE** ★

The wave-1 bitwise cert is for **bf16**. It does not transfer. This beat produces the fp16 cert, and it runs on
the **desktop** (loopback to the server just launched; the driver issues a `docker restart`, so it must run on the
container's host).

**FIVE env vars are set** (`E6_ARM` is deliberately NOT among them — its default `"test"` IS the required BI-on
arm, and naming it only creates an opportunity to set `control` and certify the wrong arm):

```
E6_URL=http://127.0.0.1:8001 \
E6_NLOGPROBS=64 \
E6_N=50 \
E6_BATCH=1,2,4,8,16 \
E6_RESTART_CONTAINER=neuro-vllm-estela-fp16 \
uv run python ops/e6_run.py 2>&1 | tee ops-e6-fp16-$(date +%Y%m%d).log
```

⚠ The driver's defaults are `:8000` and `n_logprobs=20`; **both must be overridden** or the cert is for the wrong
port and the wrong breadth. **TEE the full output** — its header line (`arm=… url=… cc=… model=… N=… batch=…
nlogprobs=… restart=…`) and its `JSON_SUMMARY` (carrying `arm`, `batch_invariant`, `restart_count`) are the
evidence that the right arm ran.

**EXPECT: `1 distinct signature`, zero divergence.**

⚠ **THE CERT RECORDS NO DTYPE.** `run_e6` consumes no dtype — it decides on the signature COUNT alone, so a bf16
server would pass an "fp16 cert" identically. **File the beat-1 launch command and the CHECK-1.1 log lines
alongside the cert log**; the dtype is an operator-declared launch fact, not measured evidence. CHECK 2.1 below
is what converts that into measured evidence.

### ★ CHECK 2.1 — E6 SIGNATURE CROSS-CHECK against the banked bf16 reference

**The rationale, stated because it is the whole point.** An accidental `auto`→bf16 server reproduces wave-1's
outputs **bitwise** — same weights, same kernels, same BI flag, same prompt, same seed. So **signature EQUALITY
is a DECISIVE DETECTOR of exactly the mislabel `capture/gridcheck.py` cannot see** (the bf16↔fp16 identifiability
wall: log-softmax is shift-invariant, so no captured-logprob analysis can separate the two). This is the one
place in the whole procedure where that wall can be got around — and it works only because a *prior* bf16
measurement exists to compare against.

**THE REFERENCE IS RETRIEVABLE — verified before this step was written, not assumed.** Canonical `run_id=1`
(`run_key=a2-17/convergence/v1`) is a permanent bf16 capture whose configuration is the E6 configuration:

| Field | `run_id=1` (bf16 reference) | E6-fp16 run | Comparable? |
|---|---|---|---|
| prompt | the capital-of-France MCQ = `DEFAULT_TARGET_PROMPT` | same | ✓ |
| `temperature` / `max_tokens` / `seed` | `0` / `1` / `1234` | same | ✓ |
| `VLLM_BATCH_INVARIANT` / runner / stack | `true` / `V1` / `vllm-0.23.0` | same | ✓ |
| `logprobs` | **20** | **64** | ✓ **for the top-1 only** — `n_logprobs` selects how many entries are RETURNED, not how they are computed, so the top-1 pair is unaffected. **Compare the top-1, never the full signature tuple** (a 20-entry and a 64-entry tuple are not comparable). |
| **top-1 `(token_id, logprob)`** | **`(1102, -0.5908788442611694)`** | must **DIFFER** | — |

*Provenance of the reference value: banked at `log:27` (the verbatim 243 B request + `" C"=token_id:1102
logprob -0.5908788442611694`) and `log:175`, and carried in the state-doc preamble's Canonical-DB row. It is
**not a one-off**: log:175 records it as **byte-identical to a June capture a month earlier**, i.e. already
reproduced bitwise across two independent bf16 runs — which is what makes it a trustworthy reference rather
than a single observation.*

**Re-derive it from canonical rather than trusting the copy above** (read-only, VM loopback as `neuro_timer`):
```sql
SELECT response_text FROM neuro.capture_events WHERE run_id = 1;
-- the verbatim server reply; its top-1 entry is the reference pair
```

**⚠ `ops/e6_run.py` DOES NOT PRINT SAMPLE VALUES** — `JSON_SUMMARY` is built as
`{k: v for k, v in vars(result).items() if k != "samples"}`, so the signature is dropped. Issue **one** separate
request against the fp16 server (no code change, no DB write):
```
curl.exe -s http://127.0.0.1:8001/v1/completions -H "Content-Type: application/json" -d "{\"model\":\"mistralai/Mistral-7B-v0.3\",\"prompt\":\"Question: What is the capital of France?\nA) Berlin\nB) Madrid\nC) Paris\nD) Rome\nAnswer with a single letter:\",\"max_tokens\":1,\"temperature\":0,\"seed\":1234,\"logprobs\":64,\"echo\":false}"
```
(the `_completion_payload` shape, `capture/adapters/vllm.py:263-276`). Read the top-1 logprob.

| Observation | Verdict |
|---|---|
| top-1 logprob **==** `-0.5908788442611694` | ⛔ **STOP — the server is bf16.** DECISIVE: an fp16 lm_head cannot reproduce a bf16 value bitwise. The `--dtype` flag did not take, or the container is the wrong one. |
| top-1 logprob **differs** | ✓ Proceed. ⚠ **Confirmatory, NOT proof** — it rules out *this* mislabel, not every possible substrate deviation. |
| `token_id` still `1102` | Expected and **NOT** the discriminator — the distribution is peaked, so the argmax is stable across dtypes. Compare the **logprob**, not the token. |

⚠ **If `run_id=1` is absent from canonical, that is a FINDING, never a gap to seed** (it would contradict
log:174). In that case CHECK 1.1 stands as the sole dtype mechanism and the gap is recorded — do **not**
fabricate a reference.

### ⛔ STOP CONDITION

**If the cert is NOT bitwise (more than 1 distinct signature): STOP. Do not improvise a tolerance lane.**
Whether the campaign may run at `EXPECTED=tolerance` instead of bitwise is an **owner ruling** — it touches D5's
recomputable posture, which is what makes the whole campaign's provenance claim true. A failed cert is a
**finding to bank** either way.

---

## §3 — BEAT 3: PREPARE THE VM

**§3a — MEASURE the checkout sha FIRST, then advance.** The canonical VM checkout is kept DETACHED and only
advances when a unit needs it to, so **it is routinely BEHIND the banked HEAD** (at the `0003` apply it sat 11
days stale). Do not assume.

```
ssh ubuntu@100.86.240.4
cd /home/ubuntu/neuromancer-llm
git rev-parse --short HEAD          # MEASURE. Record it.
git status --porcelain              # expect clean
git fetch origin
git merge-base --is-ancestor <old> <new>; echo $?    # expect 0 = forward-only. Use $?, never &&.
git checkout <new>
uv sync                             # report the delta HONESTLY (expect a no-op; say so if it is not)
```

**§3b — do NOT source `/etc/neuro/env`.** Its `NEURO_DATABASE_URL` is the `neuro_timer` WRITER DSN (which cannot
run a campaign), and sourcing it drags the §A·45 quarantined `AZURE_STORAGE_CONNECTION_STRING` into the shell and
every child process, for no benefit. The campaign runs as **`neuro_orch`** (a `neuro_admin` member — the
capture path spans registrar INSERTs and writer UPDATEs, so neither single role suffices).

**Credential idiom (reuse verbatim; the secret never reaches argv, `ps`, or history):**
```
read -rsp "pw: " PW; echo
export NEURO_DATABASE_URL="postgresql+psycopg://neuro_orch:$(PW="$PW" python3 -c 'import os,urllib.parse;print(urllib.parse.quote(os.environ["PW"],safe=""))')@127.0.0.1:5432/neuro"
unset PW
```
The percent-encode closes the URL-metacharacter silent-auth-failure trap **by construction**. Verify with a
read-only call; **never `echo $NEURO_DATABASE_URL`** — report `${#NEURO_DATABASE_URL}` instead.

**§3c — quiesce check.** `WHERE xact_start IS NOT NULL` **always self-matches**, so add
`AND pid <> pg_backend_pid()`. Better: list ALL connections to `neuro` — a live capture MUST hold one, so an
empty list is POSITIVE evidence rather than an inference.

**§3d — tmux.** The sweep is long and unresumable; an SSH drop otherwise kills it. Start tmux from the shell that
already exported the DSN so the session inherits it. Killing the session is also the credential scrub.

**§3e — read Appendix A now**, before any write.

---

## §4 — BEAT 4: RUN THE CAMPAIGN

```
uv run neuro capture campaign \
  --campaign-key estela-order-bias-fp16 \
  --dtype-quant fp16 \
  --serving-stack vllm --serving-version 0.23.0 \
  --base-url http://100.77.118.14:8001 \
  --corpus-root <path>/estela_text_only_mcq.jsonl \
  --corpus-commit 158a8c32248a2f4980a14075b221a78f00bbbbd7 \
  --hf-revision caa1feb0e54d415e2df31207e5f4e273e33509b1 \
  --tokenizer-file <path>/tokenizer.json \
  --lake-root /home/ubuntu/estela-lake-fp16 \
  --n-logprobs 64
```

⚠ **TOPOLOGY IS NOT OPTIONAL.** The campaign runs **ON THE VM** with `--base-url` pointed at the desktop's vLLM
over the tailnet. A capture is 31 statements + 18 transactions; over an SSH tunnel that is ~92% of wall-clock
(a 10-hour sweep). VM-side the DB is loopback and only 2 HTTP calls cross the network: **measured 18× faster,
33m14s for 6,000**.

⚠ **A NEW `--lake-root`.** Do not write wave-2 parquets into wave-1's `/home/ubuntu/estela-lake`.

**THE FAIL-FAST START runs before any durable write, and all four checks are the point:**
1. `require_dtype_quant` / `require_substrate` / `require_campaign_key` — blank grades refused;
2. **`assert_substrate_matches_wire`** — declared `vllm`/`0.23.0` vs the adapter self-report and the server's
   `/version`. ★ **This is the registered A1 live-verify (log:248 residual).** Its first pass against a real
   server DISCHARGES it — record that explicitly in the bank;
3. `assert_corpus_matches_pin` — the parsed-content digest of the pinned corpus (never file bytes);
4. the **§D Layer-2 grid preflight** — a controlled probe on a fixed prompt.

**PASTE THE GRID PREFLIGHT NOTE INTO THE TRANSCRIPT AND READ IT.** Declared-fp16 reads QUANTIZED and can never
raise, so the note is **informational**. Expect a step consistent with **2⁻⁷ at the observed binade** (wave-1's
bf16 note read `0.0625` = 2⁻⁴). ⚠ The note names both a bf16 and an fp16 binade because the two are not
disambiguable from log-softmax — **it is not a dtype verification**, and must not be reported as one.

---

## §5 — BEAT 5: VERIFY (read-only; VM, loopback, as `neuro_timer` — no credential handling needed)

`grants.sql:24` grants `SELECT ON ALL TABLES` to `neuro_writer`, and `neuro_timer ∈ neuro_writer` with its DSN
already in `/etc/neuro/env`. Do NOT reach for the admin DSN for a SELECT.

| Check | Expectation |
|---|---|
| runs / capture_events / bundles / artifacts / table_manifests / run_metrics under the new key | **6,000 each** |
| DISTINCT fingerprints | **6,000** |
| questions | **74** (k! histogram 24→30, 120→44); q18 dropped (D6) |
| holes (any count ≠ 6,000) | **0** |
| **DISTINCT `model_id` across all 6,000** | **exactly 1, and it must NOT be `model_id=1`** |
| `censored_cells_total` | ~0 at n_logprobs=64 |
| spilled events | 0 (raw is INLINE, F2) |

★ **The identity check is the one that matters most**, and `neuro runs show <run_id>` exists precisely for it —
it surfaces `model_id` plus a bounded tokenizer-sha pointer. **Expect the SAME tokenizer identity as wave-1**
(the tokenizer is dtype-independent) and a **DIFFERENT `model_id`**. Both halves are the wave-2 split.

⚠ **The typo backstop.** `require_campaign_key` closes the BLANK case only — a *typo'd* key
(`estela-order-bias-fp16x`) collides with nothing and completes silently. The campaign's final line echoes
`campaign=<key>` **as reported by the runner**, and the counts above are keyed on it: **verify the key you
intended is the key that carries 6,000 rows.**

**Then seal D5 for the new identity:** a spot-recompute reproducing one capture **bitwise** from
`(corpus_commit, uid, perm_index)` alone.

---

## §6 — BEAT 6: THE SCIENCE (read-only)

```
uv run neuro runs position-bias --campaign-key estela-order-bias-fp16 --corpus-root <path> --max-k 5
```
Report **k=4 and k=5 separately**.

★ **THE MEASURABLE PREDICTION, RECORDED BEFORE THE RUN:** wave-1's **13.7% tie rate (820/6000)** was
grid-driven — every pairwise letter difference was an exact multiple of 0.0625 (2⁻⁴). At fp16 the grid is 2⁻⁷,
**8× finer**, so the tie rate should fall by roughly an order of magnitude. **MEASURE IT; DO NOT FORCE IT.** A
tie rate that does *not* fall is a finding about the mechanism, not a failure of the run.

Compare against the wave-1 numbers (state doc §D): k=4 content 0.5857 / position 0.5765 / accuracy 0.3867;
k=5 content 0.5784 / position 0.4210 / accuracy 0.3849; the cross-arity **late-position penalty** (D 0.112 at
k=4; E 0.019 at k=5). **No overclaim beyond one model / one template / one corpus.**

---

## §7 — BEAT 7: TEARDOWN + SCRUB

1. `docker stop neuro-vllm-estela-fp16`; confirm no `:8001` listener.
2. Kill the tmux session (this is also the credential scrub).
3. **History scrub — BOTH idioms self-poison; use the banked forms.**
   - bash: `history | grep -v 'grep' | grep -c -E 'neuro_orch:[^$]'` → **expect 0**. Strip checker lines FIRST,
     because both the naive and the corrected pattern leave their own regex literal in history and match
     themselves on the next read. Use `-c` so a real secret can never be echoed into a transcript while being
     audited. History flushes on logout — settle it before closing.
   - PowerShell: grep the secret SHAPE with `-notmatch 'Select-String'` to exclude the checker.
4. **Lake disposition:** `/home/ubuntu/estela-lake-fp16` stays **single-copy, recompute-only**, NOT rsynced into
   the B-7 lake root — the wave-1 precedent (log:232). Authoritative raw is the three-way-durable inline wire in
   canonical PG; the lake is DERIVED.
   ⚠ **ACCEPTED COST, STATED NOT GLOSSED:** the per-capture parquet fan-out is still unretired (§D #10), so this
   adds **~30,000 more inodes** (~12,000 files + ~18,000 directories, ~118 MB on disk for ~16 MB of payload).
   Noted, deliberately not fixed here.

---

## APPENDIX A — CLEANUP SQL (adapted for wave-2; use ONLY on a failed partial run)

⚠⚠ **DESTRUCTIVE, ON PRODUCTION CANONICAL.** Runs as the admin DSN. Wrapped in `BEGIN … ROLLBACK` on purpose —
**run it once as-is, read the counts, and only then change `ROLLBACK` to `COMMIT`.**

⚠ **THIS APPENDIX IS PARAMETERIZED BY *TWO* LITERALS, NOT ONE.** The campaign key **and** the lake root. A copy
that swaps only the key would delete the wave-2 rows while wiping or sparing the **wrong lake tree**.
⚠ **AND THE WAVE-1 APPENDIX'S LAKE LINE IS STALE:** it names the desktop path `C:\neuro-estela-lake`, but the
lake moved to the **VM** at the Unit-C topology change (log:231 CORRECTION 4). The path below is the VM one.

```sql
BEGIN;

CREATE TEMP TABLE w2_runs AS
SELECT r.run_id, r.fingerprint_id
FROM neuro.runs r
JOIN neuro.campaigns c ON c.campaign_id = r.campaign_id
WHERE c.campaign_key = 'estela-order-bias-fp16';   -- LITERAL 1

SELECT count(*) AS runs_to_delete FROM w2_runs;    -- SANITY: eyeball before proceeding

DELETE FROM neuro.run_metrics     WHERE run_id IN (SELECT run_id FROM w2_runs);
DELETE FROM neuro.table_manifests WHERE run_id IN (SELECT run_id FROM w2_runs);
DELETE FROM neuro.artifacts
 WHERE bundle_id IN (SELECT bundle_id FROM neuro.bundles WHERE run_id IN (SELECT run_id FROM w2_runs));
DELETE FROM neuro.capture_events WHERE run_id IN (SELECT run_id FROM w2_runs);
DELETE FROM neuro.bundles        WHERE run_id IN (SELECT run_id FROM w2_runs);
DELETE FROM neuro.runs           WHERE run_id IN (SELECT run_id FROM w2_runs);
DELETE FROM neuro.fingerprints f
 WHERE f.fingerprint_id IN (SELECT fingerprint_id FROM w2_runs WHERE fingerprint_id IS NOT NULL)
   AND NOT EXISTS (SELECT 1 FROM neuro.runs r2 WHERE r2.fingerprint_id = f.fingerprint_id);
DELETE FROM neuro.campaigns WHERE campaign_key = 'estela-order-bias-fp16';

ROLLBACK;   -- read every count above, THEN swap for COMMIT
```

**⚠⚠ DO NOT DELETE THE fp16 `model_identities` ROW — AND THIS IS WHERE WAVE-2 INVERTS THE WAVE-1 APPENDIX.**
Wave-1's appendix keeps `model_identities` because `model_id=1` belongs to `run_id=1` (someone else's data). At
wave-2 the fp16 identity row **is this campaign's own**, so an operator adapting the appendix may reason *"this
one is mine, delete it."* **Keep it.** The row is registry-idempotent and harmless, it is the identity the
**E6-fp16 cert attests**, and deleting it churns the identity a re-run would have to re-mint.

Also keep, unchanged from wave-1: `tokenizer_identities`, `metric_keys`, `methods`/`method_versions`,
`storage_backends`.

**Then clear the derived lake separately** (single-copy, not covered by the B-7 mirror — F2):
```
rm -rf /home/ubuntu/estela-lake-fp16     # LITERAL 2 — the VM path, not the desktop one
```

⚠ **MEASURE THE DELETE GRANT BEFORE RELYING ON THIS** (`neuro_admin` holds `GRANT ALL`, but that is an inference):
```sql
SELECT has_table_privilege('neuro_orch','neuro.runs','DELETE');   -- expect t
```
