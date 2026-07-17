"""Importer rank 8a — LAYER 2: the SELECTIVE PROMOTION primitive (ADR-0011; ADR-0043; ADR-0047).

Records that a Layer-1 `external_records` row BECAME a native row: one `promotions` row + one `promoted_from`
lineage edge, each promotion carrying a governance stamp this module mints. First `promotions` producer in the
system (rank 7 was the first `lineage_edges` producer); first extender of rank 7's entity-reference GRAMMAR.

SCOPE (owner-ruled 2026-07-16): the MCQ stimulus family ONLY.
    - `model_identity` is ABSENT from `PromotedKind` — **D5 by construction**, not by discipline. Promoting a
      predecessor's two-string model identity would force 'unknown' sentinels into four NOT-NULL columns PLUS a
      FABRICATED `tokenizer_hash` at a registry that raises on an unregistered tokenizer. D5 bites where the schema
      demands INVENTION; the MCQ family demands none (every NOT-NULL column is real exam content).
    - `run_summary` is ABSENT — `runs.actor_id`/`campaign_id` are NOT NULL, so promoting runs lights the ADR-0048
      layer-(b) owner-scoped-key watch-point. Its own unit.
    - `stimulus_structures` is NOT promoted (ADR-0023 reserve-until-consumed; its declared prototype
      `representation_hierarchy.py` is not in this repo).
    - `mcq_responses` is DELIBERATELY ABSENT from the schema and MUST NEVER be created (ADR-0011 / C1;
      tests/reference/phase3-ddl.sql:542-544). Rank 7's closed-noun parser already refuses `mcq_responses:9`.

============================ ADR-0011's GOVERNANCE TRIO DOES NOT ATTACH HERE ============================

ADR-0011's trio (`method_version_id` on every derived row + a `neuro derive` re-derive CLI + a parity probe
comparing the satellite to its LAKE SOURCE) governs a promoted DERIVED SATELLITE. It does not attach to this
family — on the ADR's own words and on the schema's mechanics:
    1. ADR-0011's OWN Consequences line excludes it BY NAME: "Distinct from the MCQ *stimulus* family, which is
       always-on first-class PG (ADR-0023)."
    2. NO table in the stimulus family carries a `method_version_id` column AT ALL. Schema-wide there are exactly
       FOUR carriers — `intervention_specs` (phase3-ddl.sql:129), `divergence_measurements` (:290), `promotions`
       (:630) and `lineage_edges` (:659); `:113` is `method_versions`' OWN primary key, not a carrier. The stimulus
       family (:487-541) has none. A table that cannot carry leg 1 was never the trio's subject — leg 1 is
       uncomputable there exactly as legs 2/3 are.
    3. Legs 2/3 are DEFINED on a derived table (re-derive it; compare it to its lake source). This family has no lake
       source: it comes from an `external_records` row (byte-preserved TEXT), not from parquet. `neuro derive
       run|reparity` are both STAGE-2 stubs — a unit claiming the full trio would overclaim against two stubs.
    4. Corroboration, scope stated: phase3-ddl.sql:542-544 says `mcq_responses` "is created by a future,
       demand-triggered migration paying the governance trio" — that TABLE pays the trio at ITS migration. It is
       about mcq_responses specifically and is NOT decisive for the proposition here.
What this module pays is the `promotions.method_version_id` stamp (phase3-ddl.sql:630, commented "governance trio")
on the promotion ACT — paid STRUCTURALLY: this module MINTS the version itself (see PromotionMethod). If
`mcq_responses` is ever promoted, ADR-0011's full trio + its demand-triggered migration apply, unchanged.

============================ THE TWO UN-FK'd NAMESPACES — BOTH CLOSED IN CODE ============================

`promotions.promoted_kind` is UN-CHECKed text and `promoted_pk` is a BARE bigint with NO FK (phase3-ddl.sql:628-629)
— PostgreSQL cannot express a polymorphic FK, so NOTHING in the schema catches a bad value in either. Code is the
only mechanism that exists, and both are closed here:
    - the KIND namespace by the closed `PromotedKind` StrEnum (rank 4's idiom, ingress.py:104-117);
    - the PK namespace by the target-existence guard below — WITHOUT it, a promotion naming `promoted_pk=999999`
      against an empty `mcq_items` would succeed, write an authoritative promotions row + a `mcq_item:999999
      promoted_from external_record:1` edge, and BOTH would read CLEAN forever. The raise-on-`promoted_pk`-drift
      rule would then make it PERMANENT: the later CORRECT promotion collides on the UNIQUE, sees drift, and raises
      — the API that wrote the lie refusing to fix it. Fail-open AND one-way. The FK's ABSENCE is the reason to
      check in code — the same reasoning by which rank 4's choke point carries the ADR-0020 consult that sits on no
      `Repository` path.

`promoted_kind` is a DIFFERENT NAMESPACE from the lineage nouns (rank 7, lineage.py:63-66): a `promoted_from` edge
references the CONCRETE NATIVE ROW (`mcq_item:{promoted_pk}`), NEVER the kind string. `_PROMOTED_KIND_TARGET` is the
bridge, and it carries the ORM CLASS so the existence guard resolves table + PK column FROM THE SCHEMA rather than
by string-building `f"{noun}_id"` — which is right for `mcq_item_id` BY COINCIDENCE and silently wrong at the first
of RULE 2's six discriminators (`model_identities.model_id`).

============================ IDEMPOTENCY — MIXED, AND THE RULES ARE NOT SYMMETRIC ============================

The UNIQUE is `(external_record_id, promoted_kind)`, so a re-promotion COLLIDES and the two out-of-key columns split:
    - `promoted_pk` DRIFT **RAISES** `IdentityMismatchError` **UNCONDITIONALLY** (the rank-5/6 class): two claims
      about one keyed object CONTRADICT — one record cannot have become two different native rows under one kind.
      Keeping first would strand the second native row with NO promotion record, and `promoted_pk` is un-FK'd so
      nothing in the schema would catch it.
    - `method_version_id` is **KEEP-FIRST** (the rank-7 class, ADR-0047): a re-assertion, not a contradiction. It
      attributes the FIRST ASSERTION, not the relationship. Raising would be false-loud.
THE GUARD IS SINGLE-COLUMN. `method_version_id` MUST NOT appear in the drift condition: the shape
`if row.method_version_id == mv_id and row.promoted_pk != promoted_pk:` passes a pk-only probe AND a method-only
probe while SILENTLY keep-firsting a `promoted_pk` CONTRADICTION — the exact bug class this repo has a post-mortem
for (repository.py:667-670: an extra conjunct made the guard fail-open and a bad row was "silently ADOPTED").
WHEN the keep-first branch can fire: under the module-constant method every promotion in a build resolves to ONE
identical `method_version_id` (repository.py:648-683), so it fires only ACROSS releases, via the forced
code_sha->semver-bump chain (:678-682 raises on code_sha drift at a constant semver, so any future build that edits
`promote_external_record`'s body MUST bump the semver and mint a new version). It is keep-first because RAISING
would make that v2 build's re-run over an already-promoted corpus explode on EVERY previously-promoted record — a
mass false-loud failure on the ordinary upgrade path.

============================ DURABILITY REGISTER (honest) ============================

The batch handle is a DISCIPLINARY token on this path. `promotions` has no `import_batch_id` FK (unlike
`external_records`), so nothing about THIS promotion's batch is persisted — no reader may treat the handle
requirement as ADR-0020 gate coverage on the promotions row, and nothing records which batch performed the
promotion. What IS structural: `promotions.external_record_id` is a NOT-NULL FK to `external_records`, whose
`import_batch_id` is a NOT-NULL FK to `import_batches` — so every promotions row structurally proves its REFERENCED
RECORD came through a durability-consulted `open_import_batch`. The unprovable thing is the promotion's OWN batch,
not the record's gating. Durability is NOT re-consulted here (once-per-batch at rank 4; structurally pinned in
tests/redteam/test_rt_importer_no_cloud_put.py).

NOT here: no corpus reader and no MCQ cascade writer (rank 8b — its `prompt_sets.content_hash` minting rule is an
open owner ruling: a SET spans many items, so the hash is not knowable until every item is in); no CLI (the mappers
are the consumer; `neuro importer scan` is likewise still a stage2 stub though rank 5 shipped its writer); no cloud
PUT (D3). PRECONDITION: part of the admin-DSN importer flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from ..db.orm import McqItem
from ..db.repository import IdentityMismatchError
from .ingress import ImportBatchHandle, ImporterIngressError
from .lineage import entity_ref, external_record_ref, write_lineage_edge

if TYPE_CHECKING:
    from ..db.repository import Repository


class PromotedKind(StrEnum):
    """The closed `promotions.promoted_kind` vocabulary. The COLUMN is un-CHECKed text (phase3-ddl.sql:628) whose
    three values live only in a `--` comment, so this enum is the ONLY vocabulary that exists.

    `model_identity` and `run_summary` are DELIBERATELY ABSENT (see the module docstring): their absence is what
    makes D5 and the watch-point deferral REFUSALS BY CONSTRUCTION rather than rules someone must remember. Adding a
    member is an owner ruling + a `_PROMOTED_KIND_TARGET` entry (a member without a target is refused by test).
    """

    MCQ_STIMULUS = "mcq_stimulus"


# The BRIDGE from the promoted_kind namespace to rank 7's entity-noun namespace. Carries the ORM CLASS (not a bare
# noun) so the target-existence guard resolves table + PK column FROM THE SCHEMA — never by string-building
# f"{noun}_id", which is right for mcq_item_id by coincidence and silently wrong at model_identities.model_id.
# FROZEN (MappingProxyType), matching lineage.py's ENTITY_KINDS: a caller cannot widen this vocabulary at a distance.
_PROMOTED_KIND_TARGET: MappingProxyType[PromotedKind, tuple[str, type]] = MappingProxyType(
    {PromotedKind.MCQ_STIMULUS: ("mcq_item", McqItem)}
)

PROMOTION_METHOD_KEY = "importer_promotion"  # the capture/determinism.py:170-171 precedent
PROMOTION_METHOD_SEMVER = "1.0.0"

_PROMOTIONS_INSERT = text(
    "INSERT INTO neuro.promotions (external_record_id, promoted_kind, promoted_pk, method_version_id) "
    "VALUES (:erid, :kind, :pk, :mvid) "
    "ON CONFLICT (external_record_id, promoted_kind) DO NOTHING RETURNING promotion_id"
)

_PROMOTIONS_RESELECT = text(
    "SELECT promotion_id, promoted_pk, method_version_id FROM neuro.promotions "
    "WHERE external_record_id = :erid AND promoted_kind = :kind"
)


def _resolve_target(cls: type) -> tuple[str, str]:
    """Resolve `(table, pk_column)` FROM the ORM mapper — the schema is the single source of truth.

    NEVER string-build the pk as f"{noun}_id". That is right for `mcq_item_id` BY COINCIDENCE and silently wrong at
    the first of RULE 2's six discriminators: `model_identities`'s pk is `model_id`, not `model_identity_id`. Today
    `_PROMOTED_KIND_TARGET` holds only McqItem, so a string-built variant would pass every promotion probe — which is
    exactly why this is a named function with its own discriminating probe rather than an inline expression.
    """
    mapper = sa_inspect(cls)
    if len(mapper.primary_key) != 1:
        raise ImporterIngressError(
            f"{cls.__name__} has a COMPOSITE primary key {[c.name for c in mapper.primary_key]} — refusing (fail "
            "closed). A single promoted_pk cannot name such a row, and checking only its first column would match "
            "a DIFFERENT row than the one named. RULE 1's '<noun>:<pk>' grammar cannot express a composite key."
        )
    return mapper.local_table.name, mapper.primary_key[0].name


@dataclass(frozen=True)
class PromotionResult:
    """The outcome of one promotion. Every id here is the value OF RECORD — read back from its row, never echoed
    from the caller's input — so a keep-first no-op reports what is actually persisted."""

    promotion_id: int
    # The stamp OF RECORD on the promotions row. On the keep-first path this is the FIRST version, NOT the token
    # you passed: ON CONFLICT DO NOTHING kept it, so a v2 caller re-running an already-promoted corpus gets v1.
    method_version_id: int
    lineage_edge_id: int
    promoted: bool  # True = new promotion; False = idempotent keep-first no-op (the pair already existed)


