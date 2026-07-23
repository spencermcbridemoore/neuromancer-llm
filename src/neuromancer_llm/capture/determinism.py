"""Determinism: declared / expected / measured (ADR-0004) + the E6 bring-up test harness. STAGE 2.

DECLARED mode is derived from the captured wire payload (participates in the fingerprint); EXPECTED is
the heuristic table (never identity, overridable per-run); MEASURED is replicate divergence. The E6 gate
(N=50 logprob-array exact-match under VLLM_BATCH_INVARIANT=1 on the 4090) is a mandatory, decision-bearing
Phase-4 bring-up step — it decides the bitwise-vs-tolerance canonical default. NOT settled from memory.

E6 verdict-only: this module measures and reports. It performs NO DB write — seeding
`expected_reproducibility_rules`, setting the determinism default, and wiring the batch-invariant flag
into the substrate fingerprint are the supervisor/owner's POST-gate steps. The schema is unchanged
either branch (both seated, ADR-0004); E6 produces a runtime verdict, never a migration.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import Engine, text

from neuromancer_llm.capture.adapters.vllm import LogprobSample, VLLMClient
from neuromancer_llm.db.lanes import ConfigurationError

# E6 hardware gate (capture contract §6): batch invariance requires compute capability >= 8.0. The
# 4090 is sm89 (8.9), so the gate passes here and the test skips visibly on lesser hardware.
MIN_COMPUTE_CAPABILITY = 8.0


class E6Error(RuntimeError):
    """Loud, typed E6 failure (no silent failure, per the binding constraints)."""


class E6HardwareError(E6Error):
    """The hardware does not clear the E6 gate (compute capability < 8.0, or undetectable)."""


# --- the three-layer determinism model (ADR-0004) --------------------------------------------------
class DeclaredMode(StrEnum):
    """DECLARED mode — derived from the captured wire payload; participates in the fingerprint.

    Values match the DDL `declared_mode` enum exactly (tests/reference/phase3-ddl.sql).
    """

    DETERMINISTIC_ALGO = "deterministic_algo"
    GREEDY = "greedy"
    SEEDED_SAMPLING = "seeded_sampling"
    UNSEEDED_SAMPLING = "unseeded_sampling"


class ExpectedLevel(StrEnum):
    """EXPECTED reproducibility level — the heuristic table's range; NEVER touches identity.

    Values match the DDL `expected_level` enum exactly.
    """

    BITWISE = "bitwise"
    TOLERANCE = "tolerance"
    DISTRIBUTIONAL = "distributional"
    NONE = "none"


def declared_mode_from_request(*, temperature: float, seed: int | None) -> DeclaredMode:
    """DECLARED is read off the request (what was asked for): temperature==0 is greedy; with a
    temperature a present seed is seeded_sampling, an absent one unseeded_sampling. E6 runs GREEDY.

    (`deterministic_algo` is reserved for an explicit deterministic-kernel request and is not produced
    from a plain temperature/seed pair.)
    """
    if temperature == 0:
        return DeclaredMode.GREEDY
    return DeclaredMode.SEEDED_SAMPLING if seed is not None else DeclaredMode.UNSEEDED_SAMPLING


def host_compute_capability() -> float:
    """Read the host GPU compute capability via nvidia-smi (the E6 gate, CC >= 8.0). Fail loud."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise E6HardwareError(f"could not read compute capability via nvidia-smi: {exc}") from exc
    first = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    try:
        return float(first)
    except ValueError as exc:
        raise E6HardwareError(f"unparseable compute_cap from nvidia-smi: {first!r}") from exc


def substrate_key(compute_capability: float, *, batch_invariant: bool) -> str:
    """The substrate key a PASS would seed, e.g. 'vllm-bi-on@sm89'. Report-only (never written here)."""
    sm = f"sm{int(round(compute_capability * 10))}"
    return f"vllm-bi-{'on' if batch_invariant else 'off'}@{sm}"


