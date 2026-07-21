"""The versioned answer-letter projection (ESTELA campaign, owner rulings §A12 D4b; Unit-B GO 2026-07-20).

Projects one capture's raw top-N next-token logprob distribution onto the injected answer letters A..E: a
k-entry ``{letter: logprob}`` dict, homed in ``run_metrics`` under a VERSION-BEARING metric_key
``answer_letter_logprobs@<semver>`` (provenance shape A, owner nod). The derivation is registered in
``method_versions`` (register-first, code_sha parity) so a later fix re-derives from the RETAINED raw top-N
under a bumped semver — the raw top-N stays authoritative in the ``capture_events`` wire bytes.

The letter->token_id resolver is the correctness core: it maps each answer letter to the token-id the model
would emit for that answer, via the SERVED tokenizer (``VLLMClient.tokenize`` over ``/tokenize`` — the SAME
id-space as the id-keyed logprobs). It is FAIL-LOUD: it probes the exact prompt tail and asserts a
strict-prefix + exactly-one-token delta, raising rather than resolving a wrong token-id if the tokenizer is
not prefix-stable at the ``Answer:`` boundary (a base-model tokenizer caveat: `` A`` vs ``A``).
"""

from __future__ import annotations

import inspect
import math
from typing import TYPE_CHECKING

from ..bundles.bundlespec import canonical_json
from ..db.identity import sha256_bytes
from ..registry.metric_keys import ANSWER_LETTER_LOGPROBS_V1
from .adapters.vllm import LogprobSample, VLLMAdapterError, VLLMClient

if TYPE_CHECKING:
    from ..db.repository import Repository

ANSWER_LETTER_METHOD_KEY = "answer_letter_logprobs"
ANSWER_LETTER_SEMVER = "1.0.0"
#: Shape A: the run_metrics metric_key IS version-bearing (== the metric_keys vocabulary row's key).
ANSWER_LETTER_METRIC_KEY = ANSWER_LETTER_LOGPROBS_V1.metric_key  # "answer_letter_logprobs@1.0.0"


def resolve_letter_token_ids(
    client: VLLMClient, *, model: str, base_prompt: str, letters: str
) -> dict[str, int]:
    r"""Resolve each answer letter to its next-token token-id after the prompt tail (FAIL-LOUD).

    For each letter L, tokenize ``base_prompt`` and ``base_prompt + " " + L`` and require the second to
    EXTEND the first by EXACTLY ONE token (a strict prefix + a single-token delta). That delta token is L's
    token-id — in the SAME id-space as ``LogprobSample.logprob_map`` (both come from the served tokenizer).
    Raises ``VLLMAdapterError`` if the tokenizer is not prefix-stable at the boundary (never a silent
    wrong-id pick; the derivation is versioned, so bump the semver + refine rather than guess).

    Resolve ONCE per server session: every ESTELA prompt shares the identical ``...\n\nAnswer:`` tail, so the
    map is stable — but the assertion still runs so a tokenizer change trips it.
    """
    base_tokens = client.tokenize(base_prompt, model=model)
    n = len(base_tokens)
    resolved: dict[str, int] = {}
    for letter in letters:
        probe_tokens = client.tokenize(base_prompt + " " + letter, model=model)
        if probe_tokens[:n] != base_tokens or len(probe_tokens) != n + 1:
            raise VLLMAdapterError(
                f"answer-letter token resolution failed for {letter!r}: appending ' {letter}' to the prompt "
                f"tail did not extend the tokenization by exactly one token (base={n}, probe="
                f"{len(probe_tokens)}) — the served tokenizer is not prefix-stable at the 'Answer:' boundary, "
                "so a naive delta would resolve a WRONG token-id. Fail loud (bump the method semver + refine "
                "the resolver rather than guess)."
            )
        resolved[letter] = probe_tokens[n]
    return resolved


def project_answer_letters(
    sample: LogprobSample, letter_token_ids: dict[str, int]
) -> dict[str, float | None]:
    """Project a capture's top-N logprobs onto the answer letters: ``{letter: logprob or None}``.

    A letter whose token-id is absent from the captured top-N distribution -> ``None`` (JSON ``null``), the
    HONEST 'censored below top-N' marker — NEVER a floor value (which would overstate its mass) and NEVER
    ``-inf`` (which ``canonical_json`` would serialize as invalid ``-Infinity``). Keeps ALL k keys so
    'measured' is distinguishable from 'censored'."""
    logprob_map = sample.logprob_map
    projection: dict[str, float | None] = {}
    for letter, tid in letter_token_ids.items():
        lp = logprob_map.get(tid)
        if lp is not None and not math.isfinite(lp):
            # Defensive: a top-N logprob is finite by construction; a non-finite value would serialize to
            # invalid JSON (-Infinity / NaN). Fail loud rather than store an unparseable value_json.
            raise VLLMAdapterError(
                f"answer-letter {letter!r} has a non-finite logprob {lp!r}; refusing to serialize invalid JSON."
            )
        projection[letter] = lp
    return projection


def project_to_value_json(projection: dict[str, float | None]) -> str:
    """Serialize the k-entry projection to the deterministic value_json TEXT (sorted keys; null for absent)."""
    return canonical_json(projection).decode("utf-8")


def count_censored(projection: dict[str, float | None]) -> int:
    """The null-cell QA counter: how many answer letters censored below the captured top-N (a QA signal)."""
    return sum(1 for v in projection.values() if v is None)


def answer_letter_method_code_sha() -> bytes:
    """The code_sha for method_versions parity (ADR-0011): sha256 over the extraction impl source. A change to
    the resolver / projection / serialization without a semver bump raises at register-first."""
    src = "".join(
        inspect.getsource(fn)
        for fn in (resolve_letter_token_ids, project_answer_letters, project_to_value_json)
    )
    return sha256_bytes(src.encode("utf-8"))


def register_answer_letter_method(repo: Repository) -> int:
    """Register the versioned answer-letter method (ONCE per campaign, never per row) AND seed its
    version-bearing metric_key (shape A). Returns ``method_version_id``. ``register_method_version`` is
    register-first / code_sha-parity; ``seed_metric_key`` is fail-loud on a divergent re-seed."""
    mv_id = repo.register_method_version(
        method_key=ANSWER_LETTER_METHOD_KEY,
        semver=ANSWER_LETTER_SEMVER,
        code_sha=answer_letter_method_code_sha(),
        set_active=True,
    )
    repo.seed_metric_key(
        metric_key=ANSWER_LETTER_LOGPROBS_V1.metric_key,
        value_kind=ANSWER_LETTER_LOGPROBS_V1.value_kind,
        description=ANSWER_LETTER_LOGPROBS_V1.description,
    )
    return mv_id


def write_answer_letter_projection(
    repo: Repository, *, run_id: int, sample: LogprobSample, letter_token_ids: dict[str, int]
) -> tuple[int, int]:
    """Project one capture + write its ``run_metrics`` row. Returns ``(run_metric_id, censored_count)``. The
    metric_key + its method_version must be registered first (``register_answer_letter_method``)."""
    projection = project_answer_letters(sample, letter_token_ids)
    run_metric_id = repo.write_run_metric(
        run_id=run_id,
        metric_key=ANSWER_LETTER_METRIC_KEY,
        value_json=project_to_value_json(projection),
    )
    return run_metric_id, count_censored(projection)
