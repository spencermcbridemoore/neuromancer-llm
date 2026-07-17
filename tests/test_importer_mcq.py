"""Rank 8b — the MCQ cascade mapper (importer/mcq.py).

HARNESS: the head-built shared `repo` fixture (conftest.py: `alembic upgrade head`), NOT the rank-4 `canonical_url`
fixture — `canonical_url` builds from the FROZEN 0001-scoped DDL, which has no `confidentiality` /
`derived_by_predecessor`, so the rank-5 mirror every probe here needs would die on UndefinedColumn. 8b has no
durability consult of its own (it inherits rank 4's once-per-batch), so there is no canonical-lane property to
exercise. The rank-8a precedent, verbatim.

The corpus is SYNTHETIC (built in tmp_path). No probe reads the real MOSART corpus: it is exam-restricted,
machine-specific and outside the repo.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from neuromancer_llm.db.identity import content_hash
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.importer.ingress import (
    Confidentiality,
    ImporterIngressError,
    SourceSystem,
    open_import_batch,
)
from neuromancer_llm.importer.mcq import (
    MCQ_RECORD_KIND,
    MCQ_SOURCE_TABLE,
    PROMPT_SET_SCHEME,
    McqMapResult,
    map_mcq_corpus,
    prompt_set_content_hash,
)
from neuromancer_llm.importer.promote import register_promotion_method

pytestmark = pytest.mark.pg

_LETTERS = ("a", "b", "c", "d", "e")


def _question(qid: str, doc: str, num: int, **over) -> dict:
    q = {
        "question_id": qid,
        "document_id": doc,
        "item_num": num,
        "stem": f"stem-{qid}",
        "correct_answer": "c",
        "has_picture": False,
        "options_are_pictures": False,
        "phantom_suspected": False,
        "percent_correct": 50,  # an extra column: the shape check is a SUBSET check, extras are tolerated
    }
    q.update({f"opt_{ell}_text": f"opt-{ell}-{qid}" for ell in _LETTERS})
    q.update(over)
    return q


def _options(qid: str, correct: str | None = "c", **over) -> list[dict]:
    rows = []
    for ell in _LETTERS:
        row = {
            "question_id": qid,
            "letter": ell,
            "option_text": f"opt-{ell}-{qid}",
            "is_correct": (ell == correct) if correct is not None else None,
        }
        row.update(over.get(ell, {}))
        rows.append(row)
    return rows


def _corpus(root: Path, questions: list[dict], options: list[dict] | None = None) -> Path:
    """Write a synthetic MOSART-shaped corpus. Options default to matching each question's own texts + key."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if options is None:
        options = []
        for q in questions:
            rows = _options(q["question_id"], q["correct_answer"])
            for r in rows:
                r["option_text"] = q[f"opt_{r['letter']}_text"]
            options += rows
    documents = [
        {"document_id": d, "title": d, "n_items_in_test": 0}
        for d in sorted({q["document_id"] for q in questions})
    ]
    root.mkdir(parents=True, exist_ok=True)
    for name, rows in (("questions", questions), ("question_options", options), ("documents", documents)):
        pq.write_table(pa.Table.from_pylist(rows), root / f"{name}.parquet")
    return root


def _run(repo, root: Path) -> McqMapResult:
    handle = open_import_batch(
        repo, source_system=SourceSystem.LOCAL_FS, confidentiality=Confidentiality.EXAM_RESTRICTED
    )
    method = register_promotion_method(repo)
    return map_mcq_corpus(repo, handle, method, corpus_root=root)


def _count(repo, table: str) -> int:
    with repo.engine.begin() as c:
        return int(c.execute(text(f"SELECT count(*) FROM neuro.{table}")).scalar_one())  # noqa: S608 — literal