def resolve_expected_level(
    engine: Engine, *, declared_mode: str, substrate_key: str, override: str | None = None
) -> ExpectedLevel | None:
    """Resolve a run's EXPECTED reproducibility level (ADR-0004): the per-run override wins; else the
    heuristic rule for (declared_mode, substrate_key); else None (no rule — the caller records the gap).

    This is the ASSESSMENT layer — read at run time, NEVER identity, overridable. The capture lane reads
    it after seeding the E6 rule; for the owned greedy lane on sm89 it resolves to 'bitwise'."""
    if override is not None:
        return ExpectedLevel(override)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT expected FROM neuro.expected_reproducibility_rules "
                "WHERE declared_mode = :m AND substrate_key = :s"
            ),
            {"m": str(declared_mode), "s": substrate_key},
        ).scalar_one_or_none()
    return ExpectedLevel(row) if row is not None else None


# --- MEASURED layer: divergence between two id-keyed distributions ---------------------------------
@dataclass(frozen=True)
class Divergence:
    """Divergence between a reference distribution and another (ADR-0004 MEASURED metrics).

    `bitwise_identical` is decided on IEEE-754 bytes (the E6 PASS criterion). The diff magnitudes are
    over the shared token ids; a changed top-k membership is flagged separately (and already makes the
    pair non-identical).
    """

    max_abs_diff: float
    max_rel_diff: float
    argmax_flip: bool
    token_set_changed: bool
    bitwise_identical: bool


def compare(reference: LogprobSample, other: LogprobSample) -> Divergence:
    ref = reference.logprob_map
    oth = other.logprob_map
    token_set_changed = reference.token_ids != other.token_ids
    if token_set_changed:
        # C7: a changed / disjoint top-k membership means at least one candidate's logprob is INCOMPARABLE
        # (present in one distribution, absent in the other) — that is MAXIMUM divergence, not 0.0 over the
        # (possibly empty) shared overlap. The fail-open this closes: a fully-disjoint top-k scored
        # max_abs_diff=0.0 and slipped through the TOLERANCE band.
        max_abs = float("inf")
        max_rel = float("inf")
    else:
        max_abs = 0.0
        max_rel = 0.0
        for tid in ref.keys() & oth.keys():
            diff = abs(ref[tid] - oth[tid])
            max_abs = max(max_abs, diff)
            denom = abs(ref[tid])
            if denom > 0.0:
                max_rel = max(max_rel, diff / denom)
    return Divergence(
        max_abs_diff=max_abs,
        max_rel_diff=max_rel,
        argmax_flip=reference.generated_token_id != other.generated_token_id,
        token_set_changed=token_set_changed,
        bitwise_identical=reference.signature() == other.signature(),
    )


# --- MEASURED layer: the registered divergence method + the EXPECTED close-the-loop assertion -------
# The divergence method is REGISTERED before any divergence_measurements row references it (register-first,
# ADR-0011). Its code_sha is the content hash of the compare() implementation, so changing compare() WITHOUT
# bumping the semver is caught as registry/runtime drift at register-time.
DIVERGENCE_METHOD_KEY = "logprob_divergence"
DIVERGENCE_METHOD_SEMVER = "1.0.0"

# Per-dtype absolute-logprob tolerance for the TOLERANCE branch (nats). HEURISTIC placeholders for substrates
# whose own E6 has NOT certified bitwise — refined per substrate, never identity (ADR-0004). The owned
# vllm-bi-on@sm89 lane is BITWISE (E6 2026-06-20), so this branch is not exercised there. An UNMAPPED grade
# FAILS CLOSED in tolerance_for (no default band — a wrong grade must not inherit the loosest one; log:246).
TOLERANCE_BY_DTYPE = {"bf16": 5e-2, "fp16": 5e-2, "fp32": 1e-3}


class DivergenceVerdictError(RuntimeError):
    """MEASURED divergence VIOLATES the EXPECTED reproducibility level: the substrate drifted from its E6
    verdict (e.g. a non-zero divergence on a bitwise lane). Loud, never a silent pass (ADR-0004)."""


