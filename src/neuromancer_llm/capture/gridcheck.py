"""dtype grid-consistency guard (§D dtype-belt Layer 2; the sibling of the Layer-1 `require_dtype_quant` belt).

The Layer-1 belt (`capture/events.py::require_dtype_quant`) forces a caller to ASSERT a runtime dtype grade
but cannot tell a VALID-BUT-WRONG grade (``"fp32"`` declared while the server actually ran bf16) from a correct
one. This leaf catches the ONE such lie that is reliably detectable, and is honest about the rest.

WHY only one direction is reliable (the identifiability wall — measured, not asserted).
  The capture stores LOG-SOFTMAX values (`--logprobs-mode raw_logprobs`), not raw logits, and softmax is
  shift-invariant (``log_softmax(x) == log_softmax(x + C)``), so the ABSOLUTE logit magnitude — hence the binade
  — is unrecoverable from the output. What survives is the pairwise-DIFFERENCE grid: ``pi - pj = xi - xj`` (the
  logsumexp cancels), an exact multiple of ``2**(e_min - m)`` for a dtype with ``m`` mantissa bits and the
  smaller-magnitude operand's binade ``e_min`` (bf16 m=7, fp16 m=10, fp32 m=23; the bf16-depth diagnostic,
  log:244). Consequences:
    * bf16 vs fp16 is NOT identifiable from log-softmax — an 8x-finer grid at one binade is observationally
      identical to a coarser grid three binades up. This guard does NOT attempt it; Layer 1 (a conscious grade
      assertion) plus a wave-2 re-capture's NEW model identity + NEW E6 cert are that axis's guarantees.
    * A COARSE quantization grid is POSITIVE evidence of quantization that continuous (fp32) logprobs essentially
      never produce (false-fire ~1e-15). Its mere ABSENCE is NOT evidence of fp32 — honest bf16/fp16 at low
      binade also lacks a coarse grid. So the only positive verdict this guard emits is ``QUANTIZED``; the
      absence of a grid is ``INCONCLUSIVE`` (abstain), never a "continuous" verdict.

WHAT it hard-raises on — EXACTLY ONE direction:
    declared ``dtype_quant`` is a CONTINUOUS grade (``fp32``) AND the captured top-``M_TOP`` logprobs carry a
    coarse quantization grid  =>  ``ConfigurationError`` (the runtime was demonstrably quantized, so a full-depth
    grade must not fold onto it — the D1-grade identity-integrity idiom, as `tolerance_for`/`require_dtype_quant`).
It ABSTAINS (no raise) on: declared-quantized + anything (the guard has no business raising on a quantized
declaration — bf16/fp16 both read QUANTIZED, and their sub-family is not identifiable); and declared-fp32 +
INCONCLUSIVE (a low-binade honest capture indistinguishable from fp32 — the benign under-claim direction).

Verdicts are magnitude-free over pairwise differences; ties (0-multiples, on every grid) are excluded and
counted; and the top-``M_TOP`` restriction keeps the operands high-binade so the coarse grid stays above the
resolution floor (a near-zero-logit tail would otherwise drag the finest common grid down to the fp32 scale).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..db.lanes import ConfigurationError

# --- pinned constants (derivations in scratch/dtype-gridcheck-buildgo-2026-07-26.md §3.3) ----------------
#: Analyse only the top-M_TOP LARGEST logprobs (= the highest-binade logits, monotone with the logits). This
#: excludes the near-zero-logit tail whose sub-floor ULP is indistinguishable from fp32; measured median
#: min|logit| ~2.9 (binade 1) at top-32 vs sub-floor at top-64 for a peaked capture, so 32 keeps the operands
#: high-binade (the guard stays ACTIVE) while a flat capture with no coarse grid abstains (INCONCLUSIVE).
M_TOP = 32
#: Membership tolerance as a fraction of the candidate step. Absolute slack TAU*g exceeds the ~1.2e-7 fp32
#: log-softmax slop for any g above ~1.2e-4; at G_FLOOR the slack is ~9.8e-7 (~8x the slop).
TAU = 1e-3
#: A pair with |d| below this is a TIE (0-multiple; sits on every grid) — excluded from the grid fit, counted
#: as QA. Above the fp32 slop (~1.2e-7), below the finest in-scope 1-ULP diff (top-M_TOP high-binade => >=~0.008).
TIE_EPS = 1e-5
#: Coarseness floor: the coarsest search stops here. ~8x above the reliably-testable floor (slop/TAU ~1.2e-4)
#: and ~1000x above the fp32-ULP scale, so a genuine coarse grid clears it and fp32's grid never does.
K_FLOOR = -10
G_FLOOR = 2.0**K_FLOOR
#: A candidate grid explains the data when at least (1 - ALPHA) of non-tie diffs are on it. Absorbs a small
#: minority off-grid tail.
ALPHA = 0.02
#: Fewer than this many non-tie pairs is under-powered => INCONCLUSIVE (no verdict).
MIN_PAIRS = 28
#: The CONTINUOUS/full-depth grades (the project's canonical spelling; cf. determinism.TOLERANCE_BY_DTYPE). Any
#: grade NOT in this closed set — bf16, fp16, an unmapped int8/awq/typo — reads QUANTIZED, so the guard takes NO
#: in-path action on it: the free-string dtype_quant ruling (log:245) is NOT narrowed, and the only fail mode is
#: a (nonexistent) "unknown continuous" grade being missed. Deliberately UNLIKE tolerance_for's fail-closed —
#: there an unmapped grade picks a WRONG band (a hazard); here it picks NO action (safe).
CONTINUOUS_DTYPES = frozenset({"fp32"})


class GridClass(StrEnum):
    """The quantization CLASS a declared dtype grade implies."""

    QUANTIZED = "quantized"
    CONTINUOUS = "continuous"


class GridObservation(StrEnum):
    """What the captured logprobs' difference-grid shows. There is NO 'continuous' observation — the absence of
    a coarse grid is INCONCLUSIVE (it cannot be told from honest low-binade quantized data), never a verdict."""

    QUANTIZED = "quantized"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class GridVerdict:
    """The magnitude-free classification of one capture's top-logprob difference-grid."""

    observed: GridObservation
    step: float | None  # the coarse grid step (nats) when QUANTIZED, else None
    on_grid_fraction: float | None  # fraction of non-tie pairs on that step, else None
    n_pairs: int  # non-tie pairs analysed
    n_ties: int  # excluded 0-multiple pairs (QA)