def _decoys(repo) -> None:
    """Break the RESTART-IDENTITY LOCKSTEP before the flagship's real run.

    `_truncate_all` does `RESTART IDENTITY`, so the first row of EVERY table is pk=1 — and 8b advances
    prompt_items, mcq_items and promotions in LOCKSTEP (exactly one each per promoted question), so one decoy per
    table would simply re-collide at the next integer (the banked rank-8a lesson: decoys in DIFFERENT tables
    re-collide). The counts must therefore DIFFER per table: 1 set / 3 items / 2 mcq_items / 0 promotions puts the
    real run's first row on set=2, item=4, mcq=3, promotion=1 — pairwise distinct.

    This is not decoration: measured without it, the flagship's four ids were [1, 1, 1, 1] and a crossed id would
    have passed silently. Two prior ranks shipped exactly that. `mcq_items.prompt_item_id` is UNIQUE, so the decoy
    mcq count must stay strictly below the decoy item count.
    """
    with repo.engine.begin() as c:
        set_id = c.execute(
            text(
                "INSERT INTO neuro.prompt_sets (content_hash, set_kind) VALUES (:h, 'decoy') RETURNING prompt_set_id"
            ),
            {"h": b"\xde" * 32},
        ).scalar_one()
        items = [
            c.execute(
                text(
                    "INSERT INTO neuro.prompt_items (prompt_set_id, item_ordinal, prompt_text) "
                    "VALUES (:s, :o, 'decoy') RETURNING prompt_item_id"
                ),
                {"s": set_id, "o": ordinal},
            ).scalar_one()
            for ordinal in (901, 902, 903)
        ]
        for prompt_item_id in items[:2]:
            c.execute(
                text(
                    "INSERT INTO neuro.mcq_items (prompt_item_id, correct_letter, num_options) VALUES (:p, 'a', 4)"
                ),
                {"p": prompt_item_id},
            )


# ---------------------------------------------------------------- P1: the flagship


def test_maps_the_cascade_and_every_returned_id_is_pinned_against_its_row(repo, tmp_path):
    """The counts are what THIS CALL inserted, pinned against the DB by literal. Ids are asserted PAIRWISE
    DISTINCT: _truncate_all does RESTART IDENTITY, so the first row of every table is pk=1 and two consecutive
    ranks shipped probes blind to a crossed id. Measure, do not reason — see `_decoys`."""
    _decoys(repo)
    qs = [_question("d1-q1", "doc1", 1), _question("d1-q2", "doc1", 2), _question("d2-q1", "doc2", 1)]
    result = _run(repo, _corpus(tmp_path / "c", qs))

    assert result == McqMapResult(
        prompt_sets=2,
        prompt_items=3,
        mcq_items=3,
        mcq_options=15,
        external_records=3,
        promotions=3,
        refusals=(),
    )
    # totals = the _decoys rows (1 set / 3 items / 2 mcq_items) + this run's
    assert (_count(repo, "prompt_sets"), _count(repo, "prompt_items")) == (1 + 2, 3 + 3)
    assert (_count(repo, "mcq_items"), _count(repo, "mcq_options")) == (2 + 3, 15)
    assert (_count(repo, "external_records"), _count(repo, "promotions")) == (3, 3)
    # the promoted_from edges rank 8a wrote on our behalf
    assert _count(repo, "lineage_edges") == 3

    with repo.engine.begin() as c:
        rows = c.execute(
            text(
                "SELECT q.payload_jsonb->>'question_id' AS qid, s.prompt_set_id, i.prompt_item_id, i.item_ordinal, "
                "       m.mcq_item_id, m.correct_letter, m.num_options, p.promotion_id, q.external_record_id "
                "FROM neuro.promotions p "
                "JOIN neuro.external_records q ON q.external_record_id = p.external_record_id "
                "JOIN neuro.mcq_items m ON m.mcq_item_id = p.promoted_pk "
                "JOIN neuro.prompt_items i ON i.prompt_item_id = m.prompt_item_id "
                "JOIN neuro.prompt_sets s ON s.prompt_set_id = i.prompt_set_id "
                "ORDER BY qid"
            )
        ).all()
    assert [r.qid for r in rows] == ["d1-q1", "d1-q2", "d2-q1"]
    assert [r.item_ordinal for r in rows] == [1, 2, 1]  # item_ordinal IS the corpus's item_num
    assert {r.correct_letter for r in rows} == {"c"}
    assert {r.num_options for r in rows} == {5}
    # doc1's two items share a set; doc2's does not — the ruled document grain, asserted not assumed
    assert rows[0].prompt_set_id == rows[1].prompt_set_id != rows[2].prompt_set_id
    # PAIRWISE DISTINCT across every id family this probe asserts, so a crossed id reddens instead of going blind
    ids = [rows[0].prompt_set_id, rows[0].prompt_item_id, rows[0].mcq_item_id, rows[0].promotion_id]
    assert len(set(ids)) == len(ids), f"ids collided -> this probe is blind to a crossed id: {ids}"


