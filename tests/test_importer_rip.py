"""Unit 2 — the RIP lane mapper (`importer/rip.py`) + `close_import_batch`.

The identity ruling under test: `source_pk`/`payload_text` = {scheme, source_system, uri} ONLY, so
the key survives re-curation. The fixtures below are the FALSIFYING half of that claim — each
curation field gets a probe that would redden if it crept back into the preimage.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib

import pytest
from sqlalchemy import text

from neuromancer_llm.db.identity import canonical_bytes
from neuromancer_llm.importer import rip
from neuromancer_llm.importer.external_records import write_external_record
from neuromancer_llm.importer.ingress import (
    Confidentiality,
    ImporterIngressError,
    SourceSystem,
    close_import_batch,
    open_import_batch,
)
from neuromancer_llm.importer.rip import (
    EXCLUDED_FAMILIES,
    RIP_POINTER_SCHEME,
    RIP_RECORD_KIND,
    RIP_SOURCE_TABLE,
    blank_to_none,
    build_sidecar,
    canonical_pointer_bytes,
    derived_by_predecessor_for,
    map_corpus_file,
    parse_shape,
    pointer_bytes_for_row,
)


def _row(tmp_path, **over) -> dict:
    p = tmp_path / "payload.bin"
    if not p.exists():
        p.write_bytes(b"corpus-bytes")
    base = {
        "uri": "mi/vllm-lens/sparse/req_00000.safetensors",
        "kind": "export",
        "size_bytes": "12",
        "dtype": "",
        "shape": "",
        "retention": "keep_forever",
        "confidentiality": "exam_restricted",
        "source_system": "local-fs",
        "family": "mi-activation-capture",
        "workflow_seat": "4-sae",
        "source_abspath": str(p),
        "note": "a per-row note",
    }
    base.update(over)
    return base


# --- THE PREIMAGE: curation must NOT move the identity ---------------------------------------


@pytest.mark.parametrize(
    "field",
    ["note", "confidentiality", "size_bytes", "family", "retention", "workflow_seat", "kind", "dtype"],
)
def test_curation_does_not_move_the_source_pk(tmp_path, field: str) -> None:
    """THE RULING, AS A FALSIFYING FIXTURE. A whole-row preimage re-minted 100% of source_pks at one
    manifest revision; each of these fields is one that churned. If any creeps back into the
    preimage, exactly this probe reddens."""
    a = _row(tmp_path)
    b = _row(tmp_path, **{field: "SOMETHING-ELSE-ENTIRELY"})
    # ⚠ THROUGH `pointer_bytes_for_row`, WHICH TAKES THE WHOLE ROW. Calling `canonical_pointer_bytes`
    # with two scalars here compares f(x) to f(x) — a TAUTOLOGY that cannot fail, which is what the
    # first cut of this probe did. It survived the mutation matrix only because a DIFFERENT assertion
    # reddened. Routing through the row is what makes a widened preimage actually move the bytes.
    assert pointer_bytes_for_row(source_system="local-fs", row=a) == pointer_bytes_for_row(
        source_system="local-fs", row=b
    )


def test_source_abspath_is_not_in_the_identity(tmp_path) -> None:
    """Machine-local (owner ruling). It DOES ride the sidecar — that is the only dereference path —
    but it must never reach the key."""
    assert (
        rip.MACHINE_LOCAL_COLUMN
        not in canonical_pointer_bytes(source_system="local-fs", uri="a/b.bin").decode()
    )


@pytest.mark.parametrize(
    ("kwargs_a", "kwargs_b"),
    [
        ({"source_system": "local-fs", "uri": "a/b"}, {"source_system": "local-fs", "uri": "a/c"}),
        (
            {"source_system": "local-fs", "uri": "a/b"},
            {"source_system": "study-query-llm", "uri": "a/b"},
        ),
    ],
)
def test_the_two_identity_coordinates_do_move_it(kwargs_a: dict, kwargs_b: dict) -> None:
    assert canonical_pointer_bytes(**kwargs_a) != canonical_pointer_bytes(**kwargs_b)


def test_the_scheme_tag_is_inside_the_preimage() -> None:
    """The rank-8b idiom: a future pointer mapper under a different rule cannot silently collide."""
    raw = canonical_pointer_bytes(source_system="local-fs", uri="a/b").decode()
    assert RIP_POINTER_SCHEME in raw
    assert (
        raw
        == canonical_bytes({"scheme": RIP_POINTER_SCHEME, "source_system": "local-fs", "uri": "a/b"}).decode()
    )  # one implementation per concept


# --- shape / dtype coercion: the two measured fail-opens ---------------------------------------


def test_blank_shape_becomes_none_never_an_empty_array() -> None:
    """★ THE FAIL-OPEN. A blank binds through ARRAY(Integer) to an EMPTY int4 array, and an empty
    array IS NOT NULL — so `artifacts_tensor_shape` would be SATISFIED by a shapeless tensor row.
    None is what lets the CHECK fire."""
    assert parse_shape("") is None
    assert parse_shape("   ") is None
    assert parse_shape(None) is None


def test_shape_is_json_parsed_to_ints_not_left_as_a_string() -> None:
    """★ THE OTHER FAIL-OPEN. A raw string handed to an ARRAY(Integer) bind is treated as a SEQUENCE
    OF CHARACTERS. The parsed value must be a real list of ints."""
    got = parse_shape("[323, 100]")
    assert got == [323, 100]
    assert all(isinstance(d, int) for d in got)


@pytest.mark.parametrize("bad", ["not-json", "{}", "[1, 'x']", '"[1,2]"', "[1.5]"])
def test_unparseable_shape_raises_rather_than_degrading_to_none(bad: str) -> None:
    """Degrading to None would turn a corrupt manifest into a silently shapeless row."""
    with pytest.raises(ImporterIngressError, match="shape"):
        parse_shape(bad)


@pytest.mark.parametrize(("raw", "want"), [("", None), ("  ", None), (None, None), ("F32", "F32")])
def test_blank_to_none_both_directions(raw, want) -> None:
    assert blank_to_none(raw) is want or blank_to_none(raw) == want


# --- the sidecar --------------------------------------------------------------------------------


def test_sidecar_kind_is_top_level_and_unshadowed_by_the_rows_own_kind(tmp_path) -> None:
    """`external_records_kind_idx` reads the TOP level and the manifest has its OWN `kind` column.
    Flattening would let the manifest value shadow the index key and mis-index every row."""
    side = build_sidecar(_row(tmp_path, kind="export"))
    assert side["kind"] == RIP_RECORD_KIND
    assert side["row"]["kind"] == "export"
    assert side["kind"] != side["row"]["kind"]


def test_sidecar_carries_the_whole_row_including_the_only_homes_of_note_and_grade(tmp_path) -> None:
    """R-D ground iii: the sidecar is the ONLY place the per-row note and the historical
    confidentiality persist — payload_text carries the identity triple and nothing else."""
    row = _row(tmp_path)
    side = build_sidecar(row)
    assert side["row"] == row
    assert side["row"]["note"] == "a per-row note"
    assert side["row"]["confidentiality"] == "exam_restricted"
    assert side["row"][rip.MACHINE_LOCAL_COLUMN] == row[rip.MACHINE_LOCAL_COLUMN]


def test_sidecar_is_json_serialisable() -> None:
    json.dumps(build_sidecar({"a": "1"}))


# --- vocabulary + wiring ------------------------------------------------------------------------


def test_the_denylist_is_the_three_ruled_families() -> None:
    assert set(EXCLUDED_FAMILIES) == {"competition-trace-the-ace", "stimuli-estela", "mi-sae-asset"}


def test_source_table_is_the_owner_ruled_file_grain_noun() -> None:
    assert RIP_SOURCE_TABLE == "corpus_files"  # noqa: PLR0133 - the literal IS the pin


@pytest.mark.parametrize(
    ("ss", "want"), [(SourceSystem.STUDY_QUERY_LLM, True), (SourceSystem.LOCAL_FS, False)]
)
def test_derived_by_predecessor_is_a_function_of_the_batch(ss, want: bool) -> None:
    assert derived_by_predecessor_for(ss) is want


def test_rip_module_does_not_consult_the_durability_gate() -> None:
    """The consult is ONCE PER BATCH at the ingress choke point. AST-based: a prose mention in a
    docstring must not satisfy it."""
    tree = ast.parse(pathlib.Path(inspect.getsourcefile(rip)).read_text(encoding="utf-8"))
    assert [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ] == []


def test_rip_module_writes_no_sql_itself() -> None:
    """It COMPOSES the three shipped writers, so every INSERT stays in the module that owns it and
    the single-module red-team scans stay true."""
    src = pathlib.Path(inspect.getsourcefile(rip)).read_text(encoding="utf-8")
    assert "INSERT INTO" not in src.upper()


# --- pg: the write path -------------------------------------------------------------------------


def _handle(repo, ss=SourceSystem.LOCAL_FS):
    return open_import_batch(
        repo, source_system=ss, confidentiality=Confidentiality.OPEN, note="unit-2 probe"
    )


def _counts(repo) -> dict:
    with repo.engine.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT count(*) FROM neuro.{t}")).scalar_one()
            for t in ("artifacts", "external_records", "lineage_edges")
        }


@pytest.mark.pg
def test_one_file_writes_the_whole_triple(repo, tmp_path) -> None:
    row = _row(tmp_path)
    res = map_corpus_file(repo, _handle(repo), row, derived_by_predecessor=False)
    assert res.registered and res.mirrored and res.linked
    assert _counts(repo) == {"artifacts": 1, "external_records": 1, "lineage_edges": 1}
    with repo.engine.connect() as conn:
        rec = (
            conn.execute(
                text(
                    "SELECT source_table, source_pk, payload_text, payload_jsonb FROM neuro.external_records"
                )
            )
            .mappings()
            .one()
        )
    assert rec["source_table"] == "corpus_files"  # the LITERAL: comparing to the constant is vacuous
    # payload_text IS the identity triple, byte-for-byte the preimage
    assert rec["payload_text"] == canonical_pointer_bytes(source_system="local-fs", uri=row["uri"]).decode()
    assert rec["payload_jsonb"]["kind"] == RIP_RECORD_KIND
    assert rec["payload_jsonb"]["row"]["note"] == "a per-row note"


@pytest.mark.pg
def test_full_pass_idempotency_second_run_is_all_no_ops(repo, tmp_path) -> None:
    """THE ACCEPTANCE CRITERION, across all three ranks at once."""
    row = _row(tmp_path)
    h1 = _handle(repo)
    first = map_corpus_file(repo, h1, row, derived_by_predecessor=False)
    before = _counts(repo)
    second = map_corpus_file(repo, _handle(repo), row, derived_by_predecessor=False)
    assert (second.registered, second.mirrored, second.linked) == (False, False, False)
    assert second.artifact_id == first.artifact_id
    assert second.external_record_id == first.external_record_id
    assert second.lineage_edge_id == first.lineage_edge_id
    assert _counts(repo) == before


@pytest.mark.pg
def test_a_re_curated_row_keeps_first_and_the_sidecar_goes_stale(repo, tmp_path) -> None:
    """★ THE RULING'S COST, PINNED AS A FACT rather than left for someone to discover. The key is
    curation-independent, so a re-curated row lands on the SAME source_pk, the INSERT no-ops, and
    the FIRST write's sidecar persists — un-updated and UN-RAISED."""
    map_corpus_file(repo, _handle(repo), _row(tmp_path), derived_by_predecessor=False)
    map_corpus_file(
        repo,
        _handle(repo),
        _row(tmp_path, note="A CORRECTED NOTE", family="mi-perplexity-nll"),
        derived_by_predecessor=False,
    )
    with repo.engine.connect() as conn:
        rows = conn.execute(text("SELECT payload_jsonb FROM neuro.external_records")).all()
    assert len(rows) == 1, "a re-curation must NOT mint a second identity"
    assert rows[0][0]["row"]["note"] == "a per-row note", "first write wins; the mirror is stale"


