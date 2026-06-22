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
    assert_meets_expected,
    compare,
    measure_divergence,
    near_tie_margin_nats,
)
from neuromancer_llm.capture.events import capture_logprob, replicate_and_measure
from neuromancer_llm.composer import new_invocation_id
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

    # no rule / distributional / none -> nothing to assert at this grain
    assert assert_meets_expected(None, big, dtype_quant="bf16") is True
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
    r2 = repo.create_run(
        "c-test/slug/dig2",
        campaign_id=seeded["campaign_id"],
        work_slug="slug",
        variant_digest="dig2",
        actor_id=seeded["actor_id"],
    )
    link = repo.link_replicate(original_run_id=seeded["run_id"], replicate_run_id=r2)
    assert repo.link_replicate(original_run_id=seeded["run_id"], replicate_run_id=r2) == link  # idempotent
    with pytest.raises(ValueError):  # the replicate_distinct CHECK, guarded for a clear error
        repo.link_replicate(original_run_id=seeded["run_id"], replicate_run_id=seeded["run_id"])


@pytest.mark.pg
def test_record_divergence_idempotent(seeded):
    repo = seeded["repo"]
    r2 = repo.create_run(
        "c-test/slug/dig2",
        campaign_id=seeded["campaign_id"],
        work_slug="slug",
        variant_digest="dig2",
        actor_id=seeded["actor_id"],
    )
    link = repo.link_replicate(original_run_id=seeded["run_id"], replicate_run_id=r2)
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