class UnassessedExpectationError(RuntimeError):
    """FIX #2: there is NO applicable EXPECTED reproducibility rule for this (declared_mode, substrate_key)
    — `resolve_expected_level` returned Python None (no rule), which is DISTINCT from the deliberate
    ExpectedLevel.NONE. A measure must NOT silently pass when its lane was never assessed; this is a typed
    refusal (the divergence row is persisted first, so the unassessed gap is visible data). Seed an
    expected_reproducibility_rule for the substrate, or set it deliberately to ExpectedLevel.NONE."""


def divergence_method_code_sha() -> bytes:
    """Content hash of the divergence implementation (compare's source). The registered method_version's
    code_sha — a change to compare() without a semver bump raises at register-time (ADR-0011 parity)."""
    import inspect

    from ..db.identity import sha256_bytes

    return sha256_bytes(inspect.getsource(compare).encode("utf-8"))


def near_tie_margin_nats(sample: LogprobSample) -> float | None:
    """The top-1 vs top-2 logprob gap (nats) of a distribution — how close the argmax is to flipping. A
    small margin means a tiny perturbation could change the answer (first-class MCQ fragility data, ADR-0004).
    None when the distribution has fewer than two candidates (no tie to measure)."""
    lps = sorted((lp for _, lp in sample.top_logprobs), reverse=True)
    if len(lps) < 2:
        return None
    return lps[0] - lps[1]


def measure_divergence(reference: LogprobSample, other: LogprobSample) -> dict[str, float | None]:
    """Map compare() onto the divergence_measurements columns (ADR-0004). A single replicate pair, so the
    argmax-flip 'rate' is 0/1. answer_letter_flip_rate is NULL here: answer-letter extraction is a separate
    versioned derivation method, and the many-item MCQ position-bias measure is a later gate."""
    d = compare(reference, other)
    return {
        "max_abs_diff": d.max_abs_diff,
        "max_rel_diff": d.max_rel_diff,
        "argmax_flip_rate": 1.0 if d.argmax_flip else 0.0,
        "answer_letter_flip_rate": None,
        "near_tie_margin_nats": near_tie_margin_nats(reference),
    }


def tolerance_for(dtype_quant: str) -> float:
    """The per-dtype TOLERANCE-branch band (nats). FAIL CLOSED on an unmapped grade: an unknown/typo dtype
    (fp8/int8/'bfloat16') must NOT silently inherit a DEFAULT band — the loosest one (5e-2, 50x looser than
    fp32) is exactly wrong for a high-precision runtime and would swallow a real divergence. The D1
    "no default-clean member" idiom (the dtype_quant belt, log:245) applied to the tolerance table."""
    try:
        return TOLERANCE_BY_DTYPE[dtype_quant]
    except KeyError:
        raise ConfigurationError(
            f"no TOLERANCE band registered for dtype_quant={dtype_quant!r} "
            f"(known: {sorted(TOLERANCE_BY_DTYPE)}) — refusing to apply a default band to an unmapped grade "
            "(fail closed; a loose default could swallow a real divergence on a high-precision runtime)."
        ) from None


