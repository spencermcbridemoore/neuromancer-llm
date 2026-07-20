"""`neuro crawl` + the quarterly crawl-rebuild drill + the manifest golden corpus. STAGE 2.

Reconstructs the registry from the self-describing bundles on a backend (SCOPED manifest-wins reconcile,
capture contract §8) and sweeps blob orphans the unsealed-bundle GC leaves behind.

BUILT so far — the TORN-BLOB RECONCILE (Panel #1 finding; ops-hardening sweep 2026-07-19): reconcile_artifacts
sweeps the LIVE artifacts catalog and re-verifies each committed (sha256, size_bytes) against the ACTUAL blob
bytes — the offline, catalog-wide analogue of capture/reader.py::verify_artifact's per-read binding. It closes
the #7 seam-clobber + #10 GC-orphan windows: a concurrency loss can leave a committed artifacts.sha256
disagreeing with a clobbered blob, and no read of an unrelated run would ever notice. Unlike verify_artifact (a
READ-PATH guard that RAISES on the first mismatch), the reconcile COLLECTS every torn/missing/unresolved
artifact into a report — a sweep must survive one bad blob to find the rest. READ-ONLY (SELECT + backend.get;
no write, no fix — the operator acts on the report).

STILL A STUB (deliberately out of this unit's scope; each registered as a separate crawl lesson): the
manifest-wins crawl-REBUILD, the blob-ORPHAN sweep (blobs on the backend with NO artifact row — the reverse
direction, needs backend listing), and the manifest re-verify-on-read. This unit built ONLY the torn-blob
reconcile (the register's item (c)).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import RowMapping, text

from ..db.identity import sha256_bytes

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from ..storage.backends import StorageBackend


@dataclass(frozen=True)
class BackendRef:
    """The identity of a registered storage backend, handed to the caller's `backend_for` so it can build the
    host-specific adapter. Host config (a local_fs physical root, an Azure connection string) lives in the
    CALLER, never in the catalog — for local_fs `base_uri` is the host-INDEPENDENT logical id, not a path. The
    reconcile itself is host-agnostic; this seam is the ONE place host config enters (the BackupDriver idiom)."""

    backend_id: int
    backend_key: str
    driver: str
    base_uri: str


@dataclass(frozen=True)
class ArtifactFinding:
    """One non-OK artifact from the reconcile. `status`: 'torn' (blob present but sha256/size disagree with the
    catalog — a clobber/corruption), 'missing' (blob absent or unreadable at its uri), 'unresolved' (its backend
    could not be built — e.g. an Azure credential absent in the reconcile environment). `detail` carries the
    concrete cause (the diverging hashes/sizes, or the exception type)."""

    artifact_id: int
    backend_key: str
    uri: str
    kind: str
    status: str
    detail: str


@dataclass(frozen=True)
class ReconcileReport:
    """The torn-blob reconcile result. `checked` = LIVE (non-deleted) artifacts examined; `ok` = blobs that
    matched sha256+size; `findings` = every non-OK artifact (torn/missing/unresolved), in artifact_id order.
    Invariant: `checked == ok + len(findings)`."""

    checked: int
    ok: int
    findings: tuple[ArtifactFinding, ...]

    @property
    def clean(self) -> bool:
        """True iff every live artifact's blob matched its committed sha256+size."""
        return not self.findings

    def with_status(self, status: str) -> tuple[ArtifactFinding, ...]:
        """The findings of one status ('torn' | 'missing' | 'unresolved')."""
        return tuple(f for f in self.findings if f.status == status)


# LIVE artifacts only — a deleted artifact's blob is intentionally gone (deletion RECORDED, phase0 Q6), so its
# absence is expected, not a torn-blob finding. Joined to storage_backends for the driver/base_uri the caller's
# backend_for needs. A pure SELECT — runnable by a SELECT-only (neuro_reader) connection.
_LIVE_ARTIFACTS_SQL = (
    "SELECT a.artifact_id, a.kind, a.uri, a.sha256, a.size_bytes, "
    "sb.backend_id, sb.backend_key, sb.driver, sb.base_uri "
    "FROM neuro.artifacts a JOIN neuro.storage_backends sb ON sb.backend_id = a.backend_id "
    "WHERE a.deleted_at IS NULL "
    "ORDER BY a.artifact_id"
)


