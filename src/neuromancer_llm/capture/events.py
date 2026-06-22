"""capture_events writer + the 'logprob capture done right' slice orchestration (capture contract §2/§4).

This module owns two things:

  * write_capture_event — the immutable capture_events row writer: the TRUE wire payload stored verbatim
    as TEXT inline up to 8 KB, else each side spilled INDEPENDENTLY to its own blob artifact (A2 dual
    spill; ADR-0003/0022), with the actor/origin stamp on every capture (RULING 1). One transaction.

  * capture_logprob — the Stage-2 vertical slice end to end: capture ONE real next-token logprob pass
    VERBATIM -> register-first identity (tokenizer/model/fingerprint; ADR-0005) -> seed the E6 expected
    rule -> label the run -> write the capture_events verbatim row -> write the per-token logprob PARQUET
    shard through the W1-W8 registrar -> fan out one table_manifests row per queryable parquet artifact ->
    resolve the EXPECTED reproducibility level.

The fix this slice delivers: the predecessor's logprob runs BYPASSED raw capture (repository=None) and
stored a RECONSTRUCTED request + a {"text"} response. Here the wire bytes are captured verbatim and the
parquet logprob array is DERIVED from them — never a substitute for them.

DEFERRED (later gates; one-line homes noted): the READ + VERIFY half (DuckDB read via the reader role;
the replicate/divergence MEASURED loop over replicate_links + divergence_measurements); the first-class
prompt_set/stimulus registry; adhoc auto-mint (capture/adhoc.py). This slice captures a LABELED run.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..bundles.registrar import BundleRegistrar, SeamIntegrityError, TableManifestSpec
from ..composer import compose_run_key
from ..config.semantic import build_semantic_config
from ..db.identity import content_hash, fingerprint_hash, sha256_bytes
from ..db.repository import Repository
from ..storage.backends import StorageBackend
from .adapters.vllm import LogprobSample, VLLMClient
from .determinism import (
    DEFAULT_TARGET_PROMPT,
    ExpectedLevel,
    declared_mode_from_request,
    resolve_expected_level,
)

# capture_events.{request,response}_text octet cap (mirrors the capture_inline_cap CHECK; ADR-0003/0022).
INLINE_CAP = 8192


@dataclass(frozen=True)
class CaptureEventResult:
    """What was written + the per-side inline/spill decision (the auditable end-state of one capture)."""

    capture_event_id: int
    request_inlined: bool
    response_inlined: bool
    request_spill_artifact_id: int | None
    response_spill_artifact_id: int | None
    request_bytes: int
    response_bytes: int


def _spill(conn: Connection, backend: StorageBackend, *, backend_id: int, key: str, data: bytes) -> int:
    """Spill one verbatim wire body to a blob artifact (kind='wire_payload'). Fail-loud, never overwrite:
    re-spilling the same key with DIFFERENT bytes raises (verbatim integrity, R2 discipline)."""
    backend.put(key, data)
    digest = sha256_bytes(data)
    row = conn.execute(
        text(
            "INSERT INTO neuro.artifacts (bundle_id, kind, backend_id, uri, sha256, size_bytes, retention) "
            "VALUES (NULL, 'wire_payload', :be, :uri, :sha, :sz, 'ttl') "
            # self-assign no-op on conflict so RETURNING yields the EXISTING sha256 (not overwritten)
            "ON CONFLICT (backend_id, uri) DO UPDATE SET size_bytes = neuro.artifacts.size_bytes "
            "RETURNING artifact_id, sha256"
        ),
        {"be": backend_id, "uri": key, "sha": digest, "sz": len(data)},
    ).one()
    if bytes(row.sha256) != digest:
        raise SeamIntegrityError(
            f"wire spill {key} already exists with a different sha256 — verbatim payload diverged."
        )
    return row.artifact_id


def write_capture_event(
    engine: Engine,
    backend: StorageBackend,
    *,
    run_id: int,
    event_key: str,
    model_id: int,
    actor_id: int,
    origin: str,
    request_body: bytes,
    response_body: bytes,
    backend_id: int,
    partition_path: str,
    job_id: int | None = None,
    provenance_header: str | None = None,
) -> CaptureEventResult:
    """Write the immutable capture_events row with the VERBATIM wire bytes.

    Each side is stored inline as TEXT when its UTF-8 byte length is <= 8 KB, else spilled INDEPENDENTLY
    to its own blob artifact (A2 dual spill) with the spill FK set. Honors the capture_inline_cap CHECK.
    Spills + the row are one transaction, so the capture is atomic. Idempotent on (run_id, event_key)."""
    req_inline = len(request_body) <= INLINE_CAP
    resp_inline = len(response_body) <= INLINE_CAP
    with engine.begin() as conn:
        req_spill = (
            None
            if req_inline
            else _spill(
                conn,
                backend,
                backend_id=backend_id,
                key=f"{partition_path}/wire/{event_key}.request.json",
                data=request_body,
            )
        )
        resp_spill = (
            None
            if resp_inline
            else _spill(
                conn,
                backend,
                backend_id=backend_id,
                key=f"{partition_path}/wire/{event_key}.response.json",
                data=response_body,
            )
        )
        # Inline path: decode the verbatim UTF-8 wire bytes to TEXT (round-trips exactly; the JSON wire is
        # UTF-8, so octet_length(text) == len(bytes) <= 8192, satisfying the CHECK).
        request_text = request_body.decode("utf-8") if req_inline else None
        response_text = response_body.decode("utf-8") if resp_inline else None
        eid = conn.execute(
            text(
                "INSERT INTO neuro.capture_events "
                "(run_id, job_id, event_key, model_id, actor_id, origin, request_text, response_text, "
                "request_spill_artifact_id, response_spill_artifact_id, provenance_header) "
                "VALUES (:run, :job, :ek, :mid, :aid, :o, :rt, :respt, :rs, :resps, :ph) "
                "ON CONFLICT (run_id, event_key) DO NOTHING RETURNING capture_event_id"
            ),
            {
                "run": run_id,
                "job": job_id,
                "ek": event_key,
                "mid": model_id,
                "aid": actor_id,
                "o": origin,
                "rt": request_text,
                "respt": response_text,
                "rs": req_spill,
                "resps": resp_spill,
                "ph": provenance_header,
            },
        ).scalar_one_or_none()
        if eid is None:  # idempotent resume: the (run, event_key) row already exists
            eid = conn.execute(
                text(
                    "SELECT capture_event_id FROM neuro.capture_events "
                    "WHERE run_id = :run AND event_key = :ek"
                ),
                {"run": run_id, "ek": event_key},
            ).scalar_one()
    return CaptureEventResult(
        capture_event_id=eid,
        request_inlined=req_inline,
        response_inlined=resp_inline,
        request_spill_artifact_id=req_spill,
        response_spill_artifact_id=resp_spill,
        request_bytes=len(request_body),
        response_bytes=len(response_body),
    )


def logprob_parquet(sample: LogprobSample) -> tuple[bytes, int]:
    """Serialize the next-token top-k distribution to a deterministic PARQUET shard (DuckDB-friendly lake
    bulk; ADR-0001/0002). One row per candidate token: rank, token_id, logprob, is_generated. This is the
    queryable projection DERIVED from the verbatim response — the wire bytes remain the authority."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    pairs = sorted(sample.top_logprobs, key=lambda p: (-p[1], p[0]))  # logprob desc, token_id tiebreak
    table = pa.table(
        {
            "rank": pa.array(list(range(len(pairs))), pa.int32()),
            "token_id": pa.array([tid for tid, _ in pairs], pa.int64()),
            "logprob": pa.array([lp for _, lp in pairs], pa.float64()),
            "is_generated": pa.array([tid == sample.generated_token_id for tid, _ in pairs], pa.bool_()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue(), table.num_rows


@dataclass(frozen=True)
class LogprobCaptureResult:
    """The durable end-state of one logprob capture (every id the gate quotes)."""

    run_id: int
    run_key: str
    tokenizer_id: int
    model_id: int
    fingerprint_id: int
    capture_event_id: int
    bundle_id: int
    declared_mode: str
    expected_level: str | None
    substrate_key: str
    semantic_config: str
    event_key: str
    partition_path: str
    parquet_row_count: int
    request_inlined: bool
    response_inlined: bool
    request_bytes: int
    response_bytes: int
    served_model: str


def capture_logprob(
    *,
    repo: Repository,
    backend: StorageBackend,
    backend_id: int,
    client: VLLMClient,
    expected_lane: str,
    hf_repo: str,
    hf_revision: str,
    tokenizer_hash: bytes,
    campaign_key: str,
    work_slug: str,
    variant_digest: str,
    actor_key: str,
    origin: str,
    dtype_quant: str = "bf16",
    serving_stack: str = "vllm",
    serving_version: str = "0.23.0",
    arch_family: str = "llama",
    prompt: str | None = None,
    n_logprobs: int = 20,
    seed: int = 1234,
    batch_invariant: bool = True,
    runner: str = "V1",
    serving_version_tag: str = "vllm-0.23.0",
    substrate_key: str = "vllm-bi-on@sm89",
    dataset_name: str = "logprobs",
    expected_uuid: uuid.UUID | str | None = None,
    event_key: str | None = None,
    actor_kind: str = "agent",
) -> LogprobCaptureResult:
    """Drive ONE real next-token logprob capture end to end ('logprob capture done right').

    Every DB write goes through the identity-verified engine the Repository was constructed with. The
    fingerprint's semantic_config carries the three numerics-affecting BI facts (batch_invariant=on,
    serving_version='vllm-0.23.0', runner='V1') so substrates never silently pool (E6 banking 2026-06-20).
    """
    prompt = prompt if prompt is not None else DEFAULT_TARGET_PROMPT

    # 1. CAPTURE the true wire payload verbatim (request + response bytes), with the derived distribution.
    served = client.served_model()
    captured = client.next_token_logprobs_capture(prompt, model=served, n_logprobs=n_logprobs, seed=seed)

    # 2. IDENTITY — register-first, fail-loud (registrar role; ADR-0005). INSERT-only; raise on mismatch.
    tokenizer_id = repo.register_tokenizer_identity(
        tokenizer_hash=tokenizer_hash, hf_repo=hf_repo, hf_revision=hf_revision
    )
    model_id = repo.register_model_identity(
        hf_repo=hf_repo,
        hf_revision=hf_revision,
        dtype_quant=dtype_quant,
        tokenizer_id=tokenizer_id,
        tokenizer_hash=tokenizer_hash,
        serving_stack=serving_stack,
        serving_version=serving_version,
        arch_family=arch_family,
    )

    # 3. FINGERPRINT — the semantic section (hashed wholesale) carries the 3 BI facts (E6 banking).
    declared = declared_mode_from_request(temperature=0, seed=seed)  # E6 / this lane is greedy
    semantic_config = build_semantic_config(
        declared_mode=declared.value,
        decoding={
            "temperature": 0,
            "max_tokens": 1,
            "seed": seed,
            "n_logprobs": n_logprobs,
            "logprobs_mode": "raw_logprobs",
        },
        batch_invariant=batch_invariant,
        serving_version=serving_version_tag,
        runner=runner,
        prompt_identity=content_hash(prompt).hex(),
    )
    fp_hash = fingerprint_hash(semantic_config)
    fingerprint_id = repo.register_fingerprint(
        fingerprint_hash=fp_hash,
        model_id=model_id,
        declared_mode=declared.value,
        semantic_config=semantic_config,
    )

    # 4. SEED the E6 expected rule (idempotent config seed; ADR-0004 heuristic table, never identity).
    repo.seed_expected_rule(
        declared_mode=declared.value,
        substrate_key=substrate_key,
        expected=ExpectedLevel.BITWISE.value,
        note="E6 gate 2026-06-20: BI-on greedy next-token logprobs bitwise on sm89",
    )

    # 5. RUN — a LABELED experiment run (fingerprint_id set at insert; expected_level_override stays NULL).
    run_key = compose_run_key(campaign_key, work_slug, variant_digest)
    actor_id = repo.get_or_create_actor(actor_key, kind=actor_kind)
    campaign_id = repo.get_or_create_campaign(campaign_key, actor_id)
    run_id = repo.get_or_create_run(
        run_key,
        campaign_id=campaign_id,
        work_slug=work_slug,
        variant_digest=variant_digest,
        actor_id=actor_id,
        origin=origin,
        run_kind="experiment",
        fingerprint_id=fingerprint_id,
    )
    expected = resolve_expected_level(
        repo.engine, declared_mode=declared.value, substrate_key=substrate_key
    )  # the rule -> bitwise

    # 6. capture_events — the immutable verbatim wire row (writer role; inline<=8KB else dual spill).
    ek = event_key or f"{dataset_name}/{variant_digest}/next-token"
    partition_path = f"{dataset_name}/run={run_id}/part-0000"
    provenance = json.dumps(
        {"http_status": captured.http_status, "content_type": captured.content_type, "served_model": served},
        sort_keys=True,
        separators=(",", ":"),
    )
    ev = write_capture_event(
        repo.engine,
        backend,
        run_id=run_id,
        event_key=ek,
        model_id=model_id,
        actor_id=actor_id,
        origin=origin,
        request_body=captured.request_body,
        response_body=captured.response_body,
        backend_id=backend_id,
        partition_path=partition_path,
        provenance_header=provenance,
    )

    # 7. PARQUET bulk -> W1-W8 registrar -> table_manifests fan-out (one row per queryable parquet artifact).
    pq_bytes, row_count = logprob_parquet(captured.sample)
    shard_name = "logprobs-0000.parquet"
    registrar = BundleRegistrar(
        repo.engine, backend, expected_lane=expected_lane, expected_uuid=expected_uuid
    )
    bundle_id = registrar.register(
        run_id=run_id,
        backend_id=backend_id,
        dataset_name=dataset_name,
        partition_path=partition_path,
        shards={shard_name: pq_bytes},
        artifact_kinds={shard_name: "token_table"},
        table_manifests=[
            TableManifestSpec(
                shard_name=shard_name,
                dataset_name=dataset_name,
                row_count=row_count,
                model_id=model_id,
                schema_major=1,
            )
        ],
    )

    return LogprobCaptureResult(
        run_id=run_id,
        run_key=run_key,
        tokenizer_id=tokenizer_id,
        model_id=model_id,
        fingerprint_id=fingerprint_id,
        capture_event_id=ev.capture_event_id,
        bundle_id=bundle_id,
        declared_mode=declared.value,
        expected_level=expected.value if expected is not None else None,
        substrate_key=substrate_key,
        semantic_config=semantic_config,
        event_key=ek,
        partition_path=partition_path,
        parquet_row_count=row_count,
        request_inlined=ev.request_inlined,
        response_inlined=ev.response_inlined,
        request_bytes=ev.request_bytes,
        response_bytes=ev.response_bytes,
        served_model=served,
    )
