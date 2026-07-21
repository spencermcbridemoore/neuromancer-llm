"""Unit estela-unit-d — db/position_bias_report.py (the SECOND read module) + `neuro runs position-bias`.

Probe classes, matching the runs-show precedent and extending it:
* PURE measure math on hand-built projections — no DB, so the arithmetic is mutation-verifiable on its own.
  ★ The flagship is the PERMUTATION INVERSION probe: it uses a 3-CYCLE, because for an involution
  ``perm[i] == perm.index(i)`` and a forward/inverse transposition would go SILENTLY GREEN.
* The TIE RULE, censored cells, and holes — each pinned, none assumed unreachable.
* Corpus probes: scope comes from the campaign's OWN filters, the `*_shuffled` trap, the key cross-check.
* PAYLOAD DISCIPLINE: the metric_key allowlist refuses a foreign key BEFORE any DB is opened, and a structural
  scan pins that no capture_events payload column is ever selected.
* pg end-to-end + CliRunner, with EVERY filter de-blinded (precedent E-18): a second campaign AND a second
  metric_key, each with a DIFFERENT count, so dropping either filter reddens.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from neuromancer_llm.capture.answer_letter import ANSWER_LETTER_METRIC_KEY
from neuromancer_llm.capture.campaign import enumerate_permutations
from neuromancer_llm.cli.runs import app as runs_app
from neuromancer_llm.db.position_bias_report import (
    PositionBiasError,
    argmax_letter,
    build_position_bias_report,
    read_answer_keys,
    score_question,
)

_runner = CliRunner()

# The 3-cycle used by the inversion flagship: perm[0]=1 but perm.index(0)=2, so a forward/inverse transposition
# CHANGES the answer. (An identity or a single swap is an involution and would hide the mutation.)
_THREE_CYCLE = (1, 2, 0)
_THREE_CYCLE_INDEX = enumerate_permutations(3).index(_THREE_CYCLE)


def _proj(k: int, best_letter: int, *, base: float = -5.0, top: float = -0.5) -> dict[str, float | None]:
    """A k-entry projection whose argmax is `best_letter`, with distinct non-tied fillers."""
    return {chr(ord("A") + i): (top if i == best_letter else base - i) for i in range(k)}


# --- the permutation inversion: THE acceptance criterion -------------------------------------------
def test_three_cycle_actually_distinguishes_forward_from_inverse():
    """The fixture's own de-blinding: if this ever became an involution the flagship below would pass under a
    transposed implementation, so the property the probe RELIES ON is asserted first."""
    assert _THREE_CYCLE[0] != _THREE_CYCLE.index(0)
    assert _THREE_CYCLE[1] != _THREE_CYCLE.index(1)


def test_letter_maps_to_source_option_by_forward_index():
    """Letter A under perm (1,2,0) displayed SOURCE OPTION 1 (perm[0]), not option 2 (perm.index(0)).
    ★ Transposing perm[i] <-> perm.index(i) reddens this."""
    q = score_question("uid-1", [(_THREE_CYCLE_INDEX, _proj(3, 0))])
    assert q.letter_counts == (1, 0, 0)
    assert q.source_counts == (0, 1, 0)  # option 1 — the FORWARD index


def test_answer_position_uses_the_true_inverse():
    """The correct answer (source option 0) was DISPLAYED at letter perm.index(0) = 2, not perm[0] = 1.
    ★ Transposing reddens this in the other direction."""
    q = score_question("uid-1", [(_THREE_CYCLE_INDEX, _proj(3, 0))], answer_index=0)
    assert q.scored_by_position == (0, 0, 1)
    assert q.correct_count == 0  # the model picked option 1, the key is option 0


def test_correct_pick_is_credited_at_the_key_position():
    q = score_question("uid-1", [(_THREE_CYCLE_INDEX, _proj(3, 0))], answer_index=1)
    assert q.correct_count == 1
    assert q.correct_by_position == (1, 0, 0)  # option 1 was displayed at letter perm.index(1) = 0
    assert q.accuracy == 1.0


# --- the two orthogonal statistics, against their EXACT nulls --------------------------------------
def _all_perms_picking_source(k: int, source: int) -> list[tuple[int, dict[str, float | None]]]:
    """Perfect content-tracking: for every permutation, the model picks whichever letter shows `source`."""
    return [(i, _proj(k, perm.index(source))) for i, perm in enumerate(enumerate_permutations(k))]


def _all_perms_picking_letter(k: int, letter: int) -> list[tuple[int, dict[str, float | None]]]:
    """Pure position bias: the model always picks the same LETTER, whatever it shows."""
    return [(i, _proj(k, letter)) for i, _perm in enumerate(enumerate_permutations(k))]


@pytest.mark.parametrize("k", [3, 4])
def test_perfect_content_tracking_gives_exactly_uniform_letters(k):
    """★ THE NULL IS EXACT, NOT SIMULATED: complete enumeration puts each source option at each letter exactly
    (k-1)! times, so perfect content-tracking makes the letter marginal EXACTLY uniform."""
    q = score_question("uid-1", _all_perms_picking_source(k, source=k - 1))
    assert q.content_consistency == 1.0
    assert q.position_concentration == pytest.approx(1.0 / k)
    assert set(q.letter_counts) == {q.scored_count // k}  # every letter identical — no sampling noise


@pytest.mark.parametrize("k", [3, 4])
def test_pure_position_bias_is_the_mirror_image(k):
    q = score_question("uid-1", _all_perms_picking_letter(k, letter=0))
    assert q.position_concentration == 1.0
    assert q.content_consistency == pytest.approx(1.0 / k)


def test_position_bias_lowers_accuracy_only_off_the_favoured_letter():
    """A pure A-picker is right exactly when the key happens to sit at A — the accuracy-by-position readout."""
    k = 3
    q = score_question("uid-1", _all_perms_picking_letter(k, letter=0), answer_index=2)
    assert q.accuracy == pytest.approx(1.0 / k)
    assert q.correct_by_position == (2, 0, 0)  # only when option 2 was displayed at letter A
    assert q.scored_by_position == (2, 2, 2)


# --- the TIE RULE, censoring, holes ----------------------------------------------------------------
def test_exact_tie_is_excluded_and_counted_never_broken():
    """★ THE TIE RULE. A letter-ordered tie-break would inject position bias into a position-bias measure."""
    tied: dict[str, float | None] = {"A": -0.5, "B": -0.5, "C": -3.0}
    assert argmax_letter(tied).reason == "tie"
    q = score_question("uid-1", [(0, tied)])
    assert q.tie_count == 1
    assert q.scored_count == 0
    assert q.letter_counts == (0, 0, 0)  # the tie contributed to NO letter — not silently to A
    assert q.position_concentration is None  # never a fabricated 0.0


def test_letter_index_is_alphabetical_never_insertion_order():
    """The letter INDEX is the position, so it must come from the sorted letter set — not from whatever order
    the JSON object happened to serialise in. Dropping the sort reddens this."""
    proj: dict[str, float | None] = {"C": -3.0, "A": -0.5, "B": -5.0}  # deliberately NOT letter-ordered
    assert argmax_letter(proj).letter_index == 0  # A


def test_censored_letter_cannot_be_the_argmax_but_the_rest_still_scores():
    """A JSON null fell below the retained top-N floor, hence below every retained letter."""
    proj: dict[str, float | None] = {"A": None, "B": -0.5, "C": -3.0}
    q = score_question("uid-1", [(0, proj)])
    assert q.censored_cell_count == 1
    assert q.scored_count == 1
    assert q.letter_counts == (0, 1, 0)


def test_all_letters_censored_is_unscoreable_not_a_guess():
    proj: dict[str, float | None] = {"A": None, "B": None, "C": None}
    assert argmax_letter(proj).reason == "all-censored"
    q = score_question("uid-1", [(0, proj)])
    assert q.unscoreable_count == 1
    assert q.scored_count == 0


def test_a_missing_permutation_surfaces_as_an_honest_hole():
    q = score_question("uid-1", [(0, _proj(3, 0)), (1, _proj(3, 1))])
    assert q.perm_count == 2
    assert q.expected_perm_count == 6  # 3! — the shortfall is visible, not hidden


def test_disagreeing_k_raises():
    with pytest.raises(PositionBiasError, match="disagree on k"):
        score_question("uid-1", [(0, _proj(3, 0)), (1, _proj(4, 1))])


def test_perm_index_out_of_range_raises():
    with pytest.raises(PositionBiasError, match="out of range"):
        score_question("uid-1", [(999, _proj(3, 0))])


def test_answer_index_out_of_range_raises():
    with pytest.raises(PositionBiasError, match="answer_index"):
        score_question("uid-1", [(0, _proj(3, 0))], answer_index=7)


# --- payload discipline ----------------------------------------------------------------------------
def test_foreign_metric_key_is_refused_before_any_db_is_opened():
    """★ The fail-closed allowlist. `engine` is None: if the guard did not run FIRST this would AttributeError
    instead of raising PositionBiasError, so the probe pins the ORDER as well as the refusal."""
    with pytest.raises(PositionBiasError, match="outside the"):
        build_position_bias_report(
            cast("Engine", None), campaign_key="c", metric_key="capture_request_text@1.0.0"
        )


def test_module_never_selects_a_capture_event_payload_column():
    """Structural pin: the wire text (EXAM_RESTRICTED) must appear in NO select list in this module."""
    src = Path("src/neuromancer_llm/db/position_bias_report.py").read_text(encoding="utf-8")
    sql_region = src.split('_PROJECTIONS_SQL = """')[1]
    assert "request_text" not in sql_region
    assert "response_text" not in sql_region
    assert "capture_events" not in sql_region