@pytest.mark.pg
def test_blank_shape_persists_as_NULL_not_an_empty_array(repo, tmp_path) -> None:
    """★ The measured fail-open, pinned at the DATABASE. An empty int4 array is NOT NULL and would
    satisfy artifacts_tensor_shape for a shapeless tensor row."""
    map_corpus_file(repo, _handle(repo), _row(tmp_path), derived_by_predecessor=False)
    with repo.engine.connect() as conn:
        got = (
            conn.execute(text("SELECT shape, dtype, shape IS NULL AS shape_null FROM neuro.artifacts"))
            .mappings()
            .one()
        )
    assert got["shape_null"] is True
    assert got["shape"] is None and got["dtype"] is None


@pytest.mark.pg
def test_a_tensor_kind_with_a_blank_shape_is_refused(repo, tmp_path) -> None:
    """Because the blank became NULL, rank 6's fail-closed guard can actually fire."""
    with pytest.raises(ImporterIngressError, match="shape"):
        map_corpus_file(
            repo,
            _handle(repo),
            _row(tmp_path, kind="derived_feature"),
            derived_by_predecessor=False,
        )
    assert _counts(repo)["artifacts"] == 0


@pytest.mark.pg
def test_a_tensor_kind_with_a_blank_shape_but_a_REAL_dtype_is_refused(repo, tmp_path) -> None:
    """ISOLATES THE SHAPE ARM. The sibling probe above passes even if `shape` degrades to an empty
    list, because its `dtype` is blank too and rank 6 refuses on THAT. Supplying a real dtype leaves
    shape as the only thing that can refuse — measured: without it, a shape fail-open ships green."""
    with pytest.raises(ImporterIngressError, match="shape"):
        map_corpus_file(
            repo,
            _handle(repo),
            _row(tmp_path, kind="derived_feature", dtype="F32"),
            derived_by_predecessor=False,
        )
    assert _counts(repo)["artifacts"] == 0


