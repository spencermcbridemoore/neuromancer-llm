"""Importer rank 4 — the ingress batch-open gate (phase3-importer-spec.md D1/D3; ADR-0020).

`open_import_batch` is the ONE structurally-forced entry every importer write passes through. It carries the three
importer invariants and mints the `import_batches` row, returning an `ImportBatchHandle` capability token that rank 5's
`external_records` writer REQUIRES — and `external_records.import_batch_id` is a NOT NULL FK — so **no external_records
row can exist without a batch that passed this gate** (coverage by MECHANISM, not discipline; spec D1). The token is
constructed ONLY here (pinned by tests/redteam/test_rt_importer_no_cloud_put.py), so it cannot be forged.

Invariants:
  (a) ADR-0020 durability — assert_durability_ok(repo.engine) on the CANONICAL lane, ONCE at batch-open (the batch
      boundary; readiness §4·3), BEFORE the import_batches INSERT so a blocked interlock leaves NO orphan batch. The
      gate is NOT on any Repository path (its only two callsites are capture/events.py + bundles/registrar.py), so the
      choke point must carry the consult itself.
  (b) D1/D2 stamp — `confidentiality` + `source_system` are REQUIRED (no default) closed-vocabulary grades, validated
      fail-closed; an unstamped/out-of-vocab batch-open is REFUSED (ImporterIngressError). Owner rulings 2026-07-14:
      confidentiality ∈ {exam_restricted, open} with NO default-clean member (D1); source_system ∈ {study-query-llm,
      local-fs} (D2 closed vocab). The per-record derived-by-predecessor marking is rank 5.
  (c) D3 register-in-place — the importer performs NO cloud put(); this module constructs no storage backend (pinned
      statically by the redteam scan).

Lane trust: uses repo.expected_lane (VERIFIED at Repository construction) DIRECTLY — no free lane param, closing the
A2-11b F-1 fail-open by construction. Canonical-lane ONLY (A2-11b, owner-ruled 2026-07-07): test/staging imports are
ungated.

PRECONDITION: the importer Repository is built on the ADMIN DSN — the gate INSERTs a registrar table AND
assert_durability_ok may flip system_health.status (writer/admin only), so neither single role suffices (the same
control-plane posture as capture_logprob). The engine-grade fail-closed via InsufficientPrivilege holds on the
DEGRADED (flip) path only; a healthy gate is SELECT-only, so healthy-path admin discipline rests on the caller
building the Repository on the admin DSN, not on the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import text

from ..governance.health import assert_durability_ok

if TYPE_CHECKING:
    from ..db.repository import Repository


class Confidentiality(StrEnum):
    """D1 confidentiality grade (owner-ratified 2026-07-15). Closed vocabulary with NO default-clean member — an
    unstamped write is refused, never defaulted to 'open' (spec D1: a gap reads DIRTY, not clean)."""

    EXAM_RESTRICTED = (
        "exam_restricted"  # midterm exam content (the MCQ corpora); phase0 soft rule, made mechanical
    )
    OPEN = "open"  # found data with no confidentiality concern


class SourceSystem(StrEnum):
    """D2 source_system closed vocabulary (owner-ruled 2026-07-14): real provenance vs found data. A new value is a
    deliberate commit, never a typo."""

    STUDY_QUERY_LLM = "study-query-llm"  # the predecessor; original IDs exist
    LOCAL_FS = "local-fs"  # found data; no provenance


class ImporterIngressError(RuntimeError):
    """The importer REFUSED (fail closed) — the shared refusal type across every importer writer: an absent/invalid
    confidentiality or source_system stamp at batch-open (here); a non-text/NUL payload or an invalid handle/marking
    at the Layer-1 write (external_records.py); a bad handle/kind/retention/shape or an unreadable path at
    register-in-place (register_in_place.py); an unknown edge_kind or a non-canonical/unknown-noun entity ref
    (lineage.py); an invalid handle/method/kind or a promoted_pk naming no live row (promote.py). Distinct from the
    re-raised DurabilityGateError so a caller can tell an importer-side refusal from 'durability gate blocked', and
    from IdentityMismatchError, which is reserved for DRIFT (a divergent claim about an already-keyed object)."""


@dataclass(frozen=True)
class ImportBatchHandle:
    """The capability token minted ONLY by open_import_batch, after the stamp + durability checks pass. Construction
    is pinned to this module by tests/redteam/test_rt_importer_no_cloud_put.py — the token cannot be forged to bypass
    the gate.

    ⚠ THE COVERAGE IT BUYS DIFFERS BY CONSUMER — do not generalize from rank 5. For rank 5's external_records writer
    the routing is STRUCTURAL: external_records.import_batch_id is a NOT NULL FK, so a persisted record proves its
    batch passed this gate. For every OTHER consumer the token is DISCIPLINARY ONLY — artifacts (rank 6),
    lineage_edges (rank 7) and promotions (rank 8a) have NO import_batch_id column, so nothing about the batch is
    persisted on those rows and NO READER MAY TREAT the handle requirement as ADR-0020 gate coverage on them. What it
    still forces on every path is the once-per-batch consult itself: assert_durability_ok is on no Repository path
    (only bundles/registrar.py + capture/events.py), so holding a token is the only thing that makes an importer
    writer route through a gate at all."""

    import_batch_id: int
    source_system: SourceSystem
    confidentiality: Confidentiality


def open_import_batch(
    repo: Repository,
    *,
    source_system: SourceSystem,
    confidentiality: Confidentiality,
    note: str | None = None,
) -> ImportBatchHandle:
    """Open an import batch through the ingress choke point (see the module docstring for the three invariants).
    Returns the ImportBatchHandle token rank 5's external_records writer requires. Raises ImporterIngressError on an
    invalid/absent stamp, or DurabilityGateError (re-raised) when the canonical durability interlock is blocking."""
    # (b) D1/D2 stamp — REQUIRED + fail-closed. A frozen dataclass and a parameter annotation do NOT type-check at
    # runtime, so a raw str / None / wrong type would otherwise slip through and later be persisted/counted as a real
    # grade. isinstance against the closed StrEnum is the load-bearing refusal.
    if not isinstance(confidentiality, Confidentiality):
        raise ImporterIngressError(
            f"confidentiality must be a Confidentiality member (one of {[c.value for c in Confidentiality]}), got "
            f"{confidentiality!r} — refusing an unstamped/out-of-vocab import (fail closed; D1)."
        )
    if not isinstance(source_system, SourceSystem):
        raise ImporterIngressError(
            f"source_system must be a SourceSystem member (one of {[s.value for s in SourceSystem]}), got "
            f"{source_system!r} — refusing (fail closed; D2 closed vocabulary)."
        )
    # (a) ADR-0020 durability consult — canonical only (A2-11b), BEFORE the INSERT so a blocked interlock leaves no
    # orphan batch. repo.expected_lane is the VERIFIED lane (Repository construction), trusted directly (no free param).
    if repo.expected_lane == "canonical":
        assert_durability_ok(repo.engine)
    # Mint the import_batches row — the NOT NULL FK anchor. This is the only import_batches INSERT path in src today
    # (there is deliberately no Repository.create_import_batch bypass off this choke point). The coverage guarantee is
    # the un-forgeable ImportBatchHandle token — scan-pinned in tests/redteam/test_rt_importer_no_cloud_put.py — which
    # rank 5's external_records writer must hold. (c) D3: no put() / no storage backend is constructed on this path.
    with repo.engine.begin() as conn:
        batch_id = conn.execute(
            text(
                "INSERT INTO neuro.import_batches (source_system, note) VALUES (:s, :n) RETURNING import_batch_id"
            ),
            {"s": source_system.value, "n": note},
        ).scalar_one()
    return ImportBatchHandle(
        import_batch_id=int(batch_id), source_system=source_system, confidentiality=confidentiality
    )
