"""Importer rank 7 — lineage-edge writer + entity-reference grammar probes (importer/lineage.py).

Rank 7 is the FIRST lineage_edges producer, so the grammar it invents is inherited by every future reader. These probes
pin the three normative rules (canonical form / the noun / the direction), the closed-noun refusal, the handle
requirement, and PURE keep-first (ruled 2026-07-16). All run on the head-built shared `repo` fixture (expected_lane=
'test', so open_import_batch skips the canonical durability consult — no canonical fixture needed), mirroring
tests/test_importer_register_in_place.py.

Probe separation, stated honestly: the NOUN probes are perfectly separated (all 6 cases are valid FORM, so all 6 redden
only when the closed-noun check is dropped). The FORM probes mostly use the known noun 'artifact' and 8 of 11 pin the
regex alone; 3 ('Artifacts:5', '_:1', ' artifact:45') are additionally caught by the noun check. Neither guard greens
the other's probe.
"""

from __future__ import annotations

import re
from typing import cast

import pytest
from sqlalchemy import text

from neuromancer_llm.importer.external_records import write_external_record
from neuromancer_llm.importer.ingress import (
    Confidentiality,
    ImportBatchHandle,
    ImporterIngressError,
    SourceSystem,
    open_import_batch,
)
from neuromancer_llm.importer.lineage import (
    ARTIFACT_MANIFEST_EDGE_KIND,
    ENTITY_KINDS,
    LineageEdgeResult,
    artifact_ref,
    entity_ref,
    external_record_ref,
    link_registered_artifact,
    parse_entity_ref,
    write_lineage_edge,
)
from neuromancer_llm.importer.register_in_place import register_artifact_in_place

pytestmark = pytest.mark.pg


def _open(repo) -> ImportBatchHandle:
    return open_import_batch(repo, source_system=SourceSystem.LOCAL_FS, confidentiality=Confidentiality.OPEN)


def _edge_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM neuro.lineage_edges")).scalar_one()


def _edges(engine) -> list[tuple[str, str, str]]:
    with engine.connect() as conn:
        return [
            (r.edge_kind, r.src_entity, r.dst_entity)
            for r in conn.execute(
                text(
                    "SELECT edge_kind, src_entity, dst_entity FROM neuro.lineage_edges ORDER BY lineage_edge_id"
                )
            )
        ]


# --- RULE 1: canonical form (a non-canonical alias would defeat the UNIQUE keep-first rests on) ------


@pytest.mark.parametrize(
    "bad",
    [
        "artifact:007",  # leading zeros — the headline alias: would be a 2nd row for one artifact
        "artifact:0",  # pk 0 — no identity PK is ever 0 (GENERATED ALWAYS AS IDENTITY starts at 1)
        "artifact:45\n",  # trailing newline — pins re.fullmatch over re.match (`$` alone accepts it)
        "artifact:١٢٣",  # Arabic-Indic digits: [0-9] rejects them, but .isdigit()/int() would ACCEPT —
        #                  a regression pin against a future refactor to an isdigit()/int() check
        "artifact:-1",
        "artifact:",
        "artifact:1:2",
        "Artifacts:5",  # uppercase
        "_:1",  # bare/leading underscore
        "artifact: 45",  # inner space
        " artifact:45",  # leading space
    ],
)
def test_non_canonical_ref_is_refused(bad):
    """RULE 1: exactly one string denotes one entity. Most cases use the KNOWN noun 'artifact', so the probe targets the
    FORM regex rather than the noun check — 8 of the 11 redden on the regex alone; 'Artifacts:5', '_:1' and
    ' artifact:45' are additionally caught by the noun check. Reddens if the regex is loosened to `[0-9]+` or to
    re.match."""
    with pytest.raises(ImporterIngressError):
        parse_entity_ref(bad)


@pytest.mark.parametrize("good", ["artifact:1", "artifact:45", "external_record:123", "artifact:1000000"])
def test_canonical_ref_round_trips(good):
    """A ref that parses must re-format to ITSELF — the canonicalization property write_lineage_edge relies on."""
    kind, pk = parse_entity_ref(good)
    assert entity_ref(kind, pk) == good


def test_entity_ref_builds_canonical_form():
    assert entity_ref("artifact", 45) == "artifact:45"
    assert external_record_ref(123) == "external_record:123"
    assert artifact_ref(7) == "artifact:7"


