"""The ESTELA order-bias capture campaign runner (owner rulings §A12 D1-D6; Unit-B GO 2026-07-20).

An in-process loop over the REAL ``capture_logprob`` (one engine / one Repository / one vLLM client), driving
the answer-order-permutation sweep over the pinned public MCQ bank: for each in-scope question, enumerate its
k! answer orders (the pinned lexicographic rule), render the labeled prompt (the pinned byte template), capture
the next-token logprobs to the canonical DB + local-lake, and write the versioned answer-letter projection to
``run_metrics`` (D4b, shape A). D2: run-per-permutation, organized as ``campaign_key="estela-order-bias"`` /
``work_slug=<uid>`` / ``variant_digest=<perm-index>`` (one capture_event per run). D5: recomputable — the three
pins (template, enumeration, batch-invariance) live here + at the server launch.

The testable core (``run_campaign``) takes an INJECTED ``repo``/``backend``/``backend_id``/``client``/
``tokenizer_hash`` so a GPU-free test can substitute a fake client; the thin ``neuro capture campaign`` CLI verb
does the real construction (exactly as ``capture_logprob`` <- ``cli/capture.py``).
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .answer_letter import (
    register_answer_letter_method,
    resolve_letter_token_ids,
    write_answer_letter_projection,
)
from .events import capture_logprob, require_dtype_quant

if TYPE_CHECKING:
    from ..db.repository import Repository
    from ..storage.backends import StorageBackend
    from .adapters.vllm import VLLMClient

#: The campaign coordinates (D2). Fixed for the whole ESTELA order-bias sweep.
CAMPAIGN_KEY = "estela-order-bias"
#: Injected answer labels A, B, C, ... (the campaign assigns its OWN labels; §1 of the readiness doc).
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
#: Charter scope: answer-order permutations for k <= this (owner scope; k>5 banks are excluded).
DEFAULT_MAX_K = 5
#: The pinned prompt-template version (D5 pin i). Recorded in the method/coordinates; a drift breaks recompute.
PROMPT_TEMPLATE_VERSION = "v1"


@dataclass(frozen=True)
class EstelaQuestion:
    """One in-scope MCQ: the source-order choices are the permutation base (position is the IV)."""

    uid: str
    stem: str
    choices: tuple[str, ...]
    num_choices: int


@dataclass(frozen=True)
class CampaignResult:
    """The end-state summary of one campaign run (for the runbook transcript + the state-doc bank)."""

    corpus_commit: str
    method_version_id: int
    questions_captured: int
    permutations_captured: int
    censored_cells_total: int
    dropped_duplicate_option_uids: tuple[str, ...]


class EstelaCampaignError(ValueError):
    """A campaign data/config error (malformed corpus, out-of-range arity, non-distinct uids). Typed so the
    CLI catches THESE fail-loud, without swallowing an unrelated ``ValueError`` from deep in the capture
    stack (which must surface with its true origin, not be masked as a clean 'campaign failed')."""


def read_estela_jsonl(path: Path, *, max_k: int = DEFAULT_MAX_K) -> list[EstelaQuestion]:
    """Read the ESTELA JSONL into in-scope questions (FAIL-LOUD field validation; text-only; k <= max_k).

    Fields required per record: ``uid``, ``question``, ``choices``, ``num_choices``, ``has_figure`` (the campaign
    injects its own A-E labels, so the dataset ``labels`` field is not used for the prompt). A missing field, a
    ``len(choices) != num_choices`` disagreement, or a malformed line raises (the T-DATA preflight).

    ⚠ THE STEM FIELD IS ``question``, NOT ``stem`` — MEASURED against the pinned corpus 2026-07-21, after the
    original build assumed ``stem`` (the readiness doc's PROSE name) and aborted at line 1 on the real file.
    ``stem`` exists nowhere in the corpus. ``question`` is also the correct partner for ``choices``: both carry
    ``$...$`` LaTeX, whereas ``question_raw``/``choices_raw`` carry ``<latex>...</latex>`` tags, and
    ``question``'s byte profile (min 114 / mean 333 / max 491) is the one the readiness doc §1 actually measured.
    The internal ``EstelaQuestion.stem`` attribute keeps its name; only the JSON key changed. ``has_figure`` is REQUIRED (fail-loud, not silent-falsy) so a misnamed figure flag cannot
    let a figure question leak into the text-only campaign; a truthy ``has_figure`` record is skipped. Records
    with ``num_choices`` outside ``[2, max_k]`` are out of scope (a degenerate k<2 has no order to permute)."""
    if max_k > len(LETTERS):
        raise EstelaCampaignError(
            f"max_k={max_k} exceeds the {len(LETTERS)} injectable labels (A-Z) — cannot label that many options."
        )
    questions: list[EstelaQuestion] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EstelaCampaignError(f"ESTELA JSONL line {lineno}: malformed JSON ({exc}).") from exc
        for field in ("uid", "question", "choices", "num_choices"):
            if field not in rec:
                raise EstelaCampaignError(
                    f"ESTELA JSONL line {lineno}: missing required field {field!r} "
                    f"(got keys {sorted(rec)}) — confirm the dataset field names (T-DATA)."
                )
        choices = rec["choices"]
        k = int(rec["num_choices"])
        if len(choices) != k:
            raise EstelaCampaignError(
                f"ESTELA JSONL line {lineno} uid={rec['uid']!r}: len(choices)={len(choices)} disagrees with "
                f"num_choices={k} (corpus integrity)."
            )
        if "has_figure" not in rec:
            raise EstelaCampaignError(
                f"ESTELA JSONL line {lineno} uid={rec['uid']!r}: missing 'has_figure' — required so a misnamed "
                "figure flag cannot silently leak a figure question into the text-only campaign (T-DATA)."
            )
        if rec["has_figure"]:
            continue  # text-only campaign (all ESTELA records are text-only)
        if not 2 <= k <= max_k:
            continue  # out of charter scope (k<2 has no order to permute; k>max_k is the variable-arity bank)
        questions.append(
            EstelaQuestion(
                uid=str(rec["uid"]),
                stem=str(rec["question"]),
                choices=tuple(str(c) for c in choices),
                num_choices=k,
            )
        )
    return questions


def partition_campaign_questions(
    questions: list[EstelaQuestion],
) -> tuple[list[EstelaQuestion], list[str]]:
    """Select the runnable campaign set: RAISE on non-distinct uids; DROP duplicate-option questions (D6).

    A non-distinct uid (a bank-id alias, readiness §1 note #5) would collide two run_keys and abort the
    campaign mid-run with an IdentityMismatchError after GPU cost — so it fails loud at zero cost here.
    A question with two byte-identical options has an ill-defined position-bias signal (D6 grounds), so it is
    DROPPED (generically — not just q18); the dropped uids are reported for the runbook."""
    uids = [q.uid for q in questions]
    if len(set(uids)) != len(uids):
        dups = sorted({u for u in uids if uids.count(u) > 1})
        raise EstelaCampaignError(
            f"ESTELA questions have non-distinct uids {dups} — a duplicate uid collides run_keys and would "
            "abort mid-campaign (investigate the bank-id aliasing before running; do not run)."
        )
    kept: list[EstelaQuestion] = []
    dropped: list[str] = []
    for q in questions:
        if len(set(q.choices)) != len(q.choices):
            dropped.append(q.uid)  # D6: byte-identical options -> ill-defined position bias -> drop
        else:
            kept.append(q)
    return kept, dropped


def enumerate_permutations(k: int) -> list[tuple[int, ...]]:
    """The pinned enumeration rule (D5 pin ii): lexicographic ``itertools.permutations(range(k))``; perm-index
    p in 0..k!-1 selects ``permutations[p]``. FROZEN — this is the recompute contract."""
    return list(itertools.permutations(range(k)))


def permuted_labeled_options(choices: tuple[str, ...], perm: tuple[int, ...]) -> list[tuple[str, str]]:
    """Assign labels A.. to the source-order choices reordered by ``perm``: [(letter, option), ...]."""
    return [(LETTERS[i], choices[perm[i]]) for i in range(len(perm))]


def render_prompt_v1(stem: str, labeled_options: list[tuple[str, str]]) -> str:
    r"""Prompt template v1 (D5 pin i) — pinned to the byte; FROZEN (a drift breaks recompute).

    ``{stem}\n\n{L}. {opt}\n{L2}. {opt2}\n...\n\nAnswer:`` — UTF-8, option text inserted VERBATIM
    (LaTeX/values preserved), no trailing space and no trailing newline after ``Answer:``."""
    lines = "\n".join(f"{letter}. {opt}" for letter, opt in labeled_options)
    return f"{stem}\n\n{lines}\n\nAnswer:"


def run_campaign(
    *,
    repo: Repository,
    backend: StorageBackend,
    backend_id: int,
    client: VLLMClient,
    tokenizer_hash: bytes,
    questions: list[EstelaQuestion],
    hf_repo: str,
    hf_revision: str,
    dtype_quant: str,
    corpus_commit: str,
    expected_lane: str = "canonical",
    n_logprobs: int = 64,
    actor_key: str = "owner",
    origin: str,
    progress: Callable[[int, int], None] | None = None,
) -> CampaignResult:
    """Drive the whole permutation sweep (the injectable core). Registers the versioned answer-letter method
    ONCE, resolves the letter token-ids ONCE (fail-loud), then per (question, perm-index) captures + projects.

    ``n_logprobs`` defaults to 64 (owner nod 2026-07-20, amends D4b's top-20 -> §G): near-exact for k<=5 and
    frozen-at-capture (unrecoverable without a GPU re-run), so it is set high here."""
    # Fail closed FAST on an absent/blank dtype grade — before the corpus scan, the method-version INSERT, and
    # the capture loop — so a mislabeled campaign never registers a method or writes a single identity row.
    require_dtype_quant(dtype_quant)
    kept, dropped = partition_campaign_questions(questions)
    if not kept:
        raise EstelaCampaignError("no ESTELA questions in scope after selection (empty or all dropped).")
    max_k = max(q.num_choices for q in kept)
    letters = LETTERS[:max_k]

    method_version_id = register_answer_letter_method(repo)

    # Resolve the answer-letter token-ids ONCE (every prompt shares the identical '...\n\nAnswer:' tail): use
    # the first kept question's identity-permutation prompt as the exact-context base. The resolver asserts a
    # strict-prefix + single-token delta and raises on a non-prefix-stable tokenizer (never a silent wrong id).
    served_model = client.served_model()
    base_perm = tuple(range(kept[0].num_choices))
    base_prompt = render_prompt_v1(kept[0].stem, permuted_labeled_options(kept[0].choices, base_perm))
    letter_token_ids = resolve_letter_token_ids(
        client, model=served_model, base_prompt=base_prompt, letters=letters
    )

    total = sum(math.factorial(q.num_choices) for q in kept)
    done = 0
    censored_total = 0
    for q in kept:
        sub_map = {letter: letter_token_ids[letter] for letter in LETTERS[: q.num_choices]}
        for perm_index, perm in enumerate(enumerate_permutations(q.num_choices)):
            prompt = render_prompt_v1(q.stem, permuted_labeled_options(q.choices, perm))
            result = capture_logprob(
                repo=repo,
                backend=backend,
                backend_id=backend_id,
                client=client,
                expected_lane=expected_lane,
                hf_repo=hf_repo,
                hf_revision=hf_revision,
                dtype_quant=dtype_quant,
                tokenizer_hash=tokenizer_hash,
                campaign_key=CAMPAIGN_KEY,
                work_slug=q.uid,
                variant_digest=str(perm_index),
                actor_key=actor_key,
                origin=origin,
                prompt=prompt,
                n_logprobs=n_logprobs,
                seed=1234,  # D5 pin (iii) — pinned HERE in the frozen recompute module, not inherited from a default
                batch_invariant=True,  # D5 pin (iii) — the E6 BI-on bitwise cert; frozen with the recipe (F4)
            )
            _run_metric_id, censored = write_answer_letter_projection(
                repo, run_id=result.run_id, sample=result.sample, letter_token_ids=sub_map
            )
            censored_total += censored
            done += 1
            if progress is not None:
                progress(done, total)

    return CampaignResult(
        corpus_commit=corpus_commit,
        method_version_id=method_version_id,
        questions_captured=len(kept),
        permutations_captured=done,
        censored_cells_total=censored_total,
        dropped_duplicate_option_uids=tuple(dropped),
    )
