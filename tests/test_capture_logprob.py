"""The 'logprob capture done right' slice: capture_events verbatim + dual spill, the parquet bundle's
per-queryable-artifact table_manifests fan-out, the parquet shard validity, and the live end-to-end gate.

Markers:
  * pg   — needs real Postgres (testcontainers / CI service container).
  * gpu  — the end-to-end test ALSO needs a GPU and a reachable vLLM server (skips visibly otherwise).
The parquet-roundtrip test is pure (pyarrow only) so it runs everywhere.
"""

from __future__ import annotations

import io
import json
import os

import pytest
from sqlalchemy import text

from neuromancer_llm.capture.adapters.vllm import LogprobSample
from neuromancer_llm.capture.events import INLINE_CAP, capture_logprob, logprob_parquet, write_capture_event
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.storage.backends import LocalFsBackend

# The pinned Mistral-7B-v0.3 revision (E6 launch recipe, 2026-06-20).
HF_REPO = "mistralai/Mistral-7B-v0.3"
HF_REVISION = "caa1feb0e54d415e2df31207e5f4e273e33509b1"


def _seed_model(repo) -> int:
    """Register a tokenizer + model identity through the register-first path; return model_id."""
    tok = repo.register_tokenizer_identity(tokenizer_hash=b"tok-hash-fixture", hf_repo=HF_REPO)
    return repo.register_model_identity(
        hf_repo=HF_REPO,
        hf_revision=HF_REVISION,
        dtype_quant="bf16",
        tokenizer_id=tok,
        tokenizer_hash=b"tok-hash-fixture",
        serving_stack="vllm",
        serving_version="0.23.0",
        arch_family="llama",
    )


def _synthetic_sample(n: int = 20) -> LogprobSample:
    pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(n))  # descending logprobs
    return LogprobSample(prompt="p", generated_token_id=1000, top_logprobs=pairs)


# --- parquet shard validity (pure; DuckDB-friendly) ------------------------------------------------
def test_logprob_parquet_is_valid_and_derived():
    import pyarrow.parquet as pq

    sample = _synthetic_sample(20)
    data, row_count = logprob_parquet(sample)
    assert row_count == 20
    table = pq.read_table(io.BytesIO(data))
    assert table.num_rows == 20
    assert set(table.column_names) == {"rank", "token_id", "logprob", "is_generated"}
    cols = table.to_pydict()
    # rank 0 is the argmax (the generated token); exactly one is_generated row.
    assert cols["rank"][0] == 0
    assert cols["token_id"][0] == sample.generated_token_id
    assert sum(cols["is_generated"]) == 1
    assert cols["is_generated"][0] is True


# --- capture_events writer: inline + dual spill ----------------------------------------------------
@pytest.mark.pg
def test_capture_event_inline_small(seeded, tmp_path):
    repo = seeded["repo"]
    model_id = _seed_model(repo)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    backend = LocalFsBackend(tmp_path)
    req = b'{"model":"m","prompt":"hi"}'
    resp = b'{"choices":[{"logprobs":{}}]}'
    result = write_capture_event(
        repo.engine,
        backend,
        run_id=seeded["run_id"],
        event_key="ev-small",
        model_id=model_id,
        actor_id=seeded["actor_id"],
        origin="test-host",
        request_body=req,
        response_body=resp,
        backend_id=backend_id,
        partition_path="logprobs/run=x/part-0000",
    )
    assert result.request_inlined and result.response_inlined
    assert result.request_spill_artifact_id is None and result.response_spill_artifact_id is None
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT request_text, response_text, request_spill_artifact_id, response_spill_artifact_id, "
                    "model_id, actor_id, origin FROM neuro.capture_events WHERE capture_event_id = :i"
                ),
                {"i": result.capture_event_id},
            )
            .mappings()
            .one()
        )
    # verbatim round-trip: stored TEXT is byte-identical to the wire bytes
    assert row["request_text"].encode("utf-8") == req
    assert row["response_text"].encode("utf-8") == resp
    assert (
        row["model_id"] == model_id and row["actor_id"] == seeded["actor_id"] and row["origin"] == "test-host"
    )


