"""The MEASURED determinism loop (ADR-0004): the registered divergence method (register-first, drift-loud),
replicate links, divergence persistence, the EXPECTED close-the-loop assertion, and replicate_and_measure
driven through the REAL capture path with a fake client (a bitwise pass + a divergence on a bitwise lane
that fails LOUD while still persisting the signal).

Pure determinism-math tests run everywhere; the orchestration + repo tests are pg (real Postgres).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from neuromancer_llm.capture.adapters.vllm import CapturedLogprobs, LogprobSample
from neuromancer_llm.capture.determinism import (
    DivergenceVerdictError,
    ExpectedLevel,
    UnassessedExpectationError,
    assert_meets_expected,
    compare,
    measure_divergence,
    near_tie_margin_nats,
)
from neuromancer_llm.capture.events import capture_logprob, replicate_and_measure
from neuromancer_llm.composer import new_invocation_id
from neuromancer_llm.db.identity import fingerprint_hash
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.storage.backends import LocalFsBackend


def _sample(n: int = 20, generated: int = 1000) -> LogprobSample:
    pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(n))  # descending; argmax = token 1000
    return LogprobSample(prompt="p", generated_token_id=generated, top_logprobs=pairs)


def _perturb(sample: LogprobSample, index: int, delta: float) -> LogprobSample:
    pairs = list(sample.top_logprobs)
    pairs[index] = (pairs[index][0], pairs[index][1] + delta)
    return LogprobSample(
        prompt=sample.prompt, generated_token_id=sample.generated_token_id, top_logprobs=tuple(pairs)
    )


class _FakeClient:
    """A vLLM stand-in that returns precomputed CapturedLogprobs (one per call), so the REAL capture +
    measure path runs without a GPU. The wire bytes are synthetic-but-verbatim (stored as captured)."""

    def __init__(self, samples: list[LogprobSample]) -> None:
        self._samples = samples
        self._calls = 0

    def served_model(self) -> str:
        return "fake/Mistral-7B-v0.3"

    def next_token_logprobs_capture(self, prompt, *, model, n_logprobs, seed) -> CapturedLogprobs:
        s = self._samples[self._calls]
        self._calls += 1
        req = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "max_tokens": 1,
                "temperature": 0,
                "seed": seed,
                "logprobs": n_logprobs,
            }
        ).encode("utf-8")
        top = {f"token_id:{t}": lp for t, lp in s.top_logprobs}
        resp = json.dumps(
            {
                "choices": [
                    {"logprobs": {"tokens": [f"token_id:{s.generated_token_id}"], "top_logprobs": [top]}}
                ]
            }
        ).encode("utf-8")
        return CapturedLogprobs(
            sample=s, request_body=req, response_body=resp, http_status=200, content_type="application/json"
        )


def _capture(repo, tmp_path, client, **over):
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    kw = dict(
        repo=repo,
        backend=backend,
        backend_id=backend_id,
        client=client,
        expected_lane="test",
        hf_repo="r",
        hf_revision="rev",
        tokenizer_hash=b"T",
        campaign_key="c",
        work_slug="slug",
        variant_digest="v1",
        actor_key="owner",
        origin="test",
        n_logprobs=20,
        seed=1234,
    )
    kw.update(over)
    return capture_logprob(**kw)


def _two_runs_same_fp(seeded) -> tuple[int, int]:
    """Two DISTINCT runs sharing ONE non-NULL fingerprint — a valid replicate pair under BLOCK 3."""
    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    repo.register_tokenizer_identity(tokenizer_hash=b"Tlink")  # register-first (FIX #9)
    mid = repo.register_model_identity(
        hf_repo="r",
        hf_revision="rev",
        dtype_quant="bf16",
        tokenizer_hash=b"Tlink",
        serving_stack="vllm",
        serving_version="0.23.0",
        arch_family="llama",
    )
    fp = repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash("clink"),
        model_id=mid,
        declared_mode="greedy",
        semantic_config="clink",
    )
    r1 = repo.get_or_create_run(
        "c-test/lk/v1",
        campaign_id=cid,
        work_slug="lk",
        variant_digest="v1",
        actor_id=aid,
        origin="o",
        fingerprint_id=fp,
    )
    r2 = repo.get_or_create_run(
        "c-test/lk/v2",
        campaign_id=cid,
        work_slug="lk",
        variant_digest="v2",
        actor_id=aid,
        origin="o",
        fingerprint_id=fp,
    )
    return r1, r2


# --- pure determinism math (no DB) -----------------------------------------------------------------
def test_measure_divergence_mapping_identical_and_perturbed():
    s = _sample(20)
    m = measure_divergence(s, s)
    assert m["max_abs_diff"] == 0.0 and m["argmax_flip_rate"] == 0.0
    assert m["answer_letter_flip_rate"] is None  # the many-item MCQ measure is a later gate
    assert m["near_tie_margin_nats"] == pytest.approx(0.01)  # top1 -0.01, top2 -0.02

    m2 = measure_divergence(s, _perturb(s, 3, -0.5))
    assert m2["max_abs_diff"] == pytest.approx(0.5)
    assert m2["argmax_flip_rate"] == 0.0  # generated token unchanged


def test_near_tie_margin_degenerate():
    one = LogprobSample(prompt="p", generated_token_id=7, top_logprobs=((7, -0.1),))
    assert near_tie_margin_nats(one) is None  # no second candidate -> no tie to measure


def test_assert_meets_expected_bitwise_and_tolerance():
    s = _sample(20)
    assert assert_meets_expected(ExpectedLevel.BITWISE, compare(s, s), dtype_quant="bf16") is True

    tiny = compare(s, _perturb(s, 2, -1e-6))  # a 1e-6 nat drift
    with pytest.raises(DivergenceVerdictError):  # bitwise lane: ANY divergence is loud
        assert_meets_expected(ExpectedLevel.BITWISE, tiny, dtype_quant="bf16")
    assert assert_meets_expected(ExpectedLevel.TOLERANCE, tiny, dtype_quant="bf16") is True  # within 5e-2

    big = compare(s, _perturb(s, 2, -0.5))
    with pytest.raises(DivergenceVerdictError):  # exceeds the bf16 tolerance
        assert_meets_expected(ExpectedLevel.TOLERANCE, big, dtype_quant="bf16")

    # FIX #2: no APPLICABLE rule is a TYPED refusal (split from the deliberate NONE, which still passes)
    with pytest.raises(UnassessedExpectationError):
        assert_meets_expected(None, big, dtype_quant="bf16")
    assert assert_meets_expected(ExpectedLevel.NONE, big, dtype_quant="bf16") is True


# --- repo registry + link + divergence (pg) --------------------------------------------------------
@pytest.mark.pg
def test_register_method_version_sets_active_and_detects_drift(repo):
    sha_a = b"\xaa" * 32
    mv1 = repo.register_method_version(method_key="logprob_divergence", semver="1.0.0", code_sha=sha_a)
    with repo.engine.connect() as conn:
        active = conn.execute(
            text("SELECT active_version_id FROM neuro.methods WHERE method_key = 'logprob_divergence'")
        ).scalar_one()
    assert active == mv1
    # idempotent: same semver + same sha -> same id
    assert (
        repo.register_method_version(method_key="logprob_divergence", semver="1.0.0", code_sha=sha_a) == mv1
    )
    # drift: same semver, DIFFERENT sha -> raises (ADR-0011 registry/runtime parity)
    with pytest.raises(IdentityMismatchError):
        repo.register_method_version(method_key="logprob_divergence", semver="1.0.0", code_sha=b"\xbb" * 32)
    # a new semver coexists
    mv2 = repo.register_method_version(method_key="logprob_divergence", semver="1.1.0", code_sha=b"\xcc" * 32)
    assert mv2 != mv1


@pytest.mark.pg
def test_link_replicate_distinct_and_idempotent(seeded):
    repo = seeded["repo"]
    r1, r2 = _two_runs_same_fp(seeded)  # a valid replicate pair (one shared fingerprint)
    link = repo.link_replicate(original_run_id=r1, replicate_run_id=r2)
    assert repo.link_replicate(original_run_id=r1, replicate_run_id=r2) == link  # idempotent
    with pytest.raises(ValueError):  # the replicate_distinct CHECK, guarded for a clear error
        repo.link_replicate(original_run_id=r1, replicate_run_id=r1)


@pytest.mark.pg
def test_link_replicate_requires_same_fingerprint(seeded):
    """BLOCK 3: a replicate pair must be the SAME experiment — two runs with DIFFERENT fingerprints raise
    ReplicateMismatchError BEFORE any replicate_links row is written (MEASURED divergence is meaningless
    across experiments)."""
    from neuromancer_llm.db.repository import ReplicateMismatchError

    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    repo.register_tokenizer_identity(tokenizer_hash=b"Tmm")  # register-first (FIX #9)
    mid = repo.register_model_identity(
        hf_repo="r",
        hf_revision="rev",
        dtype_quant="bf16",
        tokenizer_hash=b"Tmm",
        serving_stack="vllm",
        serving_version="0.23.0",
        arch_family="llama",
    )
    fp1 = repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash("a"), model_id=mid, declared_mode="greedy", semantic_config="a"
    )
    fp2 = repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash("b"), model_id=mid, declared_mode="greedy", semantic_config="b"
    )
    r1 = repo.get_or_create_run(
        "c-test/mm/v1",
        campaign_id=cid,
        work_slug="mm",
        variant_digest="v1",
        actor_id=aid,
        origin="o",
        fingerprint_id=fp1,
    )
    r2 = repo.get_or_create_run(
        "c-test/mm/v2",
        campaign_id=cid,
        work_slug="mm",
        variant_digest="v2",
        actor_id=aid,
        origin="o",
        fingerprint_id=fp2,
    )
    with pytest.raises(ReplicateMismatchError):
        repo.link_replicate(original_run_id=r1, replicate_run_id=r2)
    with repo.engine.connect() as conn:  # no link row written
        assert conn.execute(text("SELECT count(*) FROM neuro.replicate_links")).scalar_one() == 0


@pytest.mark.pg
def test_record_divergence_idempotent(seeded):
    repo = seeded["repo"]
    r1, r2 = _two_runs_same_fp(seeded)
    link = repo.link_replicate(original_run_id=r1, replicate_run_id=r2)
    mv = repo.register_method_version(method_key="logprob_divergence", semver="1.0.0", code_sha=b"\x01" * 32)
    common = dict(
        replicate_link_id=link,
        method_version_id=mv,
        max_abs_diff=0.0,
        max_rel_diff=0.0,
        argmax_flip_rate=0.0,
        answer_letter_flip_rate=None,
        near_tie_margin_nats=0.01,
    )
    assert repo.record_divergence(**common) == repo.record_divergence(**common)  # idempotent on the UNIQUE


# --- replicate_and_measure through the real capture path (pg, fake client) --------------------------
@pytest.mark.pg
def test_replicate_and_measure_bitwise_closes_loop(repo, tmp_path):
    s = _sample(20)
    client = _FakeClient([s, s])  # identical captures -> bitwise
    original = _capture(repo, tmp_path, client, invocation_id=None)
    replicate = _capture(repo, tmp_path, client, invocation_id=new_invocation_id())
    assert original.run_id != replicate.run_id  # distinct runs (the replicate is a re-invocation)

    result = replicate_and_measure(repo=repo, original=original, replicate=replicate)
    assert result.bitwise_identical and result.meets_expected
    assert result.max_abs_diff == 0.0 and result.expected_level == "bitwise"

    with repo.engine.connect() as conn:
        link = (
            conn.execute(
                text(
                    "SELECT original_run_id, replicate_run_id FROM neuro.replicate_links "
                    "WHERE replicate_link_id = :l"
                ),
                {"l": result.replicate_link_id},
            )
            .mappings()
            .one()
        )
        assert link["original_run_id"] == original.run_id and link["replicate_run_id"] == replicate.run_id
        dm = (
            conn.execute(
                text(
                    "SELECT max_abs_diff, method_version_id, answer_letter_flip_rate "
                    "FROM neuro.divergence_measurements WHERE replicate_link_id = :l"
                ),
                {"l": result.replicate_link_id},
            )
            .mappings()
            .one()
        )
    assert dm["max_abs_diff"] == 0.0 and dm["method_version_id"] == result.method_version_id
    assert dm["answer_letter_flip_rate"] is None


@pytest.mark.pg
def test_replicate_and_measure_divergent_fails_loud_on_bitwise_lane(repo, tmp_path):
    """A non-zero divergence on a bitwise lane is a LOUD signal — replicate_and_measure raises AND persists
    the divergence row (the signal is data, never a silent pass). Mirrors the E6 control arm: token stable,
    logprobs drift."""
    s = _sample(20)
    client = _FakeClient([s, _perturb(s, 5, -0.1)])  # the replicate drifts by 0.1 nat on one candidate
    original = _capture(repo, tmp_path, client, invocation_id=None)
    replicate = _capture(repo, tmp_path, client, invocation_id=new_invocation_id())

    with pytest.raises(DivergenceVerdictError):
        replicate_and_measure(repo=repo, original=original, replicate=replicate)

    # the divergence was RECORDED before the raise (loud, not swallowed)
    with repo.engine.connect() as conn:
        dm = (
            conn.execute(text("SELECT max_abs_diff, argmax_flip_rate FROM neuro.divergence_measurements"))
            .mappings()
            .one()
        )
    assert dm["max_abs_diff"] == pytest.approx(0.1) and dm["argmax_flip_rate"] == 0.0


# --- BLOCK 2: the model is INSIDE the fingerprint hash ---------------------------------------------
def test_semantic_config_carries_model_identity():
    """Pure: the model_identity is part of the hashed material, so two models with identical decode/prompt/
    substrate produce DISTINCT fingerprint hashes (and the same model is idempotent)."""
    from neuromancer_llm.config.semantic import build_semantic_config
    from neuromancer_llm.db.identity import fingerprint_hash

    base = dict(
        declared_mode="greedy",
        decoding={"seed": 1234},
        batch_invariant=True,
        serving_version="vllm-0.23.0",
        runner="V1",
        prompt_identity="ph",
    )
    cfg_a = build_semantic_config(**base, model_identity="AAAA")
    cfg_b = build_semantic_config(**base, model_identity="BBBB")
    assert '"model_identity":"AAAA"' in cfg_a
    assert fingerprint_hash(cfg_a) != fingerprint_hash(cfg_b)  # the model is inside the hash
    assert fingerprint_hash(cfg_a) == fingerprint_hash(build_semantic_config(**base, model_identity="AAAA"))


@pytest.mark.pg
def test_fingerprint_distinguishes_models(repo, tmp_path):
    """BLOCK 2: two captures differing only by model (dtype) get DISTINCT fingerprints (no collision, no
    misleading reject); same model+config is idempotent (one fingerprint). Distinct run coords keep the
    runs from colliding so the fingerprint comparison is the only thing under test."""
    s = _sample(20)
    a = _capture(repo, tmp_path, _FakeClient([s]), variant_digest="va", dtype_quant="bf16")
    b = _capture(repo, tmp_path, _FakeClient([s]), variant_digest="vb", dtype_quant="bf16")
    assert a.fingerprint_id == b.fingerprint_id  # same model+config -> same experiment identity
    c = _capture(repo, tmp_path, _FakeClient([s]), variant_digest="vc", dtype_quant="fp16")
    assert c.fingerprint_id != a.fingerprint_id  # different model -> DISTINCT fingerprint, no reject


# --- BLOCK 4: the capture lane's role posture is honest (admin-only path) ---------------------------
@pytest.mark.pg
def test_capture_path_role_boundary(repo, provisioned_roles, role_url, tmp_path):
    """BLOCK 4: on the REAL capture path, neuro_writer is DENIED at the first registry INSERT (it cannot mint
    identities); the control-plane path succeeds only under an admin DSN (it also needs the writer-side bundle
    UPDATEs, so neither single role suffices)."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import ProgrammingError

    from neuromancer_llm.db.repository import Repository

    backend = LocalFsBackend(tmp_path)
    # pre-seed the storage backend as superuser so the role test begins exactly at the first REGISTRY insert
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    s = _sample(20)

    def _run_as(role: str):
        eng = create_engine(role_url(provisioned_roles, role), future=True)
        try:
            r = Repository(eng, expected_lane="test")
            return capture_logprob(
                repo=r,
                backend=backend,
                backend_id=backend_id,
                client=_FakeClient([s]),
                expected_lane="test",
                hf_repo="r",
                hf_revision="rev",
                tokenizer_hash=b"Trole",
                campaign_key="rc",
                work_slug="rs",
                variant_digest="v1",
                actor_key="owner",
                origin="t",
                n_logprobs=20,
                seed=1234,
            )
        finally:
            eng.dispose()

    with pytest.raises(ProgrammingError) as exc:  # writer: denied at register_tokenizer_identity
        _run_as("neuro_writer")
    assert "permission denied" in str(exc.value).lower()

    result = _run_as("neuro_admin")  # admin: the full control-plane path succeeds
    assert result.run_id is not None and result.fingerprint_id is not None


