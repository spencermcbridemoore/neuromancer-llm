# neuromancer-llm

An LLM experimentation platform with mechanistic-interpretability capture, provenance, and export
discipline. Successor to study-query-llm.

> **Status — Phase 4, Stage 1 (scaffold).** The package skeleton, the canonical schema as an Alembic
> migration proven from zero against real PostgreSQL 18, least-privilege role provisioning, the tiered
> CI, the golden-snapshot queue harness, the W1–W8 seam kill-tests, and the byte-stable generated-docs
> gate. The vertical slice — "logprob capture done right" — is **Stage 2**, not built yet.

## Architecture

Thin **PostgreSQL 18** is the control plane — the identity core, the single SKIP-LOCKED job queue,
per-run scalar metrics, and manifest rows. All bulk lives outside it: per-token / per-feature scalars
in a hive-partitioned **parquet lake**, residual/attention tensors in a compute-local **safetensors
TTL dense lane**. Postgres never stores an O(tokens) row — `capture_events` is the cardinality ceiling
(ADR-0002). There is **no UI**: the product is capture + provenance + export discipline, read through a
SELECT-only role by notebooks, DuckDB, and chat agents.

The design is **Postgres-only** (ADR-0039 Reconsidered 2026-06-17); the schema is owned by Alembic
migrations against real Postgres and is never created with `metadata.create_all`.

The full design of record lives under `engagement/phase3/` (44 ADRs, the canonical DDL, the ORM, the
Alembic design, the capture contract, the module layout, and the Appendix-A importer spec).

## Six interpretability workflows (adoption order)

1. per-token logprob capture → 2. logit lens → 3. attention analysis → 4. SAE feature browsing →
5. activation patching → 6. steering. The schema seats all six **without migrations**.

## Quickstart (development)

```bash
uv sync                                   # create .venv, install deps, write uv.lock
neuro --version

# Bring up a local Postgres 18 (Docker) and run migrations-from-zero:
docker run -d --name neuro-pg -e POSTGRES_PASSWORD=neuro -p 5432:5432 postgres:18
export NEURO_DATABASE_URL="postgresql+psycopg://postgres:neuro@localhost:5432/postgres"
neuro db migrate                          # alembic upgrade head — materializes the canonical schema
neuro db provision --lane test            # write the lanes-v2 identity row
neuro db roles                            # create roles + apply phase3-grants.sql

neuro docs build --check                  # byte-stable generated-docs gate
uv run pytest -m "not gpu and not api and not network"
```

## CLI

`neuro` is the single console entrypoint (ADR-0038); every `cli/*` module is a thin delegate over the
library. Run `neuro --help` for the command surface (`db`, `runs`, `bundles`, `workers`, `storage`,
`capture`, `derive`, `importer`, `spend`, `probe`, `docs`).

## License

MIT — see [LICENSE](LICENSE).