@pytest.mark.pg
def test_a_tensor_kind_with_a_real_shape_round_trips(repo, tmp_path) -> None:
    map_corpus_file(
        repo,
        _handle(repo),
        _row(tmp_path, kind="derived_feature", dtype="F32", shape="[323, 100]"),
        derived_by_predecessor=False,
    )
    with repo.engine.connect() as conn:
        got = conn.execute(text("SELECT shape, dtype FROM neuro.artifacts")).mappings().one()
    assert list(got["shape"]) == [323, 100] and got["dtype"] == "F32"


@pytest.mark.pg
@pytest.mark.parametrize(
    "family",
    # ⚠ LITERALS, never `sorted(EXCLUDED_FAMILIES)`: parametrizing over the constant under test means
    # removing a family also removes its own probe, so a shrinking denylist reddens NOTHING here.
    # Measured — the mutation matrix caught exactly that.
    ["competition-trace-the-ace", "stimuli-estela", "mi-sae-asset"],
)
def test_an_excluded_family_is_refused_before_any_write(repo, tmp_path, family: str) -> None:
    """The denylist is DEFAULT-ON in the library, not only in the driver — so a caller cannot import
    an excluded family by forgetting a flag."""
    with pytest.raises(ImporterIngressError, match="EXCLUDED"):
        map_corpus_file(repo, _handle(repo), _row(tmp_path, family=family), derived_by_predecessor=False)
    assert _counts(repo) == {"artifacts": 0, "external_records": 0, "lineage_edges": 0}