@dataclass(frozen=True)
class PromotionMethod:
    """The BATCH-SCOPED promotion-method token, minted ONLY by `register_promotion_method` (construction is
    scan-pinned to this module, mirroring rank 4's batch handle).

    It exists so `promote_external_record` can carry a governance stamp WITHOUT either (a) taking a raw
    `method_version_id` — which would let a caller supply an unminted id and defeat the register-first parity the
    stamp exists to provide — or (b) registering PER ROW. Per-row registration would fire
    `register_method_version`'s `if set_active:` (repository.py:684, which sits OUTSIDE the newly-inserted branch and
    so runs on EVERY call) once per promoted row: N unconditional UPDATEs writing an UNCHANGING value to one shared
    `methods` row, serializing a bulk import on that row-lock and leaving a dead tuple per promotion — plus an
    INSERT...ON CONFLICT + a re-SELECT per row, the same 2-round-trips-per-row cost the durability consult is
    deliberately hoisted to once-per-batch to avoid.

    It is a TOKEN rather than a memo on purpose: a module-level cache would have to be keyed on the engine/DB
    identity, and even then tests/conftest.py's `_truncate_all` empties `method_versions` between tests on the SAME
    database with the SAME identity — so a keyed memo would still hand back a stale id naming a deleted row. A token
    has no cache and no invalidation: the caller re-registers per batch.
    """

    method_version_id: int