@pytest.mark.pg
def test_capture_event_dual_spill(seeded, tmp_path):
    """A2 dual spill: request AND response can both exceed 8 KB; each spills INDEPENDENTLY to its own
    blob artifact (the inline columns go NULL and the spill FKs are set), exercising the spill path."""
    repo = seeded["repo"]
    model_id = _seed_model(repo)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    backend = LocalFsBackend(tmp_path)
    big_req = b'{"model":"m","prompt":"' + b"R" * (INLINE_CAP + 100) + b'"}'  # > 8 KB
    big_resp = b'{"choices":["' + b"S" * (INLINE_CAP + 500) + b'"]}'  # > 8 KB
    assert len(big_req) > INLINE_CAP and len(big_resp) > INLINE_CAP
    result = write_capture_event(
        repo.engine,
        backend,
        run_id=seeded["run_id"],
        event_key="ev-big",
        model_id=model_id,
        actor_id=seeded["actor_id"],
        origin="test-host",
        request_body=big_req,
        response_body=big_resp,
        backend_id=backend_id,
        partition_path="logprobs/run=x/part-0000",
    )
    assert not result.request_inlined and not result.response_inlined
    assert result.request_spill_artifact_id is not None and result.response_spill_artifact_id is not None
    assert result.request_spill_artifact_id != result.response_spill_artifact_id  # independent artifacts
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT request_text, response_text, request_spill_artifact_id, response_spill_artifact_id "
                    "FROM neuro.capture_events WHERE capture_event_id = :i"
                ),
                {"i": result.capture_event_id},
            )
            .mappings()
            .one()
        )
        # inline columns NULL (honors the <=8KB CHECK); spill FKs set
        assert row["request_text"] is None and row["response_text"] is None
        uris = (
            conn.execute(
                text("SELECT uri, kind FROM neuro.artifacts WHERE artifact_id IN (:a, :b)"),
                {"a": result.request_spill_artifact_id, "b": result.response_spill_artifact_id},
            )
            .mappings()
            .all()
        )
    assert {u["kind"] for u in uris} == {"wire_payload"}
    # the spilled blobs are the VERBATIM bytes (round-trip from the lake)
    assert backend.get("logprobs/run=x/part-0000/wire/ev-big.request.json") == big_req
    assert backend.get("logprobs/run=x/part-0000/wire/ev-big.response.json") == big_resp


# --- register-first, fail-loud identity: negative (drift) tests (ADR-0005) -------------------------
@pytest.mark.pg
def test_register_model_identity_raises_on_drift(repo):
    """The fail-loud binding has teeth: if a stored model_identity row drifts from what its identity_hash
    encodes, re-registering the SAME identity detects the mismatch and refuses to adopt it (ADR-0005)."""
    tok = repo.register_tokenizer_identity(tokenizer_hash=b"tok")
    components = dict(
        hf_repo="r",
        hf_revision="rev1",
        dtype_quant="bf16",
        tokenizer_id=tok,
        tokenizer_hash=b"tok",
        serving_stack="vllm",
        serving_version="0.23.0",
        arch_family="llama",
    )
    repo.register_model_identity(**components)
    # corrupt the recorded hf_revision so the stored row no longer matches its identity_hash
    with repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.model_identities SET hf_revision = 'TAMPERED'"))
    with pytest.raises(IdentityMismatchError):
        repo.register_model_identity(**components)  # same hash, drifted recorded component -> raise


@pytest.mark.pg
def test_register_fingerprint_raises_on_drift(seeded):
    """Re-registering the SAME fingerprint_hash with a DIFFERENT semantic_config raises (force-new-run is an
    explicit NEW hash, never a silent overwrite; ADR-0005 insert-only, raise-on-mismatch)."""
    repo = seeded["repo"]
    model_id = _seed_model(repo)
    repo.register_fingerprint(
        fingerprint_hash=b"H", model_id=model_id, declared_mode="greedy", semantic_config="cfgA"
    )
    assert (
        repo.register_fingerprint(
            fingerprint_hash=b"H", model_id=model_id, declared_mode="greedy", semantic_config="cfgA"
        )
        is not None
    )  # idempotent for the SAME config
    with pytest.raises(IdentityMismatchError):
        repo.register_fingerprint(
            fingerprint_hash=b"H", model_id=model_id, declared_mode="greedy", semantic_config="cfgB"
        )


