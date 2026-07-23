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
from neuromancer_llm.db.lanes import ConfigurationError


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


def test_rt_tolerance_unmapped_dtype_fails_closed():
    """The TOLERANCE branch must NOT silently apply a default band to an unmapped/unknown dtype grade — an
    fp8/int8/typo grade gets NO band (a loud ConfigurationError), never the loosest one (5e-2, which would
    swallow a real divergence on a high-precision runtime). Fail closed, the D1 'no default-clean member'
    idiom applied to the tolerance table (the dtype_quant belt, log:245)."""
    s = _sample()
    div = compare(s, s)  # within any band: max_abs_diff=0.0, no argmax flip, no token-set change
    assert div.max_abs_diff == 0.0 and not div.argmax_flip and not div.token_set_changed
    # a MAPPED grade PASSES (0.0 within 5e-2) — proves tolerance_for is genuinely reached, not short-circuited
    assert assert_meets_expected(ExpectedLevel.TOLERANCE, div, dtype_quant="bf16") is True
    # an UNMAPPED grade fails closed instead of silently passing at the old default band
    with pytest.raises(ConfigurationError):
        assert_meets_expected(ExpectedLevel.TOLERANCE, div, dtype_quant="fp8")


def test_rt_compare_disjoint_topk_is_max_divergence():
    """C7: compare() must score a disjoint / low-overlap top-k as MAXIMUM divergence (not 0.0) — a fully
    disjoint distribution used to score max_abs_diff=0.0 (shared set empty) and pass tolerance."""
    s = _sample(base=1000)
    disjoint = _sample(base=5000)
    div = compare(s, disjoint)
    assert math.isinf(div.max_abs_diff)  # maximum divergence, never 0.0
    assert not div.bitwise_identical


# --- C7 fold-in (a): a disjoint top-k persists max_abs_diff=inf into PG, then raises ----------------
@pytest.mark.pg
def test_rt_disjoint_topk_persists_inf_before_raise(repo, tmp_path, rt_capture):
    """C7 fold-in (a): a disjoint top-k replicate scores compare().max_abs_diff=inf; replicate_and_measure
    records that divergence row (max_abs_diff=inf) BEFORE asserting the verdict, then raises
    DivergenceVerdictError on the bitwise lane. New end-to-end coverage of an already-correct path (no
    RED->GREEN required): inf round-trips into divergence_measurements.max_abs_diff (double precision) and is
    visible to a FRESH connection (record_divergence commits before the loud raise)."""
    from sqlalchemy import create_engine

    from neuromancer_llm.capture.events import replicate_and_measure
    from neuromancer_llm.composer import new_invocation_id

    original = rt_capture(repo, tmp_path, _sample(base=1000, generated=1000), invocation_id=None)
    replicate = rt_capture(
        repo, tmp_path, _sample(base=5000, generated=5000), invocation_id=new_invocation_id()
    )

    with pytest.raises(DivergenceVerdictError):  # disjoint top-k is not bitwise-identical on the bitwise lane
        replicate_and_measure(repo=repo, original=original, replicate=replicate, dtype_quant="bf16")

    fresh = create_engine(repo.engine.url, future=True)
    try:
        with fresh.connect() as conn:
            persisted = conn.execute(
                text("SELECT max_abs_diff FROM neuro.divergence_measurements")
            ).scalar_one()
    finally:
        fresh.dispose()
    assert math.isinf(persisted)  # the disjoint-top-k MAX divergence persisted as inf (never a silent 0.0)


# --- C4.3 (audit 2026-07-02): a wrongly-seeded EXPECTED level cannot be silently sticky -------------
@pytest.mark.pg
def test_rt_expected_rule_reseed_drift_raises(repo):
    """C4.3 (audit 2026-07-02): `seed_expected_rule` compared NOTHING on conflict — a wrongly-seeded
    EXPECTED level was silently sticky (ABSENCE is loud via FIX #2; wrongness was not). Seed BITWISE,
    re-seed the same (declared_mode, substrate_key) as TOLERANCE -> IdentityMismatchError; the stored
    rule is unchanged; a SAME-value re-seed stays idempotent."""
    from neuromancer_llm.db.repository import IdentityMismatchError

    rid = repo.seed_expected_rule(
        declared_mode="greedy", substrate_key="sm89/bi-on", expected=ExpectedLevel.BITWISE.value
    )
    # same-value re-seed: idempotent (returns the same rule)
    assert (
        repo.seed_expected_rule(
            declared_mode="greedy", substrate_key="sm89/bi-on", expected=ExpectedLevel.BITWISE.value
        )
        == rid
    )
    with pytest.raises(IdentityMismatchError):  # drift the level -> loud, never silently sticky
        repo.seed_expected_rule(
            declared_mode="greedy", substrate_key="sm89/bi-on", expected=ExpectedLevel.TOLERANCE.value
        )
    with repo.engine.connect() as conn:  # the stored rule is unchanged after the refused drift
        stored = conn.execute(
            text("SELECT expected FROM neuro.expected_reproducibility_rules WHERE rule_id = :r"),
            {"r": rid},
        ).scalar_one()
    assert stored == ExpectedLevel.BITWISE.value


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
        replicate_and_measure(repo=repo, original=original, replicate=replicate, dtype_quant="bf16")

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
        replicate_and_measure(repo=repo, original=original, replicate=replicate, dtype_quant="bf16")
    # the divergence WAS recorded before the refusal (the gap is visible data, not a silent pass)
    with repo.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM neuro.divergence_measurements")).scalar_one() == 1
