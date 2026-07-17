"""Importer rank 7 — the FIRST `lineage_edges` producer + THE ONE-SHOT entity-reference GRAMMAR (ADR-0043; ADR-0047).

This module is the first code in the system to write a lineage edge. `lineage_edges.src_entity`/`dst_entity` are
`text NOT NULL` and DELIBERATELY UN-FK'd (the frozen DDL seeds only the example `'run:1234'`), so the reference grammar
is a CONVENTION THIS MODULE INVENTS AND EVERY FUTURE READER INHERITS. Nothing in the schema enforces it. It is
NORMATIVE — read it before writing any edge, and extend it deliberately (never by guessing at a call site).

============================ THE GRAMMAR (normative — three rules) ============================

RULE 1 — FORMAT / CANONICAL FORM: `<entity-noun>:<pk>`, matched by `_REF_RE` with `re.fullmatch`.
    EXACTLY ONE STRING DENOTES ONE ENTITY. No leading zeros; no `pk=0` (every PK is GENERATED ALWAYS AS IDENTITY ⇒
    starts at 1); no trailing newline (`fullmatch`, not `match`); no bare/leading underscore.
    WHY IT IS LOAD-BEARING: the `UNIQUE (edge_kind, src_entity, dst_entity)` is on the RAW STRING. If two spellings of
    one entity were legal, `artifact:7` and `artifact:007` would be TWO rows for ONE artifact, `ON CONFLICT` would never
    fire, keep-first would silently fail to dedupe, and a reader traversing `dst_entity='artifact:7'` would MISS the
    `artifact:007` edge — on the D1-confidentiality-recovery path (see `link_registered_artifact`), that is a fail-open.
    Callers never hand a raw string to the DB: `write_lineage_edge` re-formats every ref through `entity_ref` first.

RULE 2 — THE NOUN: the ORM CLASS NAME in snake_case (equivalently: the singularized `__tablename__`).
    ***NEVER THE PK-COLUMN STEM.*** The rule is total and collision-free across all 45 ORM classes and reproduces every
    case where the two derivations agree (`Run`→`run`, `Artifact`→`artifact`, `ExternalRecord`→`external_record`,
    matching the frozen DDL's seeded `'run:1234'`). NINE classes discriminate; the SIX REFERENCEABLE ones are pinned
    below — these are exactly what a future extender gets wrong. The other three are INEXPRESSIBLE under RULE 1's
    `[1-9][0-9]*` pk group and must NEVER be added to ENTITY_KINDS: `database_identity` (pk `only_row`, Boolean),
    `job_dependencies` (COMPOSITE pk), `system_health` (pk `health_key`, Text). ⚠ `job_dependencies` is the live trap:
    its pk stem is `job_id`, so a naive stem-derivation mints `job:5` — which PASSES `_REF_RE` while denoting a `runs`-
    adjacent `jobs` row, NOT a `job_dependencies` row. The six referenceable discriminators:

        table (PK column)                          ✅ noun                              ❌ NOT
        model_identities (model_id)                model_identity:88                   model:88
        tokenizer_identities (tokenizer_id)        tokenizer_identity:5                tokenizer:5
        storage_backends (backend_id)              storage_backend:1                   backend:1
        divergence_measurements (divergence_id)    divergence_measurement:3            divergence:3
        expected_reproducibility_rules (rule_id)   expected_reproducibility_rule:2     rule:2
        work_leases (lease_id)                     work_lease:9                        lease:9

    `model_identity` is independently corroborated by the one entity-kind vocabulary the schema already ships
    (`promotions.promoted_kind` value `'model_identity'`).

RULE 3 — THE DIRECTION: A ROW READS AS THE ENGLISH SENTENCE `src <edge_kind> dst`.
    Equivalently: src = the referring / derived / annotating party; dst = the referent / source / annotated party.
    ***COPY THIS RULE — NEVER THE FIRST CUSTOMER'S src-POSITION.*** The 8 `edge_kind` labels span THREE directional
    families, so a position copied from one family is backwards in another:

        passive / backward (src = the dependent):  derived_from, replicate_of, promoted_from, paraphrase_of, generated_by
        active  / forward  (src = the agent):      annotates, curates
        non-directional:                           linked_call  (capture_event↔capture_event; convention DEFERRED to
                                                                 STAGE 2 — do not infer one from this module)

    One worked example per directional family:
        `external_record:M annotates artifact:N`            — the manifest annotates the binary  (THIS module, rank 7)
        `mcq_item:N promoted_from external_record:M`        — the native row was promoted FROM the record  (rank 8a)
        `model_identity:N promoted_from external_record:M`  — a FUTURE promotion; D5 defers model-identity promotion
                                                              out of v1, so nothing writes this today
    Note neither `promoted_from` example is `external_record promoted_from <native>`: a mapper that copied rank 7's
    src-position instead of the sentence rule would write it backwards. Corroborated by the typed-FK precedent
    `prompt_sets.derived_from_run_id` — the derived party holds the pointer to its source, i.e. src→dst.

============================ SCOPE, NAMESPACE, IDEMPOTENCY ============================

ENTITY_KINDS is LOAD-BEARING, not advisory: `parse_entity_ref` (and therefore `write_lineage_edge`) refuses an unknown
noun, so a typo (`extenral_record:1`), a plural table name (`artifacts:45` — the mistake RULE 2 forbids), or a ref to a
forbidden table (`mcq_responses:9`) cannot reach the DB. The map is FROZEN (`MappingProxyType`), so the vocabulary
cannot be widened at a distance either — a caller doing `ENTITY_KINDS['run'] = 'runs'` in its own module gets a
TypeError, not a silent new noun. Rank 7 seeded ONLY the Layer-1 nouns; rank 8a added `mcq_item` (its promotion
target). Further nouns (`run`, `model_identity`, `prompt_set`, `campaign`, `actor`, …) arrive the same way — a
DELIBERATE one-line addition HERE, under RULE 2, one per noun — never a guess at a call site. The exact-equality test
on this map is the no-front-run mechanism: it must never be loosened to a subset check.

NAMESPACE: `promotions.promoted_kind` (un-CHECKed text: 'model_identity' | 'mcq_stimulus' | 'run_summary') is a
DIFFERENT namespace that encodes a similar relationship. A `promoted_from` edge references the CONCRETE NATIVE ROW
(`mcq_item:{promoted_pk} promoted_from external_record:{M}` — rank 8a's shipped instance), NEVER the `promoted_kind`
string; 'mcq_stimulus'/'run_summary' have no entity noun (there is no `McqStimulus` class and no `mcq_stimuli` table
— rank 8a BRIDGES the kind to a DIFFERENT noun in `importer/promote.py`, it does not make the kind a noun).

IDEMPOTENCY = PURE KEEP-FIRST (ADR-0047; ruled 2026-07-16). A re-write of an existing `(edge_kind, src, dst)` triple
with a different `method_version_id` or `note` KEEPS THE FIRST — it does not raise and does not update. That is a real
semantic, stated deliberately: the UNIQUE declares that THE RELATIONSHIP is the identity, and `method_version_id`
attributes the FIRST ASSERTION, not the relationship. "Method v1 asserted X annotates Y" and "method v2 asserted X
annotates Y" are BOTH TRUE — a re-assertion, not a contradiction — unlike a divergent D1 stamp (rank 5) or divergent
bytes (rank 6), where two claims about one keyed object genuinely contradict and therefore RAISE. Raising here would be
false-loud. (`method_version_id` is nullable and deliberately EXCLUDED from the UNIQUE by the DDL.)

Durability is NOT re-consulted here — the rank-4 gate `open_import_batch` consulted `assert_durability_ok` ONCE per
batch (readiness §4·3); a per-edge consult is forbidden and structurally pinned
(tests/redteam/test_rt_importer_no_cloud_put.py). The `ImportBatchHandle` requirement is what makes that true: on
importer paths, holding the token is the ONLY thing forcing the ADR-0020 consult (the gate is on no `Repository` path),
and unlike rank 5 there is NO `import_batch_id` FK here to carry it. Note the handle is a DISCIPLINARY token — nothing
about the batch is persisted on the edge, so NO READER MAY TREAT the handle requirement as gate coverage ON the edge.

NOT here: no Layer-2 edges (promotions/runs/model identities — D5), no corpus fan-out (the 4 corpora's
derived_from/promoted_from/generated_by/paraphrase_of mappings ride with the appendix-A mappers / rank 8), no CLI (no
predecessor-corpus reader exists yet), no cloud PUT (D3). PRECONDITION: part of the admin-DSN importer flow.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from sqlalchemy import text

from ..db.orm import EdgeKind
from .ingress import ImportBatchHandle, ImporterIngressError

if TYPE_CHECKING:
    from ..db.repository import Repository

# The entity nouns rank 7 can reference, mapped to their table (so a reader resolving `external_record:123` back to a
# row gets the table for free). RULE 2 governs every addition. Layer-2 nouns arrive at rank 8, deliberately.
# FROZEN (MappingProxyType) so "a Layer-2 noun is a deliberate addition, never a guess at a call site" is a TypeError
# rather than prose — a caller that did `ENTITY_KINDS['run'] = 'runs'` in its own module could otherwise widen this
# module's closed vocabulary at a distance. Matches the closed-vocabulary idiom of `_EDGE_KINDS` below (and rank 4's
# StrEnums / rank 6's frozenset).
ENTITY_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "external_record": "external_records",
        "artifact": "artifacts",
        "mcq_item": "mcq_items",  # rank 8a — the FIRST Layer-2 noun (McqItem -> mcq_item; RULE 2, both derivations agree)
    }
)

_EDGE_KINDS = frozenset(EdgeKind.enums)  # derived from the ORM enum — no hand-copy drift (the rank-6 idiom)

# RULE 1, canonical form: lowercase snake_case noun; pk >= 1 with no leading zeros. Applied with fullmatch (NOT match —
# with `$` alone, `re.match` accepts a trailing newline, e.g. 'artifact:45\n').
_REF_RE = re.compile(r"^[a-z][a-z_]*:[1-9][0-9]*$")

# The ruled first-customer edge kind (owner nod 2026-07-16): the rank-5 stamped manifest ANNOTATES the rank-6 binary.
ARTIFACT_MANIFEST_EDGE_KIND = "annotates"

_INSERT = text(
    "INSERT INTO neuro.lineage_edges (edge_kind, src_entity, dst_entity, method_version_id, note) "
    "VALUES (CAST(:ek AS neuro.edge_kind), :src, :dst, :mvid, :note) "
    "ON CONFLICT (edge_kind, src_entity, dst_entity) DO NOTHING RETURNING lineage_edge_id"
)

_RESELECT = text(
    "SELECT lineage_edge_id FROM neuro.lineage_edges "
    "WHERE edge_kind = CAST(:ek AS neuro.edge_kind) AND src_entity = :src AND dst_entity = :dst"
)


@dataclass(frozen=True)
class LineageEdgeResult:
    """The outcome of one lineage-edge write."""

    lineage_edge_id: int
    inserted: bool  # True = new edge; False = idempotent keep-first no-op (the triple already existed)


def entity_ref(kind: str, pk: int) -> str:
    """Build a canonical entity reference `<noun>:<pk>` (RULE 1 + RULE 2). `kind` must be a known noun in ENTITY_KINDS
    (fail closed — a typo or a plural table name is refused, never written); `pk` must be a real positive int. This is
    the ONLY sanctioned way to construct a ref: the grammar lives here, not at call sites."""
    if kind not in ENTITY_KINDS:
        raise ImporterIngressError(
            f"entity kind {kind!r} is not a known entity noun (one of {sorted(ENTITY_KINDS)}) — refusing (fail closed). "
            "The noun is the ORM class name in snake_case, NEVER the PK-column stem (e.g. 'model_identity', not "
            "'model'); a Layer-2 noun is a deliberate ENTITY_KINDS addition at rank 8, not a call-site guess."
        )
    # bool is a subclass of int, so an unguarded `entity_ref('artifact', True)` mints 'artifact:True' (NOT 'artifact:1'
    # — f-string formats the bool). That is caught downstream by _REF_RE, so it never reaches the DB either way; the
    # guard is here so the refusal happens WHERE THE GRAMMAR IS DEFINED, typed and named, rather than as an incidental
    # regex miss at the writer (the rank-5 derived_by_predecessor precedent). An annotation does not type-check at
    # runtime, so the isinstance is what makes the rule real.
    if isinstance(pk, bool) or not isinstance(pk, int):
        raise ImporterIngressError(
            f"pk must be an int, got {pk!r} — refusing (fail closed; a coercible/bool pk would mint a wrong ref)."
        )
    if pk < 1:
        raise ImporterIngressError(
            f"pk must be >= 1, got {pk!r} — refusing (fail closed; every PK is GENERATED ALWAYS AS IDENTITY and starts "
            "at 1, so a 0/negative ref is permanently dangling)."
        )
    return f"{kind}:{pk}"


def parse_entity_ref(ref: str) -> tuple[str, int]:
    """Parse a canonical entity reference into `(noun, pk)` — the reader half of the grammar (every future reader parses
    these). Fail-closed on a non-canonical FORM (RULE 1: leading zeros, pk 0, trailing newline, bad case) and on an
    unknown NOUN (ENTITY_KINDS is load-bearing, so `artifacts:45`/`mcq_responses:9`/`extenral_record:1` are refused)."""
    if not isinstance(ref, str):
        raise ImporterIngressError(f"entity ref must be a str, got {ref!r} — refusing (fail closed).")
    if _REF_RE.fullmatch(ref) is None:
        raise ImporterIngressError(
            f"entity ref {ref!r} is not canonical — refusing (fail closed). Required form: '<noun>:<pk>' (lowercase "
            "snake_case noun; pk >= 1, no leading zeros, no surrounding whitespace). EXACTLY ONE STRING DENOTES ONE "
            "ENTITY — a non-canonical alias would defeat the UNIQUE that keep-first idempotency rests on."
        )
    kind, _, digits = ref.partition(":")
    if kind not in ENTITY_KINDS:
        raise ImporterIngressError(
            f"entity ref {ref!r} names an unknown entity noun {kind!r} (one of {sorted(ENTITY_KINDS)}) — refusing (fail "
            "closed). The noun is the ORM class name in snake_case (singular), NEVER the plural table name nor the "
            "PK-column stem."
        )
    return kind, int(digits)


def external_record_ref(pk: int) -> str:
    """The canonical ref for an `external_records` row (`ExternalRecordResult.external_record_id`)."""
    return entity_ref("external_record", pk)


def artifact_ref(pk: int) -> str:
    """The canonical ref for an `artifacts` row (`RegisteredArtifact.artifact_id`)."""
    return entity_ref("artifact", pk)


def write_lineage_edge(
    repo: Repository,
    handle: ImportBatchHandle,
    *,
    edge_kind: str,
    src_entity: str,
    dst_entity: str,
    method_version_id: int | None = None,
    note: str | None = None,
) -> LineageEdgeResult:
    """Write ONE lineage edge, keep-first idempotent (see the module docstring for the grammar and the keep-first
    semantic). The row reads as the sentence `src_entity <edge_kind> dst_entity` (RULE 3). Requires an
    `ImportBatchHandle` from `open_import_batch` — on importer paths that token is the only thing forcing the ADR-0020
    consult, and unlike rank 5 there is no `import_batch_id` FK here to carry it. Raises `ImporterIngressError` on an
    invalid handle, an unknown `edge_kind`, or a non-canonical/unknown-noun ref."""
    # Fail-closed input validation — annotations do NOT type-check at runtime (ingress.py:92-94), so these guards are
    # load-bearing. All of them fire BEFORE any DB work, so a refusal writes nothing.
    if not isinstance(handle, ImportBatchHandle):
        raise ImporterIngressError(
            "handle must be an ImportBatchHandle from open_import_batch — a raw id is refused (fail closed). On "
            "importer paths the handle is the only thing forcing the once-per-batch ADR-0020 durability consult."
        )
    if edge_kind not in _EDGE_KINDS:
        raise ImporterIngressError(
            f"edge_kind {edge_kind!r} is not a valid edge_kind (one of {sorted(_EDGE_KINDS)}) — refusing (fail closed). "
            "The vocabulary is closed: a new kind is a migration + an owner ruling, never a typo."
        )
    # Validate AND canonicalize: parse (form + closed noun) then re-format, so the string that reaches the UNIQUE is
    # always `entity_ref` output rather than a caller-supplied spelling. RULE 1's regex already rejects non-canonical
    # forms, so today this re-format is an identity — it is the belt that preserves the property if the regex is ever
    # loosened, and it means no caller string is passed through to the DB unexamined.
    src_entity = entity_ref(*parse_entity_ref(src_entity))
    dst_entity = entity_ref(*parse_entity_ref(dst_entity))
    params = {
        "ek": edge_kind,
        "src": src_entity,
        "dst": dst_entity,
        "mvid": method_version_id,
        "note": note,
    }
    # CAST(:ek AS neuro.edge_kind) is a second fail-closed belt (PG raises SQLSTATE 22P02 on a bad label). Do NOT
    # rewrite as `:ek::neuro.edge_kind` — SQLAlchemy text() mis-parses a named param adjacent to `::`
    # (db/repository.py:999-1002). psycopg3 binds the str as oid=0 (UNKNOWN), so PG infers the enum from context in
    # both the INSERT and the re-SELECT's WHERE.
    with repo.engine.begin() as conn:
        new_id = conn.execute(_INSERT, params).scalar_one_or_none()
        if new_id is not None:
            return LineageEdgeResult(int(new_id), inserted=True)
        # keep-first: the (edge_kind, src, dst) triple exists. There is deliberately NO drift check — see the module
        # docstring: the UNIQUE declares the relationship as the identity, so a differing method_version_id/note is a
        # re-assertion, not a contradiction (ADR-0047; ruled 2026-07-16).
        row = conn.execute(_RESELECT, {"ek": edge_kind, "src": src_entity, "dst": dst_entity}).one()
    return LineageEdgeResult(int(row.lineage_edge_id), inserted=False)


def link_registered_artifact(
    repo: Repository,
    handle: ImportBatchHandle,
    *,
    artifact_id: int,
    external_record_id: int,
    note: str | None = None,
) -> LineageEdgeResult:
    """Tie a rank-6 register-in-place binary `artifact` to its PAIRED rank-5 stamped `external_records` manifest — the
    first customer of this module, and the ONLY tie between the two (there is no FK between `artifacts` and
    `external_records`; the edge IS the relationship).

    Writes `external_record:{external_record_id} annotates artifact:{artifact_id}` (owner nod 2026-07-16): the manifest
    is the ANNOTATING party (src), the binary the ANNOTATED party (dst) — RULE 3's active/forward family. `annotates`,
    not `derived_from`: the manifest DESCRIBES the binary, it is not a transformation of it (rank 5 mints its
    `source_pk` from the manifest TEXT, not from the binary's bytes), and `derived_from` stays reserved for the genuine
    transformations rank 8's fan-out writes. The asymmetry is the point: a TEXT corpus is byte-preserved directly by
    rank 5 with no artifact and no edge — this edge exists ONLY for the binary pairing, where the external_record is a
    stand-in describing data it does not contain.

    HONEST REGISTER — read before relying on this: `artifacts` has no confidentiality column, so a binary's D1 grade
    lives on the paired manifest and THIS EDGE IS HOW A READER FINDS IT. But writing the link is a mapper DISCIPLINE
    (rank-6 owner ruling, discipline-for-v1), NOT a mechanism: nothing forces a mapper to call this, and detecting an
    unlinked binary by a retroactive edge scan is exactly the shape ADR-0049's Addendum permanently closed. So an absent
    edge does NOT mean an unstamped binary, and the presence of edges must never be relied on as coverage. A future
    mechanism (a confidentiality column on `artifacts`, or a choke point that writes both) would have to enter at the
    house gate standard. Note also that a reader traversing FROM a binary queries the un-indexed `dst_entity` end (the
    only index leads with `(edge_kind, src_entity)`) — a deliberate, recorded cost: the direction is un-unwindable, an
    index is additive and routine."""
    return write_lineage_edge(
        repo,
        handle,
        edge_kind=ARTIFACT_MANIFEST_EDGE_KIND,
        src_entity=external_record_ref(external_record_id),
        dst_entity=artifact_ref(artifact_id),
        note=note,
    )
