"""Red-team: no silent failure in the determinism close-the-loop (L8) + FIX #2 + C7.

* FIX #2 — a no-APPLICABLE-rule resolution (Python None) is a TYPED refusal (UnassessedExpectationError),
  split from the DELIBERATE ExpectedLevel.NONE (which still passes). NOT an env flag.
* C7 — the TOLERANCE branch ALSO fails on argmax_flip / token_set_changed, and compare() treats a
  disjoint / low-overlap top-k as MAXIMUM divergence (not 0.0) — closing the "answer flipped but passed
  tolerance" fail-open.
* L8 — persist-before-raise on a SEVERE divergence (argmax flip) and durability across a fresh connection.
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy import text

from neuromancer_llm.capture.adapters.vllm import LogprobSample
from neuromancer_llm.capture.determinism import (
    DivergenceVerdictError,
    ExpectedLevel,
    UnassessedExpectationError,
    assert_meets_expected,
    compare,
)


def _sample(n=20, generated=1000, base=1000):
    return LogprobSample(
        prompt="p",
        generated_token_id=generated,
        top_logprobs=tuple((base + i, -0.01 * (i + 1)) for i in range(n)),
    )


# --- FIX #2: no-rule is a TYPED refusal, split from the deliberate NONE -----------------------------
def test_rt_no_rule_is_typed_refusal_not_silent_pass():
    """FIX #2: resolve_expected_level returns Python None when NO rule applies — assert_meets_expected must
    NOT silently return True for that; it raises UnassessedExpectationError (a typed refusal). The DELIBERATE
    ExpectedLevel.NONE is a different thing and still passes (the split)."""
    s = _sample()
    div = compare(s, _sample(generated=1001))  # any divergence
    with pytest.raises(UnassessedExpectationError):
        assert_meets_expected(None, div, dtype_quant="bf16")
    # deliberate NONE / DISTRIBUTIONAL are still no-point-assertion passes (the split is honored)
    assert assert_meets_expected(ExpectedLevel.NONE, div, dtype_quant="bf16") is True
    assert assert_meets_expected(ExpectedLevel.DISTRIBUTIONAL, div, dtype_quant="bf16") is True


# --- C7: the TOLERANCE branch is not fail-open ------------------------------------------------------
def test_rt_tolerance_fails_on_argmax_flip():
    """C7: an MCQ answer-flip with a SUB-tolerance logprob delta used to PASS tolerance (the exact 'answer
    flipped' signal the system exists to catch). The TOLERANCE branch must also fail on argmax_flip."""
    s = _sample(generated=1000)
    flipped = _sample(generated=1001)  # same candidate set + logprobs, DIFFERENT generated token
    div = compare(s, flipped)
    assert div.argmax_flip and not div.token_set_changed and div.max_abs_diff == 0.0
    with pytest.raises(DivergenceVerdictError):
        assert_meets_expected(ExpectedLevel.TOLERANCE, div, dtype_quant="bf16")


def test_rt_tolerance_fails_on_token_set_changed():
    """C7: a changed top-k membership is a real divergence even if the (empty) shared overlap scores 0.0."""
    s = _sample(base=1000)
    other = _sample(base=5000)  # fully DISJOINT candidate set
    div = compare(s, other)
    assert div.token_set_changed
    with pytest.raises(DivergenceVerdictError):
        assert_meets_expected(ExpectedLevel.TOLERANCE, div, dtype_quant="bf16")


def test_rt_compare_disjoint_topk_is_max_divergence():
    """C7: compare() must score a disjoint / low-overlap top-k as MAXIMUM divergence (not 0.0) — a fully
    disjoint distribution used to score max_abs_diff=0.0 (shared set empty) and pass tolerance."""
    s = _sample(base=1000)
    disjoint = _sample(base=5000)
    div = compare(s, disjoint)
    assert math.isinf(div.max_abs_diff)  # maximum divergence, never 0.0
    assert not div.bitwise_identical


# --- L8: persist-before-raise on a SEVERE (argmax-flip) divergence ----------------------------------
@pytest.mark.pg
def test_rt_argmax_flip_persists_before_raise(repo, tmp_path, rt_capture):
    """L8 (sharpens the divergent-bitwise probe that kept argmax stable): an argmax FLIP on a bitwise lane
    raises DivergenceVerdictError AND the divergence row (argmax_flip_rate==1.0) is persisted BEFORE the
    raise, visible to a FRESH connection (record_divergence commits in its own txn)."""
    from neuromancer_llm.capture.events import replicate_and_measure
    from neuromancer_llm.composer import new_invocation_id

    s = _sample(generated=1000)  # argmax = token 1000 (logprob -0.01)
    # a REALISTIC argmax flip: token 1001 is now the peak (its logprob is highest), same candidate set —
    # the served distribution genuinely changed, so the argmax flips to 1001.
    flipped_pairs = ((1000, -0.5), (1001, -0.001)) + tuple((1000 + i, -0.01 * (i + 1)) for i in range(2, 20))
    flipped = LogprobSample(prompt="p", generated_token_id=1001, top_logprobs=flipped_pairs)
    original = rt_capture(repo, tmp_path, s, invocation_id=None)
    # the replicate must share the SAME fingerprint -> same inputs, only the served distribution differs
    replicate = rt_capture(repo, tmp_path, flipped, invocation_id=new_invocation_id())

    with pytest.raises(DivergenceVerdictError):
        replicate_and_measure(repo=repo, original=original, replicate=replicate)

    # the divergence row was persisted BEFORE the raise, and is visible to a FRESH engine/connection
    from sqlalchemy import create_engine

    fresh = create_engine(repo.engine.url, future=True)
    try:
        with fresh.connect() as conn:
            row = (
                conn.execute(text("SELECT argmax_flip_rate FROM neuro.divergence_measurements"))
                .mappings()
                .one()
            )
    finally:
        fresh.dispose()
    assert row["argmax_flip_rate"] == 1.0  # the SEVERE signal is data, persisted before the loud raise


# --- FIX #2: an unassessed measure records the divergence THEN refuses (persist-before-raise) -------
@pytest.mark.pg
def test_rt_unassessed_measure_records_then_refuses(repo, tmp_path, rt_capture):
    """FIX #2 end-to-end: a measure over a substrate whose rule was never seeded records the divergence row
    (visible data) and then raises UnassessedExpectationError — never a silent meets_expected=True."""
    from neuromancer_llm.capture.events import replicate_and_measure
    from neuromancer_llm.composer import new_invocation_id

    s = _sample()
    original = rt_capture(repo, tmp_path, s, invocation_id=None)
    replicate = rt_capture(repo, tmp_path, s, invocation_id=new_invocation_id())
    # remove the seeded rule so resolve_expected_level returns None (no APPLICABLE rule)
    with repo.engine.begin() as conn:
        conn.execute(text("DELETE FROM neuro.expected_reproducibility_rules"))

    with pytest.raises(UnassessedExpectationError):
        replicate_and_measure(repo=repo, original=original, replicate=replicate)
    # the divergence WAS recorded before the refusal (the gap is visible data, not a silent pass)
    with repo.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM neuro.divergence_measurements")).scalar_one() == 1
