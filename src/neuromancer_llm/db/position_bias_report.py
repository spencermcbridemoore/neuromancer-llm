"""`db/position_bias_report.py` — ESTELA Unit D: the answer-position/order-bias measure over a capture campaign.

The SECOND read module (`db/run_report.py` set the idiom, §A·61): a module-level function over an
already-verified engine, opening a read-only ``connect()``, returning a frozen report the
``neuro runs position-bias`` delegate renders. **This module WRITES NOTHING** — no `run_metrics` row, no
`divergence_measurements` row, no table. The per-QUESTION aggregate has no home in `run_metrics` (which is
per-RUN, and a run is per-PERMUTATION), and run-per-question is blocked on the deferred stimulus registry, so
the owner ruling is to compute on demand and persist nothing (ADR-0017 render-not-compute).

``divergence_measurements.answer_letter_flip_rate`` STAYS NULL, deliberately. It is named and documented for
position bias (ADR-0004 layer 3: "Answer flips are first-class MCQ-position-bias data") but seated in a
REPLICATE-grain table (`divergence_measurements` -> `replicate_links`, two runs of the SAME prompt) — and
position bias is structurally unmeasurable when position is held fixed. Writing this campaign's
permutation-siblings there would be silently accepted (the same-prompt rule is ADR-0004 semantics, not a CHECK)
and unrecoverably mislabeled, because a stored float carries no grain with it. That is an ADR-level tension
referred to wave-2, not something wave-1 resolves by filling the column.

★ PAYLOAD DISCIPLINE — THE MECHANISM IS THE SQL NARROWING, NOT A PROMISE. `db/run_report.py` never selects
`run_metrics.value_json` CONTENT (only `octet_length`) because `value_json` is general-purpose and the capture
side carries EXAM_RESTRICTED stimulus text. This module MUST read that content, so the read is confined by a
FAIL-CLOSED ALLOWLIST (the A2-17 idiom): only a `metric_key` in the ``answer_letter_logprobs@`` family — whose
payload is LOGPROBS, never stimulus text — can be read, the CLI does not expose the parameter at all, and NO
payload column of `capture_events` (`request_text` / `response_text`) is selected anywhere here. Read the SQL
and see it.

★ THE RESOLUTION FLOOR — MEASURED ON CANONICAL 2026-07-21, AND IT BOUNDS WHAT THIS MODULE CAN SEE. The captured
answer-letter logprobs sit on a **0.0625-nat (2^-4) quantization grid**: in every sampled projection, every
pairwise difference between letters is an exact multiple of 0.0625, and each projection is one arbitrary float32
constant (the log-softmax normalizer) minus an integer number of grid steps. That is the signature of **bf16
logits with magnitude in [8, 16)**, where the bf16 ULP is exactly 2^3 * 2^-7 = 0.0625 — a property of the pinned
capture recipe (bf16, ``--logprobs-mode raw_logprobs``), applying retroactively to all 6,000 ESTELA captures.

Consequences a reader must not miss: the answer letters span only ~4 distinguishable levels, so **13.7% of
permutations have no resolvable argmax at all** (they tie, and are excluded + counted); and no increase in
``n_logprobs`` can help, because the limit is DEPTH, not breadth — the D4b 20 -> 64 supersession (log:227) fixed
censoring, which is a different axis. A campaign needing finer resolution needs float32 logits, not more of them.

THE LOAD-BEARING SUBTLETY: the projection is keyed by LETTER (= POSITION), so measuring content-vs-position
requires mapping each letter back through the permutation. BOTH DIRECTIONS ARE USED AND THEY ARE NOT THE SAME:
letter ``i`` displayed source option ``perm[i]`` (a FORWARD index, `capture/campaign.py:171`), while the correct
answer sat at letter ``perm.index(answer_index)`` (the TRUE INVERSE). Swapping them silently measures the wrong
thing. The enumeration itself is imported from `capture/campaign.py` — it is the FROZEN recompute contract and
re-implementing it here would be a second implementation of one concept.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, text

from ..capture.answer_letter import ANSWER_LETTER_METHOD_KEY, ANSWER_LETTER_METRIC_KEY
from ..capture.campaign import (
    DEFAULT_MAX_K,
    LETTERS,
    enumerate_permutations,
    partition_campaign_questions,
    read_estela_jsonl,
)

#: Fail-closed allowlist for the payload-bearing read (see the module docstring). A metric_key outside this
#: family is REFUSED — so this module can never be pointed at an arbitrary `value_json` payload.
_ALLOWED_METRIC_PREFIX = f"{ANSWER_LETTER_METHOD_KEY}@"


class PositionBiasError(ValueError):
    """A data/config error in the position-bias read (unknown campaign, malformed projection, corpus mismatch).
    Typed so the CLI fails loud on THESE without swallowing an unrelated ValueError from deeper in the stack."""


# --- the argmax outcome -----------------------------------------------------------------------------
#: Why a permutation could not be scored, or that it was. A censored letter (JSON null) fell below the retained
#: top-N floor, hence below EVERY retained letter, so it can never be the argmax — it is excluded from the
#: candidate set rather than treated as -inf-and-scored. If EVERY letter is censored there is no argmax at all.
_OK, _TIE, _CENSORED = "ok", "tie", "all-censored"


@dataclass(frozen=True)
class Argmax:
    """The argmax letter index of one projection, or why there is none. `reason` is 'ok' | 'tie' | 'all-censored'."""

    letter_index: int | None
    reason: str


def argmax_letter(projection: dict[str, float | None]) -> Argmax:
    """The index of the highest-logprob letter, with the TIE RULE applied.

    ★ THE TIE RULE (explicit, never assumed away): an EXACT tie for the maximum is NOT broken — it returns
    reason='tie' and the caller EXCLUDES and COUNTS it. Any deterministic letter-ordered tie-break ("lowest
    letter wins") would inject position bias into a position-bias measure and manufacture precisely the
    A-favoured result under test. Exclusion is the only rule that cannot fabricate the finding.

    ⚠ TIES ARE COMMON, NOT RARE — MEASURED, NOT ASSUMED. This docstring previously claimed exact ties are
    "improbable at k <= 5". The first canonical run REFUTED that: **820 / 6,000 = 13.7%** of the ESTELA
    permutations tie, spread across every question. The cause is the capture recipe, not the model — see the
    bf16 quantization note in the module docstring. So the rule is not a formality guarding an unreachable
    branch; it decides the disposition of one permutation in seven, and the excluded fraction MUST be reported
    (it is, as `tie_count`, per-question and per-k)."""
    live = [(i, v) for i, (_letter, v) in enumerate(sorted(projection.items())) if v is not None]
    if not live:
        return Argmax(None, _CENSORED)
    best = max(v for _i, v in live)
    winners = [i for i, v in live if v == best]
    if len(winners) > 1:
        return Argmax(None, _TIE)
    return Argmax(winners[0], _OK)


@dataclass(frozen=True)
class QuestionBias:
    """One question's position-bias measure over its k! answer-order permutations.

    `content_consistency` = modal SOURCE-OPTION share: 1.0 under perfect content-tracking, 1/k under pure
    position bias. `position_concentration` = modal LETTER share: 1/k under perfect content-tracking (EXACTLY —
    the enumeration is complete, so each source option occupies each letter position exactly (k-1)! times),
    1.0 under pure position bias. The two are orthogonal by construction."""

    uid: str
    k: int
    perm_count: int  # permutations actually found in the DB for this question
    expected_perm_count: int  # k! — a shortfall is an honest HOLE, surfaced not hidden
    tie_count: int
    censored_cell_count: int
    unscoreable_count: int
    scored_count: int
    letter_counts: tuple[int, ...]  # index = letter position 0..k-1
    source_counts: tuple[int, ...]  # index = source option 0..k-1
    content_consistency: float | None  # None when nothing was scoreable
    position_concentration: float | None
    # --- accuracy: present ONLY when the corpus answer key was supplied ---
    answer_index: int | None
    correct_count: int | None
    accuracy: float | None
    correct_by_position: tuple[int, ...] | None  # correct, keyed by the letter the ANSWER was displayed at
    scored_by_position: tuple[int, ...] | None


@dataclass(frozen=True)
class KAggregate:
    """The measure aggregated over every question of one arity k. Reported per-k because k=4 (24 permutations)
    and k=5 (120) have different resolution — pooling them would hide that."""

    k: int
    question_count: int
    scored_count: int
    null_share: float  # 1/k — the EXACT null, but see questions_above_position_null for when it is ATTAINABLE
    mean_content_consistency: float | None
    mean_position_concentration: float | None
    #: Questions whose letter concentration exceeds the SMALLEST value attainable at their own denominator.
    #: ⚠ NOT `> 1/k`: `max(letter_counts)/n` is a MAX statistic bounded below by `ceil(n/k)/n`, which is
    #: STRICTLY greater than 1/k whenever `n % k != 0` — arithmetically, not probabilistically. So a bare
    #: `> 1/k` test fires for a PERFECT content-tracker whenever the denominator was reduced by an excluded
    #: tie, an unscoreable permutation, or a DB hole — i.e. by this module's own exclusion rules. The floor
    #: is compared instead, and the reductions are carried below so a reader can see when one applied.
    questions_above_position_null: int
    tie_count: int  # per-k denominator reductions, so "above the null" is auditable rather than asserted
    unscoreable_count: int
    hole_count: int  # permutations MISSING from the DB relative to k!
    pooled_letter_counts: tuple[int, ...]
    pooled_letter_shares: tuple[float, ...]  # the "which letter is favoured" readout, per-k
    accuracy_question_count: int  # the DENOMINATOR of mean_accuracy — a SUBSET of question_count
    mean_accuracy: float | None
    accuracy_by_position: tuple[float | None, ...] | None  # accuracy given the KEY sat at letter j


@dataclass(frozen=True)
class CorpusValidation:
    """Coverage + answer-key integrity of the DB campaign against the pinned corpus.

    ⚠ Validated over PARSED CONTENT, never file bytes: the same pinned commit checks out CRLF on Windows
    (211808 B) and LF on Linux (211713 B), so a byte hash is machine-dependent and would falsely redden."""

    corpus_path: str
    corpus_scope_count: int  # in-scope questions after the campaign's OWN filters + the D6 drop
    dropped_duplicate_option_uids: tuple[str, ...]
    uids_in_db_not_corpus: tuple[str, ...]
    uids_in_corpus_not_db: tuple[str, ...]
    #: The *_shuffled trap check RAN and FAILED (answer_text != choices[answer_index]).
    answer_key_mismatch_uids: tuple[str, ...]
    #: In scope, but the corpus record carries NO `answer_index` — no key, so no accuracy. Surfaced rather
    #: than skipped: an unreported question would be silently absent from the accuracy denominator.
    uids_without_answer_key: tuple[str, ...]
    #: Has `answer_index` but NO `answer_text`, so the *_shuffled cross-check COULD NOT RUN. Accuracy is
    #: WITHHELD for these — an unverifiable key must not yield a number under a green tick. (The check used
    #: to be a CONJUNCT `answer_text and answer_text != ...`, which silently accepted this case; house rule:
    #: guards are SINGLE-PURPOSE, and the asymmetric-NULL obligation demands BOTH null cases be probed.)
    uids_without_answer_text: tuple[str, ...]
    #: `len(choices)` in the corpus disagrees with the arity `k` observed in the DB projection for that uid —
    #: the only structural defence, given the corpus file is deliberately NOT hashed (CRLF/LF ruling), against
    #: pointing --corpus-root at a DIFFERENT but internally-consistent ESTELA export.
    arity_mismatch_uids: tuple[str, ...]


@dataclass(frozen=True)
class PositionBiasReport:
    """The campaign-wide position-bias measure, ready to render. Computed on demand; NOTHING is persisted."""

    campaign_key: str
    lane: str
    metric_key: str
    question_count: int
    run_count: int
    scored_count: int
    tie_count: int
    unscoreable_count: int
    censored_cell_count: int
    questions: tuple[QuestionBias, ...]
    by_k: tuple[KAggregate, ...]
    corpus: CorpusValidation | None


# The ONE payload-bearing select in the library's read side, and it is narrowed to the answer-letter family by
# the allowlist above (the metric_key is always a bound param; the narrowing is not cosmetic). NO capture_events
# payload column appears here — the wire text is never read by this module.
_PROJECTIONS_SQL = """
    SELECT r.work_slug AS uid, r.variant_digest AS perm_index, rm.value_json AS value_json
    FROM neuro.runs r
    JOIN neuro.campaigns c ON c.campaign_id = r.campaign_id
    JOIN neuro.run_metrics rm ON rm.run_id = r.run_id
    WHERE c.campaign_key = :campaign_key
      AND rm.metric_key = :metric_key
    ORDER BY r.work_slug, r.run_id
"""

_CAMPAIGN_EXISTS_SQL = "SELECT 1 FROM neuro.campaigns WHERE campaign_key = :campaign_key"


def _parse_projection(uid: str, perm_index: int, raw: str | None) -> dict[str, float | None]:
    """Parse one `value_json` projection into `{letter: logprob|None}`, FAIL-LOUD on anything malformed. The
    letters must be exactly the first k of A.. — a gap or a stray key means the projection does not describe a
    k-option question and no measure over it would be meaningful."""
    if raw is None:
        raise PositionBiasError(
            f"{uid} perm {perm_index}: run_metrics.value_json is NULL for the projection."
        )
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PositionBiasError(f"{uid} perm {perm_index}: malformed projection JSON ({exc}).") from exc
    if not isinstance(obj, dict) or not obj:
        raise PositionBiasError(f"{uid} perm {perm_index}: projection is not a non-empty object.")
    k = len(obj)
    expected = set(LETTERS[:k])
    if set(obj) != expected:
        raise PositionBiasError(
            f"{uid} perm {perm_index}: projection keys {sorted(obj)} are not the first {k} letters "
            f"{sorted(expected)} — the letter set must be contiguous from A."
        )
    out: dict[str, float | None] = {}
    for letter, value in obj.items():
        if value is None:
            out[letter] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[letter] = float(value)
        else:
            raise PositionBiasError(
                f"{uid} perm {perm_index}: projection[{letter!r}] is {type(value).__name__}, not a number/null."
            )
    return out


def score_question(
    uid: str,
    projections: list[tuple[int, dict[str, float | None]]],
    *,
    answer_index: int | None = None,
) -> QuestionBias:
    """Score ONE question from its (perm_index, projection) pairs. Pure — no DB, no I/O — so the measure math is
    unit-testable and mutation-verifiable on its own.

    ★ THE INVERSION, BOTH DIRECTIONS: the model's chosen letter maps to the SOURCE OPTION it displayed via the
    FORWARD index ``perm[letter_index]``; the correct answer was displayed at letter ``perm.index(answer_index)``,
    the TRUE INVERSE. They coincide only for involutions, so a transposition is invisible on an identity or a
    single swap and must be probed with a 3-cycle."""
    if not projections:
        raise PositionBiasError(f"{uid}: no projections to score.")
    ks = {len(p) for _idx, p in projections}
    if len(ks) != 1:
        raise PositionBiasError(
            f"{uid}: permutations disagree on k (saw {sorted(ks)}) — one question, one arity."
        )
    k = ks.pop()
    perms = enumerate_permutations(k)
    if answer_index is not None and not 0 <= answer_index < k:
        raise PositionBiasError(f"{uid}: answer_index {answer_index} out of range for k={k}.")

    letter_counts = [0] * k
    source_counts = [0] * k
    correct_by_position = [0] * k
    scored_by_position = [0] * k
    ties = censored_cells = unscoreable = correct = 0

    for perm_index, projection in projections:
        if not 0 <= perm_index < len(perms):
            raise PositionBiasError(
                f"{uid}: perm_index {perm_index} out of range for k={k} ({len(perms)} permutations)."
            )
        perm = perms[perm_index]
        censored_cells += sum(1 for v in projection.values() if v is None)
        outcome = argmax_letter(projection)
        if outcome.reason == _TIE:
            ties += 1
            continue
        if outcome.letter_index is None:
            unscoreable += 1
            continue
        letter_index = outcome.letter_index
        # FORWARD (perm[i]): the letter the model chose -> the source option that letter displayed.
        source_index = perm[letter_index]
        letter_counts[letter_index] += 1
        source_counts[source_index] += 1
        if answer_index is not None:
            # INVERSE (perm.index(x)): the correct source option -> the letter it was displayed at.
            # These two are NOT interchangeable; they coincide only for involutions.
            key_position = perm.index(answer_index)
            scored_by_position[key_position] += 1
            if source_index == answer_index:
                correct += 1
                correct_by_position[key_position] += 1

    scored = sum(letter_counts)
    return QuestionBias(
        uid=uid,
        k=k,
        perm_count=len(projections),
        expected_perm_count=math.factorial(k),
        tie_count=ties,
        censored_cell_count=censored_cells,
        unscoreable_count=unscoreable,
        scored_count=scored,
        letter_counts=tuple(letter_counts),
        source_counts=tuple(source_counts),
        content_consistency=(max(source_counts) / scored) if scored else None,
        position_concentration=(max(letter_counts) / scored) if scored else None,
        answer_index=answer_index,
        correct_count=correct if answer_index is not None else None,
        accuracy=(correct / scored) if (answer_index is not None and scored) else None,
        correct_by_position=tuple(correct_by_position) if answer_index is not None else None,
        scored_by_position=tuple(scored_by_position) if answer_index is not None else None,
    )


def _exceeds_attainable_floor(q: QuestionBias) -> bool:
    """Is this question's letter concentration above the SMALLEST value its own denominator allows?

    ★ NOT `position_concentration > 1/k`. `max(letter_counts)` cannot go below `ceil(n/k)` for n scored
    permutations over k letters, and `ceil(n/k)/n > 1/k` whenever `n % k != 0` — an ARITHMETIC fact, not a
    probabilistic one. A bare `> 1/k` test therefore counts a PERFECT content-tracker as biased the moment its
    denominator is reduced by an excluded tie, an unscoreable permutation, or a missing row: exactly the
    exclusions this module defines. Comparing against the attainable floor keeps the flag meaning "more
    concentrated than it had to be" at every denominator."""
    if not q.scored_count:
        return False
    floor = -(-q.scored_count // q.k)  # ceil(n/k) without importing math.ceil on ints
    return max(q.letter_counts) > floor


def _aggregate_by_k(questions: tuple[QuestionBias, ...]) -> tuple[KAggregate, ...]:
    """Aggregate per arity. k=4 and k=5 are NEVER pooled — 24 vs 120 permutations is a resolution difference a
    pooled mean would hide."""
    out: list[KAggregate] = []
    for k in sorted({q.k for q in questions}):
        group = [q for q in questions if q.k == k]
        scoreable = [q for q in group if q.scored_count]
        pooled = [0] * k
        for q in group:
            for i, c in enumerate(q.letter_counts):
                pooled[i] += c
        pooled_total = sum(pooled)
        with_key = [q for q in scoreable if q.accuracy is not None]
        correct_pos = [0] * k
        scored_pos = [0] * k
        for q in with_key:
            assert q.correct_by_position is not None and q.scored_by_position is not None
            for i in range(k):
                correct_pos[i] += q.correct_by_position[i]
                scored_pos[i] += q.scored_by_position[i]
        out.append(
            KAggregate(
                k=k,
                question_count=len(group),
                scored_count=pooled_total,
                null_share=1.0 / k,
                mean_content_consistency=(
                    sum(q.content_consistency or 0.0 for q in scoreable) / len(scoreable)
                    if scoreable
                    else None
                ),
                mean_position_concentration=(
                    sum(q.position_concentration or 0.0 for q in scoreable) / len(scoreable)
                    if scoreable
                    else None
                ),
                questions_above_position_null=sum(1 for q in scoreable if _exceeds_attainable_floor(q)),
                tie_count=sum(q.tie_count for q in group),
                unscoreable_count=sum(q.unscoreable_count for q in group),
                hole_count=sum(q.expected_perm_count - q.perm_count for q in group),
                pooled_letter_counts=tuple(pooled),
                pooled_letter_shares=tuple((c / pooled_total) if pooled_total else 0.0 for c in pooled),
                accuracy_question_count=len(with_key),
                mean_accuracy=(
                    sum(q.accuracy or 0.0 for q in with_key) / len(with_key) if with_key else None
                ),
                accuracy_by_position=(
                    tuple(
                        (correct_pos[i] / scored_pos[i]) if scored_pos[i] else None  #
                        for i in range(k)
                    )
                    if with_key
                    else None
                ),
            )
        )
    return tuple(out)


def read_answer_keys(path: Path) -> dict[str, tuple[int, str | None, tuple[str, ...]]]:
    """A SECOND projection of the same corpus file, for the answer key only — `EstelaQuestion` deliberately drops
    it (uid/stem/choices/num_choices only), so the key cannot come from `read_estela_jsonl`.

    ⚠ SCOPE DOES NOT COME FROM HERE. This reads EVERY record with a key and does no filtering; the in-scope set
    is `read_estela_jsonl` + `partition_campaign_questions` (the has_figure skip, 2<=k<=max_k, and the D6
    duplicate-option drop). Re-implementing that filter here would let the question set silently drift.

    ⚠ THE `*_shuffled` TRAP: the corpus ships its OWN de-biased shuffle (`shuffle_perm`, `choices_shuffled`,
    `answer_index_shuffled`, `answer_label_shuffled`) which is NOT the campaign's permutation. The campaign read
    source-order `choices`, so this reads `answer_index` against `choices` and never touches a `*_shuffled`
    field. `answer_text` is returned so the caller can cross-check `answer_text == choices[answer_index]`."""
    keys: dict[str, tuple[int, str | None, tuple[str, ...]]] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PositionBiasError(f"corpus line {lineno}: malformed JSON ({exc}).") from exc
        if "uid" not in rec:
            raise PositionBiasError(f"corpus line {lineno}: missing 'uid'.")
        if "answer_index" not in rec or "choices" not in rec:
            continue  # keyless: the CALLER reports it (uids_without_answer_key), it is not silently dropped
        uid = str(rec["uid"])
        choices = tuple(str(c) for c in rec["choices"])
        # `answer_text` may be genuinely ABSENT — None, distinct from an empty string, so the caller can tell
        # "no cross-check possible" apart from "cross-checked against an empty value".
        text_value = rec.get("answer_text")
        keys[uid] = (int(rec["answer_index"]), None if text_value is None else str(text_value), choices)
    return keys


def _validate_corpus(
    path: Path, db_uids: set[str], *, max_k: int, db_k_by_uid: dict[str, int] | None = None
) -> tuple[CorpusValidation, dict[str, int]]:
    """Build the coverage/integrity report and the uid -> answer_index map the measure uses.

    EVERY in-scope question lands in exactly one outlet: an accepted key, or one of the reported uid lists.
    A question that reaches none of them would be invisible in the coverage panel while silently shrinking the
    accuracy denominator, so the outlets are exhaustive by construction."""
    questions = read_estela_jsonl(path, max_k=max_k)
    kept, dropped = partition_campaign_questions(questions)
    keys = read_answer_keys(path)
    db_k = db_k_by_uid or {}

    scope_uids = {q.uid for q in kept}
    mismatches: list[str] = []
    no_key: list[str] = []
    no_text: list[str] = []
    arity_mismatch: list[str] = []
    answer_index_by_uid: dict[str, int] = {}
    for q in kept:
        entry = keys.get(q.uid)
        if entry is None:
            no_key.append(q.uid)  # in scope, no answer_index -> no accuracy, and SAID SO
            continue
        answer_index, answer_text, choices = entry
        # The corpus arity must agree with the arity actually captured. With the corpus file deliberately NOT
        # hashed (the CRLF/LF ruling), this is the structural defence against a DIFFERENT but internally
        # consistent export: uid overlap alone cannot tell two exports apart.
        # SINGLE-PURPOSE: one condition, "do the arities disagree?". The default makes an UNKNOWN DB arity
        # (the pure-corpus path supplies none) compare against itself, so availability is resolved in the
        # lookup rather than fused into the guard as a second conjunct.
        observed_k = db_k.get(q.uid, len(choices))
        if len(choices) != observed_k:
            arity_mismatch.append(q.uid)
            continue
        if not 0 <= answer_index < len(choices):
            mismatches.append(q.uid)
            continue
        # SINGLE-PURPOSE guards (no conjuncts), and BOTH asymmetric-NULL cases probed: an ABSENT answer_text
        # means the *_shuffled cross-check CANNOT RUN, which is not the same as it running and passing. A key
        # that cannot be cross-checked yields NO accuracy — it must never ride under a green tick.
        if answer_text is None:
            no_text.append(q.uid)
            continue
        # The *_shuffled cross-check: the key must address the SOURCE-order choices the campaign actually read.
        if answer_text != choices[answer_index]:
            mismatches.append(q.uid)
            continue
        answer_index_by_uid[q.uid] = answer_index

    return (
        CorpusValidation(
            corpus_path=str(path),
            corpus_scope_count=len(kept),
            dropped_duplicate_option_uids=tuple(dropped),
            uids_in_db_not_corpus=tuple(sorted(db_uids - scope_uids)),
            uids_in_corpus_not_db=tuple(sorted(scope_uids - db_uids)),
            answer_key_mismatch_uids=tuple(sorted(mismatches)),
            uids_without_answer_key=tuple(sorted(no_key)),
            uids_without_answer_text=tuple(sorted(no_text)),
            arity_mismatch_uids=tuple(sorted(arity_mismatch)),
        ),
        answer_index_by_uid,
    )


def build_position_bias_report(
    engine: Engine,
    *,
    campaign_key: str,
    corpus_path: Path | None = None,
    max_k: int = DEFAULT_MAX_K,
    lane: str = "canonical",
    metric_key: str = ANSWER_LETTER_METRIC_KEY,
) -> PositionBiasReport:
    """Compute the campaign's position-bias measure. Read-only: opens `connect()`, writes nothing, persists
    nothing. `corpus_path` is optional — without it the measure still separates content from position (the
    mapping is derivable from the DB alone: k from the projection, perm_index from `variant_digest`), and only
    ACCURACY and the coverage/answer-key validation are unavailable.

    `lane` is the already-verified lane of the caller's engine, recorded as identity context, not re-verified."""
    if not metric_key.startswith(_ALLOWED_METRIC_PREFIX):
        raise PositionBiasError(
            f"metric_key {metric_key!r} is outside the {_ALLOWED_METRIC_PREFIX!r} family — this module reads "
            "run_metrics.value_json CONTENT and is confined to the answer-letter projection by design "
            "(payload discipline); point it at another metric and it refuses rather than reading an "
            "unknown payload."
        )

    with engine.connect() as conn:
        if conn.execute(text(_CAMPAIGN_EXISTS_SQL), {"campaign_key": campaign_key}).one_or_none() is None:
            raise PositionBiasError(f"no campaign with campaign_key={campaign_key!r} on lane {lane!r}")
        rows = (
            conn.execute(text(_PROJECTIONS_SQL), {"campaign_key": campaign_key, "metric_key": metric_key})
            .mappings()
            .all()
        )
    if not rows:
        raise PositionBiasError(
            f"campaign {campaign_key!r} has no {metric_key!r} projections — nothing to measure."
        )

    grouped: dict[str, list[tuple[int, dict[str, float | None]]]] = {}
    for row in rows:
        uid = str(row["uid"])
        raw_index = str(row["perm_index"])
        try:
            perm_index = int(raw_index)
        except ValueError as exc:
            raise PositionBiasError(
                f"{uid}: variant_digest {raw_index!r} is not an integer perm-index (D2 convention)."
            ) from exc
        grouped.setdefault(uid, []).append(
            (perm_index, _parse_projection(uid, perm_index, row["value_json"]))
        )

    corpus: CorpusValidation | None = None
    answer_index_by_uid: dict[str, int] = {}
    if corpus_path is not None:
        db_k_by_uid = {uid: len(projections[0][1]) for uid, projections in grouped.items()}
        corpus, answer_index_by_uid = _validate_corpus(
            corpus_path, set(grouped), max_k=max_k, db_k_by_uid=db_k_by_uid
        )

    questions = tuple(
        score_question(uid, grouped[uid], answer_index=answer_index_by_uid.get(uid))
        for uid in sorted(grouped)
    )
    return PositionBiasReport(
        campaign_key=campaign_key,
        lane=lane,
        metric_key=metric_key,
        question_count=len(questions),
        run_count=len(rows),
        scored_count=sum(q.scored_count for q in questions),
        tie_count=sum(q.tie_count for q in questions),
        unscoreable_count=sum(q.unscoreable_count for q in questions),
        censored_cell_count=sum(q.censored_cell_count for q in questions),
        questions=questions,
        by_k=_aggregate_by_k(questions),
        corpus=corpus,
    )