def promotion_method_code_sha() -> bytes:
    """Content hash of the promotion implementation's source — the registered version's `code_sha`, so a change to
    `promote_external_record` without a semver bump raises at register-time (ADR-0011 registry/runtime parity).
    Mirrors `capture/determinism.py:193-200`.

    Known, inherited limit: the preimage is that one function. `_PROMOTED_KIND_TARGET` is a module constant OUTSIDE
    it, so re-targeting a kind would not move the sha. Tolerable because a bad target fails CLOSED at `entity_ref`
    (ENTITY_KINDS is frozen and closed) rather than silently — not a licence to widen the preimage piecemeal; the
    divergence precedent has the identical shape and they should move together if ever revisited.
    """
    import inspect

    from ..db.identity import sha256_bytes

    return sha256_bytes(inspect.getsource(promote_external_record).encode("utf-8"))


def register_promotion_method(repo: Repository) -> PromotionMethod:
    """Register `importer_promotion@SEMVER` ONCE PER BATCH and return the token `promote_external_record` requires.

    Call this once per import batch, NEVER per row (see PromotionMethod). `set_active=True` is passed explicitly (the
    kwarg has no default since the rank-1/2 hardening, so a caller cannot silently repoint the registry's
    active-version pointer); at batch scope that is one UPDATE per batch — which is what the one existing caller's
    shape (capture/events.py:741-746, one-shot per replay) actually justifies.
    """
    mv_id = repo.register_method_version(
        method_key=PROMOTION_METHOD_KEY,
        semver=PROMOTION_METHOD_SEMVER,
        code_sha=promotion_method_code_sha(),
        set_active=True,
    )
    return PromotionMethod(method_version_id=int(mv_id))


