"""Rank 8a — the Layer-2 selective-promotion primitive (importer/promote.py).

HARNESS: the head-built shared `repo` fixture (conftest.py: `alembic upgrade head`), NOT the rank-4 `canonical_url`
fixture. Two reasons, both load-bearing:
  * `canonical_url` builds its DB from the FROZEN tests/reference/phase3-ddl.sql, which is 0001-scoped — it has no
    `confidentiality` / `derived_by_predecessor` columns, so a rank-5 external_record write (which every promotion
    probe needs) would die on UndefinedColumn. Only the head-built DB carries migration 0002.
  * Rank 8a has NO durability consult of its own (it inherits rank 4's once-per-batch consult), so there is no
    canonical-lane property to exercise: `expected_lane='test'` means open_import_batch skips the consult, which is
    exactly right here.
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import text

from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.importer.external_records import write_external_record
from neuromancer_llm.importer.ingress import (
    Confidentiality,
    ImportBatchHandle,
    ImporterIngressError,
    SourceSystem,
    open_import_batch,
)
from neuromancer_llm.importer.lineage import write_lineage_edge
from neuromancer_llm.importer.promote import (
    _PROMOTED_KIND_TARGET,
    PROMOTION_METHOD_KEY,
    PROMOTION_METHOD_SEMVER,
    PromotedKind,
    PromotionMethod,
    _resolve_target,
    promote_external_record,
    promotion_method_code_sha,
    register_promotion_method,
)

pytestmark = pytest.mark.pg


def _open(repo) -> ImportBatchHandle:
    return open_import_batch(repo, source_system=SourceSystem.LOCAL_FS, confidentiality=Confidentiality.OPEN)


def _record(repo, handle, *, body: bytes = b"q1") -> int:
    return write_external_record(
        repo, handle, source_table="mcq", source_bytes=body, derived_by_predecessor=False
    ).external_record_id


def _cascade(engine, *, ordinal: int, letter: str = "A") -> int:
    """Mint a REAL mcq_items row (prompt_sets -> prompt_items -> mcq_items) and return its pk.

    Hand-built raw SQL on purpose: rank 8a records that a native row was promoted; it does not mint the native row
    (that is rank 8b's mapper). The values are ARBITRARY and deliberately so — `content_hash` is any bytea,
    `set_kind` is un-CHECKed text, `prompt_has_payload` needs only `prompt_text`, `num_options` need only fall in
    2..26. Picking an arbitrary content_hash here does NOT answer or prejudge rank 8b's open question of what rule
    MINTS it from corpus content; that ruling stays wholly in 8b.
    """
    with engine.begin() as conn:
        set_id = conn.execute(
            text(
                "INSERT INTO neuro.prompt_sets (content_hash, set_kind) VALUES (:h, 'imported') RETURNING prompt_set_id"
            ),
            {"h": bytes([ordinal]) * 32},
        ).scalar_one()
        item_id = conn.execute(
            text(
                "INSERT INTO neuro.prompt_items (prompt_set_id, item_ordinal, prompt_text) "
                "VALUES (:s, :o, 'stem') RETURNING prompt_item_id"
            ),
            {"s": set_id, "o": ordinal},
        ).scalar_one()
        return conn.execute(
            text(
                "INSERT INTO neuro.mcq_items (prompt_item_id, correct_letter, num_options) "
                "VALUES (:i, :l, 4) RETURNING mcq_item_id"
            ),
            {"i": item_id, "l": letter},
        ).scalar_one()


def _decoy_record(repo, handle, i: int = 0) -> int:
    """Mint a throwaway external_record FIRST so the real record's id is >= 2 and cannot collide with the mcq_item pk.

    LOAD-BEARING. `_truncate_all` does RESTART IDENTITY on EVERY table, so external_records and mcq_items BOTH start at
    1 — and the pre-existence-guard design (where promoted_pk was an arbitrary caller integer) could dodge this by
    simply passing a different number. It cannot any more: the target-existence guard requires promoted_pk to name a
    REAL mcq_items row, and the first one is pk 1. Without this decoy, m == n == 1 and a crossed pk (passing
    external_record_id where promoted_pk belongs) would mint the SAME ref string and pass every assertion.
    """
    return write_external_record(
        repo,
        handle,
        source_table="decoy",
        source_bytes=f"decoy{i}".encode(),
        derived_by_predecessor=False,
    ).external_record_id


def _decoy_methods(repo, n: int = 2) -> None:
    """Register `n` unrelated method versions FIRST so the promotion method's id is >= n+1.

    LOAD-BEARING, not hygiene, and the COUNT is load-bearing too. `_truncate_all` does TRUNCATE ... RESTART IDENTITY,
    so every table starts at 1 and a param cross binding external_record_id into method_version_id would write a
    promotions row FK'd to a version that did NOT perform the promotion — FK satisfied, stamp silently false — and
    pass every assertion. Rank 7's post-build vet caught this exact class (a crossed-id edge passed 44/44 GREEN
    because RESTART IDENTITY made both pks 1).

    ⚠ THE COUNT WAS MEASURED, NOT REASONED. With ONE decoy method and one decoy record the ids come out m=2 / n=1 /
    mv=2 — m and mv COLLIDE and the flagship goes blind to exactly the cross it exists to catch. Each caller passes
    the count its own assertions need (the flagship needs FIVE pairwise-distinct ids and passes 4); the default of 2
    is enough for probes that only assert the stamp is not 1. The flagship's pairwise-distinctness assert is what
    keeps any of this honest if a fixture shifts.
    """
    for i in range(n):
        repo.register_method_version(
            method_key=f"decoy{i}", semver="0.0.1", code_sha=bytes([i + 1]) * 32, set_active=False
        )


def _decoy_edge(repo, handle, i: int = 0) -> int:
    """Write a throwaway lineage edge BEFORE the promotion so lineage_edge_id advances past promotion_id.

    LOAD-BEARING. `lineage_edges` is EMPTY at promote-time in every probe, so the real edge's lineage_edge_id and the
    promotion_id BOTH land on 1 — and a crossed return (swapping those two PromotionResult fields) passes every
    assertion. Both are bigint IDENTITY in DISJOINT tables, so a crossed id is type-valid, FK-plausible and reads
    clean forever: the rank-7 crossed-id defect, reproduced one rank later in the RETURN VALUE instead of the edge.
    `lineage_edges` is deliberately un-FK'd (rank 7), so an arbitrary ref is legal here.
    """
    return write_lineage_edge(
        repo, handle, edge_kind="annotates", src_entity="external_record:1", dst_entity=f"artifact:{i + 1}"
    ).lineage_edge_id


def _promotions(engine) -> list[tuple[int, str, int, int]]:
    with engine.connect() as conn:
        return [
            (r.external_record_id, r.promoted_kind, r.promoted_pk, r.method_version_id)
            for r in conn.execute(
                text(
                    "SELECT external_record_id, promoted_kind, promoted_pk, method_version_id "
                    "FROM neuro.promotions ORDER BY promotion_id"
                )
            )
        ]


def _promotion_id_of(engine, external_record_id: int, kind: str = "mcq_stimulus") -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT promotion_id FROM neuro.promotions WHERE external_record_id = :e AND promoted_kind = :k"
            ),
            {"e": external_record_id, "k": kind},
        ).scalar_one()


def _edge_id_of(engine, src_entity: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT lineage_edge_id FROM neuro.lineage_edges WHERE src_entity = :s"),
            {"s": src_entity},
        ).scalar_one()


def _edges(engine) -> list[tuple[str, str, str, int | None]]:
    with engine.connect() as conn:
        return [
            (r.edge_kind, r.src_entity, r.dst_entity, r.method_version_id)
            for r in conn.execute(
                text(
                    "SELECT edge_kind, src_entity, dst_entity, method_version_id "
                    "FROM neuro.lineage_edges WHERE edge_kind = 'promoted_from' ORDER BY lineage_edge_id"
                )
            )
        ]


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM neuro.{table}")).scalar_one()  # noqa: S608 — test-local literal


# --- the flagship ---------------------------------------------------------------------------------


def test_promotion_writes_the_row_and_the_ruled_edge(repo):
    """The first customer end to end: a promotions row + `mcq_item:N promoted_from external_record:M`.

    The DIRECTION and the KIND are pinned by LITERALS (not by re-deriving them from the code under test), so
    reversing RULE 3 or swapping the edge_kind reddens.

    ⚠ THE DISCRIMINATION IS THE PROBE. Under TRUNCATE ... RESTART IDENTITY every table starts at 1, so this probe is
    only as good as the distinctness of the ids it asserts — rank 7's flagship passed 44/44 GREEN over a crossed-id
    edge for exactly this reason. The decoys are what make the assertions discriminating, and the assert below is what
    keeps them honest: measured, the naive fixture gives m=2 / n=1 / mv=2, so m and mv COLLIDE and a crossed
    external_record_id->method_version_id binding writes the SAME tuple the probe expects."""
    # THE DECOY COUNTS ARE LOAD-BEARING AND WERE MEASURED, NOT GUESSED. Under RESTART IDENTITY every table starts at
    # 1, so the five ids this probe asserts collide unless each is spread by a different amount. Resulting values:
    #   m=4 (3 decoy records) · n=2 (1 decoy cascade) · mv=5 (4 decoy methods) · promotion_id=1 · edge_id=3 (2 decoys)
    # The pairwise-distinctness assert below is what keeps this honest if a fixture ever shifts.
    handle = _open(repo)
    _decoy_methods(repo, 4)
    for i in range(3):
        _decoy_record(repo, handle, i)
    for i in range(2):
        _decoy_edge(repo, handle, i)
    _cascade(repo.engine, ordinal=9)  # a decoy cascade so the real mcq_item is not pk 1
    m = _record(repo, handle)
    n = _cascade(repo.engine, ordinal=1)

    method = register_promotion_method(repo)
    res = promote_external_record(
        repo,
        handle,
        method,
        external_record_id=m,
        promoted_kind=PromotedKind.MCQ_STIMULUS,
        promoted_pk=n,
        note="x",
    )

    # EVERY id this probe asserts must be PAIRWISE DISTINCT or the assertions below cannot discriminate a crossed
    # binding. Measured: the naive fixture gives m=2 / n=1 / mv=2 (m and mv collide), and promotion_id ==
    # lineage_edge_id == 1 by construction (lineage_edges is empty at promote-time). The decoys break both.
    ids = {
        "external_record_id": m,
        "promoted_pk": n,
        "method_version_id": method.method_version_id,
        "promotion_id": res.promotion_id,
        "lineage_edge_id": res.lineage_edge_id,
    }
    assert len(set(ids.values())) == len(ids), (
        f"the probe cannot discriminate a crossed binding — these must be PAIRWISE DISTINCT: {ids}. "
        "Add a decoy (see _decoy_methods / _decoy_record / _decoy_edge); do NOT relax this assertion."
    )

    assert res.promoted is True
    assert _promotions(repo.engine) == [(m, "mcq_stimulus", n, method.method_version_id)]
    # The edge, by literal: src = the native row, dst = the record. `promoted_from` is passive/backward.
    assert _edges(repo.engine) == [
        ("promoted_from", f"mcq_item:{n}", f"external_record:{m}", method.method_version_id)
    ]
    # EVERY PromotionResult field pinned against ITS OWN ROW, by literal. The unit's rigor went into the WRITES; this
    # is the OUTPUT CONTRACT, and it is rank 8b's entire consumer surface. A crossed return (swapping promotion_id and
    # lineage_edge_id) is type-valid and reads clean forever — it passed 76/76 before these three lines.
    assert res.method_version_id == method.method_version_id
    assert res.promotion_id == _promotion_id_of(repo.engine, m)
    assert res.lineage_edge_id == _edge_id_of(repo.engine, f"mcq_item:{n}")


def test_edge_carries_the_method_version_and_note(repo):
    """Rank 7 DEFERRED the edge-side method_version_id probe TO RANK 8 BY NAME (test_importer_lineage.py: "THE FIRST
    CUSTOMER ALWAYS PASSES NULL. The standing obligation stands for rank 8..."). Rank 8a is that customer — the first
    non-NULL one — so the premise expires here and the pass-through is pinned. Deleting the kwarg reddens this."""
    handle = _open(repo)
    _decoy_methods(repo)
    _decoy_record(repo, handle)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    method = register_promotion_method(repo)
    promote_external_record(
        repo,
        handle,
        method,
        external_record_id=m,
        promoted_kind=PromotedKind.MCQ_STIMULUS,
        promoted_pk=n,
        note="promoted by the mcq mapper",
    )
    with repo.engine.connect() as conn:
        row = conn.execute(text("SELECT method_version_id, note FROM neuro.lineage_edges")).one()
    assert row.method_version_id == method.method_version_id
    assert row.note == "promoted by the mcq mapper"


# --- D5 by construction ---------------------------------------------------------------------------


def test_raw_string_kind_is_refused(repo):
    """4a — THE BELT. A raw str is not a PromotedKind instance, so isinstance refuses it.

    ⚠ This does NOT pin D5. `isinstance('model_identity', PromotedKind)` is False for the SAME reason
    `isinstance('mcq_stimulus', PromotedKind)` is False — a plain str is not an enum member. Re-adding a
    MODEL_IDENTITY member would leave this probe GREEN. The vocabulary itself is pinned by the next probe."""
    handle = _open(repo)
    _decoy_methods(repo)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    method = register_promotion_method(repo)
    with pytest.raises(ImporterIngressError, match="PromotedKind member"):
        promote_external_record(
            repo,
            handle,
            method,
            external_record_id=m,
            promoted_kind="mcq_stimulus",  # type: ignore[arg-type]
            promoted_pk=n,
        )
    assert _count(repo.engine, "promotions") == 0
    assert _count(repo.engine, "lineage_edges") == 0


def test_promoted_kind_vocabulary_is_d5_scoped():
    """4b — D5 ITSELF, and the watch-point deferral, as a MECHANISM. This is the probe that reddens when someone adds
    a member; the isinstance belt above cannot.

    The keyset clause is the strongest: a member added WITHOUT a target entry would make the writer raise a bare
    KeyError instead of its designed fail-closed refusal."""
    assert {k.value for k in PromotedKind} == {"mcq_stimulus"}
    assert "model_identity" not in {k.value for k in PromotedKind}, (
        "D5: no model-identity promotion in v1 — promoting a two-string predecessor identity would require 'unknown' "
        "sentinels in four NOT-NULL columns plus a FABRICATED tokenizer_hash"
    )
    assert "run_summary" not in {k.value for k in PromotedKind}, (
        "runs light the ADR-0048 layer-(b) owner-scoped-key watch-point — deferred to their own unit"
    )
    assert set(_PROMOTED_KIND_TARGET) == set(PromotedKind), (
        "every PromotedKind member needs a _PROMOTED_KIND_TARGET entry, or the writer raises a bare KeyError instead "
        "of its typed refusal"
    )
    assert _PROMOTED_KIND_TARGET[PromotedKind.MCQ_STIMULUS][0] == "mcq_item"
    with pytest.raises(
        TypeError
    ):  # FROZEN — the bridge cannot be widened at a distance (the ENTITY_KINDS idiom)
        _PROMOTED_KIND_TARGET[PromotedKind.MCQ_STIMULUS] = ("x", str)  # type: ignore[index]


def test_target_resolution_reads_the_schema_not_the_noun():
    """The existence guard resolves (table, pk) from the ORM mapper, never by string-building f"{noun}_id".

    THE DISCRIMINATOR is the whole point: `mcq_item` + "_id" == `mcq_item_id` is CORRECT BY COINCIDENCE, so with only
    McqItem in the bridge a string-built variant would pass every promotion probe in this file. `model_identities` is
    RULE 2's first discriminator — its pk is `model_id`, so string-building would produce `model_identity_id` and the
    guard would query a column that does not exist. Pinning it here converts an otherwise-unpinnable belt into a
    mechanism, before rank 8b/9 add a discriminating kind."""
    from neuromancer_llm.db.orm import JobDependency, McqItem, ModelIdentity

    assert _resolve_target(McqItem) == ("mcq_items", "mcq_item_id")
    assert _resolve_target(ModelIdentity) == ("model_identities", "model_id")
    # COMPOSITE-PK targets fail CLOSED rather than silently taking primary_key[0] — a partial-key existence check
    # would match a DIFFERENT row than the one named, re-opening the very fail-open the guard closes. JobDependency
    # (job_id + depends_on) is the real composite in this schema, and lineage.py's grammar names it inexpressible
    # under RULE 1 for the same reason.
    with pytest.raises(ImporterIngressError, match="COMPOSITE"):
        _resolve_target(JobDependency)


# --- the un-FK'd promoted_pk ----------------------------------------------------------------------


def test_promoted_pk_naming_no_live_row_is_refused(repo):
    """THE CRITICAL. promoted_pk is a bare bigint with NO FK, so nothing in the schema catches a promotion pointing at
    nothing. Without this guard the row + edge would be written and read CLEAN forever — and raise-on-drift would then
    make the lie PERMANENT (the later correct promotion collides, sees drift, and raises)."""
    handle = _open(repo)
    _decoy_methods(repo)
    m = _record(repo, handle)
    method = register_promotion_method(repo)
    with pytest.raises(ImporterIngressError, match="does not exist"):
        promote_external_record(
            repo,
            handle,
            method,
            external_record_id=m,
            promoted_kind=PromotedKind.MCQ_STIMULUS,
            promoted_pk=999999,
        )
    assert _count(repo.engine, "promotions") == 0
    assert _count(repo.engine, "lineage_edges") == 0


@pytest.mark.parametrize("bad", [0, -1, True])
def test_malformed_promoted_pk_is_refused_at_the_grammar(repo, bad):
    """RULE 1's guards are inherited from rank 7's entity_ref and fire BEFORE the existence read — so a malformed pk
    never reaches the DB at all. (`True` is the bool-is-an-int case: it would otherwise mint 'mcq_item:True'.)"""
    handle = _open(repo)
    _decoy_methods(repo)
    m = _record(repo, handle)
    method = register_promotion_method(repo)
    with pytest.raises(ImporterIngressError):
        promote_external_record(
            repo,
            handle,
            method,
            external_record_id=m,
            promoted_kind=PromotedKind.MCQ_STIMULUS,
            promoted_pk=bad,
        )
    assert _count(repo.engine, "promotions") == 0
    assert _count(repo.engine, "lineage_edges") == 0


# --- idempotency: the MIXED rule, pinned from BOTH sides + the cross-drift branch -------------------


def test_identical_re_promote_is_keep_first(repo):
    """Idempotent: one row, one edge, promoted=False the second time. Also pins that the conflict path FALLS THROUGH
    to the edge write (a crash between the two writes must be a RESUME, not a permanent missing edge)."""
    handle = _open(repo)
    _decoy_methods(repo)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    method = register_promotion_method(repo)
    kw = {"external_record_id": m, "promoted_kind": PromotedKind.MCQ_STIMULUS, "promoted_pk": n}
    first = promote_external_record(repo, handle, method, **kw)
    second = promote_external_record(repo, handle, method, **kw)
    assert first.promoted is True and second.promoted is False
    assert second.promotion_id == first.promotion_id
    assert second.lineage_edge_id == first.lineage_edge_id
    assert _count(repo.engine, "promotions") == 1
    assert _count(repo.engine, "lineage_edges") == 1


def test_promoted_pk_drift_raises(repo):
    """§7 left column: two different native rows under one (record, kind) CONTRADICT — raise, never keep-first."""
    handle = _open(repo)
    _decoy_methods(repo)
    m = _record(repo, handle)
    n1, n2 = _cascade(repo.engine, ordinal=1), _cascade(repo.engine, ordinal=2)
    method = register_promotion_method(repo)
    promote_external_record(
        repo, handle, method, external_record_id=m, promoted_kind=PromotedKind.MCQ_STIMULUS, promoted_pk=n1
    )
    with pytest.raises(IdentityMismatchError, match="already promoted"):
        promote_external_record(
            repo,
            handle,
            method,
            external_record_id=m,
            promoted_kind=PromotedKind.MCQ_STIMULUS,
            promoted_pk=n2,
        )
    assert _promotions(repo.engine) == [(m, "mcq_stimulus", n1, method.method_version_id)]
    assert _count(repo.engine, "lineage_edges") == 1


def test_method_version_drift_keeps_first(repo, monkeypatch):
    """§7 right column: a differing method_version_id is a RE-ASSERTION, not a contradiction — keep first, no raise
    (ADR-0047). Fires only ACROSS releases: within a build the module-constant method resolves to one id, so the
    semver must be bumped to reach this branch at all.

    Also pins that rank 8a did NOT 'fix' rank 7 to raise: the EDGE still names the FIRST version."""
    handle = _open(repo)
    _decoy_methods(repo)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    kw = {"external_record_id": m, "promoted_kind": PromotedKind.MCQ_STIMULUS, "promoted_pk": n}

    v1 = register_promotion_method(repo)
    promote_external_record(repo, handle, v1, **kw)

    monkeypatch.setattr("neuromancer_llm.importer.promote.PROMOTION_METHOD_SEMVER", "2.0.0")
    v2 = register_promotion_method(repo)
    assert v2.method_version_id != v1.method_version_id
    res = promote_external_record(repo, handle, v2, **kw)

    assert res.promoted is False
    assert _promotions(repo.engine) == [(m, "mcq_stimulus", n, v1.method_version_id)], (
        "keep-first on the stamp"
    )
    # THE RESULT MUST REPORT THE STAMP OF RECORD (v1), NOT THE TOKEN PASSED (v2). Echoing the token would report a
    # stamp no row carries — on precisely the branch keep-first exists for: the ordinary upgrade path, i.e. EVERY
    # already-promoted record. The re-SELECT already fetches this column; the first build read it and threw it away.
    assert res.method_version_id == v1.method_version_id, (
        "the RESULT must report the stamp of record (v1), not the token passed (v2)"
    )
    assert _edges(repo.engine)[0][3] == v1.method_version_id, (
        "rank 7 keeps the edge's method_version_id first-write-wins"
    )


def test_cross_drift_raises_on_promoted_pk_even_when_the_method_also_drifts(repo, monkeypatch):
    """THE PRECEDENCE. §7's two rules meet here, and only here. The shape

        if row.method_version_id == mv_id and row.promoted_pk != promoted_pk: raise

    passes the pk-only probe AND the method-only probe while SILENTLY keep-firsting a promoted_pk CONTRADICTION —
    the exact extra-conjunct fail-open this repo has a post-mortem for (repository.py:667-670). promoted_pk drift
    raises UNCONDITIONALLY; method_version_id is keep-first only because it never appears in the condition.

    MUTATION IS ADDITIVE: adding `and row.method_version_id == mv_id` reddens THIS probe while the others stay green.
    """
    handle = _open(repo)
    _decoy_methods(repo)
    m = _record(repo, handle)
    n1, n2 = _cascade(repo.engine, ordinal=1), _cascade(repo.engine, ordinal=2)

    v1 = register_promotion_method(repo)
    promote_external_record(
        repo, handle, v1, external_record_id=m, promoted_kind=PromotedKind.MCQ_STIMULUS, promoted_pk=n1
    )

    monkeypatch.setattr("neuromancer_llm.importer.promote.PROMOTION_METHOD_SEMVER", "2.0.0")
    v2 = register_promotion_method(repo)
    assert v2.method_version_id != v1.method_version_id
    with pytest.raises(IdentityMismatchError, match="already promoted"):
        promote_external_record(
            repo, handle, v2, external_record_id=m, promoted_kind=PromotedKind.MCQ_STIMULUS, promoted_pk=n2
        )
    assert _promotions(repo.engine) == [(m, "mcq_stimulus", n1, v1.method_version_id)]
    assert _count(repo.engine, "lineage_edges") == 1, "no second edge"


# --- the tokens -----------------------------------------------------------------------------------


def test_raw_handle_is_refused(repo):
    handle = _open(repo)
    _decoy_methods(repo)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    method = register_promotion_method(repo)
    with pytest.raises(ImporterIngressError, match="import-batch token"):
        promote_external_record(
            repo,
            handle.import_batch_id,  # type: ignore[arg-type]
            method,
            external_record_id=m,
            promoted_kind=PromotedKind.MCQ_STIMULUS,
            promoted_pk=n,
        )
    assert _count(repo.engine, "promotions") == 0


def test_raw_method_version_id_is_refused(repo):
    """Fork B: the writer never takes a raw method_version_id — the module mints the stamp."""
    handle = _open(repo)
    _decoy_methods(repo)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    method = register_promotion_method(repo)
    with pytest.raises(ImporterIngressError, match="PromotionMethod"):
        promote_external_record(
            repo,
            handle,
            method.method_version_id,  # type: ignore[arg-type]
            external_record_id=m,
            promoted_kind=PromotedKind.MCQ_STIMULUS,
            promoted_pk=n,
        )
    assert _count(repo.engine, "promotions") == 0
    assert _count(repo.engine, "lineage_edges") == 0


def test_writer_signature_takes_no_method_version_id(repo):
    """A REGRESSION LOCK, not a mutation-verified guard (and deliberately not counted as one): it pins fork B's shape
    against a future 'convenience' parameter that would reopen the caller-supplied-stamp hole."""
    params = inspect.signature(promote_external_record).parameters
    assert "method_version_id" not in params
    assert params["method"].annotation is PromotionMethod or params["method"].annotation == "PromotionMethod"


# --- the method: registered structurally, at BATCH scope ------------------------------------------


def test_method_is_registered_with_the_real_code_sha(repo):
    handle = _open(repo)
    _decoy_methods(repo)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    method = register_promotion_method(repo)
    res = promote_external_record(
        repo, handle, method, external_record_id=m, promoted_kind=PromotedKind.MCQ_STIMULUS, promoted_pk=n
    )
    with repo.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT mv.method_version_id, mv.semver, mv.code_sha, m.active_version_id "
                "FROM neuro.method_versions mv JOIN neuro.methods m ON m.method_id = mv.method_id "
                "WHERE m.method_key = :k"
            ),
            {"k": PROMOTION_METHOD_KEY},
        ).one()
    assert bytes(row.code_sha) == promotion_method_code_sha()
    assert row.semver == PROMOTION_METHOD_SEMVER
    assert row.method_version_id == res.method_version_id
    assert row.active_version_id == res.method_version_id, "set_active=True is passed explicitly"


def test_method_registration_is_batch_scoped(repo):
    """RULING C: register once, promote many. Exactly ONE version row for the promotion method, and every promotion
    names it.

    NOTE this probe CANNOT pin the ruling by itself — register_method_version is idempotent, so a per-row
    re-registration would keep this count at 1 and stay GREEN while every row paid an unconditional
    methods.active_version_id UPDATE. The mechanism pin is the AST scan
    (test_rt_importer_no_cloud_put.py::test_rt_promote_registers_the_method_once_per_batch_not_per_row)."""
    handle = _open(repo)
    _decoy_methods(repo)
    m1, m2 = _record(repo, handle, body=b"q1"), _record(repo, handle, body=b"q2")
    n1, n2 = _cascade(repo.engine, ordinal=1), _cascade(repo.engine, ordinal=2)
    method = register_promotion_method(repo)
    for m, n in ((m1, n1), (m2, n2)):
        promote_external_record(
            repo, handle, method, external_record_id=m, promoted_kind=PromotedKind.MCQ_STIMULUS, promoted_pk=n
        )
    with repo.engine.connect() as conn:
        versions = conn.execute(
            text(
                "SELECT count(*) FROM neuro.method_versions mv JOIN neuro.methods m ON m.method_id = mv.method_id "
                "WHERE m.method_key = :k"
            ),
            {"k": PROMOTION_METHOD_KEY},
        ).scalar_one()
    assert versions == 1
    assert [p[3] for p in _promotions(repo.engine)] == [method.method_version_id] * 2


def test_promotion_stamp_resolves_to_a_live_method_version(repo):
    """A BACKSTOP assertion, not a mutation-verified guard (no memo exists to revert): the stamp must JOIN to a LIVE
    method_versions row. This is what a stale cached id would violate — the token design avoids the memo entirely, and
    `_truncate_all` empties method_versions between tests on the SAME database with the SAME identity, so a memo keyed
    on engine/DB identity would still hand back a deleted id here."""
    handle = _open(repo)
    _decoy_methods(repo)
    m, n = _record(repo, handle), _cascade(repo.engine, ordinal=1)
    method = register_promotion_method(repo)
    promote_external_record(
        repo, handle, method, external_record_id=m, promoted_kind=PromotedKind.MCQ_STIMULUS, promoted_pk=n
    )
    with repo.engine.connect() as conn:
        joined = conn.execute(
            text(
                "SELECT count(*) FROM neuro.promotions p "
                "JOIN neuro.method_versions mv ON mv.method_version_id = p.method_version_id"
            )
        ).scalar_one()
    assert joined == 1


def test_null_code_sha_version_is_refused_through_the_promotion_path(repo):
    """Fork B bought something REAL: because the module registers the method itself, an out-of-band NULL-code_sha row
    under the promotion method's (method_id, semver) fails closed AT the promotion path (repository.py:671) instead of
    being silently adopted as an unverifiable governance stamp. A writer that took a raw method_version_id would never
    reach this guard."""
    with repo.engine.begin() as conn:
        method_id = conn.execute(
            text("INSERT INTO neuro.methods (method_key) VALUES (:k) RETURNING method_id"),
            {"k": PROMOTION_METHOD_KEY},
        ).scalar_one()
        conn.execute(
            text("INSERT INTO neuro.method_versions (method_id, semver, code_sha) VALUES (:m, :s, NULL)"),
            {"m": method_id, "s": PROMOTION_METHOD_SEMVER},
        )
    with pytest.raises(IdentityMismatchError, match="NULL code_sha"):
        register_promotion_method(repo)
    assert _count(repo.engine, "promotions") == 0
    assert _count(repo.engine, "lineage_edges") == 0