# ---------------------------------------------------------------- P2: re-runnable


def test_rerun_over_an_unchanged_corpus_inserts_nothing(repo, tmp_path):
    """Idempotency end-to-end. The counts report rows INSERTED, so a second run is all-zero with identical
    refusals — and nothing raises."""
    qs = [_question("q1", "doc1", 1), _question("q2", "doc1", 2, has_picture=True)]
    root = _corpus(tmp_path / "c", qs)
    first = _run(repo, root)
    second = _run(repo, root)

    assert (first.prompt_sets, first.prompt_items, first.mcq_items, first.promotions) == (1, 1, 1, 1)
    assert second == McqMapResult(
        prompt_sets=0,
        prompt_items=0,
        mcq_items=0,
        mcq_options=0,
        external_records=0,
        promotions=0,
        refusals=first.refusals,
    )
    assert (_count(repo, "prompt_sets"), _count(repo, "mcq_items"), _count(repo, "promotions")) == (1, 1, 1)
    assert _count(repo, "external_records") == 2  # BOTH questions mirrored, incl. the refused one


# ---------------------------------------------------------------- P17 (F1): the CRITICAL the pass caught


def test_editing_one_stem_reimports_cleanly_and_preserves_the_old_set(repo, tmp_path):
    """★ THE FOLD-1 PROBE. Under a question-grained record this RAISED: the unchanged sibling's record was already
    bound to the old set's mcq_item, so its promotion collided on promotions' UNIQUE and drifted — AFTER committing
    the new set/item/mcq_item/options. A one-way wedge plus orphans with no promoted_from edge (the
    D1-recovery path), reading clean forever.

    Under the ruled SET-SCOPED record grain, editing one stem mints a new set AND new records AND new promotions,
    raises nothing, and leaves the old set intact as immutable history."""
    root = tmp_path / "c"
    _corpus(root, [_question("q1", "doc1", 1), _question("q2", "doc1", 2)])
    first = _run(repo, root)
    assert first.promotions == 2

    # edit ONE question's stem; q1 is untouched
    _corpus(root, [_question("q1", "doc1", 1), _question("q2", "doc1", 2, stem="stem-q2-EDITED")])
    second = _run(repo, root)  # must NOT raise

    assert second.prompt_sets == 1, "an edited document must mint a NEW set"
    assert second.prompt_items == 2, (
        "both items are re-inserted under the new set — the ruled price of immutability"
    )
    assert second.external_records == 2, "the record grain is SET-SCOPED, so the unchanged q1 re-mirrors too"
    assert second.promotions == 2, "new records -> new promotions -> no collision"
    assert (_count(repo, "prompt_sets"), _count(repo, "external_records")) == (2, 4)
    assert (_count(repo, "mcq_items"), _count(repo, "promotions")) == (4, 4)
    # every mcq_item is promoted: no orphan carrying no promotion (the fail-open half of the CRITICAL)
    with repo.engine.begin() as c:
        orphans = int(
            c.execute(
                text(
                    "SELECT count(*) FROM neuro.mcq_items m WHERE NOT EXISTS "
                    "(SELECT 1 FROM neuro.promotions p WHERE p.promoted_pk = m.mcq_item_id "
                    " AND p.promoted_kind = 'mcq_stimulus')"
                )
            ).scalar_one()
        )
    assert orphans == 0, (
        "an mcq_item with no promotion has no promoted_from edge -> its D1 grade is unrecoverable"
    )


# ---------------------------------------------------------------- P3/P4: the ruled hash


