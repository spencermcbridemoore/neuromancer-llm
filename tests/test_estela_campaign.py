"""Unit B — ESTELA campaign-runner + versioned answer-letter projection (owner rulings §A12; GO 2026-07-20).

Pure-function tests (template/enumeration/reader/partition/resolver/projection) run without a DB; the runner
loop + the metric_keys/run_metrics writers + the method registration run through the REAL capture path with a
GPU-free fake client (`_FakeCampaignClient`, which — unlike the shared redteam `_FakeClient` — also exposes
`tokenize()` so the projection resolver is genuinely exercised, not false-greened on all-null projections).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from neuromancer_llm.capture.adapters.vllm import CapturedLogprobs, LogprobSample, VLLMAdapterError
from neuromancer_llm.capture.answer_letter import (
    ANSWER_LETTER_METRIC_KEY,
    answer_letter_method_code_sha,
    count_censored,
    project_answer_letters,
    project_to_value_json,
    register_answer_letter_method,
    resolve_letter_token_ids,
    write_answer_letter_projection,
)
from neuromancer_llm.capture.campaign import (
    CAMPAIGN_KEY,
    EstelaCampaignError,
    EstelaQuestion,
    enumerate_permutations,
    partition_campaign_questions,
    permuted_labeled_options,
    read_estela_jsonl,
    render_prompt_v1,
    run_campaign,
)
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.registry.metric_keys import ANSWER_LETTER_LOGPROBS_V1
from neuromancer_llm.storage.backends import LocalFsBackend

# --- fake vLLM client with a prefix-stable toy tokenizer + a fixed sample ----------------------------
LETTER_TOKEN_ID = {"A": 2001, "B": 2002, "C": 2003, "D": 2004, "E": 2005}


def _sample_with_letters(letters: dict[str, float]) -> LogprobSample:
    """A sample whose top_logprobs carry exactly the given answer-letter token-ids (+ their logprobs)."""
    pairs = tuple(sorted((LETTER_TOKEN_ID[letter], lp) for letter, lp in letters.items()))
    gen = pairs[0][0] if pairs else 0
    return LogprobSample(prompt="p", generated_token_id=gen, top_logprobs=pairs)


class _FakeCampaignClient:
    """GPU-free vLLM stand-in: served_model + a prefix-stable toy tokenizer + a fixed capture sample."""

    def __init__(self, sample: LogprobSample) -> None:
        self._sample = sample
        self.capture_calls = 0
        self.tokenize_calls = 0

    def served_model(self) -> str:
        return "fake/Mistral-7B-v0.3"

    def tokenize(self, text: str, *, model: str) -> list[int]:
        # Prefix-stable: a trailing ' <LETTER>' is ONE token == LETTER_TOKEN_ID; the base is one token/char.
        self.tokenize_calls += 1
        if len(text) >= 2 and text[-2] == " " and text[-1] in LETTER_TOKEN_ID:
            return self.tokenize(text[:-2], model=model) + [LETTER_TOKEN_ID[text[-1]]]
        return [900 + (i % 50) for i in range(len(text))]

    def next_token_logprobs_capture(
        self, prompt: str, *, model: str, n_logprobs: int, seed: int
    ) -> CapturedLogprobs:
        self.capture_calls += 1
        req = json.dumps(
            {"model": model, "prompt": prompt, "max_tokens": 1, "temperature": 0, "seed": seed}
        ).encode("utf-8")
        top = {f"token_id:{t}": lp for t, lp in self._sample.top_logprobs}
        resp = json.dumps(
            {
                "choices": [
                    {
                        "logprobs": {
                            "tokens": [f"token_id:{self._sample.generated_token_id}"],
                            "top_logprobs": [top],
                        }
                    }
                ]
            }
        ).encode("utf-8")
        return CapturedLogprobs(
            sample=self._sample,
            request_body=req,
            response_body=resp,
            http_status=200,
            content_type="application/json",
        )


class _NonPrefixStableClient(_FakeCampaignClient):
    """A tokenizer that is NOT prefix-stable at the boundary (delta is not a clean single appended token)."""

    def tokenize(self, text: str, *, model: str) -> list[int]:
        return [len(text)]  # base=[L], base+' A'=[L+2] -> not a prefix extension -> resolver must raise


# --- pure functions: template + enumeration ---------------------------------------------------------
def test_render_prompt_v1_byte_spec() -> None:
    labeled = [("A", "Berlin"), ("B", "503.34 N")]
    assert render_prompt_v1("What is X?", labeled) == "What is X?\n\nA. Berlin\nB. 503.34 N\n\nAnswer:"


def test_enumerate_permutations_is_lexicographic_and_complete() -> None:
    perms = enumerate_permutations(4)
    assert len(perms) == 24
    assert perms[0] == (0, 1, 2, 3)  # perm-index 0 is the identity (source order)
    assert len(set(perms)) == 24  # all distinct
    assert perms == sorted(perms)  # lexicographic — the recompute contract


def test_permuted_labeled_options() -> None:
    assert permuted_labeled_options(("x", "y", "z"), (2, 0, 1)) == [("A", "z"), ("B", "x"), ("C", "y")]


# --- pure functions: the JSONL reader ---------------------------------------------------------------
def test_read_estela_jsonl_filters_scope(tmp_path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "uid": "q1",
                        "question": "S1",
                        "choices": ["a", "b", "c"],
                        "num_choices": 3,
                        "has_figure": False,
                    }
                ),
                json.dumps(
                    {
                        "uid": "q2",
                        "question": "S2",
                        "choices": ["a"] * 8,
                        "num_choices": 8,
                        "has_figure": False,
                    }
                ),  # k>5 -> skip
                json.dumps(
                    {
                        "uid": "q3",
                        "question": "S3",
                        "choices": ["a", "b"],
                        "num_choices": 2,
                        "has_figure": True,
                    }
                ),  # has_figure -> skip
                "",  # blank line ignored
            ]
        ),
        encoding="utf-8",
    )
    qs = read_estela_jsonl(p, max_k=5)
    assert [q.uid for q in qs] == ["q1"]
    assert qs[0].choices == ("a", "b", "c") and qs[0].num_choices == 3


def test_read_estela_jsonl_missing_field_raises(tmp_path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text(
        json.dumps({"uid": "q1", "choices": ["a", "b"], "num_choices": 2}), encoding="utf-8"
    )  # no question
    with pytest.raises(ValueError, match="missing required field 'question'"):
        read_estela_jsonl(p)


def test_read_estela_jsonl_rejects_legacy_stem_key(tmp_path) -> None:
    """REGRESSION PIN (2026-07-21): the stem field is ``question``, NOT ``stem``.

    The original build required ``stem`` -- the readiness doc's PROSE name -- and aborted at line 1 of the real
    corpus, which carries ``question`` and no ``stem`` at all. Every fixture here encoded the same wrong key, so
    19 green tests validated the design doc rather than the data. This pins the corrected key: a record shaped
    the OLD way must fail loud, naming ``question``."""
    p = tmp_path / "c.jsonl"
    p.write_text(
        json.dumps({"uid": "q1", "stem": "S", "choices": ["a", "b"], "num_choices": 2, "has_figure": False}),
        encoding="utf-8",
    )
    with pytest.raises(EstelaCampaignError, match="missing required field 'question'"):
        read_estela_jsonl(p)


def test_read_estela_jsonl_len_mismatch_raises(tmp_path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text(
        json.dumps({"uid": "q1", "question": "S", "choices": ["a", "b"], "num_choices": 3}), encoding="utf-8"
    )
    with pytest.raises(EstelaCampaignError, match="disagrees with num_choices"):
        read_estela_jsonl(p)


def test_read_estela_jsonl_missing_has_figure_raises(tmp_path) -> None:
    """has_figure is REQUIRED (fail-loud, not silent-falsy) so a misnamed figure flag can't leak (post-vet)."""
    p = tmp_path / "c.jsonl"
    p.write_text(
        json.dumps({"uid": "q1", "question": "S", "choices": ["a", "b"], "num_choices": 2}), encoding="utf-8"
    )
    with pytest.raises(EstelaCampaignError, match="has_figure"):
        read_estela_jsonl(p)