# --- registrar: per-queryable-artifact table_manifests fan-out -------------------------------------
@pytest.mark.pg
def test_table_manifest_fanout_one_row_per_queryable_artifact(seeded, tmp_path):
    from neuromancer_llm.bundles.registrar import BundleRegistrar, TableManifestSpec

    repo = seeded["repo"]
    model_id = _seed_model(repo)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    backend = LocalFsBackend(tmp_path)
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")

    p0, n0 = logprob_parquet(_synthetic_sample(8))
    p1, n1 = logprob_parquet(_synthetic_sample(12))
    shards = {"logprobs-0000.parquet": p0, "logprobs-0001.parquet": p1}
    bundle_id = reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path="logprobs/run=fan/part-0000",
        shards=shards,
        artifact_kinds={k: "token_table" for k in shards},
        table_manifests=[
            TableManifestSpec("logprobs-0000.parquet", "logprobs", row_count=n0, model_id=model_id),
            TableManifestSpec("logprobs-0001.parquet", "logprobs", row_count=n1, model_id=model_id),
        ],
    )
    assert reg.bundle_state(bundle_id) == "registered"
    with repo.engine.connect() as conn:
        # ONE manifest row per queryable parquet artifact (the resolved Stage-1 DEFER): 2 here, not 1.
        manifests = (
            conn.execute(
                text(
                    "SELECT tm.row_count, tm.model_id, a.kind, a.uri "
                    "FROM neuro.table_manifests tm JOIN neuro.artifacts a ON a.artifact_id = tm.artifact_id "
                    "WHERE tm.run_id = :r ORDER BY a.uri"
                ),
                {"r": seeded["run_id"]},
            )
            .mappings()
            .all()
        )
    assert len(manifests) == 2
    assert {m["kind"] for m in manifests} == {"token_table"}
    assert [m["row_count"] for m in manifests] == [n0, n1]
    assert all(m["model_id"] == model_id for m in manifests)


@pytest.mark.pg
def test_registrar_legacy_single_manifest_preserved(seeded, tmp_path):
    """Backward-compat: with no explicit specs the registrar still writes exactly ONE manifest (the
    generic seam-test behaviour) pointing at the first shard."""
    from neuromancer_llm.bundles.registrar import BundleRegistrar

    repo = seeded["repo"]
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    reg = BundleRegistrar(repo.engine, LocalFsBackend(tmp_path), expected_lane="test")
    reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="generic",
        partition_path="generic/p0",
        shards={"a.bin": b"aaa", "b.bin": b"bbb"},
    )
    with repo.engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM neuro.table_manifests WHERE run_id = :r"), {"r": seeded["run_id"]}
        ).scalar_one()
    assert count == 1


