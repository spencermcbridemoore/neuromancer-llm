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

from ..governance.health import assert_durability_ok
from ..storage.backends import StorageBackend
from .bundlespec import (
    RunModelIdentityBlock,
    Shard,
    TokenizationBlock,
    bundle_uuid_for,
    canonical_json,
    sha256_bytes,
)
from .manifest import build_manifest, manifest_bytes


@dataclass(frozen=True)
class TableManifestSpec:
    """One QUERYABLE parquet artifact -> one table_manifests row (resolves the Stage-1 per-artifact
    fan-out DEFER). `shard_name` selects which registered shard the manifest points at; the partition
    columns (run_id/model_id/hook_point_id) are real FK columns, never parsed (ADR-0001). `footer_kv`
    is the shard's embedded footer self-description KV (contract §5 block 14): the SAME dict is recorded
    in manifest block 14 and sha256-mirrored to table_manifests.footer_kv_sha256 — one source object;
    None keeps the column honestly NULL (a spec-less/generic shard embeds no KV)."""

    shard_name: str
    dataset_name: str
    row_count: int | None = None
    model_id: int | None = None
    hook_point_id: int | None = None
    schema_major: int = 1
    footer_kv: Mapping[str, str] | None = None


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
        # A2-11b: the verified lane — the durability consult gates CANONICAL cloud writes only (ADR-0020).
        self._expected_lane = expected_lane

    def _ensure_bundle(self, run_id: int, backend_id: int, bundle_uuid) -> int:
        # C2 (audit correction 2026-07-02): this was the ONLY registry adopting an existing row without
        # comparing its identity to the request (the raise-on-drift idiom of FIX #8 / R2 / C10a).
        # backend_id is NOT part of the bundle_uuid derivation (uuid5 over run/dataset/partition), so a
        # re-register under a DIFFERENT backend silently adopted the old row while the artifact INSERTs
        # used the NEW backend_id — a split-brain pointer. Never adopt, never update: the conflict arm
        # is a self-assign no-op (the old form re-pointed run_id) purely so RETURNING yields the
        # EXISTING row, and any backend_id/run_id drift raises before a single blob is touched.
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO neuro.bundles (bundle_uuid, run_id, backend_id, state) "
                    "VALUES (:u, :r, :be, 'unsealed') "
                    "ON CONFLICT (bundle_uuid) DO UPDATE SET run_id = neuro.bundles.run_id "
                    "RETURNING bundle_id, run_id, backend_id"
                ),
                {"u": bundle_uuid, "r": run_id, "be": backend_id},
            ).one()
            if row.backend_id != backend_id or row.run_id != run_id:
                raise SeamIntegrityError(
                    f"bundle {bundle_uuid} already exists with run_id={row.run_id}, "
                    f"backend_id={row.backend_id} — refusing to adopt/re-point it for run_id={run_id}, "
                    f"backend_id={backend_id} (C2: register-first, raise-on-drift; never adopt, never "
                    "update)."
                )
            return row.bundle_id

    def _assert_resume_consistent(self, bundle_id: int, manifest_digest: bytes) -> bool:
        """R2 (fail-loud BEFORE clobbering blobs): a sealed/registered bundle's manifest is immutable, so
        re-registering DIFFERENT bytes for the same identity is refused before any blob is overwritten.

        Returns True iff the bundle is already SEALED — manifest_sha256 is set atomically at _seal and stays
        set through 'registered', so a non-NULL value means the blobs are durable / GC-exempt (ADR-0010). A2-11b
        uses this to skip the durability consult on an already-durable resume (finishing/re-saving durable
        bytes must not be blocked by a health flip — Option A)."""
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT manifest_sha256 FROM neuro.bundles WHERE bundle_id = :b"), {"b": bundle_id}
            ).one()
        if row.manifest_sha256 is None:
            return False
        if bytes(row.manifest_sha256) != manifest_digest:
            raise SeamIntegrityError(
                f"bundle {bundle_id} is already sealed with a different manifest — refusing to re-register "
                f"divergent bytes for the same identity (R2)."
            )
        return True

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
        run_model_identity: RunModelIdentityBlock | None = None,
        tokenization: TokenizationBlock | None = None,
        not_applicable: Sequence[str] = (),
        crash_at: str | None = None,
    ) -> int:
        """Run the W1-W8 ordering. Returns the bundle_id. Idempotent on resume for IDENTICAL bytes.

        `artifact_kinds` maps shard name -> artifact_kind (default 'other'; the logprob lane passes
        'token_table' for the parquet). `table_manifests` declares ONE table_manifests row per QUERYABLE
        parquet artifact (the fan-out); when omitted, a single legacy manifest points at the first shard
        (Stage-1 behaviour preserved for the generic seam tests). `run_model_identity` / `tokenization` /
        `not_applicable` are the §5(a) identity payloads threaded VERBATIM into manifest blocks 2/5/13 —
        plain values, never queried (the manifest stays buildable with zero ambient state); omitting them
        yields an honestly-incomplete manifest (completeness `absent`), not a lie."""
        if crash_at is not None and crash_at not in CRASH_POINTS:
            raise ValueError(f"unknown crash point {crash_at!r}")

        bundle_uuid = bundle_uuid_for(run_id, dataset_name, partition_path)
        bundle_id = self._ensure_bundle(run_id, backend_id, bundle_uuid)

        # Resolve the table_manifests specs BEFORE the manifest build: block 14 records each spec's
        # footer_kv and the W6-W7 INSERT hashes the SAME object — one source of truth for the mirror.
        specs = (
            list(table_manifests)
            if table_manifests is not None
            else [
                TableManifestSpec(
                    shard_name=sorted(shards)[0], dataset_name=dataset_name, schema_major=schema_major
                )
            ]
        )
        shard_footer_kv = {s.shard_name: s.footer_kv for s in specs if s.footer_kv is not None}

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
            producer="bundle-registrar",
            run_id=run_id,
            dataset_name=dataset_name,
            shards=shard_objs,
            run_model_identity=run_model_identity,
            tokenization=tokenization,
            not_applicable=not_applicable,
            shard_footer_kv=shard_footer_kv or None,
        )
        mbytes = manifest_bytes(manifest)
        manifest_digest = sha256_bytes(mbytes)
        already_durable = self._assert_resume_consistent(bundle_id, manifest_digest)

        # A2-11b (ADR-0020, Option A): gate the CANONICAL cloud writes on the durability interlock. Consult
        # ABOVE the first backend.put so a stale/flipped system_health status stops a NEW bundle before ANY
        # blob is put or sealed (the W1-W5 writes), not merely at the later register txn. Canonical-lane ONLY
        # (ADR-0020 governs canonical writes; owner-ruled 2026-07-07) and RESUME-AWARE — an already-durable
        # bundle (sealed/registered: blobs exist, GC-exempt) is NOT gated, so finishing/re-saving durable
        # bytes is never blocked by a flip. This is minimal over hoisting the :256-257 register short-circuit,
        # which stays untouched (its idempotent-resume return still fires downstream).
        if self._expected_lane == "canonical" and not already_durable:
            assert_durability_ok(self.engine)

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
            # ONE table_manifests row per QUERYABLE parquet artifact (Stage-1 DEFER resolved); specs were
            # resolved before the manifest build so block 14 and this INSERT share one footer_kv object.
            # footer_kv_sha256 mirrors sha256(canonical_json(footer_kv)) — the manifest-side recompute is
            # identical, so file<->manifest<->DB cross-check on the SAME bytes (contract §5 block 14).
            for spec in specs:
                fkv_sha = (
                    sha256_bytes(canonical_json(dict(spec.footer_kv))) if spec.footer_kv is not None else None
                )
                conn.execute(
                    text(
                        "INSERT INTO neuro.table_manifests "
                        "(dataset_name, run_id, model_id, hook_point_id, schema_major, partition_path, "
                        "row_count, artifact_id, footer_kv_sha256) "
                        "VALUES (:ds, :r, :mid, :hp, :sm, :pp, :rc, :aid, :fkv) "
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
                        "fkv": fkv_sha,
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