def assert_meets_expected(
    expected: ExpectedLevel | None, divergence: Divergence, *, dtype_quant: str
) -> bool:
    """Close the loop the E6 gate opened: a run pair's MEASURED divergence MUST satisfy its EXPECTED level
    or this raises DivergenceVerdictError (ADR-0004; no silent failure). Returns True when it holds.

      * BITWISE      — ANY divergence is a violation (the E6-certified lane must reproduce bit-for-bit).
      * TOLERANCE    — max_abs_diff within the per-dtype tolerance AND no argmax/top-k-set change (C7).
      * DISTRIBUTIONAL / NONE — no point assertion at this grain (out of scope this gate); passes.
      * None (no rule) — FIX #2: a typed refusal (UnassessedExpectationError), NOT a silent pass — split
        from the deliberate ExpectedLevel.NONE above.
    """
    if expected is None:
        # FIX #2: no APPLICABLE rule for this (declared_mode, substrate_key). This is DISTINCT from the
        # deliberate ExpectedLevel.NONE — refuse loud rather than silently pass an unassessed lane.
        raise UnassessedExpectationError(
            "no applicable EXPECTED reproducibility rule for this (declared_mode, substrate_key) — refusing "
            "to pass an UNASSESSED lane silently (FIX #2). Seed an expected_reproducibility_rule, or set the "
            "rule deliberately to ExpectedLevel.NONE."
        )
    if expected in (ExpectedLevel.DISTRIBUTIONAL, ExpectedLevel.NONE):
        return True
    if expected == ExpectedLevel.BITWISE:
        if not divergence.bitwise_identical:
            raise DivergenceVerdictError(
                f"EXPECTED=bitwise but MEASURED diverged (max_abs_diff={divergence.max_abs_diff:.3e} nats, "
                f"argmax_flip={divergence.argmax_flip}, token_set_changed={divergence.token_set_changed}): the "
                "substrate drifted from its E6 bitwise verdict — refusing to pass silently (ADR-0004)."
            )
        return True
    # TOLERANCE
    if divergence.argmax_flip or divergence.token_set_changed:
        # C7: the argmax flipping or the top-k membership changing is the exact "answer changed" signal the
        # system exists to catch — the tolerance band must NEVER swallow it, regardless of the logprob delta.
        raise DivergenceVerdictError(
            f"EXPECTED=tolerance but the answer changed (argmax_flip={divergence.argmax_flip}, "
            f"token_set_changed={divergence.token_set_changed}) — a flipped argmax / changed top-k is a "
            "real divergence the tolerance band must not swallow; refusing to pass silently (C7)."
        )
    tol = tolerance_for(dtype_quant)
    if divergence.max_abs_diff > tol:
        raise DivergenceVerdictError(
            f"EXPECTED=tolerance but MEASURED max_abs_diff={divergence.max_abs_diff:.3e} nats exceeds the "
            f"{dtype_quant} tolerance {tol:.3e} — refusing to pass silently (ADR-0004)."
        )
    return True


# --- E6 protocol ----------------------------------------------------------------------------------
# A representative MCQ-shaped target: greedy next token over a peaked distribution (the logprob/MCQ
# workload). The specific prompt does not affect the verdict — only whether logprobs are batch-stable.
DEFAULT_TARGET_PROMPT = (
    "Question: What is the capital of France?\n"
    "A) Berlin\nB) Madrid\nC) Paris\nD) Rome\nAnswer with a single letter:"
)

# Varied-length fillers — fired only to perturb the forward-batch shape (token dimension + count).
# Pool is >= max(batch_size)-1 distinct so a batch of 16 never repeats a filler.
DEFAULT_FILLER_POOL: tuple[str, ...] = (
    "Hello.",
    "The quick brown fox jumps over the lazy dog.",
    "Summarize the theory of relativity in one sentence.",
    "1 + 1 =",
    "Translate 'good morning' into Spanish.",
    "Once upon a time, in a distant kingdom by the sea, there lived a clockmaker.",
    "List three primary colors.",
    "Why is the sky blue? Explain briefly for a curious child who keeps asking questions.",
    "def add(a, b):",
    "The mitochondria is the powerhouse of the",
    "Roses are red, violets are blue, this is a filler prompt meant only to occupy the batch.",
    "What year did the Apollo 11 mission land on the Moon?",
    "Compose a haiku about autumn leaves drifting slowly down onto a quiet pond at dusk.",
    "SELECT * FROM users WHERE",
    "The capital of Japan is",
    "Explain photosynthesis.",
    "A B C D E F G H I J K L M N O P Q R S T U V W X Y Z and then back again to the start.",
    "Pi is approximately equal to",
    "Name a programming language that runs on the JVM.",
    "In the beginning the universe was created. This has made a lot of people very angry.",
    "Two plus two equals",
    "Describe the water cycle from evaporation through condensation to precipitation and back.",
    "The chemical symbol for gold is",
    "Write a friendly one-line greeting for a new teammate joining the project today.",
)


