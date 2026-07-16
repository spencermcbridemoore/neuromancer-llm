"""Red-team: the importer is register-in-place only (D3, spec 2026-07-14) and its ingress token is un-forgeable.

  * D3 — the importer/** subsystem performs NO cloud put() and constructs NO storage backend, so "the importer never
    writes bytes" is enforced by MECHANISM, not convention (register-in-place; rank 6 records pointers). A rank-6
    builder who reaches for a cloud put shows up here and must justify it.
  * un-forgeable token — ImportBatch (the rank-4 ingress capability) is constructed ONLY in importer/ingress.py, so
    rank 5's external_records writer cannot fabricate a token to skip the gate (the coverage-by-mechanism property).
  * consult present — importer/ingress.py CALLS assert_durability_ok( so a refactor cannot silently drop the ADR-0020
    consult from the choke point (the true consult guard is test_importer_ingress.py::test_gate_blocks_on_flipped_*).

Pure filesystem scans (no DB) — mirrors the structural-scan idiom in test_rt_lanes.py:120-138 / :141-203.
"""

from __future__ import annotations

import ast
import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "neuromancer_llm"
_IMPORTER = _SRC / "importer"

# cloud-put / cloud-backend construction tokens the importer must never contain (register-in-place only, D3).
_FORBIDDEN = ("make_backend(", "resolve_capture_backend(", "guard_capture_backend(", "AzureBlobBackend")


def test_rt_importer_performs_no_cloud_put():
    """D3: no `.put(` and no cloud-backend construction anywhere in importer/** (register-in-place only). A future
    put/backend construction reddens this — update the probe ONLY with a justification.

    NON-VACUITY PIN (added rank 7, closing a latent hole that silently covered ranks 4/5/6): the real assertion is
    `offenders == []`, so if `_IMPORTER` ever mis-resolved (a rename, a move, `parents[2]` drift) the glob would yield
    ZERO files and the entire D3 guarantee would go GREEN while scanning NOTHING. Pin the scan's OWN globbed set first,
    so a mis-resolved root reddens instead of passing. Mutation-verify by pointing `_IMPORTER` at a bogus path."""
    scanned = sorted(_IMPORTER.rglob("*.py"))
    assert {p.name for p in scanned} >= {
        "ingress.py",
        "external_records.py",
        "register_in_place.py",
        "lineage.py",
    }, (
        f"the D3 scan did not glob the importer modules — did _IMPORTER move? It scanned: {[p.name for p in scanned]} "
        "(an empty/wrong glob would make the offenders assertion below vacuously GREEN)"
    )
    offenders: list[str] = []
    for py in scanned:
        rel = py.relative_to(_SRC).as_posix()
        src = py.read_text(encoding="utf-8")
        if ".put(" in src:
            offenders.append(f"{rel}: .put(")
        offenders += [f"{rel}: {tok}" for tok in _FORBIDDEN if tok in src]
    assert offenders == [], (
        f"importer subsystem is register-in-place only (D3) — no cloud put/backend allowed: {offenders}"
    )


def test_rt_import_batch_handle_is_unforgeable():
    """The ImportBatchHandle capability token is constructed ONLY in importer/ingress.py (mirrors the AzureBlobBackend
    one-callsite scan, test_rt_lanes.py:120-138) — so no writer can fabricate a token to bypass the ingress gate. The
    token type is named DISTINCTLY from the ORM `ImportBatch(Base)` row class (db/orm.py), so this scan targets only
    the capability token and a legitimate rank-5 ORM row insert (`ImportBatch(...)`) never false-reddens it."""
    callsites: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        src = py.read_text(encoding="utf-8")
        callsites += [rel for _ in re.finditer(r"(?<!class )ImportBatchHandle\(", src)]
    assert set(callsites) == {"importer/ingress.py"}, (
        f"ImportBatchHandle( constructed outside the ingress gate: {sorted(set(callsites))} — the token must be "
        "minted only by open_import_batch (update this probe only with a justification)"
    )


