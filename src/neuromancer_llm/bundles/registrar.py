"""The single W1-W8 write-ordering callsite (capture contract §4). Register-LAST, one transaction.

The Postgres/parquet seam is the design's central risk (two stores that can disagree), so write-ordering
is a CORRECTNESS property enforced here and adversarially kill-tested (tests/seam/). Canonical order:

    1. write all payload shards to blob          (W1-W2)
    2. hash each shard (sha256)                   (W3)
    3. write manifest.json + its sha256           (W4)
    4. SEAL (state='sealed', manifest_sha256)     (W5)  -> GC-exempt from here (ADR-0010)
    5. REGISTER in ONE db transaction             (W6-W7): artifacts + table_manifests + state='registered'
    (job-complete is the caller's final step      (W8))

The dangerous window is W6/W7 (bytes exist, DB doesn't know): the sealed bundle is GC-exempt, so a
resumed worker re-registers idempotently. Stage 1 registers generic payload shards (synthetic bytes);
the logprob capture lane flows through this SAME callsite in Stage 2.

Integrity is FAIL-LOUD (R2 / F4): re-register is idempotent ONLY for identical bytes — divergent bytes
for the same identity raise (sealed manifest_sha256 is immutable; artifact sha256 is compared, not
overwritten); the final state transition is CAS-guarded so a tombstoned bundle can never be resurrected.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, text

from ..storage.backends import StorageBackend
from .bundlespec import Shard, bundle_uuid_for, sha256_bytes
from .manifest import build_manifest, manifest_bytes


@dataclass(frozen=True)
class TableManifestSpec:
    """One QUERYABLE parquet artifact -> one table_manifests row (resolves the Stage-1 per-artifact
    fan-out DEFER). `shard_name` selects which registered shard the manifest points at; the partition
    columns (run_id/model_id/hook_point_id) are real FK columns, never parsed (ADR-0001)."""

    shard_name: str
    dataset_name: str
    row_count: int | None = None
    model_id: int | None = None
    hook_point_id: int | None = None
    schema_major: int = 1


# Injectable crash points (the kill-test windows). None = run to completion.
CRASH_POINTS = (
    "after_first_shard",
    "after_shards",
    "before_seal",
    "before_register",
    "mid_register",
    "after_register",
)


class CrashInjected(RuntimeError):
    """Raised at an injected W-window to simulate a worker death between ordered steps (test-only)."""


class SeamIntegrityError(RuntimeError):
    """DB integrity would desync from blob bytes (divergent re-register / immutable manifest violated)."""


class SeamStateError(RuntimeError):
    """A bundle state transition affected an unexpected number of rows (e.g. resurrect a tombstone)."""


class BundleRegistrar:
    def __init__(
        self,
        engine: Engine,
        backend: StorageBackend,
        *,
        expected_lane: str,
        expected_uuid: _uuid.UUID | str | None = None,
    ) -> None:
        # R1: no write path exists on an unverified target — verify identity ONCE at construction (ADR-0006).
        from ..db.session import verify_engine

        self.engine = verify_engine(engine, expected_lane=expected_lane, expected_uuid=expected_uuid)
        self.backend = backend

    def _ensure_bundle(self, run_id: int, backend_id: int, bundle_uuid) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.bundles (bundle_uuid, run_id, backend_id, state) "
                    "VALUES (:u, :r, :be, 'unsealed') "
                    "ON CONFLICT (bundle_uuid) DO UPDATE SET run_id = EXCLUDED.run_id "
                    "RETURNING bundle_id"
                ),
                {"u": bundle_uuid, "r": run_id, "be": backend_id},
            ).scalar_one()

    def _assert_resume_consistent(self, bundle_id: int, manifest_digest: bytes) -> None:
        """R2 (fail-loud BEFORE clobbering blobs): a sealed/registered bundle's manifest is immutable, so
        re-registering DIFFERENT bytes for the same identity is refused before any blob is overwritten."""
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT manifest_sha256 FROM neuro.bundles WHERE bundle_id = :b"), {"b": bundle_id}
            ).one()
        if row.manifest_sha256 is not None and bytes(row.manifest_sha256) != manifest_digest:
            raise SeamIntegrityError(
                f"bundle {bundle_id} is already sealed with a different manifest — refusing to re-register "
                f"divergent bytes for the same identity (R2)."
            )

    def _seal(self, bundle_id: int, manifest_sha256: bytes) -> None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT manifest_sha256 FROM neuro.bundles WHERE bundle_id = :b FOR UPDATE"),
                {"b": bundle_id},
            ).one()
            if row.manifest_sha256 is not None:
                # R2: manifest_sha256 is IMMUTABLE once sealed — verify equality, never overwrite.
                if bytes(row.manifest_sha256) != manifest_sha256:
                    raise SeamIntegrityError(
                        f"bundle {bundle_id} manifest_sha256 is immutable once sealed; a re-seal with a "
                        f"different manifest is refused (R2)."
                    )
                return  # idempotent re-seal with the SAME manifest
            res = conn.execute(
                text(
                    "UPDATE neuro.bundles SET state = 'sealed', manifest_sha256 = :m, sealed_at = now() "
                    "WHERE bundle_id = :b AND state = 'unsealed'"
                ),
                {"b": bundle_id, "m": manifest_sha256},
            )
            if res.rowcount != 1:  # F4: tombstoned/registered cannot be (re)sealed
                raise SeamStateError(
                    f"_seal affected {res.rowcount} rows for bundle {bundle_id} (expected 1; not 'unsealed')."
                )

    def register(
        self,
        *,
        run_id: int,
        backend_id: int,
        dataset_name: str,
        partition_path: str,
        shards: dict[str, bytes],
        schema_major: int = 1,
        artifact_kinds: Mapping[str, str] | None = None,
        table_manifests: Sequence[TableManifestSpec] | None = None,
        crash_at: str | None = None,
    ) -> int:
        """Run the W1-W8 ordering. Returns the bundle_id. Idempotent on resume for IDENTICAL bytes.

        `artifact_kinds` maps shard name -> artifact_kind (default 'other'; the logprob lane passes
        'token_table' for the parquet). `table_manifests` declares ONE table_manifests row per QUERYABLE
        parquet artifact (the fan-out); when omitted, a single legacy manifest points at the first shard
        (Stage-1 behaviour preserved for the generic seam tests)."""
        if crash_at is not None and crash_at not in CRASH_POINTS:
            raise ValueError(f"unknown crash point {crash_at!r}")

        bundle_uuid = bundle_uuid_for(run_id, dataset_name, partition_path)
        bundle_id = self._ensure_bundle(run_id, backend_id, bundle_uuid)

        # Build the manifest (from in-memory shard hashes) BEFORE writing any blob, so the resume-
        # consistency check can fail loud before clobbering existing bytes (R2).
        shard_objs = [Shard(name=name, data=shards[name]) for name in sorted(shards)]
        # FIX #7 (seam concurrent divergent-bytes clobber): content-address the shard storage keys (a sha256
        # directory segment), mirroring FIX #1. The shard put runs BEFORE _seal and the keys were coordinate-
        # derived ({partition}/{name}), so two concurrent registers on one bundle_uuid with DIVERGENT bytes
        # both put the SAME key — the loser's put clobbered the committed winner's blob (artifacts.sha256 !=
        # blob on disk). Content-addressing makes divergent bytes land at a DIFFERENT key, so a committed
        # artifact's blob always matches its sha256. The run/partition prefix is kept (browsable) and the
        # capture<->artifact linkage stays on the FK, never the path (ADR docs/adr/0045).
        keys = [f"{partition_path}/{shard.sha256_hex}/{shard.name}" for shard in shard_objs]
        manifest = build_manifest(
            producer="bundle-registrar", run_id=run_id, dataset_name=dataset_name, shards=shard_objs
        )
        mbytes = manifest_bytes(manifest)
        manifest_digest = sha256_bytes(mbytes)
        self._assert_resume_consistent(bundle_id, manifest_digest)

        # W1-W3: write shards to blob, hashing each.
        for i, (shard, key) in enumerate(zip(shard_objs, keys, strict=True)):
            self.backend.put(key, shard.data)
            if crash_at == "after_first_shard" and i == 0:
                raise CrashInjected("W2: died after first shard, before manifest")
        if crash_at == "after_shards":
            raise CrashInjected("W3: shards hashed, before manifest")

        # W4: write manifest blob.
        self.backend.put(f"{partition_path}/manifest.json", mbytes)
        if crash_at == "before_seal":
            raise CrashInjected("W5: manifest written, before seal")

        # W5: SEAL — GC-exempt from here (ADR-0010), so the bytes survive a crash before register.
        self._seal(bundle_id, manifest_digest)
        if crash_at == "before_register":
            raise CrashInjected("W6: sealed, before register (bytes exist, DB doesn't know)")

        # W6-W7: REGISTER in one transaction — artifacts + table_manifests + state='registered'.
        with self.engine.begin() as conn:
            state = conn.execute(
                text("SELECT state FROM neuro.bundles WHERE bundle_id = :b FOR UPDATE"), {"b": bundle_id}
            ).scalar_one()
            if state == "registered":
                return bundle_id  # idempotent: already registered (resume after W8)
            if state != "sealed":  # F4: never resurrect a tombstoned/unsealed bundle to 'registered'
                raise SeamStateError(
                    f"cannot register bundle {bundle_id} in state {state!r} (expected 'sealed')."
                )
            kinds = artifact_kinds or {}
            artifact_id_by_name: dict[str, int] = {}
            for shard, key in zip(shard_objs, keys, strict=True):
                new_sha = sha256_bytes(shard.data)
                row = conn.execute(
                    text(
                        "INSERT INTO neuro.artifacts (bundle_id, kind, backend_id, uri, sha256, size_bytes, retention) "
                        "VALUES (:b, :kind, :be, :uri, :sha, :sz, 'ttl') "
                        # self-assign no-op on conflict so RETURNING yields the EXISTING sha256 (not overwritten)
                        "ON CONFLICT (backend_id, uri) DO UPDATE SET size_bytes = neuro.artifacts.size_bytes "
                        "RETURNING artifact_id, sha256"
                    ),
                    {
                        "b": bundle_id,
                        "kind": kinds.get(shard.name, "other"),
                        "be": backend_id,
                        "uri": key,
                        "sha": new_sha,
                        "sz": shard.size_bytes,
                    },
                ).one()
                if bytes(row.sha256) != new_sha:  # R2: idempotent for same bytes, fail-loud for different
                    raise SeamIntegrityError(
                        f"artifact {key} already registered with a different sha256 — blob bytes diverged."
                    )
                artifact_id_by_name[shard.name] = row.artifact_id
            if crash_at == "mid_register":
                raise CrashInjected("W7: mid-register txn — must roll back atomically")
            # ONE table_manifests row per QUERYABLE parquet artifact (Stage-1 DEFER resolved). When no
            # explicit specs are given, fall back to a single manifest at the first shard (the generic
            # seam tests' Stage-1 behaviour); the logprob lane passes a spec per queryable parquet file.
            specs = (
                list(table_manifests)
                if table_manifests is not None
                else [
                    TableManifestSpec(
                        shard_name=shard_objs[0].name, dataset_name=dataset_name, schema_major=schema_major
                    )
                ]
            )
            for spec in specs:
                conn.execute(
                    text(
                        "INSERT INTO neuro.table_manifests "
                        "(dataset_name, run_id, model_id, hook_point_id, schema_major, partition_path, "
                        "row_count, artifact_id) "
                        "VALUES (:ds, :r, :mid, :hp, :sm, :pp, :rc, :aid) "
                        "ON CONFLICT (dataset_name, partition_path, artifact_id) DO NOTHING"
                    ),
                    {
                        "ds": spec.dataset_name,
                        "r": run_id,
                        "mid": spec.model_id,
                        "hp": spec.hook_point_id,
                        "sm": spec.schema_major,
                        "pp": partition_path,
                        "rc": spec.row_count,
                        "aid": artifact_id_by_name[spec.shard_name],
                    },
                )
            res = conn.execute(
                text(
                    "UPDATE neuro.bundles SET state = 'registered', registered_at = now() "
                    "WHERE bundle_id = :b AND state = 'sealed'"
                ),
                {"b": bundle_id},
            )
            if res.rowcount != 1:  # F4: the sealed->registered transition must affect exactly one row
                raise SeamStateError(
                    f"register transition affected {res.rowcount} rows for bundle {bundle_id} (expected 1)."
                )
        if crash_at == "after_register":
            raise CrashInjected("W8: registered + durable, before job-complete")
        return bundle_id

    def bundle_state(self, bundle_id: int) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT state FROM neuro.bundles WHERE bundle_id = :b"), {"b": bundle_id}
            ).scalar_one()

    def artifact_count(self, bundle_id: int) -> int:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM neuro.artifacts WHERE bundle_id = :b"), {"b": bundle_id}
            ).scalar_one()