@pytest.mark.pg
def test_r6_first_a_missing_file_leaves_no_manifest_row_and_no_edge(repo, tmp_path) -> None:
    """THE ORDERING, PINNED. Rank 6 is the step that can fail on a real file; running it first means
    the failure leaves nothing behind. Reversed, this would strand a manifest row describing a
    binary that was never registered — and there is no delete verb."""
    with pytest.raises(ImporterIngressError):
        map_corpus_file(
            repo,
            _handle(repo),
            _row(tmp_path, source_abspath=str(tmp_path / "does-not-exist.bin")),
            derived_by_predecessor=False,
        )
    assert _counts(repo) == {"artifacts": 0, "external_records": 0, "lineage_edges": 0}


# --- pg: close_import_batch ---------------------------------------------------------------------


def _finished(repo, batch_id: int):
    with repo.engine.connect() as conn:
        return conn.execute(
            text("SELECT finished_at FROM neuro.import_batches WHERE import_batch_id = :b"),
            {"b": batch_id},
        ).scalar_one()


@pytest.mark.pg
def test_close_import_batch_stamps_once_and_is_idempotent(repo) -> None:
    h = _handle(repo)
    assert _finished(repo, h.import_batch_id) is None, "NULL until closed"
    assert close_import_batch(repo, h) is True
    assert _finished(repo, h.import_batch_id) is not None
    assert close_import_batch(repo, h) is False, "already stamped: a no-op, never an error"


@pytest.mark.pg
def test_an_unclosed_batch_stays_null(repo, tmp_path) -> None:
    """NULL means NEVER-CLOSED, not crashed — the shipped `promote` verb never closes, so batches
    older than this writer are NULL forever and were never incomplete. A batch that did real work
    and was simply not closed is indistinguishable from one that died, which is exactly why the
    stamp is per-invocation evidence and NOT a coverage statement."""
    h = _handle(repo)
    map_corpus_file(repo, h, _row(tmp_path), derived_by_predecessor=False)
    assert _finished(repo, h.import_batch_id) is None


@pytest.mark.pg
def test_close_refuses_a_raw_id(repo) -> None:
    with pytest.raises(ImporterIngressError, match="ImportBatchHandle"):
        close_import_batch(repo, 1)  # type: ignore[arg-type]


