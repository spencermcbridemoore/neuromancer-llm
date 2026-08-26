# RUNBOOK — the research-corpus RIP import

**EXECUTION: guided-required** (one command at a time). **DRAFTED, NOT EXECUTED.**

Mandatory mode: every registered row is a **one-way door** (there is no delete verb for `artifacts`,
`external_records` or `lineage_edges` anywhere in `src/`), and this is first-time territory — no
corpus pointer has ever been written to canonical.

⚠ **Nothing in this runbook has been run.** The counts below are measured from the committed
manifest, not from a database.

---

## §0 — What this registers, and what it does not

**1,753 rows / 8,568,575,168 B (8.57 GB)** as pointer triples: a rank-6 `artifacts` row (no bytes
copied, no cloud upload), the paired rank-5 `external_records` row, and a rank-7 `annotates` edge.

**Two batches, by `source_system`** — `local-fs` **949**, `study-query-llm` **804**. The
confidentiality grade is retired (R-A), so batches no longer split on it.

**Excluded by owner ruling, DEFAULT-ON in code** (`importer/rip.py::EXCLUDED_FAMILIES`, enforced in
the library as well as the driver): `competition-trace-the-ace` (22,825) · `stimuli-estela` (280) ·
`mi-sae-asset` (1). ⚠ The Trace-the-Ace key is the FULL string — `trace-the-ace` matches nothing,
and a filter written that way silently registers 22,825 permanent rows.

### Facts that will mislead a reader unless you carry them

- ⚠ **`open` means UNGRADED BY POLICY, not "no concern"** (R-A, from 2026-08-11). **387** of these
  rows carry `exam_restricted` in the upstream manifest and are stamped `open` anyway; the
  historical grade survives only in `external_records.payload_jsonb.row.confidentiality`. ⚠ These
  387 are **NOT** the 360 historic MOSART `external_records` rows, which keep `exam_restricted`
  un-edited because it is true of them. Two different MOSART-adjacent numbers.
- ⚠ **Provenance, NOT durability.** The 804 `study-query-llm` rows point at pickles whose upstream
  Postgres copy was already dropped. Registering a pointer records *where a file was*; it copies no
  bytes and backs nothing up. A `retention` of `keep_forever` on a row inside a backed-up database
  does not mean the FILE is backed up.
- ⚠ **292 rows carry `retention='ttl'`.** No TTL reaper exists for register-in-place pointers, so
  `ttl` here is a LABEL, not a lifecycle — nothing will ever collect them. Registered as declared
  rather than silently coerced.
- ⚠ **The pointers are not dereferenceable by the system.** `uri` is a hand-authored corpus relabel
  whose DIRECTORY PREFIX is invented: the whole uri is a path-suffix of `source_abspath` in **0 of
  1,753** rows. ⚠ The **BASENAME matches in all 1,753** — say it that way, because the flat "zero
  derivability" phrasing overclaims: **a filename is recoverable, a location is not.** The only path
  back to a file is `payload_jsonb.row.source_abspath`, which is MACHINE-LOCAL — true of this
  desktop and nowhere else.
