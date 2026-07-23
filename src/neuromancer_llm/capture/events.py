"""capture_events writer + the 'logprob capture done right' slice orchestration (capture contract §2/§4).

This module owns the WRITE + MEASURE orchestration of the slice:

  * write_capture_event — the immutable capture_events row writer: the TRUE wire payload stored verbatim
    as TEXT inline up to 8 KB, else each side spilled INDEPENDENTLY to its own blob artifact (A2 dual
    spill; ADR-0003/0022), with the actor/origin stamp on every capture (RULING 1). One transaction.

  * capture_logprob — capture ONE real next-token logprob pass VERBATIM -> register-first identity
    (tokenizer/model/fingerprint; ADR-0005) -> seed the E6 expected rule -> label the run (an optional
    invocation_id makes it an explicit re-invocation/replicate) -> write the capture_events verbatim row ->
    write the per-token logprob PARQUET shard through the W1-W8 registrar -> fan out one table_manifests row
    per queryable parquet artifact -> resolve the EXPECTED reproducibility level.

  * replicate_and_measure — the MEASURED determinism loop (ADR-0004) that closes the loop E6 opened:
    register the divergence method, link the original/replicate run pair, measure divergence with the
    EXISTING compare(), persist divergence_measurements, and assert MEASURED meets EXPECTED (fail loud).

The fix this slice delivers: the predecessor's logprob runs BYPASSED raw capture (repository=None) and
stored a RECONSTRUCTED request + a {"text"} response. Here the wire bytes are captured verbatim and the
parquet logprob array is DERIVED from them — never a substitute. The READ side (a SELECT-only consumer
reconstructs + integrity-verifies the captured logprobs from the lake) lives in capture/reader.py.

DEFERRED (later gates): the first-class prompt_set/stimulus registry (this slice uses content_hash(prompt)
inline); adhoc auto-mint (capture/adhoc.py); the many-item MCQ position-bias measure (this gate's MEASURED
divergence is the single-capture replicate, answer_letter_flip_rate left NULL until that gate).
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from ..bundles.bundlespec import RunModelIdentityBlock, TokenizationBlock, canonical_json
from ..bundles.registrar import BundleRegistrar, SeamIntegrityError, TableManifestSpec
from ..composer import compose_run_key
from ..config.semantic import build_semantic_config
from ..db.identity import content_hash, fingerprint_hash, model_identity_hash, sha256_bytes
from ..db.lanes import ConfigurationError
from ..db.repository import IdentityMismatchError, Repository
from ..governance.health import assert_durability_ok
from ..registry.backends import QuotaGuardedBackend, assert_cloud_put_guarded
from ..storage.backends import StorageBackend
from .adapters.vllm import LogprobSample, VLLMClient
from .determinism import (
    DEFAULT_TARGET_PROMPT,
    DIVERGENCE_METHOD_KEY,
    DIVERGENCE_METHOD_SEMVER,
    ExpectedLevel,
    assert_meets_expected,
    compare,
    declared_mode_from_request,
    divergence_method_code_sha,
    measure_divergence,
    resolve_expected_level,
)
from .determinism import substrate_key as derive_substrate_key

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


def _spill(conn: Connection, backend: StorageBackend, *, backend_id: int, prefix: str, data: bytes) -> int:
    """Spill one verbatim wire body to a CONTENT-ADDRESSED blob artifact (kind='wire_payload').

    FIX #1 (the orphan-blob / divergent-overwrite window): the storage key is a FUNCTION of the content
    sha256 (`{prefix}/{sha256}.json`), so a divergent overwrite AT THE SAME KEY is unrepresentable —
    different bytes land at a DIFFERENT key, so the non-atomic put-before-insert orphan (a blob whose
    artifact INSERT never committed) can never be silently clobbered by a later divergent spill, and an
    identical re-spill is idempotent (one artifact, no re-write). The capture_events->artifact linkage is
    the FK, never the path (ADR docs/adr/0045: content-addressed wire-spill URI scheme). The queryable
    parquet keeps its run/partition paths — only the verbatim wire-payload spill is content-addressed.

    FIX-A (Panel #2 — the concurrent same-content collision): because the key is f(sha256), two CONCURRENT
    spills of identical content from DIFFERENT event_keys race on the `(backend_id, uri)` unique — both
    SELECT-miss above, both INSERT, and the loser raised an unhandled `IntegrityError`. The targeted
    `ON CONFLICT (backend_id, uri) DO NOTHING` -> re-SELECT-and-verify dedupes them to ONE artifact (the SAME
    SELECT-then-INSERT class as BLOCK-1/1b). The targeted arbiter is correct here because `(backend_id, uri)`
    is the ONLY relevant unique (unlike BLOCK-1's bare form, forced by `model_identities`' two non-PK uniques);
    identical-byte `backend.put` by both racers is harmless. Divergent bytes still land at a DIFFERENT key."""
    digest = sha256_bytes(data)
    key = f"{prefix}/{digest.hex()}.json"
    existing = conn.execute(
        text(
            "SELECT artifact_id, sha256, size_bytes FROM neuro.artifacts "
            "WHERE backend_id = :be AND uri = :uri"
        ),
        {"be": backend_id, "uri": key},
    ).one_or_none()
    if existing is not None:
        # By construction (key = f(sha256)) the bytes match; verify defensively before the idempotent return
        # so a corrupted row / impossible sha-collision is still loud, never a silent adopt.
        if bytes(existing.sha256) != digest or existing.size_bytes != len(data):
            raise SeamIntegrityError(
                f"wire spill {key} already exists with a sha256/size that disagrees with its content-addressed "
                "key — refusing to adopt a corrupted/colliding artifact (R2; fail loud)."
            )
        return existing.artifact_id  # idempotent: identical bytes already spilled
    backend.put(key, data)  # only write the blob once we know it is new (or proven identical above)
    inserted = conn.execute(
        text(
            "INSERT INTO neuro.artifacts (bundle_id, kind, backend_id, uri, sha256, size_bytes, retention) "
            "VALUES (NULL, 'wire_payload', :be, :uri, :sha, :sz, 'ttl') "
            "ON CONFLICT (backend_id, uri) DO NOTHING RETURNING artifact_id"
        ),
        {"be": backend_id, "uri": key, "sha": digest, "sz": len(data)},
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    # FIX-A: lost a concurrent first-spill race — another writer committed this exact content key between
    # our SELECT-miss and our INSERT. Re-SELECT the winner and verify its sha256/size match the content
    # before adopting its id (R2: never a silent adopt of a corrupted/colliding row — the same defensive
    # check as the idempotent SELECT-existing branch above).
    raced = conn.execute(
        text(
            "SELECT artifact_id, sha256, size_bytes FROM neuro.artifacts "
            "WHERE backend_id = :be AND uri = :uri"
        ),
        {"be": backend_id, "uri": key},
    ).one()
    if bytes(raced.sha256) != digest or raced.size_bytes != len(data):
        raise SeamIntegrityError(
            f"wire spill {key} was concurrently registered with a sha256/size that disagrees with its "
            "content-addressed key — refusing to adopt a corrupted/colliding artifact (R2; fail loud)."
        )
    return raced.artifact_id


def _assert_capture_consistent(
    conn: Connection,
    existing,
    *,
    request_body: bytes,
    response_body: bytes,
    req_inline: bool,
    resp_inline: bool,
    model_id: int,
    actor_id: int,
    origin: str,
    provenance_header: str | None,
) -> None:
    """An EXISTING (run_id, event_key) capture row is IMMUTABLE: verify it holds the SAME verbatim wire
    bytes AND the SAME immutable attribution stamps as this re-capture and raise on ANY drift (R2). Each
    side's bytes are checked at its storage tier — inline re-encode equality, or the spilled artifact's
    sha256+size — and the inline/spill decision must match.

    C10(a) (attribution stamp drift): the bytes were verified but model_id/actor_id/origin/provenance_header
    were NOT — a same-bytes resume with a different model_id returned the old id with the OLD stamp (a silent
    attribution lie). The stamps are now part of the immutability check; a drift raises SeamIntegrityError.
    We keep ONE capture per (run_id, event_key) — the stamps are NOT folded into the event identity."""
    for stamp, want in (
        ("model_id", model_id),
        ("actor_id", actor_id),
        ("origin", origin),
        ("provenance_header", provenance_header),
    ):
        if existing[stamp] != want:
            raise SeamIntegrityError(
                f"capture {existing['capture_event_id']} {stamp}={existing[stamp]!r} differs from the "
                f"re-capture's {want!r} — refusing to resume a capture whose immutable attribution stamp "
                "drifted (C10a; one capture per (run_id, event_key), stamps immutable)."
            )
    for side, body, inline, text_col, spill_col in (
        ("request", request_body, req_inline, "request_text", "request_spill_artifact_id"),
        ("response", response_body, resp_inline, "response_text", "response_spill_artifact_id"),
    ):
        stored_text = existing[text_col]
        stored_spill = existing[spill_col]
        if inline:
            if stored_text is None or stored_text.encode("utf-8") != body:
                raise SeamIntegrityError(
                    f"capture {existing['capture_event_id']} {side} differs from the stored verbatim bytes "
                    "— refusing to resume a divergent capture (R2)."
                )
        else:
            if stored_spill is None:
                raise SeamIntegrityError(
                    f"capture {existing['capture_event_id']} {side} is stored inline but the re-capture "
                    "would spill — capture row drift (R2)."
                )
            art = conn.execute(
                text("SELECT sha256, size_bytes FROM neuro.artifacts WHERE artifact_id = :a"),
                {"a": stored_spill},
            ).one()
            if bytes(art.sha256) != sha256_bytes(body) or art.size_bytes != len(body):
                raise SeamIntegrityError(
                    f"capture {existing['capture_event_id']} {side} spill diverges from the stored "
                    "sha256/size — refusing to resume a divergent capture (R2)."
                )


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
    Spills + the row are one transaction, so the capture is atomic.

    Resume is fail-loud (R2): on a (run_id, event_key) conflict the EXISTING row is verified byte-for-byte
    against this re-capture (inline re-encode AND spilled sha256/size) and a drift raises — never a blind
    return of the old id while reporting new byte counts. Idempotent ONLY for identical bytes."""
    # C1 (GO-D-cost): the _spill put below fires BEFORE any registrar exists and only on >8 KB bodies — a
    # registrar-only guard would lie dormant until the first large payload. A raw cloud backend is refused
    # HERE, at the entry every (present and future/adhoc) capture-event caller passes through.
    assert_cloud_put_guarded(backend)
    # GO-D-cost fold 4: a guarded backend is stamped with its storage_backends row; a mis-threaded
    # backend_id would land the spill pointer under a row whose base_uri points elsewhere. Fail closed.
    if isinstance(backend, QuotaGuardedBackend) and backend_id != backend.backend_id:
        raise ConfigurationError(
            f"capture-event backend_id={backend_id} disagrees with the resolved backend's storage_backends "
            f"row (backend.backend_id={backend.backend_id}) — refusing the split-brain pointer (fail "
            "closed; thread resolve_capture_backend's (backend_id, backend) pair together)."
        )
    req_inline = len(request_body) <= INLINE_CAP
    resp_inline = len(response_body) <= INLINE_CAP
    # FIX #1: the verbatim wire spill is content-addressed under the run/partition `wire/` prefix (the key is
    # f(sha256), so a divergent overwrite at the same key is unrepresentable — see _spill).
    wire_prefix = f"{partition_path}/wire"
    cols = (
        "capture_event_id, request_text, response_text, request_spill_artifact_id, response_spill_artifact_id, "
        "model_id, actor_id, origin, provenance_header"
    )
    with engine.begin() as conn:
        # C10(b): bind capture_events.model_id to the run's fingerprint model (a write-path guard mirroring
        # assert_assign_once). The in-schema trigger is the role-split-time hardening — see the
        # Deferred-Obligation Register's "worker/registrar privilege SPLIT" entry, which now names
        # capture_events.model_id as the INSERT-side in-schema-trigger obligation at split time (when an
        # untrusted neuro_writer connects directly, this write-path check must become a trigger). A run with a
        # labeled fingerprint pins its model; a capture claiming a DIFFERENT model_id is refused (closes the
        # model_id<->run hole). NULL fingerprint (adhoc) = no bind.
        bound_model = conn.execute(
            text(
                "SELECT f.model_id FROM neuro.runs r JOIN neuro.fingerprints f "
                "ON f.fingerprint_id = r.fingerprint_id WHERE r.run_id = :run"
            ),
            {"run": run_id},
        ).scalar_one_or_none()
        if bound_model is not None and bound_model != model_id:
            raise IdentityMismatchError(
                f"capture model_id={model_id} does not match run {run_id}'s fingerprint model "
                f"{bound_model} — refusing to attribute a capture to a model the run is not fingerprinted "
                "for (C10b: capture_events.model_id is bound to the run's fingerprint model)."
            )
        existing = (
            conn.execute(
                text(f"SELECT {cols} FROM neuro.capture_events WHERE run_id = :run AND event_key = :ek"),
                {"run": run_id, "ek": event_key},
            )
            .mappings()
            .one_or_none()
        )
        if (
            existing is not None
        ):  # idempotent resume — verify byte-identity + stamps, raise on drift (R2/C10a)
            _assert_capture_consistent(
                conn,
                existing,
                request_body=request_body,
                response_body=response_body,
                req_inline=req_inline,
                resp_inline=resp_inline,
                model_id=model_id,
                actor_id=actor_id,
                origin=origin,
                provenance_header=provenance_header,
            )
            return CaptureEventResult(
                capture_event_id=existing["capture_event_id"],
                request_inlined=req_inline,
                response_inlined=resp_inline,
                request_spill_artifact_id=existing["request_spill_artifact_id"],
                response_spill_artifact_id=existing["response_spill_artifact_id"],
                request_bytes=len(request_body),
                response_bytes=len(response_body),
            )

        # NEW capture: spill (content-addressed / idempotent) then INSERT.
        req_spill = (
            None
            if req_inline
            else _spill(conn, backend, backend_id=backend_id, prefix=wire_prefix, data=request_body)
        )
        resp_spill = (
            None
            if resp_inline
            else _spill(conn, backend, backend_id=backend_id, prefix=wire_prefix, data=response_body)
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
        if eid is None:  # rare race: a concurrent insert landed after our SELECT — verify + adopt it
            raced = (
                conn.execute(
                    text(f"SELECT {cols} FROM neuro.capture_events WHERE run_id = :run AND event_key = :ek"),
                    {"run": run_id, "ek": event_key},
                )
                .mappings()
                .one()
            )
            _assert_capture_consistent(
                conn,
                raced,
                request_body=request_body,
                response_body=response_body,
                req_inline=req_inline,
                resp_inline=resp_inline,
                model_id=model_id,
                actor_id=actor_id,
                origin=origin,
                provenance_header=provenance_header,
            )
            eid = raced["capture_event_id"]
            req_spill = raced["request_spill_artifact_id"]
            resp_spill = raced["response_spill_artifact_id"]
    return CaptureEventResult(
        capture_event_id=eid,
        request_inlined=req_inline,
        response_inlined=resp_inline,
        request_spill_artifact_id=req_spill,
        response_spill_artifact_id=resp_spill,
        request_bytes=len(request_body),
        response_bytes=len(response_body),
    )


def logprob_parquet(
    sample: LogprobSample, *, dataset_name: str = "logprobs", schema_major: int = 1
) -> tuple[bytes, int, dict[str, str]]:
    """Serialize the next-token top-k distribution to a deterministic PARQUET shard (DuckDB-friendly lake
    bulk; ADR-0001/0002). One row per candidate token: rank, token_id, logprob, is_generated. This is the
    queryable projection DERIVED from the verbatim response — the wire bytes remain the authority.

    The shard is SELF-DESCRIBING (contract §5 block 14): a deterministic `neuromancer.*` str->str KV is
    embedded in the parquet schema/footer metadata and returned so the caller records the SAME dict in
    manifest block 14 and mirrors its sha256 to table_manifests.footer_kv_sha256. Only the neuromancer.*
    subset is the contract — never the raw footer (whose ARROW:schema blob is pyarrow-version-dependent).
    No timestamps/UUIDs: identical inputs must keep yielding identical bytes (resume idempotency + the
    sealed-manifest immutability both ride on it)."""
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
    columns = [{"name": f.name, "type": str(f.type)} for f in table.schema]
    footer_kv = {
        "neuromancer.format": "neuromancer-bundle/1",
        "neuromancer.dataset_name": dataset_name,
        "neuromancer.schema_major": str(schema_major),
        "neuromancer.columns": canonical_json(columns).decode("utf-8"),
        "neuromancer.row_count": str(table.num_rows),
    }
    table = table.replace_schema_metadata(footer_kv)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue(), table.num_rows, footer_kv


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
    sample: LogprobSample  # the parsed distribution this capture observed (for the MEASURED replicate loop)


def require_dtype_quant(dtype_quant: str) -> None:
    """Fail-closed guard for the caller-asserted runtime dtype/quant model-identity grade.

    `dtype_quant` folds into the MODEL IDENTITY + fingerprint (`capture_logprob`) and the E6 divergence
    TOLERANCE (`replicate_and_measure`), yet it is NOT on the wire (`capture/adapters/vllm.py`: revision/dtype/
    quant are launch-time facts) — so it is caller-asserted and, per the D1 "no default-clean member" idiom
    (`importer/ingress.py`), REQUIRED with NO default. Omission is a required-kwarg TypeError; an EMPTY/blank
    grade is refused HERE, fail closed, before any identity or tolerance decision. This belt forbids the SILENT
    default and the empty grade ONLY — a WRONG-but-present grade (a valid label on the wrong runtime, e.g.
    "bf16" declared while the server ran fp16) is the grid-consistency guard's job (registered §D follow-on;
    the dtype leaves a 2^-4/2^-7/continuous fingerprint in the logprobs — the bf16-depth diagnostic, log:244)."""
    if not dtype_quant or not dtype_quant.strip():
        raise ConfigurationError(
            "dtype_quant is REQUIRED and must be a non-empty model-identity grade (e.g. bf16/fp16/fp32); it is "
            "NOT on the wire, so the caller must assert the REAL runtime dtype — refusing an absent/blank grade "
            "(fail closed; a silent default would fold a wrong dtype into the model identity)."
        )


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
    dtype_quant: str,
    serving_stack: str = "vllm",
    serving_version: str = "0.23.0",
    arch_family: str = "llama",
    prompt: str | None = None,
    n_logprobs: int = 20,
    seed: int = 1234,
    batch_invariant: bool = True,
    runner: str = "V1",
    serving_version_tag: str = "vllm-0.23.0",
    compute_capability: float = 8.9,
    dataset_name: str = "logprobs",
    expected_uuid: uuid.UUID | str | None = None,
    event_key: str | None = None,
    actor_kind: str = "agent",
    invocation_id: uuid.UUID | None = None,
) -> LogprobCaptureResult:
    """Drive ONE real next-token logprob capture end to end ('logprob capture done right').

    Every DB write goes through the identity-verified engine the Repository was constructed with. The
    fingerprint's semantic_config carries the model identity AND the three numerics-affecting BI facts
    (batch_invariant=on, serving_version='vllm-0.23.0', runner='V1') so two captures differing only by model
    or substrate never collide or silently pool (E6 banking 2026-06-20; BLOCK 2). The substrate_key is
    DERIVED from batch_invariant + compute_capability (C5) so the key can never contradict the flag.

    CONTROL-PLANE op: this spans neuro_registrar registry INSERTs (tokenizer/model/fingerprint/run) AND
    neuro_writer bundle-lifecycle UPDATEs (seal/register), so it runs under an admin DSN — neither single
    role suffices. The Postgres grants enforce this; this path only succeeds for a role holding both.
    DEFER (BLOCK 4): the worker(writer)/control-plane(registrar) SPLIT of the lane — capture wire on the
    worker, identity/bundle registration on the control plane — belongs with the distributed worker runtime
    (workers/runtime.py); not built now.
    """
    # Fail closed on an absent/blank runtime dtype grade BEFORE any wire call or durable write — it folds into
    # the model identity below and must be a conscious caller assertion, never a silent default (D1 idiom).
    require_dtype_quant(dtype_quant)
    # A2-11b: the durability consult (below) and the internal bundle registrar gate/verify on the lane, which
    # must be TRUSTWORTHY — `expected_lane` is a free param, but `repo` was verify_engine'd for its OWN lane at
    # construction. A param that DISAGREES with the repo's verified lane is a caller misconfiguration that must
    # fail CLOSED here, BEFORE any durable write — else a canonical repo called with expected_lane='test' would
    # skip the canonical consult and write identity/fingerprint/wire rows to the canonical DB ungated (the
    # BundleRegistrar re-verify only errors later, after those writes). After this, expected_lane == the
    # verified lane, so the canonical gate is trustworthy.
    if expected_lane != repo.expected_lane:
        raise ConfigurationError(
            f"capture_logprob expected_lane={expected_lane!r} disagrees with the repository's verified lane "
            f"{repo.expected_lane!r} — refusing (fail closed; the durability consult must trust the lane)."
        )
    # C1 (GO-D-cost): refuse a raw cloud backend HERE — before served_model()/the capture wire call (fold 9),
    # so a mis-wired cloud lane costs zero adapter I/O. Defense-in-depth over the write_capture_event and
    # registrar-construction guards downstream (grep-verified: every src put path passes one of the three).
    assert_cloud_put_guarded(backend)

    prompt = prompt if prompt is not None else DEFAULT_TARGET_PROMPT
    # C5: the substrate_key is a FUNCTION of the flag + hardware, never a free string that could disagree.
    substrate_key = derive_substrate_key(compute_capability, batch_invariant=batch_invariant)

    # 1. CAPTURE the true wire payload verbatim (request + response bytes), with the derived distribution.
    served = client.served_model()
    captured = client.next_token_logprobs_capture(prompt, model=served, n_logprobs=n_logprobs, seed=seed)

    # A2-11b (ADR-0020): gate the CANONICAL capture on the durability interlock, BEFORE the FIRST durable
    # write (the register-first identity INSERTs below). A stale/flipped system_health status refuses the
    # capture before any identity, fingerprint, wire-payload, parquet, or bundle row is persisted. Canonical-
    # lane ONLY (owner-ruled 2026-07-07); the bundle registrar re-consults before its blob puts.
    if expected_lane == "canonical":
        assert_durability_ok(repo.engine)

    # 2. IDENTITY — register-first, fail-loud (registrar role; ADR-0005). INSERT-only; raise on mismatch.
    tokenizer_id = repo.register_tokenizer_identity(
        tokenizer_hash=tokenizer_hash, hf_repo=hf_repo, hf_revision=hf_revision
    )
    model_id = repo.register_model_identity(
        hf_repo=hf_repo,
        hf_revision=hf_revision,
        dtype_quant=dtype_quant,
        tokenizer_hash=tokenizer_hash,  # FIX #9: tokenizer_id is derived from this hash inside (register-first)
        serving_stack=serving_stack,
        serving_version=serving_version,
        arch_family=arch_family,
    )

    # 3. FINGERPRINT — the semantic section (hashed wholesale) carries the MODEL identity + the 3 BI facts.
    # BLOCK 2: folding model_identity INTO the hashed material means two captures that differ only by model
    # produce DISTINCT fingerprint_hashes (no collision on the global UNIQUE); model_id stays the loud backstop.
    declared = declared_mode_from_request(temperature=0, seed=seed)  # E6 / this lane is greedy
    model_identity_hex = model_identity_hash(
        hf_repo=hf_repo,
        hf_revision=hf_revision,
        dtype_quant=dtype_quant,
        tokenizer_hash=tokenizer_hash,
        serving_stack=serving_stack,
        serving_version=serving_version,
        arch_family=arch_family,
    ).hex()
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
        model_identity=model_identity_hex,
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
    # A non-NULL invocation_id is an explicit re-invocation (the replicate; ADR-0005): the run_key gets the
    # /inv-{uuid8} suffix and the run coexists under runs_experiment_variant_uq (NULLS NOT DISTINCT, C1).
    run_key = compose_run_key(campaign_key, work_slug, variant_digest, invocation_id=invocation_id)
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
        invocation_id=invocation_id,
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
    pq_bytes, row_count, footer_kv = logprob_parquet(captured.sample, dataset_name=dataset_name)
    shard_name = "logprobs-0000.parquet"
    registrar = BundleRegistrar(
        repo.engine, backend, expected_lane=expected_lane, expected_uuid=expected_uuid
    )
    # §5(a): manifest blocks 2/5 carry the SAME values that fed the registered identity rows above —
    # natural keys (hashes + components), no DB surrogate ids (a crawl-rebuild mints fresh ids; §8's
    # identity compare quarantines on the hashes). Blocks 4/6/7/12 are structurally N/A for a logprob
    # table shard (no hook points / tensor metadata / chunks / tensor stats) and completeness says so.
    bundle_id = registrar.register(
        run_id=run_id,
        backend_id=backend_id,
        dataset_name=dataset_name,
        partition_path=partition_path,
        shards={shard_name: pq_bytes},
        artifact_kinds={shard_name: "token_table"},
        run_model_identity=RunModelIdentityBlock(
            run_key=run_key,
            fingerprint_hash_hex=fp_hash.hex(),
            declared_mode=declared.value,
            semantic_config=semantic_config,
            identity_hash_hex=model_identity_hex,
            hf_repo=hf_repo,
            hf_revision=hf_revision,
            dtype_quant=dtype_quant,
            tokenizer_hash_hex=tokenizer_hash.hex(),
            serving_stack=serving_stack,
            serving_version=serving_version,
            arch_family=arch_family,
        ),
        tokenization=TokenizationBlock(
            tokenizer_hash_hex=tokenizer_hash.hex(),
            hf_repo=hf_repo,
            hf_revision=hf_revision,
            token_table_shard=shard_name,
        ),
        not_applicable=("chunk_map", "hook_point_map", "payloads", "per_tensor_stats"),
        table_manifests=[
            TableManifestSpec(
                shard_name=shard_name,
                dataset_name=dataset_name,
                row_count=row_count,
                model_id=model_id,
                schema_major=1,
                footer_kv=footer_kv,
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
        sample=captured.sample,
    )


@dataclass(frozen=True)
class ReplicateMeasureResult:
    """The durable end-state of one MEASURED determinism loop (every id/metric the gate quotes)."""

    replicate_link_id: int
    method_version_id: int
    original_run_id: int
    replicate_run_id: int
    max_abs_diff: float
    max_rel_diff: float
    argmax_flip_rate: float
    answer_letter_flip_rate: float | None
    near_tie_margin_nats: float | None
    bitwise_identical: bool
    token_set_changed: bool
    expected_level: str | None
    meets_expected: bool


def replicate_and_measure(
    *,
    repo: Repository,
    original: LogprobCaptureResult,
    replicate: LogprobCaptureResult,
    method_semver: str = DIVERGENCE_METHOD_SEMVER,
    expected_override: str | None = None,
    dtype_quant: str,
) -> ReplicateMeasureResult:
    """Close the determinism loop E6 opened (ADR-0004 MEASURED): register the divergence method, link the
    original/replicate run pair, measure divergence with the EXISTING compare(), persist it, then assert the
    MEASURED level satisfies the EXPECTED rule — raising DivergenceVerdictError on violation (a non-zero
    divergence on a bitwise lane is a loud signal, never a silent pass).

    `original` and `replicate` are two REAL captures of the SAME experiment (same fingerprint; the replicate
    is a distinct re-invocation per ADR-0005), NOT copied rows. MEASURED is an observation linked to the run
    pair — it NEVER feeds the fingerprint (ADR-0004). The divergence row is recorded BEFORE the assertion, so
    a violation is persisted (the loud signal is data) AND raised.
    """
    # Fail closed on an absent/blank dtype grade before it selects the divergence tolerance (a wrong grade
    # would pick the wrong per-dtype tolerance band and could pass a real divergence silently).
    require_dtype_quant(dtype_quant)
    # register-first: the divergence method version must exist before any divergence row references it.
    method_version_id = repo.register_method_version(
        method_key=DIVERGENCE_METHOD_KEY,
        semver=method_semver,
        code_sha=divergence_method_code_sha(),
        set_active=True,  # explicit (no default): this repoints methods.active_version_id
    )
    link_id = repo.link_replicate(original_run_id=original.run_id, replicate_run_id=replicate.run_id)
    divergence = compare(original.sample, replicate.sample)
    measured = measure_divergence(original.sample, replicate.sample)
    repo.record_divergence(
        replicate_link_id=link_id,
        method_version_id=method_version_id,
        max_abs_diff=measured["max_abs_diff"],
        max_rel_diff=measured["max_rel_diff"],
        argmax_flip_rate=measured["argmax_flip_rate"],
        answer_letter_flip_rate=measured["answer_letter_flip_rate"],
        near_tie_margin_nats=measured["near_tie_margin_nats"],
    )

    expected = resolve_expected_level(
        repo.engine,
        declared_mode=original.declared_mode,
        substrate_key=original.substrate_key,
        override=expected_override,
    )
    # close the loop — raises DivergenceVerdictError if MEASURED violates EXPECTED (no silent pass).
    meets = assert_meets_expected(expected, divergence, dtype_quant=dtype_quant)
    return ReplicateMeasureResult(
        replicate_link_id=link_id,
        method_version_id=method_version_id,
        original_run_id=original.run_id,
        replicate_run_id=replicate.run_id,
        max_abs_diff=divergence.max_abs_diff,
        max_rel_diff=divergence.max_rel_diff,
        argmax_flip_rate=measured["argmax_flip_rate"] or 0.0,
        answer_letter_flip_rate=measured["answer_letter_flip_rate"],
        near_tie_margin_nats=measured["near_tie_margin_nats"],
        bitwise_identical=divergence.bitwise_identical,
        token_set_changed=divergence.token_set_changed,
        expected_level=expected.value if expected is not None else None,
        meets_expected=meets,
    )