@dataclass(frozen=True)
class E6Config:
    """E6 run configuration. Defaults encode the capture-contract §6 protocol (N=50, varied batches)."""

    model: str
    target_prompt: str = DEFAULT_TARGET_PROMPT
    n_replicates: int = 50
    batch_sizes: tuple[int, ...] = (1, 2, 4, 8, 16)
    n_logprobs: int = 20
    seed: int = 1234
    filler_pool: tuple[str, ...] = DEFAULT_FILLER_POOL
    ready_deadline_s: float = 600.0


@dataclass
class E6Result:
    """The E6 verdict for one arm. Report-only — nothing here is written to the DB."""

    arm: str  # "test" (VLLM_BATCH_INVARIANT=1) or "control" (flag unset)
    passed: bool  # True iff every replicate is bitwise-identical to the reference
    n_samples: int
    batch_sizes: tuple[int, ...]
    restart_count: int
    distinct_signatures: int
    divergence_appeared: bool
    max_abs_diff: float
    max_rel_diff: float
    argmax_flips: int
    first_divergence: dict | None
    declared_mode: DeclaredMode
    compute_capability: float
    batch_invariant: bool
    proposed_rule: dict | None  # what a PASS WOULD seed — REPORT ONLY, never written
    samples: list[LogprobSample] = field(default_factory=list)


def _pick_fillers(pool: tuple[str, ...], k: int, replicate_index: int) -> list[str]:
    """k fillers, cycling by replicate so batch composition varies across replicates of the same size."""
    if k <= 0:
        return []
    n = len(pool)
    return [pool[(replicate_index + j) % n] for j in range(k)]


