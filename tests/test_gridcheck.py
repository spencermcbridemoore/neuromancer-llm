"""The dtype grid-consistency guard (§D dtype-belt Layer 2; capture/gridcheck.py).

Layer 1 (`require_dtype_quant`) forces a caller to ASSERT a dtype grade; this Layer-2 leaf catches the ONE
reliably detectable WRONG-but-present grade: a declared full-depth `fp32` whose captured logprobs carry a
coarse quantization grid. The design is magnitude-free (over pairwise LOG-SOFTMAX differences) and honest about
its limit — bf16<->fp16 is not identifiable from log-softmax (softmax shift-invariance), so the guard abstains
there. Every acceptance criterion below has a FALSIFYING fixture (precedent 20). The synthetic grids are exact
powers of two (0.0625 = 2^-4 = the measured Unit-D bf16 grid; 0.0078125 = 2^-7 = fp16 @ [8,16)).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from neuromancer_llm.capture.campaign import run_campaign
from neuromancer_llm.capture.events import capture_logprob
from neuromancer_llm.capture.gridcheck import (
    GridClass,
    GridObservation,
    assert_grid_consistent,
    classify_grid,
    grid_class_for,
    grid_preflight_note,
)
from neuromancer_llm.db.lanes import ConfigurationError


# --- fixtures: logprob sequences on known difference-grids ---------------------------------------------
def _grid(step: float, n: int = 40) -> list[float]:
    """n descending logprobs whose pairwise differences are exact multiples of `step` (a clean grid)."""
    return [-step * i for i in range(n)]


def _continuous(n: int = 40) -> list[float]:
    """Descending logprobs on NO coarse dyadic grid (a stand-in for fp32/continuous): a non-dyadic sequence."""
    return sorted((-(i * 0.137 + (i * i % 11) * 0.0193 + i * 0.00061) for i in range(n)), reverse=True)


SUITE_FIXTURE = [-0.01 * (i + 1) for i in range(20)]  # the universal synthetic capture fixture (non-dyadic)
BF16 = _grid(0.0625)  # Unit-D's measured bf16 @ [8,16) grid
FP16 = _grid(0.0078125)  # fp16 @ [8,16) grid (8x finer)


# --- (d)/(g) grid_class_for: free-string safe, continuous is a closed set ------------------------------
def test_grid_class_for_continuous_is_a_closed_set() -> None:
    assert grid_class_for("fp32") is GridClass.CONTINUOUS
    assert grid_class_for("bf16") is GridClass.QUANTIZED
    assert grid_class_for("fp16") is GridClass.QUANTIZED
    # an UNMAPPED free-string grade reads QUANTIZED (no in-path action) — the log:245 free string is NOT narrowed
    assert grid_class_for("int8") is GridClass.QUANTIZED
    assert grid_class_for("bfloat16") is GridClass.QUANTIZED


# --- (a') declared fp32 + coarse-quantized data RAISES (the ONE hard-raise direction) ------------------
@pytest.mark.parametrize("grid", [BF16, FP16], ids=["bf16-grid", "fp16-grid"])
def test_declared_fp32_over_coarse_grid_raises(grid) -> None:
    with pytest.raises(ConfigurationError, match="demonstrably QUANTIZED"):
        assert_grid_consistent(grid, dtype_quant="fp32")


def test_declared_fp32_over_bf16_grid_names_the_step() -> None:
    with pytest.raises(ConfigurationError, match="0.0625"):
        assert_grid_consistent(BF16, dtype_quant="fp32")


# --- (b') declared fp32 + continuous data does NOT raise (INCONCLUSIVE; the benign abstain) ------------
def test_declared_fp32_over_continuous_does_not_raise() -> None:
    v = assert_grid_consistent(_continuous(), dtype_quant="fp32")  # no raise
    assert v.observed is GridObservation.INCONCLUSIVE


# --- (c) honest bf16 PASSES; ties excluded + counted --------------------------------------------------
def test_honest_bf16_passes_and_classifies_quantized() -> None:
    v = assert_grid_consistent(BF16, dtype_quant="bf16")  # no raise (declared quantized never raises)
    assert v.observed is GridObservation.QUANTIZED
    assert v.step == 0.0625


def test_ties_are_excluded_and_counted() -> None:
    # three copies of 0.0 and two of -0.0625 => C(3,2)+C(2,2)=3+1=4 tie pairs (|d|<TIE_EPS), excluded from the fit.
    logprobs = [0.0, 0.0, 0.0, -0.0625, -0.0625, *(-0.0625 * i for i in range(2, 30))]
    v = classify_grid(logprobs)
    assert v.n_ties == 4  # falsifies (f): dropping the tie filter makes n_ties == 0
    assert v.observed is GridObservation.QUANTIZED  # the non-tie pairs still resolve the 0.0625 grid


# --- (e)/(k) NO false-fire: absence of a coarse grid never raises, and the suite fixture is safe -------
@pytest.mark.parametrize("dtype", ["bf16", "fp16", "fp32"])
def test_suite_synthetic_fixture_never_raises(dtype) -> None:
    # the universal `[-0.01*(i+1)]` fixture (used across the pg suite) is non-dyadic -> INCONCLUSIVE, never a
    # raise for ANY declared grade: bf16/fp16 declared-quantized never raise; fp32 -> INCONCLUSIVE (not QUANTIZED).
    v = assert_grid_consistent(SUITE_FIXTURE, dtype_quant=dtype)
    assert v.observed is GridObservation.INCONCLUSIVE


@pytest.mark.parametrize("dtype", ["bf16", "fp16"])
def test_declared_quantized_never_raises_even_on_continuous_data(dtype) -> None:
    # the no-false-fire core: a declared-quantized grade is NEVER in the raise path, whatever the data looks like.
    assert_grid_consistent(_continuous(), dtype_quant=dtype)  # must not raise
    assert_grid_consistent(BF16, dtype_quant=dtype)  # must not raise


# --- (l) the top-M_TOP restriction is load-bearing ----------------------------------------------------
def test_top_m_restriction_keeps_the_head_grid_visible() -> None:
    # 32 head logprobs on the 0.0625 grid (least negative) + a deeper NON-dyadic tail (more negative, so the
    # top-M_TOP=32 head is analysed and the tail excluded). Over the head the guard sees QUANTIZED; folding the
    # tail in (M_TOP -> 64) would push >ALPHA of pairs off-grid -> INCONCLUSIVE, so a M_TOP mutation reddens this.
    head = [-0.0625 * i for i in range(32)]  # 0 .. -1.9375
    tail = [-2.0 - j * 0.00713 - (j % 5) * 0.00131 for j in range(32)]  # < -1.9375, non-dyadic
    v = classify_grid(head + tail)
    assert v.observed is GridObservation.QUANTIZED
    assert v.step == 0.0625
    assert v.n_pairs <= 32 * 31 // 2  # only the top-32 head pairs were analysed


# --- (j) the preflight note is informational: no verdict, no false 'looks like fp16', never raises -----
def test_preflight_note_on_honest_bf16_is_informational_not_a_verdict() -> None:
    note = grid_preflight_note(BF16, dtype_quant="bf16")
    assert "QUANTIZED" in note and "0.0625" in note
    assert (
        "bf16@binade" in note and "fp16@binade" in note
    )  # states the (dtype, binade) AMBIGUITY, not a verdict
    assert "not disambiguable" in note
    assert "looks like fp16" not in note  # never a spurious bf16<->fp16 verdict on honest data


def test_preflight_note_on_continuous_is_inconclusive() -> None:
    note = grid_preflight_note(_continuous(), dtype_quant="fp32")
    assert "INCONCLUSIVE" in note and "abstain" in note


# --- under-power: too few pairs => INCONCLUSIVE (the k<=5 projection case) ------------------------------
def test_few_logprobs_are_inconclusive() -> None:
    v = classify_grid([-0.5, -1.5, -2.5])  # 3 pairs < MIN_PAIRS
    assert v.observed is GridObservation.INCONCLUSIVE


# --- the resolution floor is the central anti-false-fire invariant — PIN it (precedent 20) -------------
def test_resolution_floor_pins_out_subfloor_grids() -> None:
    # A genuine sub-floor DYADIC grid (step 2^-11 < G_FLOOR=2^-10): honest low-binade quantized data whose ULP
    # sits below the floor MUST abstain (INCONCLUSIVE). The floor is what keeps honest fp32/low-binade data from
    # false-firing; lowering K_FLOOR/G_FLOOR to admit this grid re-introduces that false-fire, so this fixture
    # reddens a K_FLOOR/G_FLOOR mutation (the GO §8 floor pin, previously unenforced by any test).
    v = classify_grid(_grid(2**-11))
    assert v.observed is GridObservation.INCONCLUSIVE


# --- non-finite inputs fail SAFE (dropped -> abstain), never an OverflowError deep in the search --------
def test_non_finite_logprobs_fail_safe_never_crash() -> None:
    inf, ninf, nan = float("inf"), float("-inf"), float("nan")
    # +inf top-logprob: dropped; the 32 finite bf16-grid values still resolve -> QUANTIZED (no OverflowError).
    assert classify_grid([inf, *(-0.0625 * i for i in range(1, 33))]).observed is GridObservation.QUANTIZED
    # -inf padding with < M_TOP finite values: dropped -> degenerate -> INCONCLUSIVE (pre-fix -inf filled the
    # top-32 and finite-(-inf) diffs -> +inf -> crash).
    assert classify_grid([*(-0.0625 * i for i in range(5)), *([ninf] * 40)]).observed is (
        GridObservation.INCONCLUSIVE
    )
    # NaN is dropped, not silently bucketed as a tie (abs(nan) >= TIE_EPS is False).
    assert classify_grid([*(-0.0625 * i for i in range(30)), nan]).n_ties == 0
    # the in-path assert never crashes on a non-finite capture, even declared fp32.
    assert_grid_consistent([inf, nan, -0.5], dtype_quant="fp32")


# --- (h)/(i) AST wiring pins: the guard is actually INVOKED at both callsites --------------------------
def _body_invokes(fn, callee: str) -> bool:
    """True iff fn's body contains a Call to `callee` (AST — a docstring/comment mention does NOT count)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee
        for n in ast.walk(tree)
    )


def test_capture_logprob_invokes_the_guard() -> None:
    assert _body_invokes(capture_logprob, "assert_grid_consistent"), (
        "capture_logprob must invoke assert_grid_consistent(...) in-path (deleting it is a fail-open)"
    )


def test_run_campaign_invokes_the_guard_and_the_preflight_note() -> None:
    assert _body_invokes(run_campaign, "assert_grid_consistent"), (
        "run_campaign preflight must invoke assert_grid_consistent(...)"
    )
    assert _body_invokes(run_campaign, "grid_preflight_note"), (
        "run_campaign preflight must build grid_preflight_note(...)"
    )
