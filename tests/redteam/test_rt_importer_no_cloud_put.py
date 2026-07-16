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
    put/backend construction reddens this — update the probe ONLY with a justification."""
    offenders: list[str] = []
    for py in sorted(_IMPORTER.rglob("*.py")):
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