@pytest.mark.parametrize("bad_pk", [0, -1, True, "7", 1.0, None])
def test_entity_ref_refuses_bad_pk(bad_pk):
    """pk must be a real positive int. `True` is the sharp one: bool subclasses int, so an unguarded build mints
    'artifact:True' (NOT 'artifact:1' — the f-string formats the bool). _REF_RE would catch that downstream, so this
    guard is about refusing it WHERE THE GRAMMAR IS DEFINED, typed and named, rather than as an incidental regex miss."""
    with pytest.raises(ImporterIngressError):
        entity_ref("artifact", cast(int, bad_pk))


# --- RULE 2: the noun is closed + load-bearing (valid FORM, so this targets the noun check) ----------


@pytest.mark.parametrize(
    "bad_ref",
    [
        "artifacts:45",  # the PLURAL table name — the exact mistake RULE 2 forbids
        "external_records:1",
        "extenral_record:1",  # a typo
        "mcq_responses:9",  # a table that must never exist (readiness §7 trap 6)
        "model:88",  # the PK-column stem — RULE 2's headline error (would be model_identity at rank 8)
        "foo:1",
    ],
)
def test_unknown_noun_is_refused(bad_ref):
    """ENTITY_KINDS is load-bearing, not advisory: an unknown noun never reaches the DB. Each case is VALID FORM, so
    this reddens only if the closed-noun check is dropped."""
    with pytest.raises(ImporterIngressError):
        parse_entity_ref(bad_ref)


def test_entity_ref_refuses_unknown_kind():
    with pytest.raises(ImporterIngressError):
        entity_ref("model", 88)  # RULE 2: never the PK-column stem; a Layer-2 noun is a rank-8 addition


def test_entity_kinds_maps_noun_to_table():
    """The registry is a noun→table map so a reader resolving a ref gets the table for free. Rank 7 seeded ONLY the
    Layer-1 nouns; rank 8a added EXACTLY ONE Layer-2 noun (`mcq_item`, its promotion target).

    ⚠ THE EXACT EQUALITY IS THE MECHANISM — never loosen it to a subset/membership check. It is the only thing that
    makes a new noun a DELIBERATE, reviewed act: loosened, a future rank could slip in `model_identity` (D5) or
    `run_summary` (the ADR-0048 watch-point) with nothing reddening."""
    assert ENTITY_KINDS == {
        "external_record": "external_records",
        "artifact": "artifacts",
        "mcq_item": "mcq_items",
    }


def test_entity_kinds_values_obey_rule_2():
    """RULE 2 as a MECHANISM, not a docstring: every noun→table entry must equal snake_case(ORM class name) for the
    class owning that table. The exact-equality test above pins the KEYSET; without this, the VALUE side is unpinned
    (nothing in src reads a value — the writer only tests membership), so a typo like {'mcq_item': 'mcq_item'} would
    pass every other probe. Derived from the ORM registry so it cannot drift from the schema."""
    from neuromancer_llm.db.orm import Base

    by_table = {
        m.class_.__tablename__: m.class_.__name__
        for m in Base.registry.mappers
        if hasattr(m.class_, "__tablename__")
    }
    for noun, table in ENTITY_KINDS.items():
        assert table in by_table, f"ENTITY_KINDS[{noun!r}] names {table!r}, which is not an ORM table"
        expected = re.sub(r"(?<!^)(?=[A-Z])", "_", by_table[table]).lower()
        assert noun == expected, (
            f"RULE 2: the noun for {table!r} must be snake_case(ORM class {by_table[table]!r}) = {expected!r}, "
            f"not {noun!r} (never the PK-column stem)"
        )


# --- the generic writer --------------------------------------------------------------------------


def test_write_lineage_edge_writes_the_row(repo):
    handle = _open(repo)
    r = write_lineage_edge(
        repo, handle, edge_kind="annotates", src_entity="external_record:1", dst_entity="artifact:2"
    )
    assert isinstance(r, LineageEdgeResult)
    assert r.inserted is True
    assert _edges(repo.engine) == [("annotates", "external_record:1", "artifact:2")]