def reconcile_artifacts(
    engine: Engine, *, backend_for: Callable[[BackendRef], StorageBackend]
) -> ReconcileReport:
    """Sweep the LIVE artifacts catalog and re-verify each committed (sha256, size_bytes) against the ACTUAL
    blob bytes — the offline, catalog-wide analogue of capture/reader.py::verify_artifact's per-read binding
    (#7 seam-clobber + #10 GC-orphan). Every torn/missing/unresolved artifact is COLLECTED into the report; the
    sweep never aborts on one bad blob, so it finds them ALL — the distinction from the read-path guard, which
    RAISES on the first mismatch.

    READ-ONLY: a SELECT over artifacts + storage_backends, then `backend.get(uri)` per artifact — no write, no
    fix (the operator acts on the report). `engine` may be a SELECT-only (neuro_reader) connection. `backend_for`
    builds the host-specific adapter for a registered backend (host config lives in the CALLER, never the
    catalog); a backend it cannot build (raise OR a None return) makes THAT backend's artifacts 'unresolved'
    rather than crashing the sweep. Backends are built ONCE per backend_id and reused.

    ⚠ The DB access is read-only, but a BACKEND's *construction* is the caller's and is not guaranteed so:
    today's AzureBlobBackend create_container()s on construct, so a least-privilege READ-ONLY Azure credential
    would fail to build that backend and mark its artifacts 'unresolved' (not verify them). A read-only sweep of
    a cloud lane therefore needs a credential permitting container-create, or a construct-without-create adapter
    (a registered follow-on) — the local/DB lane has no such caveat.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(_LIVE_ARTIFACTS_SQL)).mappings().all()

    backends: dict[int, StorageBackend | None] = {}  # per-backend_id cache; None == unresolvable
    unresolved: dict[int, str] = {}
    ok = 0
    findings: list[ArtifactFinding] = []
    for row in rows:
        bid = int(row["backend_id"])
        if bid not in backends:
            try:
                built = backend_for(BackendRef(bid, row["backend_key"], row["driver"], row["base_uri"]))
            except Exception as exc:  # noqa: BLE001 — collect, don't abort: a backend that can't be built
                # (missing cred / unknown driver) makes every artifact on it a COLLECTED 'unresolved' finding.
                built = None
                unresolved[bid] = f"backend unresolved ({type(exc).__name__}): {exc}"
            if built is None and bid not in unresolved:
                # a backend_for that RETURNS None (rather than raising) for an unresolvable backend is ALSO
                # collected as 'unresolved' — never a KeyError aborting the very sweep collect-not-raise exists
                # to prevent (the type says StorageBackend, but a real dict.get-style seam can return None).
                unresolved[bid] = "backend_for returned None (treated as unresolved)"
            backends[bid] = built
        backend = backends[bid]
        if backend is None:
            findings.append(_finding(row, "unresolved", unresolved[bid]))
            continue
        try:
            blob = backend.get(row["uri"])
        except Exception as exc:  # noqa: BLE001 — collect, don't abort: a missing/unreadable blob is a finding,
            # so the sweep survives one bad blob and finds the rest (the reconcile-vs-read-guard distinction).
            findings.append(_finding(row, "missing", f"blob unreadable ({type(exc).__name__}): {exc}"))
            continue
        got_size, got_sha = len(blob), sha256_bytes(blob)
        want_sha = bytes(row["sha256"])
        if got_size != row["size_bytes"] or got_sha != want_sha:
            findings.append(
                _finding(
                    row,
                    "torn",
                    f"blob {got_size}B sha256={got_sha.hex()[:12]} != catalog "
                    f"{row['size_bytes']}B sha256={want_sha.hex()[:12]}",
                )
            )
            continue
        ok += 1
    return ReconcileReport(checked=len(rows), ok=ok, findings=tuple(findings))


def _finding(row: RowMapping, status: str, detail: str) -> ArtifactFinding:
    """Build an ArtifactFinding from a catalog row (a SQLAlchemy RowMapping) + the classified status."""
    return ArtifactFinding(
        artifact_id=int(row["artifact_id"]),
        backend_key=row["backend_key"],
        uri=row["uri"],
        kind=row["kind"],
        status=status,
        detail=detail,
    )