@pytest.mark.pg
def test_the_rank7_edge_endpoints_are_read_back_not_merely_written(repo, tmp_path) -> None:
    """RANK 7 WAS WRITTEN AND NEVER READ. Measured by a vet: swapping the two kwargs at the
    `link_registered_artifact` callsite left the whole suite green, because nothing asserted WHICH
    entity sits on WHICH end. The direction is the semantics — `external_record annotates artifact`,
    never the reverse — and it is un-unwindable once written."""
    # ⚠ DE-BLIND THE IDS FIRST. `artifacts` and `external_records` advance in LOCKSTEP through the
    # mapper (one row each per file), so under RESTART IDENTITY the first file gets
    # artifact_id == external_record_id == 1 and a SWAP IS INVISIBLE. Measured: the first cut of this
    # probe passed under the swapped-endpoint mutation. Minting a standalone external_record first
    # skews the two sequences so the swap has somewhere to show.
    h = _handle(repo)
    write_external_record(repo, h, source_table="decoy", source_bytes=b"decoy", derived_by_predecessor=False)
    res = map_corpus_file(repo, h, _row(tmp_path), derived_by_predecessor=False)
    assert res.artifact_id != res.external_record_id, "the sequences must be skewed for this to bite"
    with repo.engine.connect() as conn:
        edge = (
            conn.execute(text("SELECT edge_kind, src_entity, dst_entity, note FROM neuro.lineage_edges"))
            .mappings()
            .one()
        )
    assert edge["edge_kind"] == "annotates"
    assert edge["src_entity"] == f"external_record:{res.external_record_id}"
    assert edge["dst_entity"] == f"artifact:{res.artifact_id}"
    assert edge["src_entity"] != edge["dst_entity"]
    assert RIP_POINTER_SCHEME in edge["note"]


@pytest.mark.pg
def test_the_artifact_row_is_read_back_under_its_CORPUS_URI_not_the_machine_local_path(
    repo, tmp_path
) -> None:
    """`artifacts.uri`/`kind`/`retention` were written and never read. Measured by a vet: passing
    `source_abspath` as the uri — registering all 1,753 pointers under a MACHINE-LOCAL ABSOLUTE PATH,
    the one thing the ruling exists to keep out of the durable record — shipped green."""
    row = _row(tmp_path)
    map_corpus_file(repo, _handle(repo), row, derived_by_predecessor=False)
    with repo.engine.connect() as conn:
        got = (
            conn.execute(text("SELECT uri, kind, retention, size_bytes FROM neuro.artifacts"))
            .mappings()
            .one()
        )
    assert got["uri"] == "mi/vllm-lens/sparse/req_00000.safetensors"
    assert got["uri"] != row["source_abspath"]
    assert ":" not in got["uri"] and "\\" not in got["uri"], "no drive letter, no backslash"
    assert got["kind"] == "export"
    assert got["retention"] == "keep_forever"  # registered AS DECLARED, never coerced
    assert got["size_bytes"] == len(b"corpus-bytes")  # rank 6 MEASURED it; not the manifest's claim


@pytest.mark.pg
def test_close_stamps_only_the_batch_it_was_handed(repo) -> None:
    """`close_import_batch`'s "THIS invocation's batch" was unprobed: dropping the
    `import_batch_id = :b` predicate stamped EVERY open batch and shipped green against all 1001
    probes (measured by a vet). With two live batches, a close must move exactly one."""
    a, b = _handle(repo), _handle(repo)
    assert a.import_batch_id != b.import_batch_id
    assert close_import_batch(repo, a) is True
    assert _finished(repo, a.import_batch_id) is not None
    assert _finished(repo, b.import_batch_id) is None, "the sibling batch must be untouched"


def test_the_identity_bearing_constants_are_pinned_by_literal() -> None:
    """Two of the three had no literal pin, so every assertion touching them imported them from the
    module under test — self-referential, and unable to catch a change to the value."""
    assert RIP_POINTER_SCHEME == "neuro.rip.pointer.v1"
    assert RIP_RECORD_KIND == "corpus_file_pointer"
    assert RIP_SOURCE_TABLE == "corpus_files"


@pytest.mark.parametrize("bad", ["[]", "[true]", "[0]", "[-1]", "[1, false]"])
def test_shape_refuses_the_values_that_reopen_the_empty_array_fail_open(bad: str) -> None:
    """`[]` is valid JSON, IS a list, and vacuously satisfies all-ints — so it reached the DB as an
    EMPTY int4 array, which IS NOT NULL and SATISFIES artifacts_tensor_shape. The same fail-open the
    blank branch closes, through a different door. Booleans ride in because `isinstance(True, int)`."""
    with pytest.raises(ImporterIngressError, match="shape"):
        parse_shape(bad)