def test_write_lineage_edge_is_keep_first_idempotent(repo):
    """Keep-first: a re-write of the same triple returns the SAME id, inserted=False. (A `count == 1` assertion would be
    a schema tautology — the UNIQUE forbids duplicates regardless of writer behavior — so the id/inserted assertions are
    what actually pin the re-SELECT branch.)"""
    handle = _open(repo)
    first = write_lineage_edge(
        repo, handle, edge_kind="annotates", src_entity="external_record:1", dst_entity="artifact:2"
    )
    again = write_lineage_edge(
        repo, handle, edge_kind="annotates", src_entity="external_record:1", dst_entity="artifact:2"
    )
    assert again.inserted is False
    assert again.lineage_edge_id == first.lineage_edge_id


def test_keep_first_keeps_the_first_note_and_does_not_raise(repo):
    """PURE keep-first (ruled 2026-07-16): a re-assertion of the same triple carrying a DIFFERENT `note` keeps the FIRST
    and does NOT raise — and does not UPDATE either (the stored note is still the first). The UNIQUE declares the
    relationship as the identity; the attribution of the assertion is not part of it, so raising would be false-loud.
    Reddens if a drift-raise is ever added.

    SCOPE, stated honestly: this exercises `note` only, not `method_version_id` — a drift-raise keyed on
    method_version_id ALONE would not redden it. That is deliberate: method_version_id REFERENCES method_versions, so
    probing it needs FK setup the GO explicitly dropped, and the first customer always passes NULL. The standing
    obligation stands for rank 8 (the GO's §5 / §C.10): when it rules drift with a real method-bearing customer, it must
    probe BOTH asymmetric NULL cases — the `if x is not None and ...` shape is the one this repo already shipped a bug
    in (repository.py:667-671)."""
    handle = _open(repo)
    first = write_lineage_edge(
        repo,
        handle,
        edge_kind="annotates",
        src_entity="external_record:1",
        dst_entity="artifact:2",
        note="first",
    )
    again = write_lineage_edge(
        repo,
        handle,
        edge_kind="annotates",
        src_entity="external_record:1",
        dst_entity="artifact:2",
        note="second",
    )
    assert again.lineage_edge_id == first.lineage_edge_id
    assert again.inserted is False
    with repo.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT note FROM neuro.lineage_edges WHERE lineage_edge_id = :i"),
            {"i": first.lineage_edge_id},
        ).scalar_one()
    assert stored == "first"  # keep-first, not last-write-wins


def test_edge_kind_and_direction_are_independent(repo):
    """The same pair under a different edge_kind, and the reversed pair under the same kind, are DISTINCT edges — the
    UNIQUE is on the whole triple. This is what makes RULE 3 (direction) load-bearing rather than cosmetic."""
    handle = _open(repo)
    write_lineage_edge(
        repo, handle, edge_kind="annotates", src_entity="external_record:1", dst_entity="artifact:2"
    )
    write_lineage_edge(
        repo, handle, edge_kind="curates", src_entity="external_record:1", dst_entity="artifact:2"
    )
    write_lineage_edge(
        repo, handle, edge_kind="annotates", src_entity="artifact:2", dst_entity="external_record:1"
    )
    assert _edge_count(repo.engine) == 3


def test_unknown_edge_kind_is_refused(repo):
    handle = _open(repo)
    with pytest.raises(ImporterIngressError):
        write_lineage_edge(
            repo, handle, edge_kind="not_a_kind", src_entity="external_record:1", dst_entity="artifact:2"
        )
    assert _edge_count(repo.engine) == 0


def test_writer_refuses_non_canonical_ref_and_writes_nothing(repo):
    """The canonicalization boundary: a non-canonical ref cannot reach the UNIQUE. `external_record:007` is refused, so
    it can never sit alongside `external_record:7` as a second row for one entity."""
    handle = _open(repo)
    write_lineage_edge(
        repo, handle, edge_kind="annotates", src_entity="external_record:7", dst_entity="artifact:2"
    )
    with pytest.raises(ImporterIngressError):
        write_lineage_edge(
            repo, handle, edge_kind="annotates", src_entity="external_record:007", dst_entity="artifact:2"
        )
    assert _edges(repo.engine) == [("annotates", "external_record:7", "artifact:2")]


def test_writer_refuses_unknown_noun_and_writes_nothing(repo):
    handle = _open(repo)
    with pytest.raises(ImporterIngressError):
        write_lineage_edge(
            repo, handle, edge_kind="annotates", src_entity="artifacts:45", dst_entity="artifact:2"
        )
    assert _edge_count(repo.engine) == 0