def run_e6(
    client: VLLMClient,
    config: E6Config,
    *,
    batch_invariant: bool,
    restart_engine: Callable[[], None] | None = None,
    restart_after_fraction: float = 0.5,
    compute_capability: float | None = None,
) -> E6Result:
    """Run one E6 arm: N replicate next-token logprob passes across varied batch sizes, optionally
    crossing one engine restart, then exact-match on the logprob arrays.

    `batch_invariant` records which arm this is (the caller sets VLLM_BATCH_INVARIANT on the *server*;
    this flag is metadata for the report + the proposed rule). `restart_engine`, when given, is invoked
    once partway through (e.g. `docker restart`) and readiness re-confirmed — satisfying the
    "at least one engine restart" clause. Raises E6HardwareError if CC < 8.0 (the gate).
    """
    cc = compute_capability if compute_capability is not None else host_compute_capability()
    if cc < MIN_COMPUTE_CAPABILITY:
        raise E6HardwareError(
            f"compute capability {cc} < {MIN_COMPUTE_CAPABILITY}: E6 (batch invariance) is not "
            "supported on this hardware."
        )
    declared = declared_mode_from_request(temperature=0, seed=config.seed)  # E6 is greedy

    client.wait_until_ready(deadline_s=config.ready_deadline_s)

    restart_at = int(config.n_replicates * restart_after_fraction) if restart_engine is not None else None
    restart_count = 0
    samples: list[LogprobSample] = []
    for i in range(config.n_replicates):
        if restart_at is not None and i == restart_at:
            restart_engine()  # type: ignore[misc]  (guarded by restart_at not None => callable set)
            client.wait_until_ready(deadline_s=config.ready_deadline_s)
            restart_count += 1
        bs = config.batch_sizes[i % len(config.batch_sizes)]
        phase = "post-restart" if (restart_at is not None and i >= restart_at) else "pre-restart"
        sample = client.batched_next_token_logprobs(
            config.target_prompt,
            model=config.model,
            n_logprobs=config.n_logprobs,
            seed=config.seed,
            filler_prompts=_pick_fillers(config.filler_pool, bs - 1, i),
            phase=phase,
            replicate_index=i,
        )
        samples.append(sample)

    if not samples:
        raise E6Error("E6 produced no samples (n_replicates must be >= 1)")

    reference = samples[0]
    distinct: set[tuple[tuple[int, bytes], ...]] = {reference.signature()}
    max_abs = 0.0
    max_rel = 0.0
    argmax_flips = 0
    first_divergence: dict | None = None
    for s in samples[1:]:
        d = compare(reference, s)
        distinct.add(s.signature())
        max_abs = max(max_abs, d.max_abs_diff)
        max_rel = max(max_rel, d.max_rel_diff)
        if d.argmax_flip:
            argmax_flips += 1
        if not d.bitwise_identical and first_divergence is None:
            first_divergence = {
                "replicate_index": s.replicate_index,
                "batch_size": s.batch_size,
                "phase": s.phase,
                "max_abs_diff": d.max_abs_diff,
                "max_rel_diff": d.max_rel_diff,
                "argmax_flip": d.argmax_flip,
                "token_set_changed": d.token_set_changed,
            }

    divergence_appeared = len(distinct) > 1
    passed = not divergence_appeared  # bitwise PASS = exactly one signature across all replicates

    proposed_rule = None
    if batch_invariant and passed:
        # The heuristic-table row a PASS WOULD seed (greedy, 'vllm-bi-on@sm89') -> bitwise. REPORTED
        # for the gate, never written: banking is the post-gate owner step (ADR-0004; capture §6).
        proposed_rule = {
            "declared_mode": declared.value,
            "substrate_key": substrate_key(cc, batch_invariant=True),
            "expected": ExpectedLevel.BITWISE.value,
        }

    return E6Result(
        arm="test" if batch_invariant else "control",
        passed=passed,
        n_samples=len(samples),
        batch_sizes=config.batch_sizes,
        restart_count=restart_count,
        distinct_signatures=len(distinct),
        divergence_appeared=divergence_appeared,
        max_abs_diff=max_abs,
        max_rel_diff=max_rel,
        argmax_flips=argmax_flips,
        first_divergence=first_divergence,
        declared_mode=declared,
        compute_capability=cc,
        batch_invariant=batch_invariant,
        proposed_rule=proposed_rule,
        samples=samples,
    )


def render_e6_report(test: E6Result, control: E6Result | None = None) -> str:
    """Human-readable gate summary. Pure formatting — no decision is taken or persisted here."""
    lines = []
    verdict = "PASS (bitwise)" if test.passed else "FAIL (not bitwise)"
    lines.append(f"E6 determinism gate: {verdict}")
    lines.append(
        f"  test arm  (VLLM_BATCH_INVARIANT=1): {test.n_samples} replicates, "
        f"batch sizes {list(test.batch_sizes)}, {test.restart_count} restart(s), "
        f"{test.distinct_signatures} distinct logprob signature(s)"
    )
    lines.append(
        f"    max abs logprob diff={test.max_abs_diff:.3e}, max rel diff={test.max_rel_diff:.3e}, "
        f"argmax flips={test.argmax_flips}, CC={test.compute_capability}"
    )
    if test.first_divergence is not None:
        lines.append(f"    first divergence: {test.first_divergence}")
    if test.proposed_rule is not None:
        lines.append(f"    proposed rule (NOT written): {test.proposed_rule}")
    if control is not None:
        ok = (
            "divergence APPEARED (protocol is live)"
            if control.divergence_appeared
            else ("NO divergence — protocol may be vacuous, INVESTIGATE")
        )
        lines.append(
            f"  control arm (flag unset): {control.n_samples} replicates, "
            f"{control.distinct_signatures} distinct signature(s) -> {ok}"
        )
        lines.append(
            f"    max abs logprob diff={control.max_abs_diff:.3e}, max rel diff={control.max_rel_diff:.3e}, "
            f"argmax flips={control.argmax_flips}"
        )
    return "\n".join(lines)
