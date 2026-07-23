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
from neuromancer_llm.db.identity import fingerprint_hash, model_identity_hash
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.storage.backends import LocalFsBackend

# The pinned Mistral-7B-v0.3 revision (E6 launch recipe, 2026-06-20).
HF_REPO = "mistralai/Mistral-7B-v0.3"
HF_REVISION = "caa1feb0e54d415e2df31207e5f4e273e33509b1"


def _seed_model(repo) -> int:
    """Register a tokenizer + model identity through the register-first path; return model_id."""
    repo.register_tokenizer_identity(tokenizer_hash=b"tok-hash-fixture", hf_repo=HF_REPO)  # register-first
    return repo.register_model_identity(
        hf_repo=HF_REPO,
        hf_revision=HF_REVISION,
        dtype_quant="bf16",
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
    data, row_count, _ = logprob_parquet(sample)
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
        arts = (
            conn.execute(
                text("SELECT artifact_id, uri, kind FROM neuro.artifacts WHERE artifact_id IN (:a, :b)"),
                {"a": result.request_spill_artifact_id, "b": result.response_spill_artifact_id},
            )
            .mappings()
            .all()
        )
    assert {a["kind"] for a in arts} == {"wire_payload"}
    by_id = {a["artifact_id"]: a["uri"] for a in arts}
    # the spilled blobs are the VERBATIM bytes (round-trip from the lake, content-addressed keys; FIX #1 —
    # the linkage is the FK, never the path).
    assert by_id[result.request_spill_artifact_id].startswith("logprobs/run=x/part-0000/wire/")
    assert backend.get(by_id[result.request_spill_artifact_id]) == big_req
    assert backend.get(by_id[result.response_spill_artifact_id]) == big_resp


# --- register-first, fail-loud identity: negative (drift) tests (ADR-0005) -------------------------
@pytest.mark.pg
def test_register_model_identity_raises_on_drift(repo):
    """The fail-loud binding has teeth: if a stored model_identity row drifts from what its identity_hash
    encodes, re-registering the SAME identity detects the mismatch and refuses to adopt it (ADR-0005)."""
    repo.register_tokenizer_identity(tokenizer_hash=b"tok")  # register-first (FIX #9)
    components = dict(
        hf_repo="r",
        hf_revision="rev1",
        dtype_quant="bf16",
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
    hA = fingerprint_hash("cfgA")
    repo.register_fingerprint(
        fingerprint_hash=hA, model_id=model_id, declared_mode="greedy", semantic_config="cfgA"
    )
    assert (
        repo.register_fingerprint(
            fingerprint_hash=hA, model_id=model_id, declared_mode="greedy", semantic_config="cfgA"
        )
        is not None
    )  # idempotent for the SAME config
    with pytest.raises(
        IdentityMismatchError
    ):  # SAME hash, DIFFERENT config -> raises (now via FIX #4 recompute)
        repo.register_fingerprint(
            fingerprint_hash=hA, model_id=model_id, declared_mode="greedy", semantic_config="cfgB"
        )


@pytest.mark.pg
def test_register_tokenizer_identity_raises_on_drift(repo):
    """C10: a tokenizer_hash re-register with conflicting (non-NULL) hf_repo/hf_revision raises, mirroring
    the model/fingerprint conflict path; a NULL on the incoming side is 'unspecified', not a conflict."""
    repo.register_tokenizer_identity(tokenizer_hash=b"TK", hf_repo="repoA", hf_revision="r1")
    with pytest.raises(IdentityMismatchError):  # same hash, different non-NULL hf_repo -> drift
        repo.register_tokenizer_identity(tokenizer_hash=b"TK", hf_repo="repoB", hf_revision="r1")
    assert repo.register_tokenizer_identity(tokenizer_hash=b"TK") is not None  # NULL incoming -> idempotent


# --- UNIT 1 (§D follow-on #1): the OTHER direction — right LABEL, wrong FILE ------------------------
# The test above covers the HARMLESS direction (right file, wrong label — cosmetic, the durable identity is
# the hash and the hash is correct). These cover the DAMAGING one, which the ON CONFLICT (tokenizer_hash)
# arbiter structurally cannot reach: a NEW hash never enters the conflict branch at all.


@pytest.mark.pg
def test_register_tokenizer_identity_raises_on_label_collision(repo):
    """A NEW tokenizer_hash under an ALREADY-REGISTERED, fully pinned (hf_repo, hf_revision) RAISES rather
    than silently minting a second tokenizer identity.

    Decoys first (the RESTART IDENTITY blind-id trap): two throwaway rows so the real id is not 1. The
    surviving-row assertion is on HASH BYTES as well as the id, so it cannot go blind on an id collision."""
    d1 = repo.register_tokenizer_identity(tokenizer_hash=b"decoy-1")
    d2 = repo.register_tokenizer_identity(tokenizer_hash=b"decoy-2")
    first = repo.register_tokenizer_identity(
        tokenizer_hash=b"RIGHT-FILE", hf_repo=HF_REPO, hf_revision=HF_REVISION
    )
    assert len({d1, d2, first}) == 3  # de-blinded: three distinct ids
    with pytest.raises(IdentityMismatchError) as exc:
        repo.register_tokenizer_identity(
            tokenizer_hash=b"WRONG-FILE", hf_repo=HF_REPO, hf_revision=HF_REVISION
        )
    msg = str(exc.value)
    assert b"RIGHT-FILE".hex()[:12] in msg and b"WRONG-FILE".hex()[:12] in msg  # names BOTH hashes
    # the raise ran INSIDE the caller's txn -> the just-inserted row rolled back; nothing durable was minted
    with repo.engine.connect() as conn:
        surviving = conn.execute(
            text(
                "SELECT tokenizer_id, tokenizer_hash FROM neuro.tokenizer_identities "
                "WHERE hf_repo = :r AND hf_revision = :rev"
            ),
            {"r": HF_REPO, "rev": HF_REVISION},
        ).all()
    assert [(r.tokenizer_id, bytes(r.tokenizer_hash)) for r in surviving] == [(first, b"RIGHT-FILE")]


@pytest.mark.pg
def test_label_collision_raises_when_the_wrong_file_was_already_registered(repo):
    """The escape the post-build vet CONFIRMED against a first draft that scanned only the inserted branch.

    The wrong file's hash may ALREADY be registered — one NULL-labelled row is enough — so the call lands on
    the tokenizer_hash CONFLICT branch, which mints nothing but still hands back a tokenizer_id bound to the
    wrong BYTES; the caller then forks model + fingerprint from it exactly as before, and the drift loop
    passes it because a STORED NULL is 'unspecified'. 'Nothing was inserted' is not 'nothing was bound'.
    Reddens if the scan is moved back under `if inserted is not None:`."""
    wrong = repo.register_tokenizer_identity(tokenizer_hash=b"WRONG-FILE")  # unlabelled, registered earlier
    right = repo.register_tokenizer_identity(
        tokenizer_hash=b"RIGHT-FILE", hf_repo=HF_REPO, hf_revision=HF_REVISION
    )
    assert wrong != right
    with pytest.raises(IdentityMismatchError):
        repo.register_tokenizer_identity(
            tokenizer_hash=b"WRONG-FILE", hf_repo=HF_REPO, hf_revision=HF_REVISION
        )


def test_a_tokenizer_label_collision_really_would_fork_model_identity():
    """The damage claim MEASURED, not asserted (precedent 10): two model identities differing ONLY in
    tokenizer_hash produce DIFFERENT identity_hashes. That is what makes the second tokenizer row mint a
    second model_id — and the guard above a precondition rather than a nicety. Pure function; no DB."""
    common = {
        "hf_repo": HF_REPO,
        "hf_revision": HF_REVISION,
        "dtype_quant": "bf16",
        "serving_stack": "vllm",
        "serving_version": "0.23.0",
        "arch_family": "llama",
    }
    assert model_identity_hash(tokenizer_hash=b"RIGHT-FILE", **common) != model_identity_hash(
        tokenizer_hash=b"WRONG-FILE", **common
    )


@pytest.mark.pg
def test_tokenizer_label_guard_skips_both_asymmetric_null_cases(repo):
    """The standing both-asymmetric-NULL-cases obligation. A pair with a NULL half names no immutable
    upstream artifact, so it is 'unspecified' (the conflict branch's own convention) and must still
    INSERT — deliberately out of scope, not checked-and-passed."""
    base = repo.register_tokenizer_identity(tokenizer_hash=b"base", hf_repo=HF_REPO, hf_revision=HF_REVISION)
    no_repo = repo.register_tokenizer_identity(tokenizer_hash=b"nullrepo", hf_revision=HF_REVISION)
    no_revision = repo.register_tokenizer_identity(tokenizer_hash=b"nullrev", hf_repo=HF_REPO)
    assert len({base, no_repo, no_revision}) == 3


@pytest.mark.pg
def test_tokenizer_label_guard_allows_a_genuine_revision_bump(repo):
    """Same repo, DIFFERENT pinned revision = a different upstream artifact, so a different tokenizer.json
    is expected. The guard keys on the FULL pair; keying on hf_repo alone would falsely redden here."""
    pinned = repo.register_tokenizer_identity(
        tokenizer_hash=b"rev-one", hf_repo=HF_REPO, hf_revision=HF_REVISION
    )
    bumped = repo.register_tokenizer_identity(
        tokenizer_hash=b"rev-two", hf_repo=HF_REPO, hf_revision="deadbeef" * 5
    )
    assert pinned != bumped


@pytest.mark.pg
def test_tokenizer_label_guard_allows_a_different_repo_at_the_same_revision(repo):
    """The OTHER half of 'keys on the FULL pair' — a fork/mirror can carry the same revision sha under a
    different repo, and that is a different artifact. Without this, a mutation dropping the `hf_repo`
    conjunct survives every other probe here (the criterion had no falsifying fixture until it did)."""
    original = repo.register_tokenizer_identity(
        tokenizer_hash=b"repo-one", hf_repo=HF_REPO, hf_revision=HF_REVISION
    )
    fork = repo.register_tokenizer_identity(
        tokenizer_hash=b"repo-two", hf_repo="someone/Mistral-7B-v0.3-fork", hf_revision=HF_REVISION
    )
    assert original != fork


@pytest.mark.pg
def test_tokenizer_label_guard_ignores_a_stored_unpinned_row(repo):
    """A STORED row with a NULL half must not block a later fully pinned registration under the same repo —
    the SQL equality drops it for free, and this pins that it stays dropped."""
    unpinned = repo.register_tokenizer_identity(tokenizer_hash=b"stored-null-rev", hf_repo=HF_REPO)
    later = repo.register_tokenizer_identity(
        tokenizer_hash=b"later-pinned", hf_repo=HF_REPO, hf_revision=HF_REVISION
    )
    assert unpinned != later


# --- BLOCK 1: capture resume paths fail loud (mirror the registrar's R2) ----------------------------
@pytest.mark.pg
def test_get_or_create_run_drift_raises(seeded):
    """BLOCK 1a: a run_key conflict is idempotent ONLY when the immutable identity matches; a changed
    fingerprint (= changed semantic config) under the SAME run_key fails loud (composer note D1)."""
    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    model_id = _seed_model(repo)
    fp1 = repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash("cfgA"),
        model_id=model_id,
        declared_mode="greedy",
        semantic_config="cfgA",
    )
    fp2 = repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash("cfgB"),
        model_id=model_id,
        declared_mode="greedy",
        semantic_config="cfgB",
    )
    rid = repo.get_or_create_run(
        "c-test/exp/v1",
        campaign_id=cid,
        work_slug="exp",
        variant_digest="v1",
        actor_id=aid,
        origin="o",
        fingerprint_id=fp1,
    )
    # idempotent when the identity matches
    assert (
        repo.get_or_create_run(
            "c-test/exp/v1",
            campaign_id=cid,
            work_slug="exp",
            variant_digest="v1",
            actor_id=aid,
            origin="o",
            fingerprint_id=fp1,
        )
        == rid
    )
    # changed fingerprint under the SAME run_key -> raises (no silent reuse of the old run)
    with pytest.raises(IdentityMismatchError):
        repo.get_or_create_run(
            "c-test/exp/v1",
            campaign_id=cid,
            work_slug="exp",
            variant_digest="v1",
            actor_id=aid,
            origin="o",
            fingerprint_id=fp2,
        )