# --- the live end-to-end gate ----------------------------------------------------------------------
@pytest.mark.gpu
@pytest.mark.pg
def test_capture_logprob_end_to_end(repo, tmp_path):
    """ONE real logprob capture, durable end to end, against the live BI-on vLLM server. Asserts the
    end-state: verbatim capture_events row; fingerprint carries the 3 BI facts; parquet bundle registered
    with a token_table artifact; one table_manifests row per queryable artifact; EXPECTED resolves to
    bitwise. Skips visibly off-GPU / without a reachable server."""
    from neuromancer_llm.capture.adapters.vllm import VLLMAdapterError, VLLMClient
    from neuromancer_llm.capture.determinism import MIN_COMPUTE_CAPABILITY, host_compute_capability
    from neuromancer_llm.db.identity import sha256_bytes

    base_url = os.environ.get("NEURO_VLLM_BASE_URL", "http://127.0.0.1:8000")
    client = VLLMClient(base_url, timeout=120.0)
    if not client.is_ready():
        pytest.skip(f"no reachable vLLM server at {base_url} (set NEURO_VLLM_BASE_URL); skipped visibly")
    try:  # /health 200 alone doesn't prove it's vLLM — confirm the OpenAI route serves a model
        client.served_model()
    except VLLMAdapterError:
        pytest.skip(f"server at {base_url} is not a vLLM OpenAI server (no /v1/models); skipped visibly")
    if host_compute_capability() < MIN_COMPUTE_CAPABILITY:
        pytest.skip(f"compute capability < {MIN_COMPUTE_CAPABILITY}; capture-grade gate not applicable here")

    # tokenizer identity: the real tokenizer.json sha256 when provided, else a deterministic stand-in
    # (the assertions don't depend on the value; the operator path passes --tokenizer-file).
    tok_file = os.environ.get("NEURO_VLLM_TOKENIZER_FILE")
    tok_hash = (
        sha256_bytes(open(tok_file, "rb").read())  # noqa: SIM115
        if tok_file
        else sha256_bytes(f"{HF_REPO}@{HF_REVISION}".encode())
    )

    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    backend = LocalFsBackend(tmp_path)
    result = capture_logprob(
        repo=repo,
        backend=backend,
        backend_id=backend_id,
        client=client,
        expected_lane="test",
        hf_repo=HF_REPO,
        hf_revision=HF_REVISION,
        tokenizer_hash=tok_hash,
        campaign_key="phase4-capture",
        work_slug="mcq-next-token",
        variant_digest="v1",
        actor_key="owner",
        origin="pytest-4090",
        n_logprobs=20,
        seed=1234,
    )

    # EXPECTED resolves to bitwise (the seeded E6 rule; override stays NULL).
    assert result.expected_level == "bitwise"
    assert result.declared_mode == "greedy"

    # fingerprint carries the 3 numerics-affecting BI facts (E6 banking).
    assert '"batch_invariant":true' in result.semantic_config
    assert '"serving_version":"vllm-0.23.0"' in result.semantic_config
    assert '"runner":"V1"' in result.semantic_config

    with repo.engine.connect() as conn:
        ev = (
            conn.execute(
                text(
                    "SELECT request_text, response_text, request_spill_artifact_id, response_spill_artifact_id, "
                    "model_id, actor_id, origin FROM neuro.capture_events WHERE capture_event_id = :i"
                ),
                {"i": result.capture_event_id},
            )
            .mappings()
            .one()
        )
        assert ev["model_id"] == result.model_id and ev["origin"] == "pytest-4090"
        # at least one side present (capture_has_payload); the verbatim request must round-trip to JSON.
        assert ev["request_text"] is not None or ev["request_spill_artifact_id"] is not None
        if ev["request_text"] is not None:
            parsed = json.loads(ev["request_text"])
            assert parsed["max_tokens"] == 1 and parsed["temperature"] == 0 and parsed["logprobs"] == 20

        bundle_state = conn.execute(
            text("SELECT state FROM neuro.bundles WHERE bundle_id = :b"), {"b": result.bundle_id}
        ).scalar_one()
        assert bundle_state == "registered"

        manifests = (
            conn.execute(
                text(
                    "SELECT tm.row_count, tm.model_id, a.kind, a.uri, a.sha256, a.size_bytes "
                    "FROM neuro.table_manifests tm JOIN neuro.artifacts a ON a.artifact_id = tm.artifact_id "
                    "WHERE tm.run_id = :r"
                ),
                {"r": result.run_id},
            )
            .mappings()
            .all()
        )
    # ONE table_manifests row per queryable parquet artifact (here: the single logprob shard).
    assert len(manifests) == 1
    m = manifests[0]
    assert m["kind"] == "token_table"
    assert m["row_count"] == result.parquet_row_count == 20
    assert m["model_id"] == result.model_id

    # the parquet bytes in the lake are real + DuckDB-readable, and integrity-verified vs the manifest sha.
    import pyarrow.parquet as pq

    blob = backend.get(m["uri"])
    assert sha256_bytes(blob) == bytes(m["sha256"]) and len(blob) == m["size_bytes"]
    assert pq.read_table(io.BytesIO(blob)).num_rows == 20