# --- pure functions: the campaign selection (uid preflight + D6 drop) --------------------------------
def test_partition_drops_duplicate_option_questions() -> None:
    qs = [
        EstelaQuestion(uid="q18", stem="S", choices=("a", "b", "a"), num_choices=3),  # dup options -> D6 drop
        EstelaQuestion(uid="q2", stem="S", choices=("x", "y"), num_choices=2),
    ]
    kept, dropped = partition_campaign_questions(qs)
    assert [q.uid for q in kept] == ["q2"]
    assert dropped == ["q18"]


def test_partition_raises_on_non_distinct_uids() -> None:
    qs = [
        EstelaQuestion(uid="dup", stem="S1", choices=("a", "b"), num_choices=2),
        EstelaQuestion(uid="dup", stem="S2", choices=("x", "y"), num_choices=2),
    ]
    with pytest.raises(ValueError, match="non-distinct uids"):
        partition_campaign_questions(qs)


# --- pure functions: the letter->token_id resolver (fail-loud) --------------------------------------
def test_resolve_letter_token_ids() -> None:
    client = _FakeCampaignClient(_sample_with_letters({"A": -0.5}))
    ids = resolve_letter_token_ids(client, model="m", base_prompt="stem\n\nA. x\n\nAnswer:", letters="ABC")
    assert ids == {"A": 2001, "B": 2002, "C": 2003}


