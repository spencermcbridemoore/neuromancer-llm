"""The repo-pinned canonical instance identity (A1-15; A1 runbook §10 — OWNER-RULED option (a), 2026-07-02).

The pin is a COMMITTED CONSTANT: versioned, docs-cannot-rot, and rotation (a legitimately
re-provisioned canonical mints a NEW instance_uuid — runbook §Rollback) is an auditable commit,
exactly the event that should be loud. It is deliberately NOT an env var (option (b) rejected of
record: the pin must not live next to the DSN it independently cross-checks; the L13 red-team probe
holds this module env-free) and NEVER DB-resident (the thing being verified cannot hold its own pin).

Recorded from the live canonical instance (neuro-canonical-pg, A1 §8 chain, 2026-07-02;
scratch/a1-execution-record-2026-07-02.md).

Known residuals, stated (runbook §10.5): the pin rejects a restored clone with a FRESH instance_uuid;
the VERBATIM clone (same uuid copied in) is the other half, closed by A2-9's identity scrub — A1-15 +
A2-9 are a hard gate before any real canonical backup/restore (A2-7). The pool_pre_ping
re-verification caveat lives in lanes.py (ADR-0046 bundle); the raw make_engine pre-identity paths
stay unpinned by design (session.py); make_reader_engine's optional lane confirm is likewise
LANE-ONLY (the reader-path pin is deferred with the restore/clone-tooling obligation in the
Deferred-Obligation Register — the write choke point is what A1-15 wires).
"""

from __future__ import annotations

import uuid as _uuid

from .lanes import ConfigurationError

# The canonical instance_uuid of record (A1-12). None means "no pin recorded" (a fresh fork or
# mid-rotation state) — canonical-lane engine construction FAILS CLOSED on it, never an unpinned pass.
CANONICAL_INSTANCE_UUID: _uuid.UUID | None = _uuid.UUID("c7a1b953-485a-4127-a2e6-a18f7423742a")


def expected_uuid_for_lane(expected_lane: str) -> _uuid.UUID | None:
    """The single pin-resolution point (§10.1): canonical -> the pin, or ConfigurationError if the pin
    is absent (an optional pin that silently defaulted to None would recreate the dead-parameter
    fail-open in one hop); every other lane -> None (no pin — staging/test behavior unchanged). Lane
    VALIDITY is assert_lane's job, not this function's."""
    if expected_lane != "canonical":
        return None
    if CANONICAL_INSTANCE_UUID is None:
        raise ConfigurationError(
            "canonical instance_uuid pin is absent (db/canonical_instance.py CANONICAL_INSTANCE_UUID "
            "is None) — refusing to construct a canonical-lane engine unpinned (fail closed). Record "
            "the provisioned uuid in the pin; rotation is an auditable commit (A1 runbook §10, ruling (a))."
        )
    return CANONICAL_INSTANCE_UUID