@pytest.mark.gpu
@pytest.mark.pg
def test_capture_replicate_read_end_to_end(repo, provisioned_roles, role_url, tmp_path):
    """The READ + VERIFY gate, live: capture an experiment + a DISTINCT replicate on the BI-on server, measure
    divergence with the registered method (MUST be bitwise — EXPECTED=bitwise holds, max_abs_diff==0), and
    have a SELECT-only neuro_reader round-trip the original's parquet via the manifest (integrity-verified).
    Skips visibly off-GPU / without a reachable server."""
    from sqlalchemy import create_engine

    from neuromancer_llm.capture.adapters.vllm import VLLMAdapterError, VLLMClient
    from neuromancer_llm.capture.determinism import MIN_COMPUTE_CAPABILITY, host_compute_capability
    from neuromancer_llm.capture.events import replicate_and_measure
    from neuromancer_llm.capture.reader import read_run_logprobs
    from neuromancer_llm.composer import new_invocation_id
    from neuromancer_llm.db.identity import sha256_bytes

    base_url = os.environ.get("NEURO_VLLM_BASE_URL", "http://127.0.0.1:8000")
    client = VLLMClient(base_url, timeout=120.0)
    if not client.is_ready():
        pytest.skip(f"no reachable vLLM server at {base_url} (set NEURO_VLLM_BASE_URL); skipped visibly")
    try:
        client.served_model()
    except VLLMAdapterError:
        pytest.skip(f"server at {base_url} is not a vLLM OpenAI server (no /v1/models); skipped visibly")
    if host_compute_capability() < MIN_COMPUTE_CAPABILITY:
        pytest.skip(f"compute capability < {MIN_COMPUTE_CAPABILITY}; capture-grade gate not applicable here")

    tok_file = os.environ.get("NEURO_VLLM_TOKENIZER_FILE")
    tok_hash = (
        sha256_bytes(open(tok_file, "rb").read())  # noqa: SIM115
        if tok_file
        else sha256_bytes(f"{HF_REPO}@{HF_REVISION}".encode())
    )
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    common = dict(
        repo=repo,
        backend=backend,
        backend_id=backend_id,
        client=client,
        expected_lane="test",
        hf_repo=HF_REPO,
        hf_revision=HF_REVISION,
        tokenizer_hash=tok_hash,
        campaign_key="phase4-capture",
        work_slug="mcq-next-token",
        variant_digest="v1",
        actor_key="owner",
        origin="pytest-4090",
        n_logprobs=20,
        seed=1234,
    )
    original = capture_logprob(**common, invocation_id=None)
    replicate = capture_logprob(**common, invocation_id=new_invocation_id())

    # the determinism loop closes BITWISE: same fingerprint, distinct re-invocation, divergence == 0.
    measured = replicate_and_measure(repo=repo, original=original, replicate=replicate)
    assert measured.original_run_id != measured.replicate_run_id
    assert measured.expected_level == "bitwise"
    assert measured.bitwise_identical and measured.meets_expected
    assert measured.max_abs_diff == 0.0 and measured.argmax_flip_rate == 0.0

    with repo.engine.connect() as conn:
        link = (
            conn.execute(
                text(
                    "SELECT original_run_id, replicate_run_id FROM neuro.replicate_links "
                    "WHERE replicate_link_id = :l"
                ),
                {"l": measured.replicate_link_id},
            )
            .mappings()
            .one()
        )
        assert link["original_run_id"] == original.run_id and link["replicate_run_id"] == replicate.run_id
        # the replicate run is an explicit re-invocation (non-NULL invocation_id), the original is canonical
        invs = (
            conn.execute(
                text("SELECT run_id, invocation_id FROM neuro.runs WHERE run_id IN (:o, :r)"),
                {"o": original.run_id, "r": replicate.run_id},
            )
            .mappings()
            .all()
        )
    by_run = {row["run_id"]: row["invocation_id"] for row in invs}
    assert by_run[original.run_id] is None and by_run[replicate.run_id] is not None

    # the read half: a SELECT-only neuro_reader reconstructs the original's logprobs from the lake via the
    # manifest, integrity-verified (sha256+size), and the generated token matches the live capture bitwise.
    reader_engine = create_engine(role_url(provisioned_roles, "neuro_reader"), future=True)
    try:
        read = read_run_logprobs(reader_engine, backend, run_id=original.run_id, dataset_name="logprobs")
    finally:
        reader_engine.dispose()
    assert read.integrity_verified >= 1
    assert read.generated_token_id == original.sample.generated_token_id
    assert read.generated_logprob == original.sample.logprob_map[original.sample.generated_token_id]