def test_resolve_raises_when_not_prefix_stable() -> None:
    client = _NonPrefixStableClient(_sample_with_letters({"A": -0.5}))
    with pytest.raises(VLLMAdapterError, match="not prefix-stable"):
        resolve_letter_token_ids(client, model="m", base_prompt="xxxAnswer:", letters="A")


# --- pure functions: the projection (None-sentinel, non-finite guard, serialization) ----------------
def test_project_answer_letters_present_and_censored() -> None:
    sample = _sample_with_letters({"A": -0.5, "B": -1.5})  # C's token-id absent from top-N
    proj = project_answer_letters(sample, {"A": 2001, "B": 2002, "C": 2003})
    assert proj == {"A": -0.5, "B": -1.5, "C": None}
    assert count_censored(proj) == 1
    assert (
        project_to_value_json(proj) == '{"A":-0.5,"B":-1.5,"C":null}'
    )  # sorted keys, null (never -Infinity)


def test_project_raises_on_non_finite_logprob() -> None:
    sample = LogprobSample(prompt="p", generated_token_id=2001, top_logprobs=((2001, float("-inf")),))
    with pytest.raises(VLLMAdapterError, match="non-finite"):
        project_answer_letters(sample, {"A": 2001})


# --- DB: the metric_keys mint (fail-loud seed) ------------------------------------------------------
@pytest.mark.pg
def test_seed_metric_key_idempotent_then_raises_on_divergence(repo) -> None:
    got = repo.seed_metric_key(metric_key="m1", value_kind="json", description="d")
    assert got == "m1"
    assert repo.seed_metric_key(metric_key="m1", value_kind="json", description="d") == "m1"  # adopt
    with pytest.raises(IdentityMismatchError, match="different value_kind/description"):
        repo.seed_metric_key(metric_key="m1", value_kind="scalar", description="d")


# --- DB: the first run_metrics writer (raise-on-drift + the 8 KB valve) ------------------------------
@pytest.mark.pg
def test_write_run_metric_adopt_drift_and_valve(repo, seeded) -> None:
    run_id = seeded["run_id"]
    repo.seed_metric_key(metric_key="k", value_kind="json", description="d")
    rm_id = repo.write_run_metric(run_id=run_id, metric_key="k", value_json='{"A":-0.5}')
    assert repo.write_run_metric(run_id=run_id, metric_key="k", value_json='{"A":-0.5}') == rm_id  # adopt
    with pytest.raises(IdentityMismatchError, match="different"):
        repo.write_run_metric(run_id=run_id, metric_key="k", value_json='{"A":-9.9}')
    # the 8 KB run_metrics_valve (ADR-0017) rejects an oversized payload at the DB
    repo.seed_metric_key(metric_key="big", value_kind="json", description="d")
    with pytest.raises(IntegrityError, match="run_metrics_valve"):
        repo.write_run_metric(run_id=run_id, metric_key="big", value_json='{"x":"' + "z" * 9000 + '"}')


@pytest.mark.pg
def test_write_answer_letter_projection_records_censored(repo, seeded) -> None:
    """The projection writer end to end: a letter absent from the sample -> null, counted as censored."""
    run_id = seeded["run_id"]
    register_answer_letter_method(repo)  # seeds the version-bearing metric_key FK target
    sample = _sample_with_letters({"A": -0.5, "B": -1.5})  # C's token-id is absent from the top-N
    rm_id, censored = write_answer_letter_projection(
        repo, run_id=run_id, sample=sample, letter_token_ids={"A": 2001, "B": 2002, "C": 2003}
    )
    assert censored == 1
    with repo.engine.connect() as conn:
        vj = conn.execute(
            text("SELECT value_json FROM neuro.run_metrics WHERE run_metric_id = :i"), {"i": rm_id}
        ).scalar_one()
    assert vj == '{"A":-0.5,"B":-1.5,"C":null}'