# --- C11: runtime -> registry parity binding -------------------------------------------------------
@pytest.mark.pg
def test_registered_divergence_method_code_sha_parity(repo, tmp_path):
    """C11: the registered divergence method_version's code_sha equals divergence_method_code_sha() at
    runtime (the registry/runtime parity binding, ADR-0011)."""
    from neuromancer_llm.capture.determinism import (
        DIVERGENCE_METHOD_KEY,
        DIVERGENCE_METHOD_SEMVER,
        divergence_method_code_sha,
    )

    s = _sample(20)
    original = _capture(repo, tmp_path, _FakeClient([s]), invocation_id=None)
    replicate = _capture(repo, tmp_path, _FakeClient([s]), invocation_id=new_invocation_id())
    replicate_and_measure(repo=repo, original=original, replicate=replicate)
    with repo.engine.connect() as conn:
        code_sha = conn.execute(
            text(
                "SELECT mv.code_sha FROM neuro.method_versions mv JOIN neuro.methods m "
                "ON m.method_id = mv.method_id WHERE m.method_key = :k AND mv.semver = :s"
            ),
            {"k": DIVERGENCE_METHOD_KEY, "s": DIVERGENCE_METHOD_SEMVER},
        ).scalar_one()
    assert bytes(code_sha) == divergence_method_code_sha()
