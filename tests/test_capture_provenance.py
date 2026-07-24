"""Capture-provenance pair (Unit A2): manifest block-10 revival (A2a) + corpus-commit validation (A2b).

A2a — the recompute recipe (HOW to re-launch to regenerate a derived artifact) lived ONLY in prose/runbooks/
the e6_run.py driver; build_manifest was never passed recompute_recipe and hard-coded estimated_cost None, so
manifest block 10 + artifacts.recompute_recipe (phase0 Q6 / ADR-0034) were DEAD. Now the ONE pinned recipe
constant (capture/recipe.py) feeds block 10 AND artifacts.recompute_recipe, with the D5-MEASURED estimated_cost.

A2b — corpus_commit was a free string echoed to stdout, never compared to the file read (§D follow-on #3). Now
run_campaign validates the declared --corpus-commit against a repo-pinned PARSED-CONTENT digest (uid set +
choices, NEVER file bytes — the desktop-CRLF vs VM-LF trap). Persistence stays DEFERRED to the wave-2 stimulus
registry (run_inputs' one-referent CHECK has no home for a bare corpus_commit).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.bundlespec import Shard
from neuromancer_llm.bundles.manifest import build_manifest
from neuromancer_llm.capture.adapters.vllm import CapturedLogprobs, LogprobSample
from neuromancer_llm.capture.campaign import (
    PINNED_CORPUS_CONTENT,
    EstelaCampaignError,
    EstelaQuestion,
    assert_corpus_matches_pin,
    corpus_content_digest,
    read_estela_jsonl,
)
from neuromancer_llm.capture.events import capture_logprob
from neuromancer_llm.capture.recipe import RECOMPUTE_ESTIMATED_COST, RECOMPUTE_RECIPE_JSON
from neuromancer_llm.storage.backends import LocalFsBackend

PIN_COMMIT = "158a8c32248a2f4980a14075b221a78f00bbbbd7"


# ============================ A2a — manifest block-10 revival ============================
def test_recompute_recipe_json_pins_the_launch_recipe():
    """The ONE recipe constant must pin the invariant server-launch recipe (recipe-constant drift falsifier): a
    mutation to the container image / BI flag / logprobs-mode / gpu-util reddens here."""
    d = json.loads(RECOMPUTE_RECIPE_JSON)
    assert d["container_image"] == "vllm/vllm-openai:v0.23.0"
    assert d["env"]["VLLM_BATCH_INVARIANT"] == "1" and d["env"]["VLLM_USE_V2_MODEL_RUNNER"] == "0"
    assert "--logprobs-mode" in d["server_args"] and "raw_logprobs" in d["server_args"]
    assert "--gpu-memory-utilization" in d["server_args"]


def test_recompute_recipe_is_model_agnostic():
    """The recipe DELIBERATELY excludes the model identity / dtype (authoritative in manifest block 2) — one
    implementation per concept, and no latent precedent-15 lie for a capture whose per-call model/dtype differ
    from a hard-coded literal. Reddens if model/hf_revision/dtype are re-added to the constant."""
    d = json.loads(RECOMPUTE_RECIPE_JSON)
    assert "model" not in d and "hf_revision" not in d and "dtype" not in d


def test_estimated_cost_is_measured_with_units_not_none():
    """estimated_cost must be the D5-MEASURED figure with EXPLICIT units + basis — never a silent None, never a
    bare number (estimated-cost-silently-None falsifier)."""
    assert RECOMPUTE_ESTIMATED_COST is not None
    assert isinstance(RECOMPUTE_ESTIMATED_COST.get("wall_clock_seconds_per_capture"), (int, float))
    assert RECOMPUTE_ESTIMATED_COST.get("substrate")  # explicit substrate, not a bare number
    assert RECOMPUTE_ESTIMATED_COST.get("basis")  # explicit measured basis


def test_build_manifest_block10_carries_recipe_and_cost_when_passed():
    """build_manifest block 10 carries the recipe + estimated_cost when passed, and block 10 enters `populated`;
    a recipe-LESS build (the Stage-1 seam path) stays the {recipe:None, estimated_cost:None} PLACEHOLDER and is
    NOT populated — so the seam tests are unchanged (NO-CHURN for the generic register path)."""
    shards = [Shard(name="s-0000.parquet", data=b"payload")]
    m = build_manifest(
        producer="p",
        run_id=1,
        dataset_name="d",
        shards=shards,
        recompute_recipe=RECOMPUTE_RECIPE_JSON,
        estimated_cost=RECOMPUTE_ESTIMATED_COST,
    )
    assert m["recompute_recipe"] == {
        "recipe": RECOMPUTE_RECIPE_JSON,
        "estimated_cost": RECOMPUTE_ESTIMATED_COST,
    }
    assert "recompute_recipe" in m["completeness"]["populated"]

    m0 = build_manifest(producer="p", run_id=1, dataset_name="d", shards=shards)
    assert m0["recompute_recipe"] == {"recipe": None, "estimated_cost": None}
    assert "recompute_recipe" not in m0["completeness"]["populated"]


class _FakeClient:
    """GPU-free vLLM stand-in (with the A1 substrate-axis DERIVE surfaces)."""

    serving_stack = "vllm"

    def served_model(self) -> str:
        return "fake/Mistral-7B-v0.3"

    def server_version(self) -> str:
        return "0.23.0"

    def next_token_logprobs_capture(self, prompt, *, model, n_logprobs, seed) -> CapturedLogprobs:
        pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(n_logprobs))
        sample = LogprobSample(prompt=prompt, generated_token_id=1000, top_logprobs=pairs)
        req = json.dumps({"model": model, "prompt": prompt, "seed": seed}).encode("utf-8")
        top = {f"token_id:{t}": lp for t, lp in pairs}
        resp = json.dumps(
            {"choices": [{"logprobs": {"tokens": ["token_id:1000"], "top_logprobs": [top]}}]}
        ).encode("utf-8")
        return CapturedLogprobs(
            sample=sample,
            request_body=req,
            response_body=resp,
            http_status=200,
            content_type="application/json",
        )


@pytest.mark.pg
def test_capture_writes_recompute_recipe_to_artifact_and_block10(repo, tmp_path):
    """End to end: capture_logprob records the pinned recipe on artifacts.recompute_recipe (the queryable
    token_table shard) AND in manifest block 10 with the measured estimated_cost — the previously-dead phase0-Q6
    column + ADR-0034 block now carry provenance so a deleted derived artifact can be regenerated."""
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    res = capture_logprob(
        repo=repo,
        backend=backend,
        backend_id=backend_id,
        client=_FakeClient(),
        expected_lane="test",
        hf_repo="r",
        hf_revision="rev",
        dtype_quant="bf16",
        serving_stack="vllm",
        serving_version="0.23.0",
        tokenizer_hash=b"T",
        campaign_key="c",
        work_slug="slug",
        variant_digest="v1",
        actor_key="owner",
        origin="test",
        n_logprobs=20,
        seed=1234,
    )
    with repo.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT kind, recompute_recipe FROM neuro.artifacts WHERE bundle_id = :b ORDER BY artifact_id"
            ),
            {"b": res.bundle_id},
        ).all()
    token_tables = [r for r in rows if r.kind == "token_table"]
    assert token_tables, "expected a token_table artifact"
    assert (
        token_tables[0].recompute_recipe == RECOMPUTE_RECIPE_JSON
    )  # the pinned recipe is durable on the row

    manifest = json.loads(LocalFsBackend(tmp_path).get(f"{res.partition_path}/manifest.json"))
    block10 = manifest["recompute_recipe"]
    assert block10["recipe"] == RECOMPUTE_RECIPE_JSON
    assert block10["estimated_cost"] == RECOMPUTE_ESTIMATED_COST
    assert block10["estimated_cost"] is not None  # the hard-coded None is gone


# ============================ A2b — corpus-commit validation ============================
def _qs(*specs: tuple[str, list[str]]) -> list[EstelaQuestion]:
    return [EstelaQuestion(uid=u, stem="s", choices=tuple(c), num_choices=len(c)) for u, c in specs]


def test_corpus_digest_is_order_independent():
    a = _qs(("q1", ["a", "b"]), ("q2", ["c", "d"]))
    b = _qs(("q2", ["c", "d"]), ("q1", ["a", "b"]))
    assert corpus_content_digest(a) == corpus_content_digest(b)


def test_corpus_digest_over_uid_stem_and_choices():
    """The digest covers EVERY prompt-determining field — uid, stem, AND choices (the A2 vet fold: a stem-only
    edit is a real corpus drift because render_prompt_v1 embeds the stem verbatim). A changed stem OR a changed
    choice changes it; the uid alone does not carry the content."""
    a = _qs(("q1", ["a", "b"]))
    stem_edit = [EstelaQuestion(uid="q1", stem="A-DIFFERENT-STEM", choices=("a", "b"), num_choices=2)]
    assert corpus_content_digest(a) != corpus_content_digest(stem_edit)  # a stem-only edit IS caught now
    choice_edit = _qs(("q1", ["a", "DIFFERENT"]))
    assert corpus_content_digest(a) != corpus_content_digest(choice_edit)  # a changed choice too


def test_corpus_digest_is_crlf_independent(tmp_path):
    """The bytes-instead-of-parsed falsifier: a CRLF file and an LF file with identical content DIFFER in bytes
    but parse to the SAME digest — hashing file bytes (the desktop-CRLF vs VM-LF trap) would falsely redden."""
    recs = [
        {"uid": "q1", "question": "S1", "choices": ["a", "b", "c"], "num_choices": 3, "has_figure": False},
        {"uid": "q2", "question": "S2", "choices": ["x", "y"], "num_choices": 2, "has_figure": False},
    ]
    lines = [json.dumps(r) for r in recs]
    lf = tmp_path / "lf.jsonl"
    lf.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    crlf = tmp_path / "crlf.jsonl"
    crlf.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    assert lf.read_bytes() != crlf.read_bytes()  # the FILES differ in bytes
    assert corpus_content_digest(read_estela_jsonl(lf)) == corpus_content_digest(read_estela_jsonl(crlf))


def test_pin_mismatch_on_pinned_commit_raises():
    """The pin-mismatch-passes falsifier: a declared PINNED commit whose parsed content is not the pinned corpus
    RAISES (the corpus drifted from its commit)."""
    with pytest.raises(EstelaCampaignError, match="drifted"):
        assert_corpus_matches_pin(PIN_COMMIT, _qs(("q1", ["a", "b"])))  # not the real 75-question content


def test_pinned_content_under_wrong_commit_raises(monkeypatch):
    """The reverse direction: parsed content that IS a pinned corpus but declared under a DIFFERENT commit is a
    mislabeled provenance string -> RAISE."""
    qs = _qs(("q1", ["a", "b"]), ("q2", ["x", "y"]))
    monkeypatch.setitem(PINNED_CORPUS_CONTENT, "synthetic-commit", corpus_content_digest(qs))
    with pytest.raises(EstelaCampaignError, match="mislabeled"):
        assert_corpus_matches_pin("some-other-commit", qs)


def test_pinned_commit_correct_content_passes(monkeypatch):
    qs = _qs(("q1", ["a", "b"]), ("q2", ["x", "y", "z"]))
    monkeypatch.setitem(PINNED_CORPUS_CONTENT, "synthetic-commit", corpus_content_digest(qs))
    assert_corpus_matches_pin("synthetic-commit", qs)  # no raise


def test_unknown_corpus_is_allowed():
    """An unknown commit whose content matches NO pin passes (the pin is the ESTELA provenance guarantee, not a
    whitelist) — this is why the existing test_estela_campaign's '158a8c3-test' fixture keeps running."""
    assert_corpus_matches_pin("some-unpinned-commit", _qs(("q1", ["a", "b"])))  # no raise