# --- DB: the versioned method registration (once; code_sha parity) ----------------------------------
@pytest.mark.pg
def test_register_answer_letter_method_registers_and_seeds(repo) -> None:
    # RESTART-IDENTITY decoy discipline: a decoy method_version first so the real mv_id is NOT blindly 1.
    decoy = repo.register_method_version(
        method_key="decoy-method", semver="0.0.1", code_sha=b"\x07" * 32, set_active=False
    )
    mv_id = register_answer_letter_method(repo)
    assert mv_id != decoy and mv_id >= 2  # a real id, distinct from the decoy (not a blind PK=1)
    with repo.engine.connect() as conn:
        vk = conn.execute(
            text("SELECT value_kind FROM neuro.metric_keys WHERE metric_key = :k"),
            {"k": ANSWER_LETTER_METRIC_KEY},
        ).scalar_one()
        n_methods = conn.execute(
            text(
                "SELECT count(*) FROM neuro.method_versions mv JOIN neuro.methods m "
                "ON m.method_id = mv.method_id WHERE m.method_key = 'answer_letter_logprobs'"
            )
        ).scalar_one()
    assert vk == ANSWER_LETTER_LOGPROBS_V1.value_kind == "json"
    assert n_methods == 1
    assert register_answer_letter_method(repo) == mv_id  # idempotent


@pytest.mark.pg
def test_method_code_sha_drift_raises(repo) -> None:
    # A prior registration at the SAME semver with a DIFFERENT code_sha must make register-first raise.
    repo.register_method_version(
        method_key="answer_letter_logprobs", semver="1.0.0", code_sha=b"\x01" * 32, set_active=False
    )
    assert answer_letter_method_code_sha() != b"\x01" * 32
    with pytest.raises(IdentityMismatchError, match="different code_sha"):
        register_answer_letter_method(repo)


# --- DB: the full runner loop (GPU-free) — the integration test -------------------------------------
@pytest.mark.pg
def test_run_campaign_full_loop(repo, tmp_path) -> None:
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    # De-blinded per-question counts: q1 (k=2) -> 2 perms, q2 (k=3) -> 6 perms (2 != 6), total 8.
    questions = [
        EstelaQuestion(uid="q1", stem="S1", choices=("optx", "opty"), num_choices=2),
        EstelaQuestion(uid="q2", stem="S2", choices=("p1", "p2", "p3"), num_choices=3),
    ]
    client = _FakeCampaignClient(_sample_with_letters({"A": -0.5, "B": -1.5, "C": -2.5}))
    result = run_campaign(
        repo=repo,
        backend=backend,
        backend_id=backend_id,
        client=client,
        tokenizer_hash=b"T",
        questions=questions,
        hf_repo="r",
        hf_revision="rev",
        corpus_commit="158a8c3-test",
        expected_lane="test",
        n_logprobs=64,
        origin="test",
    )
    assert result.permutations_captured == 8
    assert result.questions_captured == 2
    assert result.censored_cells_total == 0  # A/B/C all present in the sample
    assert result.dropped_duplicate_option_uids == ()

    with repo.engine.connect() as conn:
        counts = (
            conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM neuro.runs) AS runs, "
                    "(SELECT count(DISTINCT fingerprint_id) FROM neuro.runs) AS fps, "
                    "(SELECT count(*) FROM neuro.capture_events) AS events, "
                    "(SELECT count(*) FROM neuro.run_metrics) AS metrics, "
                    "(SELECT count(*) FROM neuro.runs WHERE work_slug = 'q1') AS q1, "
                    "(SELECT count(*) FROM neuro.runs WHERE work_slug = 'q2') AS q2, "
                    "(SELECT count(*) FROM neuro.campaigns WHERE campaign_key = :ck) AS campaigns"
                ),
                {"ck": CAMPAIGN_KEY},
            )
            .mappings()
            .one()
        )
        # a POSITIVE projection assert on ONE specific run — ties run_id -> its own value_json (not just a count)
        q1_perm0_json = conn.execute(
            text(
                "SELECT rm.value_json FROM neuro.run_metrics rm JOIN neuro.runs r ON r.run_id = rm.run_id "
                "WHERE r.work_slug = 'q1' AND r.variant_digest = '0' AND rm.metric_key = :mk"
            ),
            {"mk": ANSWER_LETTER_METRIC_KEY},
        ).scalar_one()

    assert counts["runs"] == 8 and counts["events"] == 8 and counts["metrics"] == 8
    assert counts["fps"] == 8  # every permutation -> a distinct prompt -> a distinct fingerprint
    assert counts["q1"] == 2 and counts["q2"] == 6  # de-blinded per-question grouping
    assert counts["campaigns"] == 1  # one campaign for the whole sweep
    # q1 is k=2 -> only A/B projected (C excluded by the sub-map); values from the fixed sample.
    assert q1_perm0_json == '{"A":-0.5,"B":-1.5}'