# --- the corpus join -------------------------------------------------------------------------------
def _corpus_line(
    uid,
    choices,
    answer_index,
    *,
    has_figure=False,
    shuffled=True,
    answer_text=None,
    omit_answer_index=False,
    omit_answer_text=False,
):
    rec = {
        "uid": uid,
        "question": f"stem for {uid}",
        "choices": list(choices),
        "num_choices": len(choices),
        "has_figure": has_figure,
        "answer_index": answer_index,
        "answer_text": choices[answer_index] if answer_text is None else answer_text,
    }
    if omit_answer_index:
        del rec["answer_index"]
    if omit_answer_text:
        del rec["answer_text"]
    if shuffled:  # the corpus's OWN de-biased shuffle — a DIFFERENT order the campaign never read
        rec |= {
            "shuffle_perm": list(reversed(range(len(choices)))),
            "choices_shuffled": list(reversed(choices)),
            "answer_index_shuffled": len(choices) - 1 - answer_index,
            "answer_label_shuffled": "Z",
        }
    return json.dumps(rec)


def _write_corpus(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "estela.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_answer_keys_read_source_order_never_the_shuffled_fields(tmp_path):
    """⚠ THE `*_shuffled` TRAP: the key must address `choices`, not `choices_shuffled`."""
    path = _write_corpus(tmp_path, [_corpus_line("q1", ["w", "x", "y", "z"], 1)])
    keys = read_answer_keys(path)
    answer_index, answer_text, choices = keys["q1"]
    assert (answer_index, answer_text) == (1, "x")
    assert choices == ("w", "x", "y", "z")  # source order, NOT reversed


def test_key_cross_check_flags_a_mismatch_and_withholds_that_accuracy(tmp_path, monkeypatch):
    from neuromancer_llm.db import position_bias_report as mod

    path = _write_corpus(
        tmp_path,
        [
            _corpus_line("q1", ["w", "x", "y", "z"], 1),
            _corpus_line("q2", ["a", "b", "c", "d"], 1, answer_text="WRONG"),
        ],
    )
    validation, keys = mod._validate_corpus(path, {"q1", "q2"}, max_k=5)
    assert validation.answer_key_mismatch_uids == ("q2",)
    assert "q1" in keys and "q2" not in keys  # a mismatched key yields NO accuracy rather than a wrong one


def test_scope_comes_from_the_campaigns_own_filters(tmp_path):
    """A figure question and a duplicate-option question must fall out of scope through
    read_estela_jsonl / partition_campaign_questions — never a filter re-implemented here."""
    from neuromancer_llm.db import position_bias_report as mod

    path = _write_corpus(
        tmp_path,
        [
            _corpus_line("keep", ["w", "x", "y", "z"], 0),
            _corpus_line("figure", ["w", "x", "y", "z"], 0, has_figure=True),
            _corpus_line("dupe", ["w", "w", "y", "z"], 2),
        ],
    )
    validation, keys = mod._validate_corpus(path, {"keep"}, max_k=5)
    assert validation.corpus_scope_count == 1
    assert validation.dropped_duplicate_option_uids == ("dupe",)
    assert set(keys) == {"keep"}
    assert validation.uids_in_corpus_not_db == ()
    assert validation.uids_in_db_not_corpus == ()


def test_coverage_gaps_are_reported_in_both_directions(tmp_path):
    from neuromancer_llm.db import position_bias_report as mod

    path = _write_corpus(tmp_path, [_corpus_line("in-corpus-only", ["w", "x", "y", "z"], 0)])
    validation, _keys = mod._validate_corpus(path, {"in-db-only"}, max_k=5)
    assert validation.uids_in_corpus_not_db == ("in-corpus-only",)
    assert validation.uids_in_db_not_corpus == ("in-db-only",)


def test_a_keyless_record_is_reported_not_silently_dropped(tmp_path):
    """A record with no `answer_index` used to reach NO outlet: it was counted in corpus_scope_count, appeared
    in neither set-difference nor the mismatch list, and silently shrank the accuracy denominator."""
    from neuromancer_llm.db import position_bias_report as mod

    path = _write_corpus(
        tmp_path,
        [
            _corpus_line("q1", ["w", "x", "y", "z"], 1),
            _corpus_line("q2", ["a", "b", "c", "d"], 0, omit_answer_index=True),
        ],
    )
    validation, keys = mod._validate_corpus(path, {"q1", "q2"}, max_k=5)
    assert validation.uids_without_answer_key == ("q2",)
    assert set(keys) == {"q1"}


def test_absent_answer_text_withholds_accuracy_rather_than_skipping_the_cross_check(tmp_path):
    """★ The conjunct-guard fold. `if answer_text and answer_text != ...` silently ACCEPTED a record with no
    answer_text — the *_shuffled cross-check never ran, yet the render printed a green tick and the key was
    used. Both asymmetric-NULL cases are now probed, and an uncheckable key yields NO accuracy."""
    from neuromancer_llm.db import position_bias_report as mod

    path = _write_corpus(tmp_path, [_corpus_line("q1", ["a", "b", "c", "d"], 3, omit_answer_text=True)])
    validation, keys = mod._validate_corpus(path, {"q1"}, max_k=5)
    assert validation.uids_without_answer_text == ("q1",)
    assert validation.answer_key_mismatch_uids == ()  # it did not FAIL the check — it could not RUN it
    assert keys == {}  # accuracy withheld


def test_corpus_arity_must_match_the_captured_arity(tmp_path):
    """The corpus file is deliberately NOT hashed (CRLF/LF), so arity is the structural defence against a
    DIFFERENT but internally-consistent export whose uids happen to overlap."""
    from neuromancer_llm.db import position_bias_report as mod

    path = _write_corpus(tmp_path, [_corpus_line("q1", ["a", "b", "c", "d"], 1)])
    ok, keys_ok = mod._validate_corpus(path, {"q1"}, max_k=5, db_k_by_uid={"q1": 4})
    assert ok.arity_mismatch_uids == () and set(keys_ok) == {"q1"}
    bad, keys_bad = mod._validate_corpus(path, {"q1"}, max_k=5, db_k_by_uid={"q1": 5})
    assert bad.arity_mismatch_uids == ("q1",)
    assert keys_bad == {}  # a drifted corpus must not yield accuracy


def test_perfect_content_tracker_is_not_flagged_when_a_tie_reduced_its_denominator():
    """★ `max(letter_counts)/n` is bounded below by ceil(n/k)/n, which EXCEEDS 1/k whenever n % k != 0. So a
    bare `> 1/k` test flags a perfect content-tracker the moment one permutation is excluded — by this
    module's own tie rule. The flag must compare against the ATTAINABLE floor."""
    from neuromancer_llm.db import position_bias_report as mod

    k = 3
    projections = _all_perms_picking_source(k, source=0)
    projections[0] = (projections[0][0], {"A": -0.5, "B": -0.5, "C": -3.0})  # one exact tie -> excluded
    q = score_question("uid-1", projections)
    assert q.tie_count == 1 and q.scored_count == 5  # 6 - 1, so n % k != 0
    assert (q.position_concentration or 0.0) > 1.0 / k  # the naive test WOULD have fired
    assert mod._exceeds_attainable_floor(q) is False  # the attainable-floor test does not
    agg = mod._aggregate_by_k((q,))[0]
    assert agg.questions_above_position_null == 0
    assert agg.tie_count == 1  # and the reduction is DISCLOSED, not hidden


# --- pg end-to-end ---------------------------------------------------------------------------------
def _seed_campaign(repo, *, campaign_key: str, uid: str, k: int, letter: int, n_perms: int, metric_key: str):
    """Seed one campaign's runs + projections. Returns the campaign_id."""
    actor_id = repo.create_actor(f"owner:{campaign_key}", kind="human")
    campaign_id = repo.create_campaign(campaign_key, actor_id)
    repo.seed_metric_key(metric_key=metric_key, value_kind="json", description="test projection")
    for perm_index in range(n_perms):
        run_id = repo.create_run(
            f"{campaign_key}/{uid}/{perm_index}",
            campaign_id=campaign_id,
            work_slug=uid,
            variant_digest=str(perm_index),
            actor_id=actor_id,
        )
        repo.write_run_metric(run_id=run_id, metric_key=metric_key, value_json=json.dumps(_proj(k, letter)))
    return campaign_id


@pytest.mark.pg
def test_end_to_end_report_over_seeded_projections(repo):
    _seed_campaign(
        repo, campaign_key="pb-main", uid="q1", k=3, letter=0, n_perms=6, metric_key=ANSWER_LETTER_METRIC_KEY
    )
    report = build_position_bias_report(repo.engine, campaign_key="pb-main", lane="test")
    assert report.question_count == 1
    assert report.run_count == 6
    assert report.scored_count == 6
    assert report.tie_count == 0
    q = report.questions[0]
    assert q.uid == "q1" and q.k == 3
    assert q.position_concentration == 1.0  # always letter A -> pure position bias
    assert q.content_consistency == pytest.approx(1.0 / 3)
    agg = report.by_k[0]
    assert agg.k == 3 and agg.null_share == pytest.approx(1.0 / 3)
    assert agg.pooled_letter_shares[0] == 1.0
    assert report.corpus is None  # no corpus supplied -> no accuracy, honestly absent
    assert q.accuracy is None


@pytest.mark.pg
def test_both_filters_de_blinded_by_a_second_campaign_and_a_second_metric(repo):
    """★ Precedent E-18: give the decoys DIFFERENT counts on BOTH filters, so dropping EITHER the campaign_key
    or the metric_key predicate changes the answer and reddens."""
    _seed_campaign(
        repo, campaign_key="pb-main", uid="q1", k=3, letter=0, n_perms=6, metric_key=ANSWER_LETTER_METRIC_KEY
    )
    # decoy 1: a DIFFERENT campaign, different uid, different perm count (2, not 6)
    _seed_campaign(
        repo, campaign_key="pb-decoy", uid="zz", k=3, letter=1, n_perms=2, metric_key=ANSWER_LETTER_METRIC_KEY
    )
    # decoy 2: the SAME campaign, a DIFFERENT metric_key on brand-new runs (3, not 6)
    other_key = "answer_letter_logprobs@9.9.9"
    repo.seed_metric_key(metric_key=other_key, value_kind="json", description="decoy version")
    actor_id = repo.create_actor("owner:extra", kind="human")
    with repo.engine.connect() as conn:
        campaign_id = conn.execute(
            text("SELECT campaign_id FROM neuro.campaigns WHERE campaign_key = 'pb-main'")
        ).scalar_one()
    for perm_index in range(3):
        run_id = repo.create_run(
            f"pb-main/other/{perm_index}",
            campaign_id=campaign_id,
            work_slug="other",
            variant_digest=str(perm_index),
            actor_id=actor_id,
        )
        repo.write_run_metric(run_id=run_id, metric_key=other_key, value_json=json.dumps(_proj(3, 2)))

    report = build_position_bias_report(repo.engine, campaign_key="pb-main", lane="test")
    assert report.run_count == 6  # not 8 (campaign filter), not 9 (metric filter), not 11 (neither)
    assert [q.uid for q in report.questions] == ["q1"]


@pytest.mark.pg
def test_k4_and_k5_are_aggregated_separately_never_pooled(repo):
    """★ Acceptance criterion #4. 24 vs 120 permutations is a RESOLUTION difference, so the arities must not be
    pooled — and the report must carry BOTH. (This probe exists because the mutation matrix found the criterion
    UNPINNED: with only one k in every fixture, truncating the per-k loop changed nothing.)"""
    campaign_id = _seed_campaign(
        repo, campaign_key="pb-mixed", uid="q3", k=3, letter=0, n_perms=6, metric_key=ANSWER_LETTER_METRIC_KEY
    )
    actor_id = repo.create_actor("owner:k4", kind="human")
    for perm_index in range(4):  # a DIFFERENT count from the k=3 question, so a mix-up is visible
        run_id = repo.create_run(
            f"pb-mixed/q4/{perm_index}",
            campaign_id=campaign_id,
            work_slug="q4",
            variant_digest=str(perm_index),
            actor_id=actor_id,
        )
        repo.write_run_metric(
            run_id=run_id, metric_key=ANSWER_LETTER_METRIC_KEY, value_json=json.dumps(_proj(4, 1))
        )

    report = build_position_bias_report(repo.engine, campaign_key="pb-mixed", lane="test")
    assert [a.k for a in report.by_k] == [3, 4]
    k3, k4 = report.by_k
    assert (k3.question_count, k3.scored_count) == (1, 6)
    assert (k4.question_count, k4.scored_count) == (1, 4)
    assert k3.null_share == pytest.approx(1 / 3)
    assert k4.null_share == pytest.approx(1 / 4)
    assert len(k3.pooled_letter_shares) == 3
    assert len(k4.pooled_letter_shares) == 4
    assert k4.pooled_letter_shares[1] == 1.0  # the k=4 question always picked letter B


@pytest.mark.pg
def test_each_row_is_scored_under_its_own_variant_digest(repo):
    """★ DE-BLINDS THE SQL -> perm_index BINDING (precedent 18). Every other pg fixture writes the SAME
    projection to every permutation, under which `source_counts` is uniform for ANY bijection of perm_index
    onto rows — so no assertion could tell 'scored under the variant_digest on ITS OWN run' from 'scored under
    some other permutation'. Here each row gets the projection of a PERFECT CONTENT-TRACKER for its own
    perm_index, and content_consistency == 1.0 holds ONLY if the pairing is right."""
    k, source = 3, 2
    actor_id = repo.create_actor("owner:bind", kind="human")
    campaign_id = repo.create_campaign("pb-bind", actor_id)
    repo.seed_metric_key(metric_key=ANSWER_LETTER_METRIC_KEY, value_kind="json", description="t")
    perms = enumerate_permutations(k)
    for perm_index, perm in enumerate(perms):
        run_id = repo.create_run(
            f"pb-bind/q/{perm_index}",
            campaign_id=campaign_id,
            work_slug="q",
            variant_digest=str(perm_index),
            actor_id=actor_id,
        )
        repo.write_run_metric(
            run_id=run_id,
            metric_key=ANSWER_LETTER_METRIC_KEY,
            # the letter that shows `source` under THIS permutation — a different letter for different perms
            value_json=json.dumps(_proj(k, perm.index(source))),
        )
    report = build_position_bias_report(repo.engine, campaign_key="pb-bind", lane="test")
    q = report.questions[0]
    assert q.source_counts == (0, 0, 6)  # every scored permutation resolved to source option 2
    assert q.content_consistency == 1.0
    assert q.position_concentration == pytest.approx(1.0 / k)  # exactly uniform letters
    assert set(q.letter_counts) == {2}  # (k-1)! = 2 each


@pytest.mark.pg
def test_unknown_campaign_raises(repo):
    with pytest.raises(PositionBiasError, match="no campaign"):
        build_position_bias_report(repo.engine, campaign_key="nope", lane="test")


@pytest.mark.pg
def test_campaign_without_projections_raises(repo):
    actor_id = repo.create_actor("owner:empty", kind="human")
    repo.create_campaign("pb-empty", actor_id)
    with pytest.raises(PositionBiasError, match="no .* projections"):
        build_position_bias_report(repo.engine, campaign_key="pb-empty", lane="test")


@pytest.mark.pg
def test_cli_renders_the_measure(repo, monkeypatch):
    _seed_campaign(
        repo, campaign_key="pb-cli", uid="q1", k=3, letter=0, n_perms=6, metric_key=ANSWER_LETTER_METRIC_KEY
    )
    monkeypatch.setattr(
        "neuromancer_llm.db.session.make_verified_engine", lambda **_kw: repo.engine, raising=True
    )
    result = _runner.invoke(runs_app, ["position-bias", "--campaign-key", "pb-cli", "--lane", "test"])
    assert result.exit_code == 0, result.output
    assert "position-bias  campaign=pb-cli" in result.output
    assert "k=3" in result.output
    assert "null share (exact):        0.3333" in result.output


@pytest.mark.pg
def test_cli_fails_loud_on_unknown_campaign(repo, monkeypatch):
    monkeypatch.setattr(
        "neuromancer_llm.db.session.make_verified_engine", lambda **_kw: repo.engine, raising=True
    )
    result = _runner.invoke(runs_app, ["position-bias", "--campaign-key", "nope", "--lane", "test"])
    assert result.exit_code == 1
    assert "runs position-bias failed" in result.output