def test_content_hash_equals_the_ruled_manifest_by_literal(repo, tmp_path):
    """The hash is pinned to a hand-computed literal, not to the module's own function — otherwise the assertion is
    self-referential and a changed rule would stay green."""
    qs = [_question("q1", "doc1", 2), _question("q2", "doc1", 1)]  # deliberately out of item_num order
    _run(repo, _corpus(tmp_path / "c", qs))
    expected = content_hash({"scheme": PROMPT_SET_SCHEME, "items": [[1, "stem-q2"], [2, "stem-q1"]]})
    with repo.engine.begin() as c:
        stored = c.execute(text("SELECT content_hash FROM neuro.prompt_sets")).scalar_one()
    assert bytes(stored) == expected
    assert prompt_set_content_hash([(2, "stem-q1"), (1, "stem-q2")]) == expected  # order-independent


def test_a_refusal_changes_the_content_hash(repo, tmp_path):
    """★ §2.2: the manifest covers what is IN the set. If it covered the document's full upstream content instead,
    two genuinely different sets could collide on a UNIQUE column.

    The refusal is has_picture — an item WITH a stem — so the two runs differ ONLY by the filter. (A null-stem
    refusal would make this vacuous: such an item could never be in the manifest either way.)"""
    both = prompt_set_content_hash([(1, "stem-q1"), (2, "stem-q2")])
    _run(
        repo,
        _corpus(tmp_path / "c", [_question("q1", "doc1", 1), _question("q2", "doc1", 2, has_picture=True)]),
    )
    with repo.engine.begin() as c:
        stored = bytes(c.execute(text("SELECT content_hash FROM neuro.prompt_sets")).scalar_one())
    assert stored == prompt_set_content_hash([(1, "stem-q1")])
    assert stored != both, "a refused item must not be hashed into the set that does not contain it"


# ---------------------------------------------------------------- P19 (F3) + P11: refusal semantics


def test_refused_questions_are_still_mirrored_in_layer_1(repo, tmp_path):
    """Layer 1 is a FAITHFUL mirror of everything; only Layer 2 is selective. Mirroring only the promotable ones
    would let refused questions vanish with no trace at all."""
    qs = [_question("q1", "doc1", 1), _question("bad", "doc1", 2, has_picture=True)]
    result = _run(repo, _corpus(tmp_path / "c", qs))
    assert [r.question_id for r in result.refusals] == ["bad"]
    assert result.external_records == 2 and result.mcq_items == 1
    with repo.engine.begin() as c:
        mirrored = (
            c.execute(text("SELECT payload_jsonb->>'question_id' FROM neuro.external_records ORDER BY 1"))
            .scalars()
            .all()
        )
    assert mirrored == ["bad", "q1"], "the refused question must have a Layer-1 record"


def test_a_refusal_leaves_an_honest_ordinal_gap(repo, tmp_path):
    """item_ordinal IS the corpus's item_num, so a refusal is EVIDENCE. A re-minted 1..N would hide it."""
    qs = [
        _question("q1", "doc1", 1),
        _question("q2", "doc1", 2, phantom_suspected=True),
        _question("q3", "doc1", 3),
    ]
    _run(repo, _corpus(tmp_path / "c", qs))
    with repo.engine.begin() as c:
        ordinals = c.execute(text("SELECT item_ordinal FROM neuro.prompt_items ORDER BY 1")).scalars().all()
    assert ordinals == [1, 3], "the gap at 2 is the refusal, visible"


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"stem": None}, "stem is NULL"),
        ({"stem": "x" * 8193}, "exceeds 8192 bytes"),
        ({"has_picture": True}, "has_picture"),
        ({"options_are_pictures": True}, "options_are_pictures"),
        ({"phantom_suspected": True}, "phantom_suspected"),
        ({"correct_answer": None}, "correct_answer is NULL"),
    ],
)
def test_each_refusal_reason_fires_and_writes_no_cascade(repo, tmp_path, over, fragment):
    result = _run(repo, _corpus(tmp_path / "c", [_question("bad", "doc1", 1, **over)]))
    assert len(result.refusals) == 1 and result.refusals[0].question_id == "bad"
    assert fragment in result.refusals[0].reason
    assert (result.mcq_items, result.prompt_items, result.promotions) == (0, 0, 0)
    assert _count(repo, "mcq_items") == 0
    assert result.external_records == 1, "refused, but still mirrored"