def grid_class_for(dtype_quant: str) -> GridClass:
    """The quantization class a declared grade implies: CONTINUOUS iff it is a known full-depth grade, else
    QUANTIZED. Free-string safe (an unmapped grade reads QUANTIZED => the guard takes no in-path action)."""
    return GridClass.CONTINUOUS if dtype_quant in CONTINUOUS_DTYPES else GridClass.QUANTIZED


def _coarsest_grid(nonzero_diffs: list[float]) -> tuple[float | None, float]:
    """The LARGEST dyadic step g in [G_FLOOR, max|d|] on which >= (1 - ALPHA) of the diffs lie (within TAU of a
    multiple), searched coarse->fine. Returns (g, fraction) or (None, best_fraction_seen) if none qualifies."""
    peak = max(abs(d) for d in nonzero_diffs)
    if peak < G_FLOOR:  # every diff finer than the floor — nothing resolvable above it
        return None, 0.0
    k_hi = math.floor(math.log2(peak))
    best_frac = 0.0
    for k in range(k_hi, K_FLOOR - 1, -1):  # coarsest first: return the first (largest) g that qualifies
        g = 2.0**k
        frac = sum(1 for d in nonzero_diffs if abs(d / g - round(d / g)) < TAU) / len(nonzero_diffs)
        best_frac = max(best_frac, frac)
        if frac >= 1.0 - ALPHA:
            return g, frac
    return None, best_frac


