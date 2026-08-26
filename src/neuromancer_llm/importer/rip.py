"""RIP lane — the register-in-place corpus mapper (ranks 6+5+7 for ONE local file).

Registers an already-existing local file as a PERMANENT pointer triple: a rank-6 `artifacts` row (no
bytes copied, no cloud upload — D3), the PAIRED rank-5 `external_records` manifest row, and the
rank-7 `external_record:M annotates artifact:N` edge that is how a reader gets from the binary to its
provenance. This module WRITES NOTHING ITSELF: it composes the three shipped writers, so every
INSERT stays in the module that owns it and every red-team single-module scan stays true.

★ THE IDENTITY IS THE POINTER, NOT THE CURATION (owner ruling, 2026-08-13).

    source_bytes = canonical_bytes({"scheme": ..., "source_system": ..., "uri": ...})

and NOTHING else. `write_external_record` derives BOTH `source_pk` and `payload_text` from that one
value, so the key and the mirrored text are the same bytes by construction — they are ONE decision,
not two.

WHY SO NARROW, MEASURED. The corpus manifest is a MUTABLE BUILD PRODUCT, not an immutable upstream
row, so rank 8b's whole-row precedent does not transfer (E-5). Measured across the manifest's own
revision history: a whole-row preimage re-minted **100%** of `source_pk`s at one revision (a column
was ADDED) and 0.9% at another, while `uri` changed in **ZERO** rows at every revision — the curation
churns and the pointer does not. A whole-row key would also have swallowed two file-derived values
the CRLF law forbids in an identity: `size_bytes` is `os.path.getsize` (machine-varying for text),
and exactly one manifest `note` carries a sha256 OF THE POINTED-AT FILE'S OWN BYTES.

⚠ THE COST, STATED PLAINLY — this ruling trades one failure for another, it does not remove one.

  * `payload_text` is NO LONGER the byte-exact whole-row Layer-1 mirror. It carries the identity
    triple. The full manifest row rides in `payload_jsonb`, and the frozen DDL calls that column a
    "sidecar for querying", so this inverts its stated role, deliberately. ⚠ Precisely which fields
    the sidecar is the ONLY home for: `note`, the historical `confidentiality`, `family` and
    `workflow_seat` — FOUR. The other five (`kind`, `retention`, `dtype`, `shape`, `size_bytes`) are
    ALSO first-class `artifacts` columns written by rank 6, so the sidecar duplicates rather than
    uniquely preserves them. An earlier draft claimed all nine; a vet measured otherwise.
  * ★ THE MIRROR GOES SILENTLY STALE, BY DESIGN. Because the key no longer depends on curation, a
    RE-CURATED row at the same `uri` lands on the SAME `source_pk`, the INSERT no-ops, and the FIRST
    write's sidecar persists — un-updated and UN-RAISED, because rank 5's drift guard deliberately
    excludes `payload_jsonb` as a derived sidecar. So: re-running after a re-curation does NOT
    refresh the mirror. Reconciling is deliberate work, never a re-run.
  * ⚠ THE RANK-5 DRIFT RAISE IS UNREACHABLE **ONLY THROUGH THE DRIVER**, which is a DISCIPLINE, not
    a structure. Stated precisely because two earlier drafts got this wrong in OPPOSITE directions
    and a vet MEASURED both arms firing: `derived_by_predecessor` is a CALLER PARAMETER of
    `map_corpus_file`, not something derived inside it, so a library caller passing a different
    value under the same `source_system` lands on the SAME `source_pk` and the guard DOES raise.
    What makes it dormant in practice is that `ops/corpus-import/register_corpus.py` derives the
    value once per batch via `derived_by_predecessor_for` — nothing structural enforces that. The
    `confidentiality` arm is separately inert because the grade is retired. ⇒ the guard is neither
    weakened nor dead: REACHABLE from the library, DORMANT under the shipped driver.
  * ⚠ THE POINTERS ARE NOT DEREFERENCEABLE BY THE SYSTEM. `uri` is a hand-authored corpus relabel:
    its DIRECTORY PREFIX is invented, so the whole uri is a path-suffix of `source_abspath` in 0 of
    1,753 rows. ⚠ The BASENAME matches in all 1,753 — said plainly because an earlier draft claimed
    "ZERO derivability" flatly, which overclaims: a filename is recoverable, a location is not.
    Resolving a row back to a
    file rides `payload_jsonb.row.source_abspath`, which is MACHINE-LOCAL and true only of the host
    that registered it. Nothing in this repository can open one of these files from canonical alone.

`retention` is registered AS THE OPERATOR DECLARED IT. ⚠ No TTL reaper exists for register-in-place
pointers, so a `ttl` here is a LABEL, not a lifecycle: nothing will ever collect it. It is not
coerced to `keep_forever`, because silently rewriting a declared identity input is the thing this
repository refuses on principle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..db.identity import canonical_bytes
from .external_records import write_external_record
from .ingress import ImportBatchHandle, ImporterIngressError, SourceSystem
from .lineage import link_registered_artifact
from .register_in_place import register_artifact_in_place

if TYPE_CHECKING:
    from ..db.repository import Repository

#: The manifest scheme, IN the preimage (the rank-8b idiom): a future pointer mapper minting rows
#: under a different rule cannot silently collide with these, and a reader can tell which rule to
#: re-verify a hash under.
RIP_POINTER_SCHEME = "neuro.rip.pointer.v1"

#: The D2 `source_table` (owner-ruled). FILE grain — deliberately not a record-grain noun, so these
#: cannot be confused with, or collide with, rank 8b's `mosart_questions` rows.
RIP_SOURCE_TABLE = "corpus_files"

#: The `payload_jsonb` top-level `kind`, which is what `external_records_kind_idx` indexes. The
#: manifest has its OWN `kind` column, so the row is nested under "row" to keep this key unshadowed.
RIP_RECORD_KIND = "corpus_file_pointer"

#: Families excluded from this lane by owner ruling. This is a DENYLIST and it is DEFAULT-ON: the
#: driver must opt IN to any of these by name, never opt out. An inclusion-only filter would make a
#: bare run register the whole 24,859-row manifest, which is the shape of the shipped shim.
EXCLUDED_FAMILIES = frozenset(
    {
        "competition-trace-the-ace",  # deferred indefinitely; registers in its then-native form
        "stimuli-estela",  # held for the wave-2 stimulus registry
        "mi-sae-asset",  # gets an ADR-0031 assets row instead (Unit 1), never a pointer row
    }
)

#: Machine-local: excluded from the identity by owner ruling, and kept in the sidecar ONLY because it
#: is the sole path by which a row can be resolved back to a file at all (see the module docstring).
MACHINE_LOCAL_COLUMN = "source_abspath"


@dataclass(frozen=True)
class RipResult:
    """The outcome of registering ONE corpus file across all three ranks."""

    artifact_id: int
    external_record_id: int
    lineage_edge_id: int
    source_pk: str
    sha256_hex: str  # rank 6's STREAMED digest of the real bytes — the byte-integrity record
    size_bytes: int
    registered: bool  # rank 6 minted a new pointer
    mirrored: bool  # rank 5 minted a new manifest row
    linked: bool  # rank 7 minted a new edge


def canonical_pointer_bytes(*, source_system: str, uri: str) -> bytes:
    """THE preimage. `source_pk = sha256(these bytes)` and `payload_text` IS these bytes.

    Reuses `db/identity.py::canonical_bytes` — one implementation per concept; a hand-rolled
    `json.dumps` here would fork the convention on `ensure_ascii` and mint different hashes for the
    same pointer, which is the divergence that lesson exists to prevent."""
    return canonical_bytes({"scheme": RIP_POINTER_SCHEME, "source_system": source_system, "uri": uri})


def pointer_bytes_for_row(*, source_system: str, row: dict[str, Any]) -> bytes:
    """The preimage AS THE MAPPER TAKES IT — a function of the whole ROW that reads exactly two
    fields of it.

    This exists so the ruling is FALSIFIABLE. A probe calling `canonical_pointer_bytes` with two
    scalars can only compare f(x) to f(x) and is a TAUTOLOGY; routing through the row makes a widened
    preimage — one that reaches for `note` or `family` — actually change the output for two rows that
    differ only in curation. A vet measured exactly that tautology in this unit's own flagship probe,
    which had passed the mutation matrix only by reddening a DIFFERENT assertion."""
    return canonical_pointer_bytes(source_system=source_system, uri=row["uri"])


def blank_to_none(raw: str | None) -> str | None:
    """CSV has no NULL: an absent value arrives as an empty string. Rank 6 does NOT coerce, so an
    uncoerced blank persists as a real empty string rather than SQL NULL. 1,748 of the 1,753
    importable rows carry blank `dtype`/`shape`, so this is the common path, not an edge case."""
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def parse_shape(raw: str | None) -> list[int] | None:
    """Parse the manifest's `shape` TEXT into the `list[int]` rank 6's `int4[]` bind expects.

    ⚠ TWO MEASURED FAILURES THIS EXISTS TO PREVENT, both of which pass silently without it.

    (1) A RAW STRING IS NOT A LIST. The text `[323, 100]` handed to an `ARRAY(Integer)` bind is
        treated as a SEQUENCE OF CHARACTERS and binds as a list of single-character strings. The
        value must be JSON-parsed.
    (2) ★ AN EMPTY STRING BECOMES AN EMPTY ARRAY, NOT NULL. A blank binds through the same path to
        an empty int4 array — and an empty array IS NOT NULL, so the `artifacts_tensor_shape` CHECK
        (`kind NOT IN ('dense_tensor','derived_feature') OR (shape IS NOT NULL AND dtype IS NOT
        NULL)`) is SATISFIED BY A SHAPELESS TENSOR ROW. The database's own fail-closed guard is
        defeated by a value that merely looks absent. Blank must become None so the CHECK can fire.

    A present-but-unparseable value RAISES rather than degrading to None: returning None there would
    turn a corrupt manifest into a silently shapeless row for a non-tensor kind, and into a
    confusing CHECK violation for a tensor one. Fail loud, at the row, naming the value."""
    blank = blank_to_none(raw)
    if blank is None:
        return None
    try:
        parsed = json.loads(blank)
    except json.JSONDecodeError as exc:
        raise ImporterIngressError(
            f"shape {blank!r} is not valid JSON — refusing (fail closed; a silently-dropped shape "
            "becomes an EMPTY int4 array, which SATISFIES the artifacts_tensor_shape CHECK)."
        ) from exc
    if not isinstance(parsed, list) or any(isinstance(d, bool) or not isinstance(d, int) for d in parsed):
        raise ImporterIngressError(
            f"shape {blank!r} must be a JSON list of ints, got {parsed!r} — refusing (fail closed)."
        )
    if not parsed:
        raise ImporterIngressError(
            f"shape {blank!r} parses to an EMPTY list — refusing (fail closed). An empty list binds "
            "to an EMPTY int4 array, which IS NOT NULL and therefore SATISFIES the "
            "artifacts_tensor_shape CHECK for a shapeless tensor row: the SAME fail-open the blank "
            "branch above exists to close, reached through a different door."
        )
    if any(d <= 0 for d in parsed):
        raise ImporterIngressError(
            f"shape {blank!r} has a non-positive dimension — refusing (fail closed); a tensor "
            "dimension is a count."
        )
    return [int(d) for d in parsed]


def build_sidecar(row: dict[str, Any]) -> dict[str, Any]:
    """The `payload_jsonb` sidecar: the WHOLE manifest row, nested, under a top-level `kind`.

    The nesting is load-bearing. `external_records_kind_idx ON external_records
    ((payload_jsonb ->> 'kind'))` is the ONE functional JSON index the bindings permit, and it reads
    the TOP level — while the manifest carries its own `kind` column. Flattening the row would let
    the manifest's value shadow the index key and silently mis-index every row."""
    return {"kind": RIP_RECORD_KIND, "row": dict(row)}