@pytest.mark.pg
def test_capture_event_resume_drift_raises(seeded, tmp_path):
    """BLOCK 1b: re-writing the SAME (run_id, event_key) with DIFFERENT wire bytes raises; the stored bytes
    are unchanged. Identical re-capture is idempotent (same id)."""
    from neuromancer_llm.bundles.registrar import SeamIntegrityError

    repo = seeded["repo"]
    model_id = _seed_model(repo)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    backend = LocalFsBackend(tmp_path)
    req, resp = b'{"a":1}', b'{"b":2}'
    common = dict(
        run_id=seeded["run_id"],
        event_key="ev",
        model_id=model_id,
        actor_id=seeded["actor_id"],
        origin="h",
        backend_id=backend_id,
        partition_path="logprobs/run=x/part-0000",
    )
    first = write_capture_event(repo.engine, backend, request_body=req, response_body=resp, **common)
    again = write_capture_event(repo.engine, backend, request_body=req, response_body=resp, **common)
    assert again.capture_event_id == first.capture_event_id  # idempotent for identical bytes
    with pytest.raises(SeamIntegrityError):  # different request bytes, same (run, event_key) -> raises
        write_capture_event(repo.engine, backend, request_body=b'{"a":999}', response_body=resp, **common)
    with repo.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT request_text FROM neuro.capture_events WHERE capture_event_id = :i"),
            {"i": first.capture_event_id},
        ).scalar_one()
    assert stored.encode("utf-8") == req  # the stored verbatim bytes are UNCHANGED