def classify_grid(logprobs: Sequence[float]) -> GridVerdict:
    """Classify the difference-grid of a capture's top logprobs, magnitude-free (QUANTIZED or INCONCLUSIVE).

    Restricts to the top-M_TOP LARGEST FINITE logprobs (highest-binade operands), forms all pairwise
    differences, excludes+counts ties (0-multiples), and looks for a coarse dyadic grid >= G_FLOOR that
    (1 - ALPHA) of the non-tie diffs lie on. A grid found => QUANTIZED (positive evidence); none, or too few
    pairs => INCONCLUSIVE (abstain — the absence of a coarse grid is NOT evidence of fp32; honest low-binade
    data looks the same). Non-finite entries (a malformed +/-inf or NaN top-logprob) are dropped at the source
    so the guard fails SAFE (a degenerate all-non-finite input abstains via MIN_PAIRS) rather than raising an
    OverflowError deep in the grid search — the fail-closed/fail-safe contract this module commits to."""
    top = sorted((x for x in logprobs if math.isfinite(x)), reverse=True)[:M_TOP]
    diffs = [top[i] - top[j] for i in range(len(top)) for j in range(i + 1, len(top))]
    nonzero = [d for d in diffs if abs(d) >= TIE_EPS]
    n_ties = len(diffs) - len(nonzero)
    if len(nonzero) < MIN_PAIRS:
        return GridVerdict(GridObservation.INCONCLUSIVE, None, None, len(nonzero), n_ties)
    g, frac = _coarsest_grid(nonzero)
    if g is not None:
        return GridVerdict(GridObservation.QUANTIZED, g, frac, len(nonzero), n_ties)
    return GridVerdict(GridObservation.INCONCLUSIVE, None, None, len(nonzero), n_ties)


def assert_grid_consistent(logprobs: Sequence[float], *, dtype_quant: str) -> GridVerdict:
    """Fail-closed on the ONE reliable dtype lie: a declared CONTINUOUS grade (fp32) whose captured logprobs
    carry a coarse quantization grid (the runtime was demonstrably quantized). Every other combination — a
    quantized declaration, or an fp32 declaration over INCONCLUSIVE data — is left alone (see the module
    docstring for why the reverse directions are not reliably detectable from log-softmax). Returns the verdict.

    This runs at every capture (in-path, above the durable write) but only ACTS when dtype_quant is a continuous
    grade, so honest bf16/fp16 captures — all of wave-1 — never enter the raise path."""
    verdict = classify_grid(logprobs)
    if grid_class_for(dtype_quant) is GridClass.CONTINUOUS and verdict.observed is GridObservation.QUANTIZED:
        raise ConfigurationError(
            f"declared dtype_quant={dtype_quant!r} claims full-depth/continuous logprobs, but the captured "
            f"top-{M_TOP} logprobs carry a coarse {verdict.step:g}-nat quantization grid "
            f"({verdict.on_grid_fraction:.1%} of {verdict.n_pairs} non-tie pairs on-grid, {verdict.n_ties} "
            f"ties) — the runtime dtype is demonstrably QUANTIZED, not {dtype_quant!r}; refusing to fold a "
            "full-depth grade onto quantized data (dtype grid-consistency guard; bf16-depth, log:244)."
        )
    return verdict


def grid_preflight_note(logprobs: Sequence[float], *, dtype_quant: str) -> str:
    """An INFORMATIONAL preflight line for the runbook — never a verdict, never a false warning.

    Reports the observed grid and (because bf16<->fp16 is not disambiguable from log-softmax) the (dtype, binade)
    ambiguity of the observed step, so an operator can eyeball it against the runtime they intended. A
    prior-anchored bf16<->fp16 WARNING would need a live-server-measured top-logit binade band and is a
    registered follow-on; this note deliberately makes no such claim."""
    verdict = classify_grid(logprobs)
    if verdict.observed is GridObservation.QUANTIZED and verdict.step is not None:
        e = round(math.log2(verdict.step))
        return (
            f"grid preflight: observed QUANTIZED step={verdict.step:g} nats over {verdict.n_pairs} non-tie "
            f"pairs ({verdict.n_ties} ties); consistent with bf16@binade~{e + 7}, fp16@binade~{e + 10} — "
            f"bf16<->fp16 not disambiguable from log-softmax (declared={dtype_quant!r}; Layer-1 + a new E6 "
            "cert are the runtime-dtype identity guarantees)."
        )
    return (
        f"grid preflight: INCONCLUSIVE ({verdict.n_pairs} non-tie pairs, {verdict.n_ties} ties) — no coarse "
        f"grid resolvable above the floor; declared={dtype_quant!r} not contradicted (abstain)."
    )
