# neuromancer-llm

An LLM experimentation platform with mechanistic-interpretability capture, provenance, and export
discipline. Successor to study-query-llm.

> **Status — Phase 5 complete; go-remote planning underway (first checkpoint X8 pending).**
> Stage 1 (scaffold): the package skeleton, the canonical schema as an Alembic migration proven from
> zero against real PostgreSQL 18, least-privilege role provisioning, the tiered CI, the
> golden-snapshot queue harness, the W1–W8 seam kill-tests, and the byte-stable generated-docs gate.
> Stage 2 — the vertical slice, "logprob capture done right" — is **built**, both halves: verbatim wire
> capture + register-first identity + the parquet-lake write path, and the integrity-verified
> (sha256+size) read path + the MEASURED divergence loop, demonstrated live end to end. Phase 5
> red-teamed the built surface against the binding lessons (the permanent adversarial suite lives in
> `tests/redteam/`) and its correction passes have landed. Current phase: go-remote deployment
> planning (Jetstream2 VM + real Azure) — nothing provisioned yet.

## Architecture

Thin **PostgreSQL 18** is the control plane — the identity core, the single SKIP-LOCKED job queue,
per-run scalar metrics, and manifest rows. All bulk lives outside it: per-token / per-feature scalars
in a hive-partitioned **parquet lake**, residual/attention tensors in a compute-local **safetensors
TTL dense lane**. Postgres never stores an O(tokens) row — `capture_events` is the cardinality ceiling
(ADR-0002). There is **no UI**: the product is capture + provenance + export discipline, read through a
SELECT-only role by notebooks, DuckDB, and chat agents.

The design is **Postgres-only** (ADR-0039 Reconsidered 2026-06-17); the schema is owned by Alembic
migrations against real Postgres and is never created with `metadata.create_all`.

The design of record, in-repo: [docs/adr/](docs/adr/index.md) (48 ADRs, generated from
`docs/adr/_source/phase3-adrs.md`) and `tests/reference/phase3-ddl.sql` (the byte-identical in-repo
copy of the canonical frozen DDL that the migration parity test builds against). The full engagement
corpus (capture contract, module layout, importer spec, phase checkpoints) lives outside this repo.

## Six interpretability workflows (adoption order)

1. per-token logprob capture → 2. logit lens → 3. attention analysis → 4. SAE feature browsing →
5. activation patching → 6. steering. The schema seats all six **without migrations**.

## Quickstart (development)

```bash
uv sync                                   # create .venv, install deps, write uv.lock
uv run neuro --version                    # the console script lives in .venv/bin — invoke via `uv run`

# Bring up a local Postgres 18 (Docker) and run migrations-from-zero:
docker run -d --name neuro-pg -e POSTGRES_PASSWORD=neuro -p 5432:5432 postgres:18
export NEURO_DATABASE_URL="postgresql+psycopg://postgres:neuro@localhost:5432/postgres"
uv run neuro db migrate                   # alembic upgrade head — materializes the canonical schema
uv run neuro db provision --lane test     # write the lanes-v2 identity row
uv run neuro db roles                     # create roles + apply phase3-grants.sql

uv run neuro docs build --check           # byte-stable generated-docs gate
uv run pytest -m "not gpu and not api and not network"
```

## CLI

`neuro` is the single console entrypoint (ADR-0038); every `cli/*` module is a thin delegate over the
library. Run `neuro --help` for the command surface (`db`, `runs`, `bundles`, `workers`, `storage`,
`capture`, `derive`, `importer`, `spend`, `probe`, `docs`).

## Operations

Canonical Jetstream2 Postgres VM (re)provisioning is captured as code in [ops/provision-canonical.sh](ops/provision-canonical.sh) — the ADR-0041 docs-that-cannot-rot infrastructure layer (a rebuild restores the pinned identity; it never re-provisions).

## License

MIT — see [LICENSE](LICENSE).