@pytest.mark.pg
def test_spill_is_content_addressed_no_clobber(seeded, tmp_path):
    """BLOCK 1c / FIX #1: the wire spill is content-addressed — divergent bytes for the same logical event
    land at DIFFERENT keys (no overwrite, both blobs intact), and an identical re-spill is idempotent (same
    artifact). A divergent overwrite at the same key is now structurally unrepresentable."""
    from sqlalchemy import text as _text

    from neuromancer_llm.capture.events import _spill

    repo = seeded["repo"]
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    backend = LocalFsBackend(tmp_path)
    prefix = "logprobs/run=x/part-0000/wire"
    big1, big2 = b"A" * (INLINE_CAP + 100), b"B" * (INLINE_CAP + 100)
    with repo.engine.begin() as conn:
        a1 = _spill(conn, backend, backend_id=backend_id, prefix=prefix, data=big1)
    with repo.engine.begin() as conn:
        a2 = _spill(conn, backend, backend_id=backend_id, prefix=prefix, data=big2)
    assert a1 != a2  # divergent bytes -> distinct content-addressed artifact
    with repo.engine.connect() as conn:
        uris = dict(
            conn.execute(
                _text("SELECT artifact_id, uri FROM neuro.artifacts WHERE artifact_id IN (:a, :b)"),
                {"a": a1, "b": a2},
            ).all()
        )
    assert uris[a1] != uris[a2]  # DIFFERENT keys
    assert backend.get(uris[a1]) == big1 and backend.get(uris[a2]) == big2  # both blobs intact (no clobber)
    with repo.engine.begin() as conn:
        assert _spill(conn, backend, backend_id=backend_id, prefix=prefix, data=big1) == a1  # idempotent


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

    p0, n0, _ = logprob_parquet(_synthetic_sample(8))
    p1, n1, _ = logprob_parquet(_synthetic_sample(12))
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
    # keyed by shard NAME (the uri's last segment): the uri's sha256 directory segment (FIX #7
    # content-addressing) makes raw uri ORDER a function of the parquet bytes — hash luck, not identity.
    by_name = {m["uri"].rsplit("/", 1)[-1]: m["row_count"] for m in manifests}
    assert by_name == {"logprobs-0000.parquet": n0, "logprobs-0001.parquet": n1}
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
        dtype_quant="bf16",
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
        dtype_quant="bf16",
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
    measured = replicate_and_measure(repo=repo, original=original, replicate=replicate, dtype_quant="bf16")
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