def test_option_text_null_refuses(repo, tmp_path):
    q = _question("bad", "doc1", 1, opt_c_text=None)
    opts = _options("bad", "c")
    for r in opts:
        r["option_text"] = q[f"opt_{r['letter']}_text"]
    result = _run(repo, _corpus(tmp_path / "c", [q], opts))
    assert "option_text is NULL" in result.refusals[0].reason
    assert _count(repo, "mcq_items") == 0


# ---------------------------------------------------------------- P18 (F2): the untied answer key


def test_correct_answer_naming_no_written_option_is_refused(repo, tmp_path):
    """mcq_items.correct_letter and mcq_options.letter are bare text with NO CHECK and NO FK between them, so an
    answer key naming an absent option would read CLEAN forever. Code is the only mechanism."""
    q = _question("bad", "doc1", 1, correct_answer="z")
    result = _run(repo, _corpus(tmp_path / "c", [q], _options("bad", correct=None)))
    assert "is not one of the written option letters" in result.refusals[0].reason
    assert _count(repo, "mcq_items") == 0


def test_every_written_correct_letter_joins_to_a_written_option(repo, tmp_path):
    _run(repo, _corpus(tmp_path / "c", [_question("q1", "doc1", 1), _question("q2", "doc2", 1)]))
    with repo.engine.begin() as c:
        unjoinable = int(
            c.execute(
                text(
                    "SELECT count(*) FROM neuro.mcq_items m WHERE NOT EXISTS (SELECT 1 FROM neuro.mcq_options o "
                    "WHERE o.mcq_item_id = m.mcq_item_id AND o.letter = m.correct_letter)"
                )
            ).scalar_one()
        )
    assert unjoinable == 0


# ---------------------------------------------------------------- P12 (F4): the two answer-key sources


def test_contradicting_answer_key_sources_raise(repo, tmp_path):
    q = _question("q1", "doc1", 1, correct_answer="c")
    with pytest.raises(ImporterIngressError, match="CONTRADICTS"):
        _run(repo, _corpus(tmp_path / "c", [q], _options("q1", correct="d")))


def test_multiple_is_correct_flags_raise(repo, tmp_path):
    opts = _options("q1", correct="c")
    opts[0]["is_correct"] = True  # 'a' as well as 'c'
    with pytest.raises(ImporterIngressError, match="several right answers"):
        _run(repo, _corpus(tmp_path / "c", [_question("q1", "doc1", 1)], opts))


def test_unflagged_options_adopt_correct_answer(repo, tmp_path):
    """NULL means UNSPECIFIED (repository.py:423), not UNVERIFIABLE (:671) — adopt, do not refuse."""
    result = _run(repo, _corpus(tmp_path / "c", [_question("q1", "doc1", 1)], _options("q1", correct=None)))
    assert result.refusals == () and result.mcq_items == 1
    with repo.engine.begin() as c:
        assert c.execute(text("SELECT correct_letter FROM neuro.mcq_items")).scalar_one() == "c"


# ---------------------------------------------------------------- P7/P8: the reachable drift guards


def test_changed_answer_key_under_an_unchanged_stem_raises(repo, tmp_path):
    """REACHABLE: the manifest covers stems only, so a changed correct_answer yields the SAME hash, SAME set, SAME
    prompt_item — and a contradictory answer key on the same keyed object."""
    root = tmp_path / "c"
    _corpus(root, [_question("q1", "doc1", 1, correct_answer="c")])
    _run(repo, root)
    _corpus(root, [_question("q1", "doc1", 1, correct_answer="d")])
    with pytest.raises(IdentityMismatchError, match="contradictory answer key"):
        _run(repo, root)


def test_changed_option_text_under_an_unchanged_stem_raises(repo, tmp_path):
    root = tmp_path / "c"
    q = _question("q1", "doc1", 1)
    _corpus(root, [q])
    _run(repo, root)
    opts = _options("q1", "c")
    for r in opts:
        r["option_text"] = q[f"opt_{r['letter']}_text"]
    opts[0]["option_text"] = "CHANGED"
    _corpus(root, [q], opts)
    with pytest.raises(IdentityMismatchError, match="contradictory option"):
        _run(repo, root)