def test_run_campaign_invokes_corpus_validation():
    """run_campaign must actually CALL assert_corpus_matches_pin (AST, not prose) — deleting the call is a
    fail-open (corpus_commit reverts to a free string echoed but never checked; §D #3), and no data-path test
    catches it (test_estela_campaign uses an unknown corpus that passes regardless). Precedent 20: a criterion
    needs a falsifying fixture."""
    import ast
    import inspect
    import textwrap

    from neuromancer_llm.capture.campaign import run_campaign

    tree = ast.parse(textwrap.dedent(inspect.getsource(run_campaign)))
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_corpus_matches_pin"
        for n in ast.walk(tree)
    ), "run_campaign must invoke assert_corpus_matches_pin(corpus_commit, questions)"


def test_production_pin_matches_the_real_corpus():
    """Belt-and-suspenders: the SHIPPED PINNED_CORPUS_CONTENT digest must equal what the real pinned corpus reads
    (guards a typo in the constant). Skips visibly where the ESTELA clone is absent (CI)."""
    import os

    default = r"C:/Users/spenc/Cursor Repos/ESTELA-physics-problem-bank/datasets/lean-text-only-mcq/estela_text_only_mcq.jsonl"
    corpus = Path(os.environ.get("NEURO_ESTELA_CORPUS", default))
    if not corpus.exists():
        pytest.skip("ESTELA corpus clone not present; skipped visibly")
    digest = corpus_content_digest(read_estela_jsonl(corpus, max_k=5))
    assert digest == PINNED_CORPUS_CONTENT[PIN_COMMIT]