def promote_external_record(
    repo: Repository,
    handle: ImportBatchHandle,
    method: PromotionMethod,
    *,
    external_record_id: int,
    promoted_kind: PromotedKind,
    promoted_pk: int,
    note: str | None = None,
) -> PromotionResult:
    """Record that `external_record_id` was promoted into the native row `promoted_pk` of kind `promoted_kind`.

    Writes one `promotions` row and one `promoted_from` lineage edge. Idempotent keep-first on the
    `(external_record_id, promoted_kind)` pair; a divergent `promoted_pk` RAISES (see the module docstring for the
    mixed rule and why the guard is single-column).

    The edge reads as the sentence `<native> promoted_from <external_record>` (rank 7 RULE 3) — e.g.
    `mcq_item:5 promoted_from external_record:2`: the native row was promoted FROM the record. `promoted_from` sits
    in the PASSIVE/backward family (src = the dependent), unlike rank 7's own `annotates` customer, which is
    ACTIVE/forward — copying that module's src-POSITION instead of the sentence rule would write this backwards.

    Raises `ImporterIngressError` on an invalid handle/method/kind, a non-canonical ref, or a `promoted_pk` naming no
    live row; `IdentityMismatchError` on `promoted_pk` drift.
    """
    # Fail-closed input validation. Annotations do NOT type-check at runtime (ingress.py:104-106), so every isinstance
    # here is load-bearing. All of these fire BEFORE any DB work, so a refusal writes nothing.
    if not isinstance(handle, ImportBatchHandle):
        raise ImporterIngressError(
            "handle must be an import-batch token from open_import_batch — a raw id is refused (fail closed). On "
            "importer paths the handle is the only thing forcing the once-per-batch ADR-0020 durability consult."
        )
    if not isinstance(method, PromotionMethod):
        raise ImporterIngressError(
            "method must be a PromotionMethod from register_promotion_method — a raw method_version_id is refused "
            "(fail closed). The module mints the governance stamp so it cannot be supplied unregistered; register "
            "once per BATCH, never per row."
        )
    if not isinstance(promoted_kind, PromotedKind):
        raise ImporterIngressError(
            f"promoted_kind must be a PromotedKind member (one of {[k.value for k in PromotedKind]}), got "
            f"{promoted_kind!r} — refusing (fail closed). The column is un-CHECKed text, so this enum is the only "
            "vocabulary there is; 'model_identity' is absent BY CONSTRUCTION (D5)."
        )
    noun, target_cls = _PROMOTED_KIND_TARGET[promoted_kind]
    # RULE 1 + RULE 2 canonical refs, built only via rank 7's constructors (which carry the bool/int/>=1 guards).
    src_entity = entity_ref(noun, promoted_pk)
    dst_entity = external_record_ref(external_record_id)

    # TARGET EXISTENCE. promoted_pk is a bare bigint with NO FK (PostgreSQL cannot express a polymorphic FK), so this
    # READ is the only mechanism that can catch a promotion pointing at nothing. It runs BEFORE the promotions INSERT
    # so a refusal leaves no row and no edge. Table + PK column resolve FROM the ORM mapper, never string-built.
    # HONEST RESIDUAL: this check and the INSERT are separate transactions, so it is TOCTOU-NARROW, not TOCTOU-free —
    # sound only because nothing in the system deletes these rows and every PK is GENERATED ALWAYS AS IDENTITY (ids
    # are never reused, so a check that passed cannot later name a DIFFERENT row).
    table_name, pk_col = _resolve_target(target_cls)
    with repo.engine.begin() as conn:
        exists = conn.execute(
            text(f"SELECT 1 FROM neuro.{table_name} WHERE {pk_col} = :pk"),  # noqa: S608 — both from the ORM mapper
            {"pk": promoted_pk},
        ).scalar_one_or_none()
    if exists is None:
        raise ImporterIngressError(
            f"promoted_kind {promoted_kind.value!r} targets neuro.{table_name}, but {pk_col}={promoted_pk} does not "
            "exist — refusing (fail closed). promoted_pk is un-FK'd, so a promotion naming no live row would record "
            "an authoritative-looking lie that reads CLEAN forever, and the raise-on-drift rule would then make it "
            "permanent. Promote the native row first, then record the promotion."
        )

    with repo.engine.begin() as conn:
        new_id = conn.execute(
            _PROMOTIONS_INSERT,
            {
                "erid": external_record_id,
                "kind": promoted_kind.value,
                "pk": promoted_pk,
                "mvid": method.method_version_id,
            },
        ).scalar_one_or_none()
        if new_id is not None:
            promotion_id, promoted, stamp_id = int(new_id), True, method.method_version_id
        else:
            row = conn.execute(
                _PROMOTIONS_RESELECT, {"erid": external_record_id, "kind": promoted_kind.value}
            ).one()
            # SINGLE-COLUMN drift guard. method_version_id MUST NOT appear in this condition — an extra conjunct
            # would let a promoted_pk contradiction through silently (repository.py:667-670's post-mortem shape).
            if row.promoted_pk != promoted_pk:
                raise IdentityMismatchError(
                    f"external_record {external_record_id} is already promoted as {promoted_kind.value!r} to "
                    f"{noun}:{row.promoted_pk}, not {noun}:{promoted_pk} — refusing to record that one record became "
                    "two different native rows under one kind (register-first, fail-loud). Keeping the first "
                    "silently would strand the second row with no promotion record, and promoted_pk is un-FK'd so "
                    "nothing in the schema would catch it."
                )
            # The STAMP OF RECORD is the row's, not the caller's token. ON CONFLICT DO NOTHING deliberately KEPT
            # the first version, so here the persisted stamp is v1 while `method` may be v2 — echoing the token
            # would report a stamp NO ROW CARRIES, on precisely the branch keep-first exists for (a semver-bumped
            # build re-running over an already-promoted corpus: the ORDINARY upgrade path, i.e. EVERY record).
            # The re-SELECT already fetches this column, so reading it back costs nothing.
            promotion_id, promoted, stamp_id = int(row.promotion_id), False, int(row.method_version_id)

    # The edge, ALWAYS — the conflict path falls through to here, so a crash between the two writes is a RESUME (both
    # writes are keep-first), not a corruption. Order is deliberate: the promotions row is the authoritative record
    # and the edge its lineage view, so a promotion-without-edge (recoverable) beats an edge asserting a promotion
    # that does not exist. method_version_id passes through; rank 7 keeps it first-write-wins on the edge.
    # The edge carries the STAMP OF RECORD too. Where the edge already exists this is inert (rank 7 is keep-first
    # and ignores it). It matters on the HEAL path this ordering exists for — promotion committed, crash before the
    # edge, re-run under a bumped semver — where the edge is NEWLY inserted: echoing the token would stamp it v2
    # while its own promotion row says v1, so a promotion and its lineage view would disagree BY CONSTRUCTION.
    # This does NOT touch rank 7's keep-first ruling (which governs the CONFLICT path); it fixes only what rank 8
    # hands it on a fresh insert.
    edge = write_lineage_edge(
        repo,
        handle,
        edge_kind="promoted_from",
        src_entity=src_entity,
        dst_entity=dst_entity,
        method_version_id=stamp_id,
        note=note,
    )
    return PromotionResult(
        promotion_id=promotion_id,
        method_version_id=stamp_id,
        lineage_edge_id=edge.lineage_edge_id,
        promoted=promoted,
    )