# ---------------------------------------------------------------- P6: the pre-cut index seam


def test_payload_jsonb_kind_is_populated_and_queryable(repo, tmp_path):
    """external_records_kind_idx indexes payload_jsonb->>'kind' — the one functional JSON index the bindings
    permit. Nothing had ever populated the key it indexes."""
    _run(repo, _corpus(tmp_path / "c", [_question("q1", "doc1", 1)]))
    with repo.engine.begin() as c:
        found = c.execute(
            text("SELECT count(*) FROM neuro.external_records WHERE payload_jsonb->>'kind' = :k"),
            {"k": MCQ_RECORD_KIND},
        ).scalar_one()
        table = c.execute(text("SELECT DISTINCT source_table FROM neuro.external_records")).scalar_one()
    assert int(found) == 1
    assert table == MCQ_SOURCE_TABLE


# ---------------------------------------------------------------- P13/P14: fail-closed boundaries


def test_missing_corpus_table_fails_closed(repo, tmp_path):
    root = _corpus(tmp_path / "c", [_question("q1", "doc1", 1)])
    (root / "documents.parquet").unlink()
    with pytest.raises(ImporterIngressError, match="not found"):
        _run(repo, root)
    assert _count(repo, "external_records") == 0


def test_missing_required_column_fails_closed(repo, tmp_path):
    q = _question("q1", "doc1", 1)
    options = _options(
        "q1", "c"
    )  # built BEFORE the column is dropped — the corpus, not the fixture, is malformed
    del q["correct_answer"]
    with pytest.raises(ImporterIngressError, match="missing required column"):
        _run(repo, _corpus(tmp_path / "c", [q], options))
    assert _count(repo, "external_records") == 0


def test_a_raw_handle_or_method_is_refused_before_any_write(repo, tmp_path):
    from neuromancer_llm.importer.mcq import map_mcq_corpus as m

    root = _corpus(tmp_path / "c", [_question("q1", "doc1", 1)])
    handle = open_import_batch(
        repo, source_system=SourceSystem.LOCAL_FS, confidentiality=Confidentiality.OPEN
    )
    method = register_promotion_method(repo)
    with pytest.raises(ImporterIngressError, match="import-batch token"):
        m(repo, 1, method, corpus_root=root)  # type: ignore[arg-type]
    with pytest.raises(ImporterIngressError, match="promotion-method token"):
        m(repo, handle, 7, corpus_root=root)  # type: ignore[arg-type]
    with pytest.raises(ImporterIngressError, match="derived_by_predecessor must be a bool"):
        m(repo, handle, method, corpus_root=root, derived_by_predecessor="yes")  # type: ignore[arg-type]
    assert _count(repo, "external_records") == 0


# ---------------------------------------------------------------- post-build vet folds


def test_the_whole_question_row_and_option_rows_are_mirrored(repo, tmp_path):
    """LAYER 1 IS FAITHFUL AT COLUMN GRAIN, not just row grain. The unmapped columns (psychometrics, commentary,
    ...) are not PROMOTED — but they must still be PRESERVED, or the module's own "not mapped, but preserved in
    Layer 1" claim is false and a reader chasing them finds nine keys. The post-build vet caught exactly that."""
    q = _question("q1", "doc1", 1, percent_correct=73, commentary="why-c", standard="PS-1")
    _run(repo, _corpus(tmp_path / "c", [q]))
    with repo.engine.begin() as c:
        payload = json.loads(c.execute(text("SELECT payload_text FROM neuro.external_records")).scalar_one())
    mirrored = payload["question"]
    for column, value in q.items():
        assert mirrored[column] == value, f"column {column!r} was dropped from the Layer-1 mirror"
    # the option ROWS are mirrored whole too, is_correct included (it drives a raise, so it must be recoverable)
    assert set(payload["options"]) == set(_LETTERS)
    assert payload["options"]["c"]["is_correct"] is True
    assert payload["options"]["a"]["option_text"] == "opt-a-q1"