@pytest.mark.parametrize("bad_handle", [1, None, "handle", object()])
def test_writer_requires_an_import_batch_handle(repo, bad_handle):
    """A3: on importer paths, holding the handle is the ONLY thing forcing the once-per-batch ADR-0020 consult —
    lineage_edges has no import_batch_id FK to carry it (unlike rank 5). A raw id is refused."""
    with pytest.raises(ImporterIngressError):
        write_lineage_edge(
            repo,
            cast(ImportBatchHandle, bad_handle),
            edge_kind="annotates",
            src_entity="external_record:1",
            dst_entity="artifact:2",
        )
    assert _edge_count(repo.engine) == 0


def test_link_registered_artifact_requires_an_import_batch_handle(repo):
    with pytest.raises(ImporterIngressError):
        link_registered_artifact(repo, cast(ImportBatchHandle, 1), artifact_id=1, external_record_id=2)
    assert _edge_count(repo.engine) == 0


# --- the first customer: the rank-6 binary <-> its paired rank-5 stamped manifest ------------------


def test_link_registered_artifact_ties_the_manifest_to_the_binary(repo, tmp_path):
    """The FIRST customer, composed end-to-end through ranks 4→5→6→7. Pins the RULED edge (2026-07-16):
    `external_record:M annotates artifact:N` — the manifest is src, the binary dst. There is no FK between artifacts and
    external_records; this edge IS the only tie.

    The DECOY record is load-bearing: the `repo` fixture truncates RESTART IDENTITY, so without it the sole manifest and
    the sole artifact would BOTH get pk 1, both f-strings would render `…:1`, and the expected tuple would be blind to
    which id went into which noun — a crossed-id link (an edge pointing at the WRONG ROWS on the D1-recovery path) would
    pass green. The decoy makes external_record_id=2 ≠ artifact_id=1, so the id→noun binding is actually pinned."""
    handle = _open(repo)
    write_external_record(
        repo, handle, source_table="unrelated", source_bytes=b"decoy", derived_by_predecessor=True
    )
    # rank 5: the stamped TEXT manifest describing the binary
    manifest = write_external_record(
        repo,
        handle,
        source_table="corpus_manifest",
        source_bytes=b'{"path": "mcq/logprobs.parquet", "sha256": "abc", "rows": 20}',
        derived_by_predecessor=True,
    )
    # rank 6: the binary itself, registered in place (not copied)
    binary = tmp_path / "logprobs.parquet"
    binary.write_bytes(b"PAR1\x00\x01binary-bytes")
    artifact = register_artifact_in_place(repo, handle, source_path=str(binary), uri="mcq/logprobs.parquet")
    assert (
        manifest.external_record_id != artifact.artifact_id
    )  # the decoy did its job — the tuple can discriminate
    # rank 7: the tie
    edge = link_registered_artifact(
        repo, handle, artifact_id=artifact.artifact_id, external_record_id=manifest.external_record_id
    )
    assert edge.inserted is True
    # the kind is asserted as a LITERAL (not via the constant the code under test uses) so the ruling is pinned by a
    # non-self-referential assertion; the trailing constant check pins it a second time.
    assert _edges(repo.engine) == [
        (
            "annotates",
            f"external_record:{manifest.external_record_id}",
            f"artifact:{artifact.artifact_id}",
        )
    ]
    assert ARTIFACT_MANIFEST_EDGE_KIND == "annotates"  # the ruled kind, not derived_from


def test_link_registered_artifact_is_idempotent(repo, tmp_path):
    handle = _open(repo)
    write_external_record(
        repo, handle, source_table="unrelated", source_bytes=b"decoy", derived_by_predecessor=True
    )  # see the flagship probe: forces external_record_id != artifact_id under RESTART IDENTITY
    manifest = write_external_record(
        repo, handle, source_table="corpus_manifest", source_bytes=b'{"p": 1}', derived_by_predecessor=True
    )
    binary = tmp_path / "e.npy"
    binary.write_bytes(b"\x93NUMPY\x00")
    artifact = register_artifact_in_place(repo, handle, source_path=str(binary), uri="emb/e.npy")
    assert manifest.external_record_id != artifact.artifact_id
    first = link_registered_artifact(
        repo, handle, artifact_id=artifact.artifact_id, external_record_id=manifest.external_record_id
    )
    again = link_registered_artifact(
        repo, handle, artifact_id=artifact.artifact_id, external_record_id=manifest.external_record_id
    )
    assert again.inserted is False
    assert again.lineage_edge_id == first.lineage_edge_id
    assert _edge_count(repo.engine) == 1