def test_rt_importer_ingress_carries_the_durability_consult():
    """The ADR-0020 consult must live IN the ingress choke point. AST-based (NOT a string scan): count actual
    Call nodes to assert_durability_ok, so the module DOCSTRING's `assert_durability_ok(repo.engine)` prose does not
    satisfy it — only a real call does. A refactor that drops the call reddens this. The true consult guard is the
    behavioral probe test_gate_blocks_on_flipped_durability_canonical; this is an independent structural backstop that
    runs without a DB."""
    tree = ast.parse((_SRC / "importer" / "ingress.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ]
    assert calls, (
        "importer/ingress.py must CALL assert_durability_ok(...) — the ADR-0020 durability consult on the canonical "
        "lane (a docstring mention does not count)"
    )


def test_rt_external_records_writer_has_no_durability_consult():
    """The rank-5 Layer-1 writer must NOT re-consult the ADR-0020 durability gate per row — the rank-4 batch-open gate
    (open_import_batch) consulted it ONCE per batch (readiness §4·3; assert_durability_ok opens two txns per call). AST-
    based (counts Call nodes, so the module docstring's prose mention of assert_durability_ok does not satisfy it); a
    per-row consult reddens this the moment it is added."""
    tree = ast.parse((_IMPORTER / "external_records.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ]
    assert calls == [], (
        "importer/external_records.py must NOT call assert_durability_ok — the durability consult is once-per-batch at "
        "open_import_batch (rank 4), never per external_records row (readiness §4·3)"
    )


def test_rt_register_in_place_has_no_durability_consult():
    """The rank-6 register-in-place writer must NOT re-consult the ADR-0020 durability gate per artifact — the rank-4
    batch-open gate consulted it ONCE per batch (readiness §4·3). AST-based (Call nodes), so a docstring mention does not
    satisfy it; a per-artifact consult reddens this the moment it is added."""
    tree = ast.parse((_IMPORTER / "register_in_place.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ]
    assert calls == [], (
        "importer/register_in_place.py must NOT call assert_durability_ok — the durability consult is once-per-batch at "
        "open_import_batch (rank 4), never per registered artifact (readiness §4·3)"
    )


def test_rt_lineage_writer_has_no_durability_consult():
    """The rank-7 lineage writer must NOT re-consult the ADR-0020 durability gate per edge — the rank-4 batch-open gate
    consulted it ONCE per batch (readiness §4·3). What makes that TRUE rather than merely asserted is the writer's
    `ImportBatchHandle` requirement (test_importer_lineage.py::test_writer_requires_an_import_batch_handle): on importer
    paths the token is the only thing forcing the consult, since the gate is on no Repository path and lineage_edges —
    unlike external_records — has no import_batch_id FK to carry it. AST-based (Call nodes), so a docstring mention does
    not satisfy it; a per-edge consult reddens this the moment it is added."""
    tree = ast.parse((_IMPORTER / "lineage.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ]
    assert calls == [], (
        "importer/lineage.py must NOT call assert_durability_ok — the durability consult is once-per-batch at "
        "open_import_batch (rank 4), never per lineage edge (readiness §4·3)"
    )


def test_rt_importer_lineage_insert_is_scoped_to_lineage_py():
    """Within importer/**, `lineage.py` is the ONLY lineage_edges INSERT site — so the grammar's canonicalization and
    the keep-first ON CONFLICT cannot be bypassed by a second importer writer. An idempotency + grammar backstop.

    (The GO's A7 fold said to drop the "grammar" half on the grounds that the closed-kind parser already protects it.
    DECLINED deliberately, and recorded: the parser protects CALLERS OF write_lineage_edge, but a second RAW INSERT site
    inside importer/** would bypass the canonicalization exactly as it bypasses the ON CONFLICT — so the grammar half of
    the claim is true and worth keeping.)

    DELIBERATE CARVE-OUT — this is scoped to importer/** (via `_IMPORTER`, matching the rank-5/6 scans), NOT src-wide:
    grants.sql:24-27 GRANTs `neuro_writer` INSERT on lineage_edges precisely so workers can legitimately write
    linked_call + bundle-manifest edges, and capture/adapters/openrouter.py is the NAMED STAGE-2 producer of exactly
    those. `capture/` and `bundles/` are grant-sanctioned NON-importer producers and are deliberately out of scope; a
    src-wide pin would be green only because STAGE 2 is unbuilt, and would falsely redden the moment that already-
    architected path lands — pressuring its builder to route worker edges through the importer's admin-DSN module (a C3
    boundary inversion) or to delete this probe.

    Matches the FULL literal `INSERT INTO neuro.lineage_edges` as a substring (NOT a regex — the unescaped `.` would be
    a wildcard), never the bare `lineage_edges` token (which false-REDs on docstrings/comments). The `LineageEdge(`
    half matches nothing today (lineage.py writes raw SQL; `LineageEdgeResult(` does not match — the `R` intervenes) and
    is cheap forward insurance against a future `session.add(LineageEdge(...))` ORM insert. Unlike the D3 scan above
    this needs no non-emptiness pin: `set(sites) == {"importer/lineage.py"}` is a NON-EMPTY equality, so an empty/
    mis-resolved glob reddens it by construction."""
    sites: list[str] = []
    for py in sorted(_IMPORTER.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        src = py.read_text(encoding="utf-8")
        if "INSERT INTO neuro.lineage_edges" in src:
            sites.append(rel)
        sites += [rel for _ in re.finditer(r"(?<!class )LineageEdge\(", src)]
    assert set(sites) == {"importer/lineage.py"}, (
        f"lineage_edges is written from more than one importer site: {sorted(set(sites))} — the grammar + keep-first "
        "must not be bypassable (update this probe only with a justification; non-importer producers are out of scope "
        "by design, see the docstring)"
    )