def test_corpus_sha256_rides_in_the_sidecar(repo, tmp_path):
    """The module registers no artifact and writes no lineage edge for the corpus file, so the sidecar is the ONLY
    place the source file is named. Pinned to the real sha256 of the bytes on disk, by literal."""
    root = _corpus(tmp_path / "c", [_question("q1", "doc1", 1)])
    _run(repo, root)
    expected = hashlib.sha256((root / "questions.parquet").read_bytes()).hexdigest()
    with repo.engine.begin() as c:
        got = c.execute(
            text("SELECT payload_jsonb->>'corpus_sha256' FROM neuro.external_records")
        ).scalar_one()
    assert got == expected


def test_a_duplicate_document_id_item_num_fails_closed(repo, tmp_path):
    """The item_ordinal binding and the set manifest both rest on this uniqueness (measured 360/360). A duplicate
    would hash a manifest of TWO items while writing ONE prompt_item, then fire the drift guard against the WRONG
    object with a false diagnosis. `_require_columns` anticipates a corpus that "may grow" — so check, do not trust."""
    qs = [_question("q1", "doc1", 1), _question("q2", "doc1", 1)]  # same (document_id, item_num)
    with pytest.raises(ImporterIngressError, match=r"duplicate \(document_id, item_num\)"):
        _run(repo, _corpus(tmp_path / "c", qs))
    assert _count(repo, "external_records") == 0


def test_a_duplicate_question_id_fails_closed(repo, tmp_path):
    qs = [_question("dup", "doc1", 1), _question("dup", "doc2", 1)]
    with pytest.raises(ImporterIngressError, match="duplicate question_id"):
        _run(repo, _corpus(tmp_path / "c", qs))
    assert _count(repo, "external_records") == 0


def test_an_arity_other_than_the_written_set_is_refused(repo, tmp_path):
    """num_options is written as len(_OPTION_LETTERS), so a question whose real arity differs would be recorded
    with a FALSE count and its extra letter silently dropped. Closes the root of the F2 hole: validating the answer
    key against the CORPUS's letters would pass 'f' while 'f' is never written."""
    q = _question("q1", "doc1", 1, correct_answer="f")
    options = _options("q1", correct=None)
    options.append({"question_id": "q1", "letter": "f", "option_text": "opt-f", "is_correct": True})
    result = _run(repo, _corpus(tmp_path / "c", [q], options))
    assert "are not the written set" in result.refusals[0].reason
    assert (_count(repo, "mcq_items"), _count(repo, "mcq_options")) == (0, 0)
    assert result.external_records == 1  # refused, but still mirrored


def test_an_all_refused_document_mints_no_set(repo, tmp_path):
    """`if promotable:` is what stops a phantom itemless prompt_set. Without it every all-refused document would
    keep-first onto ONE shared set whose content_hash is the hash of the EMPTY manifest — a set claiming to
    contain nothing."""
    result = _run(repo, _corpus(tmp_path / "c", [_question("bad", "doc1", 1, has_picture=True)]))
    assert result.prompt_sets == 0 and _count(repo, "prompt_sets") == 0
    assert result.external_records == 1  # still mirrored


def test_a_resume_reports_only_what_it_inserted(repo, tmp_path):
    """De-blinds the counts. In every other probe prompt_items == mcq_items == promotions, so swapping two counter
    keys would redden NOTHING — the rank-8a "the rigor stops at the RETURN" lesson, attenuated. Here they DIFFER.

    It also pins the resume property the module claims and nothing else tests: every step is keep-first, so a crash
    between the set/items txn and the mcq txn heals on re-run rather than corrupting."""
    root = _corpus(tmp_path / "c", [_question("q1", "doc1", 1)])
    _run(repo, root)
    with repo.engine.begin() as c:  # simulate the crash window: the set + item survived, the rest did not
        c.execute(text("DELETE FROM neuro.lineage_edges"))
        c.execute(text("DELETE FROM neuro.promotions"))
        c.execute(text("DELETE FROM neuro.mcq_options"))
        c.execute(text("DELETE FROM neuro.mcq_items"))
    healed = _run(repo, root)
    assert healed == McqMapResult(
        prompt_sets=0,  # kept first
        prompt_items=0,  # kept first
        mcq_items=1,  # re-created
        mcq_options=5,  # re-created
        external_records=0,  # kept first
        promotions=1,  # re-created
        refusals=(),
    )