- ⚠ **`map6197-lakehouse` is 2 rows because the SAME zip is listed twice** (a byte-identical
  duplicate, which is what that row's `note` records). Separately, the 13 silver parquets INSIDE
  that zip cannot be registered at all — register-in-place cannot reach into an archive. The family
  is not "captured".

---

## §1 — Preconditions (in order; stop on any divergence)

1. **Run desktop-side.** The bytes live here; `pg_hba` on canonical is loopback-only, so reach the
   database over an SSH `-L` tunnel. ⚠ **Check the tunnel path BEFORE anything expensive** — see §2.
2. **Durability green.** `assert_backup_fresh` branch 4 blocks on **status** before staleness, so a
   blocked row fails the FIRST capture regardless of how fresh the backups are.
   ⚠ **NordVPN on this desktop silently kills the VM→desktop tailnet path** — every port times out
   while every Tailscale surface still reads healthy. Disconnect it before checking.
3. **Admin DSN.** `close_import_batch` is the importer's ONLY UPDATE and is admin-only by grant.
   The driver proves the privilege before opening a batch (§3), which is the only ordering where
   that failure is free.
4. ⚠ **NEUTRALIZE THE SUPERSEDED SHIM.** `C:\Users\spenc\Downloads\neuromancer_import\
   neuromancer_register_in_place.py` still exists beside a copy of the manifest, and its READMEs
   document a one-command path that imports **all 24,859 rows**. A repo denylist cannot reach a file
   outside the repo. Rename it and add a banner:

   ```bash
   cd /c/Users/spenc/Downloads/neuromancer_import
   printf '%s\n' '# SUPERSEDED 2026-08-13 by ops/corpus-import/register_corpus.py in neuromancer-llm.' \
                 '# Its --family filter is INCLUSION-only: a bare run registers ALL 24,859 rows.' \
                 '# DO NOT RUN.' | cat - neuromancer_register_in_place.py > SUPERSEDED_DO_NOT_RUN.py
   rm neuromancer_register_in_place.py
   ```

---

## §2 — Reachability probe (cheap, and it goes first)

The campaign path is not exercised until the first write, so probe it before the expensive steps —
this is the step whose absence cost a wave-2 session an aborted run.

```bash
timeout 5 bash -c 'cat </dev/null >/dev/tcp/127.0.0.1/15432'; echo "tunnel: $?"   # 0 = open, 124 = blocked
```

---

## §3 — Dry run (touches no database)

```bash
uv run python ops/corpus-import/register_corpus.py --dry-run
```

**Expect exactly:**

```
  belt OK: 1753 rows / 8.569 GB matches the reviewed set
1753 rows across 2 batch(es) (by source_system — the grade is retired):
    local-fs           rows=949
    study-query-llm    rows=804
dry run: no database was contacted
```

⚠ Any other row count is a **STOP**: the driver's belt refuses a drifted manifest rather than
registering a different corpus than the one reviewed. Do not "fix" it by editing
`import_manifest.csv` — the suite asserts `concat(part_*.csv) == import_manifest.csv`, so patch the
part and rebuild by concatenation.

---

## §3b — TEST-LANE SMOKE (mandatory; the driver must never meet canonical as its first live run)

This section does DOUBLE DUTY and both halves matter.

1. **It repairs the charter's topology.** The original preconditions block was un-runnable as
   written: `verify_engine` AUTO-RESOLVES the repo-pinned canonical `instance_uuid`, so a freshly
   provisioned local container on `--lane canonical` FAILS CLOSED with a fresh uuid. The smoke target
   must therefore be a NON-canonical lane. `test` also skips the ADR-0020 durability consult
   (`ingress.py` guards on `expected_lane == "canonical"`), so this exercise needs no durability-green
   preconditions at all.
2. **⚠ It is the ONLY live exercise of the DRIVER-LEVEL guards.** `--limit`, the default-on denylist,
   the manifest-drift belt and the `finished_at` privilege preflight live in `ops/`, which is OUTSIDE
   the test suite's reach — a delta-verifier measured them UNPROBED. The library mapper is covered by
   65 suite probes; the driver is covered by this section and nothing else.

```bash
docker run -d --name neuro-rip-smoke -e POSTGRES_PASSWORD=pw -p 55440:5432 postgres:18
export NEURO_DATABASE_URL="postgresql+psycopg://postgres:pw@127.0.0.1:55440/postgres"
export NEURO_MIGRATION_EXPECTED_LANE=test

uv run neuro db migrate
uv run neuro db provision --lane test
uv run neuro db roles

uv run python ops/corpus-import/register_corpus.py --lane test --family mi-intervention --limit 1
```

**Expect:** `preflight OK: postgres may stamp finished_at`, then
`batch local-fs: done (1 rows), finished_at stamped`, then `1 newly registered, 0 idempotent no-ops`.

**Then exercise the guards that only exist here** — each must REFUSE, and refusing is the pass:

```bash
uv run python ops/corpus-import/register_corpus.py --lane test --limit 0            # must refuse
uv run python ops/corpus-import/register_corpus.py --lane test     --family competition-trace-the-ace --limit 1                                    # must register 0 rows
```

⚠ The second one is the important one: `--family` is applied AFTER the denylist, so naming an
EXCLUDED family selects nothing. If it registers even one row, the denylist is not default-on and
that is a **STOP** — this is the guard standing between a mistyped command and 22,825 permanent rows.

**Verify the triple and the close stamp, then discard:**

```sql
SELECT count(*) FROM neuro.artifacts;          -- 1
SELECT count(*) FROM neuro.external_records;   -- 1
SELECT count(*) FROM neuro.lineage_edges;      -- 1
SELECT finished_at IS NOT NULL FROM neuro.import_batches;  -- t
```

```bash
docker rm -f neuro-rip-smoke && unset NEURO_DATABASE_URL NEURO_MIGRATION_EXPECTED_LANE
```

⚠ **Discard the container.** Nothing from this lane is kept; its rows exist only to prove the driver
runs. And re-export the canonical DSN deliberately before §4 — carrying a test DSN into the real
sweep would fail closed at `verify_engine`, but do not rely on that as the control.

---

## §4 — One tranche first (a 1-row family)

```bash
uv run python ops/corpus-import/register_corpus.py --lane canonical --family mi-intervention
```

⚠ "Smallest" is a THREE-WAY TIE at one row — `mi-intervention`, `mi-sae-features` and
`bibliography` — so this names one rather than implying it is uniquely smallest. Any of the three
does the job.

Expect `preflight OK`, then `batch local-fs: done (1 rows), finished_at stamped`, then
`1 newly registered, 0 idempotent no-ops`.

**Verify the triple before going further.** ⚠ `family` is NOT a database column — it lives only in
the sidecar, so verify by that or by uri prefix, never by a `family` column:

```sql
SELECT count(*) FROM neuro.external_records WHERE source_table = 'corpus_files';
SELECT count(*) FROM neuro.artifacts a
  JOIN neuro.lineage_edges e ON e.dst_entity = 'artifact:' || a.artifact_id
 WHERE e.edge_kind = 'annotates';
SELECT payload_jsonb->'row'->>'family', count(*)
  FROM neuro.external_records WHERE source_table = 'corpus_files'
 GROUP BY 1 ORDER BY 1;
```

**Then run it AGAIN** and confirm `0 newly registered, 1 idempotent no-ops` — full-pass idempotency
proven on real data before 1,752 more permanent rows.

---

## §5 — The full sweep

```bash
uv run python ops/corpus-import/register_corpus.py --lane canonical
```

⚠ **NOT ATOMIC.** Each of the three ranks opens its own transaction, and the sweep is a loop, so an
interruption leaves a PERMANENT partial import. That is not a defect to repair — it is the shape of
the shipped writers.

**If it stops part-way:** re-run the same command. Every rank keep-firsts, so completed rows become
no-ops and the sweep resumes in effect. ⚠ **`finished_at` will be NULL on the interrupted batch and
a NEW batch is opened by the re-run** — the stamp is per-invocation, so two stamped batches over one
corpus is the expected shape, not a duplicate import.

⚠ **NULL `finished_at` means NEVER CLOSED, not crashed.** Batches predating `close_import_batch` —
the canonical MOSART import among them — are NULL forever and were never incomplete.

---

## §6 — Verification (per batch)

```sql
SELECT source_system, count(*) FROM neuro.import_batches GROUP BY 1;
SELECT import_batch_id, source_system, started_at, finished_at, note FROM neuro.import_batches
 ORDER BY import_batch_id DESC LIMIT 4;
SELECT count(*) FROM neuro.external_records WHERE source_table = 'corpus_files';  -- expect 1753
SELECT derived_by_predecessor, count(*) FROM neuro.external_records
 WHERE source_table = 'corpus_files' GROUP BY 1;   -- expect true=804, false=949
SELECT confidentiality, count(*) FROM neuro.external_records
 WHERE source_table = 'corpus_files' GROUP BY 1;   -- expect open=1753 (R-A), NOT 387/1366
```

⚠ That last one is the R-A check: **every** row reads `open`, including the 387 whose upstream grade
was `exam_restricted`. If any row reads `exam_restricted`, the driver did not coerce and that is a
STOP.

---

## §7 — Registering the Qwen SAE (Unit 1's writer)

`mi-sae-asset` is excluded from the pointer lane by ruling — it gets an ADR-0031 **assets row**
instead, with `sha256` streamed here on the desktop where the 2.148 GB file lives.

```python
from neuromancer_llm.db.identity import sha256_bytes  # for the digest convention
from neuromancer_llm.registry.assets import QWEN_SCOPE_LAYER18
# stream the file in 1 MiB chunks into hashlib.sha256(), then .digest() — 32 RAW bytes, never hex
repo.register_asset(
    asset_key=QWEN_SCOPE_LAYER18.asset_key,
    asset_type=QWEN_SCOPE_LAYER18.asset_type,
    loader_format=QWEN_SCOPE_LAYER18.loader_format,
    sha256=<the 32-byte digest>,
    hf_repo=QWEN_SCOPE_LAYER18.hf_repo,
    hf_revision=QWEN_SCOPE_LAYER18.hf_revision,   # None — see below
)
```

⚠ **`hf_revision=None` is PERMANENT once written** — `register_asset` is INSERT-only and REFUSES to
backfill a NULL rather than returning a false green.

**★ OPTIONAL, AND IT IS THE ONE CHANCE TO AVOID THAT:** the revision may be establishable BEFORE
writing, by comparing the streamed sha256 against the HF repo's LFS metadata for
`Qwen/SAE-Res-Qwen3-8B-Base-W64K-L0_100`. If it matches a known revision, pass it and the row lands
at FULL identity instead of partial. If it does not, pass `None` and the partial identity stands —
**do not invent a revision.** Correcting it afterwards needs an admin UPDATE, which is exactly the
remedy the module documents.

---

## §8 — Standing consequence: re-running does NOT refresh the mirror

⚠ **The identity is the pointer, not the curation** — so a re-curated manifest row at the same `uri`
lands on the SAME `source_pk`, the INSERT no-ops, and **the FIRST write's sidecar persists,
un-updated and UN-RAISED** (rank 5's drift guard deliberately excludes the sidecar).

**So: after any manifest re-curation, re-running this driver will report clean no-ops while the
stored mirror still holds the OLD note, family and grade.** Reconciling is deliberate work — read
the affected rows, decide row by row, and record what you did. It is never a re-run.

This is the accepted cost of an identity that survives re-curation; the alternative minted a new
permanent row on every re-curation, which measured at 100% of rows for one real manifest revision.
