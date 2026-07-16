"""Importer rank 6 — register-in-place: the "register, do not copy" artifact path (phase3-importer-spec.md D3; ADR-0005).

`register_artifact_in_place` records an EXISTING local binary corpus file (a study-query-llm parquet / embedding-array
the importer will NOT copy or PUT) as a standalone `artifacts` POINTER row (sha256 / size / uri / kind), so provenance +
integrity are captured without moving the bytes. It is the first code in the system to register an artifact WITHOUT a
cloud put.

Contract:
  * The file is hashed directly (`hashlib.sha256`, streamed) — the ADR-0005 storage-integrity record ("verified on
    read"); `size` is the count of bytes actually hashed (same stream ⇒ no os.stat/read TOCTOU).
  * The pointer is registered against a DEDICATED NON-CLOUD backend (`local-corpus`, driver `local_fs`, `is_cloud=False`,
    lane `artifacts`, host-independent `base_uri='file://local-corpus'`). It is in no R3 budget group and rank 6
    constructs no backend / calls no guard, so its bytes are NEVER summed into or denied by the cloud quota (no phantom
    bytes); and it is a DISTINCT key from the capture lake `local-lake` (not conflated). No cloud PUT happens
    (register-in-place; D3, structurally pinned by tests/redteam/test_rt_importer_no_cloud_put.py).
  * IDENTITY / IDEMPOTENCY: the identity is (backend_id, uri, sha256, size). `uri` MUST be a host-independent,
    content-deterministic, ROOT-RELATIVE logical key under the local-corpus root (NEVER a machine-absolute path, never
    path+timestamp) — else re-run idempotency and cross-host portability silently break (the rank-7/8 mapper owns this).
    Keep-first `ON CONFLICT (backend_id, uri) DO NOTHING` → re-SELECT → raise `IdentityMismatchError` on sha256/size
    drift (a changed file at the same uri fails loud). `kind`/`shape`/`dtype`/`retention` are FIRST-WRITE-WINS
    descriptive metadata: a keep-first re-register does NOT reconcile them (only sha256/size drift raises).
  * D1 CONFIDENTIALITY (owner ruling 2026-07-15, discipline-for-v1): `artifacts` has NO confidentiality column, so
    `handle.confidentiality` is NOT persisted here. A register-in-place binary's D1 grade lives SOLELY on its PAIRED
    rank-5 `external_records` manifest (mechanically stamped through the rank-4 choke point) tied by a rank-7
    `derived_from` edge — a mapper DISCIPLINE, not a mechanism on the artifact. The handle is a disciplinary token (it
    ties the register to a durability-consulted batch); its stamp payload is discarded. No reader may treat the handle
    requirement as D1 coverage on the artifact.

NOT here: no `table_manifests` row (its `run_id` is NOT NULL ⇒ a promoted run ⇒ Layer 2 / rank 8); no `lineage_edges`
edge (rank 7); no bundle (bundle_id NULL); no copy; no per-row durability re-consult (once per batch at
`open_import_batch`). PRECONDITION: part of the admin-DSN importer flow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, Integer, bindparam, text

from ..db.orm import ArtifactKind, RetentionClass
from ..db.repository import IdentityMismatchError
from .ingress import ImportBatchHandle, ImporterIngressError

if TYPE_CHECKING:
    from ..db.repository import Repository

# The dedicated non-cloud backend for register-in-place corpus files. Distinct from the capture lake 'local-lake';
# driver local_fs (reuses the read path so the pointer stays verifiable); host-independent base_uri (no cross-host drift).
LOCAL_CORPUS_BACKEND_KEY = "local-corpus"
LOCAL_CORPUS_DRIVER = "local_fs"
LOCAL_CORPUS_BASE_URI = "file://local-corpus"

_TENSOR_KINDS = frozenset(
    {"dense_tensor", "derived_feature"}
)  # artifacts_tensor_shape CHECK forces shape+dtype
_ARTIFACT_KINDS = frozenset(ArtifactKind.enums)  # derived from the ORM enum — no hand-copy drift
_RETENTION_CLASSES = frozenset(RetentionClass.enums)

_CHUNK = 1024 * 1024  # 1 MiB streamed read so a large parquet is not loaded into RAM

_INSERT = text(
    "INSERT INTO neuro.artifacts (kind, backend_id, uri, sha256, size_bytes, shape, dtype, retention) "
    "VALUES (:k, :be, :u, :sha, :sz, :shp, :dt, :ret) "
    "ON CONFLICT (backend_id, uri) DO NOTHING RETURNING artifact_id"
).bindparams(
    bindparam("shp", type_=ARRAY(Integer))
)  # explicit int4[] element type (the rank-5 typed-bindparam lesson)

_RESELECT = text(
    "SELECT artifact_id, sha256, size_bytes FROM neuro.artifacts WHERE backend_id = :be AND uri = :u"
)


@dataclass(frozen=True)
class RegisteredArtifact:
    """The outcome of one register-in-place."""

    artifact_id: int
    sha256_hex: str  # sha256 of the source file (hex) — the ADR-0005 integrity record
    size_bytes: int
    registered: bool  # True = new pointer; False = idempotent keep-first no-op (same file already registered)


def register_artifact_in_place(
    repo: Repository,
    handle: ImportBatchHandle,
    *,
    source_path: str,
    uri: str,
    kind: str = "external_record",
    shape: list[int] | None = None,
    dtype: str | None = None,
    retention: str = "keep_forever",
) -> RegisteredArtifact:
    """Register an existing local binary file as a standalone artifacts pointer (register-in-place, NO copy/PUT). See the
    module docstring for the full contract (identity, the uri contract, D1 confidentiality, and what is deferred to ranks
    7/8). Raises `ImporterIngressError` on an invalid handle/kind/retention, a tensor kind missing shape/dtype, or an
    unreadable `source_path`; `IdentityMismatchError` on a divergent-bytes re-register at the same uri."""
    # Fail-closed input validation — parameter annotations do NOT type-check at runtime; these guards fire BEFORE any
    # DB write (and before the backend is minted), so a refusal leaves no artifacts row AND no orphan backend row.
    if not isinstance(handle, ImportBatchHandle):
        raise ImporterIngressError(
            "handle must be an ImportBatchHandle from open_import_batch — a raw id is refused (fail closed)."
        )
    if kind not in _ARTIFACT_KINDS:
        raise ImporterIngressError(
            f"kind {kind!r} is not a valid artifact_kind (one of {sorted(_ARTIFACT_KINDS)}) — refusing (fail closed)."
        )
    if retention not in _RETENTION_CLASSES:
        raise ImporterIngressError(
            f"retention {retention!r} is not a valid retention_class (one of {sorted(_RETENTION_CLASSES)}) — refusing "
            "(fail closed)."
        )
    if kind in _TENSOR_KINDS and (shape is None or dtype is None):
        raise ImporterIngressError(
            f"kind {kind!r} requires shape AND dtype (the artifacts_tensor_shape CHECK) — refusing (fail closed)."
        )
    # Hash the local file directly (no backend, no .put); size = the count of bytes actually hashed (no stat/read race).
    h = hashlib.sha256()
    size = 0
    try:
        with open(source_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                h.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ImporterIngressError(
            f"cannot read source_path {source_path!r}: {exc} — refusing (fail closed)."
        ) from exc
    sha_hex = h.hexdigest()
    # Register-first/idempotent non-cloud backend (is_cloud=False deliberate; never summed into a cloud budget group).
    backend_id = repo.get_or_create_storage_backend(
        LOCAL_CORPUS_BACKEND_KEY,
        driver=LOCAL_CORPUS_DRIVER,
        lane="artifacts",
        base_uri=LOCAL_CORPUS_BASE_URI,
        is_cloud=False,
    )
    params = {
        "k": kind,
        "be": backend_id,
        "u": uri,
        "sha": bytes.fromhex(sha_hex),  # sha256 is bytea (LargeBinary) — bind raw bytes (registrar precedent)
        "sz": size,
        "shp": shape,
        "dt": dtype,
        "ret": retention,
    }
    with repo.engine.begin() as conn:
        new_id = conn.execute(_INSERT, params).scalar_one_or_none()
        if new_id is not None:
            return RegisteredArtifact(int(new_id), sha_hex, size, registered=True)
        # keep-first: the (backend_id, uri) natural key exists. Re-SELECT + raise-on-drift on sha256/size — a changed
        # file at the same uri is a divergent re-register (never silently kept); an identical re-scan is a no-op.
        row = conn.execute(_RESELECT, {"be": backend_id, "u": uri}).one()
    if bytes(row.sha256) != bytes.fromhex(sha_hex) or row.size_bytes != size:
        raise IdentityMismatchError(
            f"artifact at (backend={backend_id}, uri={uri!r}) already registered with DIFFERENT bytes "
            f"(stored {bytes(row.sha256).hex()[:12]}…/{row.size_bytes}B; incoming {sha_hex[:12]}…/{size}B) — refusing "
            "to keep-first a divergent re-register (fail loud; ADR-0005/0047)."
        )
    return RegisteredArtifact(int(row.artifact_id), sha_hex, size, registered=False)
