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
        "promote.py",
        "mcq.py",
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


def test_rt_promote_has_no_durability_consult():
    """The rank-8a promotion writer must NOT re-consult the ADR-0020 durability gate per promotion — the rank-4
    batch-open gate consulted it ONCE per batch (readiness §4·3). The fourth consecutive rank to carry this pin (5/6/7
    above), which is what makes "the consult is once-per-batch, never per row" a MECHANISM rather than prose repeated in
    five docstrings. AST-based (Call nodes), so a docstring mention does not satisfy it."""
    tree = ast.parse((_IMPORTER / "promote.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ]
    assert calls == [], (
        "importer/promote.py must NOT call assert_durability_ok — the durability consult is once-per-batch at "
        "open_import_batch (rank 4), never per promotion (readiness §4·3)"
    )


def test_rt_promotion_method_token_is_unforgeable():
    """The PromotionMethod token is constructed ONLY in importer/promote.py (mirrors the ImportBatchHandle scan above),
    so a caller cannot fabricate a governance stamp. Forging `PromotionMethod(method_version_id=999)` would FK the
    promotions row to a version that did NOT perform the promotion — a silently false ADR-0011 stamp that satisfies the
    NOT-NULL FK and reads clean. This is the mechanism behind fork B's "the module mints it, never a raw
    method_version_id": without the scan, the token is only a convention.

    Scoped src-wide (like ImportBatchHandle, not like the importer-scoped INSERT scans): there is no grant-sanctioned
    non-importer producer of this token — it is this module's own type. `promote.py` is the ALLOWED site, so unlike
    `ImportBatchHandle(` it may name the constructor. The `(?<!class )` lookbehind excludes the dataclass definition;
    `PromotionResult(` cannot false-match (the `R` intervenes)."""
    callsites: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        src = py.read_text(encoding="utf-8")
        callsites += [rel for _ in re.finditer(r"(?<!class )PromotionMethod\(", src)]
    assert set(callsites) == {"importer/promote.py"}, (
        f"PromotionMethod( constructed outside importer/promote.py: {sorted(set(callsites))} — the governance stamp "
        "must be minted only by register_promotion_method (update this probe only with a justification)"
    )


def test_rt_promotions_insert_is_scoped_to_promote_py():
    """Within importer/**, `promote.py` is the ONLY promotions INSERT site — so the closed PromotedKind vocabulary
    cannot be bypassed. WITHOUT this, promote.py's "D5 by construction" is a FALSE NORMATIVE CLAIM: the enum refuses
    `model_identity` only for CALLERS of promote_external_record, and nothing makes that the only writer. A future
    appendix_a mapper (which inherits the D3 + lineage scans by rglob, and by omission would learn promotions is
    unconstrained) could raw-SQL `INSERT INTO neuro.promotions (..., 'model_identity', ...)` and every gate would stay
    GREEN: promoted_kind is un-CHECKed text (phase3-ddl.sql:628), the importer runs on the admin DSN (GRANT ALL), and
    the edge cannot backstop it (an absent edge means nothing to a reader — rank 7's honest register).

    DELIBERATE CARVE-OUT — scoped to importer/** (matching the lineage scan above), NOT src-wide: grants.sql:50-52
    sanctions `neuro_registrar` INSERT on promotions, so a src-wide pin would falsely redden a legitimate future
    non-importer producer and pressure its builder to route through the importer's admin-DSN module.

    Matches the FULL literal as a substring (NOT a regex — the unescaped `.` would be a wildcard). The `Promotion(`
    half is forward insurance against a `session.add(Promotion(...))` ORM insert; the `(?<!class )` lookbehind is
    load-bearing against `class Promotion(Base)` in db/orm.py (out of this scan's scope anyway) and `PromotionResult(`
    / `PromotionMethod(` cannot false-match (the `R`/`M` intervenes). `set(sites) == {...}` is a NON-EMPTY equality, so
    a mis-resolved glob reddens by construction — no separate non-emptiness pin needed."""
    sites: list[str] = []
    for py in sorted(_IMPORTER.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        src = py.read_text(encoding="utf-8")
        if "INSERT INTO neuro.promotions" in src:
            sites.append(rel)
        sites += [rel for _ in re.finditer(r"(?<!class )Promotion\(", src)]
    assert set(sites) == {"importer/promote.py"}, (
        f"promotions is written from more than one importer site: {sorted(set(sites))} — the closed PromotedKind "
        "vocabulary (and D5 with it) must not be bypassable (update this probe only with a justification)"
    )


def test_rt_promote_registers_the_method_once_per_batch_not_per_row():
    """RULING C (owner, 2026-07-16): the promotion method is registered at BATCH scope, never per promoted row.

    THIS is the mechanism for that ruling — a row-count probe CANNOT pin it, because register_method_version is
    idempotent: a per-row re-registration would return the same id and leave the method_versions count at 1, so the
    count stays GREEN while every row pays an INSERT...ON CONFLICT + a re-SELECT AND fires repository.py:684's
    `if set_active:` (which sits OUTSIDE the newly-inserted branch, so it runs on EVERY call) — N unconditional UPDATEs
    writing an unchanging value to one shared `methods` row, serializing a bulk import on that row-lock and leaving a
    dead tuple per promotion.

    AST-based: `register_method_version` may be called ONLY inside `register_promotion_method`, never inside
    `promote_external_record`. Moving the call back into the per-row path reddens this."""
    tree = ast.parse((_IMPORTER / "promote.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "register_method_version"
            ):
                offenders.append(fn.name)
    assert set(offenders) == {"register_promotion_method"}, (
        f"register_method_version is called from {sorted(set(offenders))} — it must be called ONLY by "
        "register_promotion_method (batch scope, ruling C). Per-row registration fires an unconditional "
        "methods.active_version_id UPDATE per promoted row (repository.py:684) and costs 2 extra round-trips per row."
    )


def test_rt_mcq_has_no_durability_consult():
    """The rank-8b MCQ mapper must NOT re-consult the ADR-0020 durability gate per question — the rank-4 batch-open
    gate consulted it ONCE per batch (readiness 4.3). The FIFTH consecutive rank to carry this pin (5/6/7/8a above),
    which is what makes "the consult is once-per-batch, never per row" a MECHANISM rather than prose repeated in six
    docstrings. AST-based (Call nodes), so a docstring mention does not satisfy it."""
    tree = ast.parse((_IMPORTER / "mcq.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "assert_durability_ok"
    ]
    assert calls == [], (
        "importer/mcq.py must NOT call assert_durability_ok — the durability consult is once-per-batch at "
        "open_import_batch (rank 4), never per mapped question (readiness 4.3)"
    )


def test_rt_mcq_never_mints_a_promotion_method():
    """RULING C at the NEW site. The existing ruling-C pin (test_rt_promote_registers_the_method_once_per_batch_not
    _per_row) parses ONLY promote.py, so a per-question `register_promotion_method(repo)` inside the mapper's loop
    would fire N registrations and redden NOTHING. Rank 8b RECEIVES the token from its caller and must mint none:
    it may call neither `register_promotion_method` nor `register_method_version`.

    AST-based on the Call nodes, so the module docstring's prose about batch-scoped registration cannot satisfy it.
    A row-count probe cannot pin this — registration is idempotent, so the method_versions count stays 1 either way
    while every row pays two extra round-trips and an unconditional methods.active_version_id UPDATE."""
    tree = ast.parse((_IMPORTER / "mcq.py").read_text(encoding="utf-8"))
    offenders = sorted(
        {
            n.func.id if isinstance(n.func, ast.Name) else n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (
                (
                    isinstance(n.func, ast.Name)
                    and n.func.id in {"register_promotion_method", "register_method_version"}
                )
                or (
                    isinstance(n.func, ast.Attribute)
                    and n.func.attr in {"register_promotion_method", "register_method_version"}
                )
            )
        }
    )
    assert offenders == [], (
        f"importer/mcq.py calls {offenders} — the promotion method is registered ONCE PER BATCH by the caller and "
        "passed in (ruling C). The mapper must never mint one."
    )


def test_rt_stimulus_family_inserts_are_scoped_to_mcq_py():
    """Within importer/**, `mcq.py` is the ONLY writer of the four stimulus-family tables — so the ruled
    content_hash rule, the item_ordinal binding, the refusal filter and the raise-on-drift guards cannot be
    bypassed by a second importer writer. Rank 8b is the FIRST producer of all four (grep-verified: zero INSERTs
    existed in src before it), so this is the unit that owes the pin — the rank-7 lineage / rank-8a promotions
    precedent.

    DELIBERATE CARVE-OUT, matching those two: scoped to importer/** via `_IMPORTER`, NOT src-wide. `grants.sql`
    GRANTs neuro_registrar INSERT on the stimulus family, so a legitimate future non-importer producer (a capture
    lane registering a first-class prompt_set — the deferred registry named in capture/events.py) must not falsely
    redden this and be pressured to route through the importer's admin-DSN module.

    Matches the FULL literal as a substring (NOT a regex — the unescaped `.` would be a wildcard). `set(sites) ==
    {...}` is a NON-EMPTY equality, so a mis-resolved glob reddens by construction; no separate non-emptiness pin
    is needed (unlike the D3 scan above, whose assertion is `offenders == []`)."""
    for table in ("prompt_sets", "prompt_items", "mcq_items", "mcq_options"):
        sites = sorted(
            {
                py.relative_to(_SRC).as_posix()
                for py in _IMPORTER.rglob("*.py")
                if f"INSERT INTO neuro.{table}" in py.read_text(encoding="utf-8")
            }
        )
        assert sites == ["importer/mcq.py"], (
            f"neuro.{table} is written from {sites} — the stimulus cascade must have exactly ONE importer writer "
            "(update this probe only with a justification; non-importer producers are out of scope by design)"
        )


def test_rt_mcq_never_writes_the_deferred_stimulus_tables():
    """mcq_permutations is DEFERRED with the runs (VERIFIED: the stimulus corpus records no permutations — they are
    run output, a deterministic function of a strategy), stimulus_structures is DEFERRED (ADR-0023
    reserve-until-consumed), and mcq_responses is DELIBERATELY ABSENT FROM THE SCHEMA and must NEVER be created
    (ADR-0011 / C1). Without this pin those three deferrals are prose in a docstring."""
    offenders = sorted(
        {
            f"{py.relative_to(_SRC).as_posix()}: {table}"
            for py in _IMPORTER.rglob("*.py")
            for table in ("mcq_permutations", "stimulus_structures", "mcq_responses")
            if f"INSERT INTO neuro.{table}" in py.read_text(encoding="utf-8")
        }
    )
    assert offenders == [], (
        f"the importer writes a DEFERRED/FORBIDDEN stimulus table: {offenders} — mcq_permutations rides with the "
        "runs, stimulus_structures is ADR-0023 reserve-until-consumed, and mcq_responses must never exist"
    )