def derived_by_predecessor_for(source_system: SourceSystem) -> bool:
    """R-D: rows physically from the predecessor repository carry the marking; found-on-a-filesystem
    rows do not. Derived from the BATCH, never guessed per row — the batch is the only place the
    provenance axis is actually established."""
    return source_system is SourceSystem.STUDY_QUERY_LLM


def map_corpus_file(
    repo: Repository,
    handle: ImportBatchHandle,
    row: dict[str, Any],
    *,
    derived_by_predecessor: bool,
) -> RipResult:
    """Register ONE manifest row across ranks 6 → 5 → 7. See the module docstring for the identity
    ruling and its costs.

    ⚠ THE ORDER IS LOAD-BEARING AND IS NOT AN OPTIMISATION TARGET. The preimage is independent of
    everything rank 6 produces, so rank 5 COULD run first — and must not. Rank 6 is the only step
    that can legitimately fail on a real file (an unreadable path, a tensor kind missing shape or
    dtype), so running it first means such a failure leaves NO manifest row and NO edge. Reversed,
    the same failure strands a rank-5 row describing a binary that was never registered, and there
    is no delete verb to retract it.

    ⚠ EACH WRITER OPENS ITS OWN TRANSACTION, so this sequence is NOT atomic across the three ranks.
    A fault between them leaves a partial triple that is PERMANENT. That is inherent to the shipped
    writers, not introduced here, and the ordering above is what makes the surviving fragment the
    least misleading one: an artifact with no manifest row reads as an unannotated pointer, whereas
    a manifest row with no artifact would be a description of nothing."""
    if not isinstance(handle, ImportBatchHandle):
        raise ImporterIngressError(
            "handle must be an ImportBatchHandle from open_import_batch — a raw id is refused."
        )
    family = row.get("family")
    if family in EXCLUDED_FAMILIES:
        raise ImporterIngressError(
            f"family {family!r} is EXCLUDED from the RIP lane by owner ruling and must not be "
            "registered — refusing (fail closed; every registered row is permanent and there is no "
            "delete verb)."
        )

    registered = register_artifact_in_place(
        repo,
        handle,
        source_path=row[MACHINE_LOCAL_COLUMN],
        uri=row["uri"],
        kind=row["kind"],
        shape=parse_shape(row.get("shape")),
        dtype=blank_to_none(row.get("dtype")),
        retention=row["retention"],
    )
    mirrored = write_external_record(
        repo,
        handle,
        source_table=RIP_SOURCE_TABLE,
        source_bytes=pointer_bytes_for_row(source_system=handle.source_system.value, row=row),
        derived_by_predecessor=derived_by_predecessor,
        payload_jsonb=build_sidecar(row),
    )
    linked = link_registered_artifact(
        repo,
        handle,
        artifact_id=registered.artifact_id,
        external_record_id=mirrored.external_record_id,
        note=f"{RIP_POINTER_SCHEME} · {family}",
    )
    return RipResult(
        artifact_id=registered.artifact_id,
        external_record_id=mirrored.external_record_id,
        lineage_edge_id=linked.lineage_edge_id,
        source_pk=mirrored.source_pk,
        sha256_hex=registered.sha256_hex,
        size_bytes=registered.size_bytes,
        registered=registered.registered,
        mirrored=mirrored.inserted,
        linked=linked.inserted,
    )
