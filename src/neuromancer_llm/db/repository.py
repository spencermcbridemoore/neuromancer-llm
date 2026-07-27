"""THE one repository interface — Postgres-only (ADR-0039 Reconsidered 2026-06-17; no SQLite backend).

This is the single home for the queue's SQL (ADR-0039): SKIP-LOCKED claim, monotonic claim_seq +
claim_token fencing, every mutation CAS-guarded by rowcount, runtime-owned leases, dependency gating
(non-claimable 'blocked' state, C2), and recursive cascade-cancellation. The golden-snapshot harness
(Q15 KEEP) exercises this against a real Postgres fixture.

Stage 1 implements the queue + minimal seed helpers (what the golden harness needs). The registration
side of the repository (capture_events / bundles / artifacts) is exercised via bundles/registrar.py;
broader query surface lands with the workflows that consume it.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Connection, Engine, text

from .identity import fingerprint_hash as _fingerprint_hash
from .identity import model_identity_hash
from .lanes import ConfigurationError


class IdentityMismatchError(RuntimeError):
    """Register-first, fail-loud: an identity/fingerprint already exists with the SAME hash but DIFFERENT
    recorded components (ADR-0005 raise-on-mismatch). Never silently adopt a divergent identity."""


class ReplicateMismatchError(RuntimeError):
    """Two runs linked as replicates must be the SAME experiment (one shared, non-NULL fingerprint_id):
    MEASURED divergence is only meaningful within a single experiment identity (ADR-0004)."""


# Lease timing (ADR-0039): 120s lease / 40s renew / 60s reaper, all server-side now(). The renewal
# thread + reaper loop (the *policy*) are owned by workers/runtime.py; the lease INTERVAL is applied
# here because this is where the lease SQL lives.
LEASE_SECONDS = 120
# The renewal cadence is DERIVED from the lease TTL by its own SAFETY FLOOR, never an independent
# literal: at least LEASE_RENEW_MIN_ATTEMPTS renewal attempts must fit inside one lease TTL. Two
# literals would be two implementations of one relationship — and the rot is silent: a later
# LEASE_SECONDS edit would leave the cadence behind and hand live workers' jobs to the reaper.
# `resolve_renew_interval` below is the fail-closed authority, and it is what the B-4 renewal thread
# calls (workers/runtime.py).
#
# ⚠ THE MARGIN, STATED EXACTLY (it is NOT `MIN_ATTEMPTS - 1`). After a renewal succeeds at T the lease
# runs to T+LEASE_SECONDS, and the loop acts-then-waits, so attempts land at T+40, T+80, T+120+ε. Only
# the first two are STRICTLY inside the lease, and the third is refused by #6a's `expires_at >= now()`.
# ⇒ at 40/120 exactly **ONE** consecutive lost heartbeat is survivable, not two. Recorded precisely
# because a module must not overstate its own guarantee: an operator sizing a retry budget against
# "two" would be sizing against a margin that does not exist. Widening it means a SMALLER divisor of
# LEASE_SECONDS, which changes the ADR-0039 cadence and is an owner call, not a silent edit.
LEASE_RENEW_MIN_ATTEMPTS = 3
RENEW_SECONDS = LEASE_SECONDS // LEASE_RENEW_MIN_ATTEMPTS  # 40 — the ADR-0039 cadence, now derived
REAPER_SECONDS = 60


def resolve_renew_interval() -> int:
    """The single renewal-cadence resolution point (the `resolve_archiver_probe_interval` /
    `resolve_backup_stale_after` pin idiom): the renewal interval in seconds, or `ConfigurationError`
    if it no longer preserves the margin its own derivation promises.

    FAIL CLOSED ON A STATED PROPERTY, not a weaker one. The check is
    `RENEW_SECONDS * LEASE_RENEW_MIN_ATTEMPTS <= LEASE_SECONDS` — at least LEASE_RENEW_MIN_ATTEMPTS
    attempts fit inside one lease TTL. A merely `RENEW < LEASE` check would admit 60/120, which leaves
    only ONE in-lease attempt and NO lost-heartbeat margin at all: that hands a running job to the
    reaper and manufactures exactly the near-expiry contention ADR-0046 exists to remove.

    ⚠ WHAT THIS DOES **NOT** PROMISE (see the constants block): the surviving margin is
    LEASE_RENEW_MIN_ATTEMPTS **minus two**, because the last of the fitted attempts lands ON the expiry
    boundary and #6a refuses it. At the shipped 40/120 that is ONE lost heartbeat, not two. The guard
    is a floor on the cadence, not a promise about consecutive failures.

    Values are read from the module globals at CALL time, so the guard is live on the real renewal path
    rather than an import-time assertion (an import-time raise would redden the whole suite as a
    collection ERROR, which verifies nothing)."""
    if RENEW_SECONDS <= 0 or RENEW_SECONDS * LEASE_RENEW_MIN_ATTEMPTS > LEASE_SECONDS:
        raise ConfigurationError(
            f"lease renewal cadence is unsafe: RENEW_SECONDS={RENEW_SECONDS}s leaves fewer than "
            f"{LEASE_RENEW_MIN_ATTEMPTS} attempts inside a {LEASE_SECONDS}s lease "
            f"(db/repository.py) — refusing to run a renewal thread with no lost-heartbeat margin "
            f"(fail closed). A change is an auditable commit."
        )
    return RENEW_SECONDS


# A job is claimable only when 'queued'. 'blocked' (unmet deps) and all in-flight/terminal states are
# excluded by construction — the claim predicate never selects them (C2).
CLAIMABLE_STATE = "queued"


@dataclass(frozen=True)
class Claim:
    job_id: int
    claim_token: uuid.UUID
    claim_seq: int


@dataclass(frozen=True)
class ReconcileResult:
    """One spend reconciliation (A2-15 / R4(c)): the LEDGER-predicted total vs the ACTUAL billed figure, the
    SIGNED divergence percent (positive = billed OVER predicted), and whether it breaches the threshold."""

    predicted_usd: Decimal
    billed_usd: Decimal
    divergence_pct: Decimal
    threshold_pct: Decimal
    breach: bool


@dataclass(frozen=True)
class SpendReport:
    """The `neuro spend report` result: per-run non-standing spend (run_id -> USD), the standing total, the
    unattributed total (non-standing spend with NO run_id), and the grand total — the SU/$ accounting that
    doubles as Jetstream2-renewal evidence (A2-15). The three buckets PARTITION every ledger row, so
    sum(per_run) + standing_usd + unattributed_usd == total_usd always holds (no spend is hidden)."""

    per_run: tuple[tuple[int, Decimal], ...]
    standing_usd: Decimal
    unattributed_usd: Decimal
    total_usd: Decimal


class Repository:
    def __init__(
        self, engine: Engine, *, expected_lane: str, expected_uuid: uuid.UUID | str | None = None
    ) -> None:
        # R1 (fail-closed write choke point): verify the target's identity ONCE at construction
        # (ADR-0006). No write path exists on an unverified target — a Repository cannot be built
        # against an unprovisioned or wrong-lane DB. Canonical-lane callers also pass expected_uuid.
        from .session import verify_engine

        self.engine = verify_engine(engine, expected_lane=expected_lane, expected_uuid=expected_uuid)
        # The VERIFIED lane (assert_lane confirmed the engine's identity matches it) — the trustworthy basis
        # for the A2-11b durability consult, which must never trust a free caller-passed lane (capture side).
        self.expected_lane = expected_lane

    # --- seed helpers (minimal; full registry write paths are registrar-side) -------------------
    def create_actor(self, actor_key: str, *, kind: str = "agent", display_name: str | None = None) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.actors (actor_key, kind, display_name) "
                    "VALUES (:k, :kind, :dn) RETURNING actor_id"
                ),
                {"k": actor_key, "kind": kind, "dn": display_name or actor_key},
            ).scalar_one()

    def create_campaign(self, campaign_key: str, actor_id: int) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.campaigns (campaign_key, actor_id) VALUES (:k, :a) RETURNING campaign_id"
                ),
                {"k": campaign_key, "a": actor_id},
            ).scalar_one()

    def create_run(
        self,
        run_key: str,
        *,
        campaign_id: int,
        work_slug: str,
        variant_digest: str,
        actor_id: int,
        origin: str = "test",
        run_kind: str = "experiment",
    ) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.runs (run_key, campaign_id, work_slug, variant_digest, run_kind, actor_id, origin) "
                    "VALUES (:rk, :c, :ws, :vd, :kind, :a, :o) RETURNING run_id"
                ),
                {
                    "rk": run_key,
                    "c": campaign_id,
                    "ws": work_slug,
                    "vd": variant_digest,
                    "kind": run_kind,
                    "a": actor_id,
                    "o": origin,
                },
            ).scalar_one()

    # --- idempotent seeds (get-or-create; the capture lane is re-runnable) -----------------------
    def get_or_create_actor(
        self, actor_key: str, *, kind: str = "agent", display_name: str | None = None
    ) -> int:
        """Register-first, fail-loud on IDENTITY drift (ADR-0048 layer (a); Deferred-Obligation Register —
        trigger = multi-user creds OR the importer, now arriving).

        IDENTITY = (`actor_key`, `kind`). Re-registering an actor_key under a DIFFERENT kind raises
        IdentityMismatchError — one human-readable key colliding across kinds (e.g. an `importer` key hitting
        an existing `agent`) is exactly the silent misattribution this closes.

        `display_name` is a mutable human LABEL, not identity: it is deliberately NOT compared, and the
        EXISTING row's label is KEPT (no last-writer-wins). The column is NOT NULL and the INSERT coerces
        `display_name or actor_key`, so there is no stored "unspecified" state to compare against; and the
        registrar holds NO UPDATE on `actors` (grants.sql), so raising on a label would be unrecoverable
        in-role. Relabeling is an admin act.

        Bare ON CONFLICT (no arbiter) + re-SELECT also closes the concurrent-first-creation race (the
        BLOCK-1/1b class): `actors` carries exactly ONE non-PK unique (`actor_key`).
        """
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.actors (actor_key, kind, display_name) "
                    "VALUES (:k, :kind, :dn) ON CONFLICT DO NOTHING RETURNING actor_id"
                ),
                {"k": actor_key, "kind": kind, "dn": display_name or actor_key},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text("SELECT actor_id, kind FROM neuro.actors WHERE actor_key = :k"),
                    {"k": actor_key},
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:  # actor_key is the ONLY non-PK unique -> a conflict must be findable
                raise IdentityMismatchError(
                    f"actor {actor_key!r} could not be created and does not exist (fail loud)."
                )
            if existing["kind"] != kind:
                raise IdentityMismatchError(
                    f"actor {actor_key!r} is already registered with kind {existing['kind']!r}, not "
                    f"{kind!r} — refusing to bind one actor_key to two kinds (register-first, fail-loud; "
                    "ADR-0048 layer (a))."
                )
            return existing["actor_id"]

    def get_or_create_campaign(self, campaign_key: str, actor_id: int) -> int:
        """Register-first, fail-loud on OWNERSHIP drift (ADR-0048 layer (a); the headline bite).

        The campaign's owner (`actor_id`) IS identity-bearing. Previously this returned the existing row by
        key with NO comparison, so re-creating a campaign_key under a DIFFERENT actor_id silently KEPT the
        old owner — a silent ownership/lineage reassignment (harmless single-user; the real bite is two
        owners colliding on a shared human-readable key, which the importer makes reachable at scale). It
        now raises IdentityMismatchError.

        Bare ON CONFLICT + re-SELECT also closes the concurrent-first-creation race; `campaigns` carries
        exactly ONE non-PK unique (`campaign_key`).
        """
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.campaigns (campaign_key, actor_id) VALUES (:k, :a) "
                    "ON CONFLICT DO NOTHING RETURNING campaign_id"
                ),
                {"k": campaign_key, "a": actor_id},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text("SELECT campaign_id, actor_id FROM neuro.campaigns WHERE campaign_key = :k"),
                    {"k": campaign_key},
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:  # campaign_key is the ONLY non-PK unique -> a conflict must be findable
                raise IdentityMismatchError(
                    f"campaign {campaign_key!r} could not be created and does not exist (fail loud)."
                )
            if existing["actor_id"] != actor_id:
                raise IdentityMismatchError(
                    f"campaign {campaign_key!r} is already owned by actor_id {existing['actor_id']}, not "
                    f"{actor_id} — refusing to silently reassign ownership/lineage (register-first, "
                    "fail-loud; ADR-0048 layer (a))."
                )
            return existing["campaign_id"]

    def get_or_create_run(
        self,
        run_key: str,
        *,
        campaign_id: int,
        work_slug: str,
        variant_digest: str,
        actor_id: int,
        origin: str,
        run_kind: str = "experiment",
        fingerprint_id: int | None = None,
        invocation_id: uuid.UUID | None = None,
        spec_hash: bytes | None = None,
        is_unlabeled: bool = False,
    ) -> int:
        """Get-or-create a run by run_key. fingerprint_id is set at INSERT for a LABELED run (the
        assign-once trigger guards only the UPDATE path, so an at-insert value is fine).

        Register-first, fail-loud (composer note D1): a run_key conflict is idempotent ONLY when the
        immutable identity fields match. A changed fingerprint / campaign / slug / digest / kind /
        invocation under the SAME run_key raises IdentityMismatchError — a changed semantic config (new
        fingerprint) must NOT silently reuse the old run. (actor/origin/spec_hash/is_unlabeled are stamps,
        not identity, and are not compared.)

        BLOCK-1b (concurrent first-creation race): runs has TWO non-PK uniques — run_key AND the PARTIAL
        runs_experiment_variant_uq (campaign, slug, digest, invocation WHERE run_kind='experiment'). The old
        SELECT-then-INSERT (no ON CONFLICT) let two concurrent creators both SELECT-miss then both INSERT, so
        the loser raised an unhandled IntegrityError. The INSERT now uses a bare ON CONFLICT DO NOTHING
        (arbitrates on EITHER unique) so the loser falls through to the re-SELECT and the same compare —
        identical-key concurrent creation dedupes to one row; a DIFFERENT run_key claiming the SAME experiment
        variant is an identity violation that raises LOUD (never a silent adopt)."""
        requested = {
            "campaign_id": campaign_id,
            "work_slug": work_slug,
            "variant_digest": variant_digest,
            "run_kind": run_kind,
            "fingerprint_id": fingerprint_id,
            "invocation_id": invocation_id,
        }

        def _verify_or_raise(existing) -> int:
            for key, want in requested.items():
                got = existing[key]
                if key == "invocation_id":  # uuid column round-trips as uuid.UUID; compare as text
                    got = str(got) if got is not None else None
                    want = str(want) if want is not None else None
                if got != want:
                    raise IdentityMismatchError(
                        f"run_key {run_key!r} already exists with {key}={existing[key]!r}, not "
                        f"{requested[key]!r} (register-first, fail-loud; composer note D1 — a changed "
                        "experiment under the same run_key must not silently reuse the old run)."
                    )
            return existing["run_id"]

        select_by_key = (
            "SELECT run_id, campaign_id, work_slug, variant_digest, run_kind, fingerprint_id, "
            "invocation_id FROM neuro.runs WHERE run_key = :rk"
        )
        with self.engine.begin() as conn:
            existing = conn.execute(text(select_by_key), {"rk": run_key}).mappings().one_or_none()
            if existing is not None:
                return _verify_or_raise(existing)
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.runs (run_key, campaign_id, work_slug, variant_digest, run_kind, "
                    "fingerprint_id, actor_id, origin, is_unlabeled, spec_hash, invocation_id) "
                    "VALUES (:rk, :c, :ws, :vd, :kind, :fp, :a, :o, :ul, :sh, :inv) "
                    "ON CONFLICT DO NOTHING RETURNING run_id"
                ),
                {
                    "rk": run_key,
                    "c": campaign_id,
                    "ws": work_slug,
                    "vd": variant_digest,
                    "kind": run_kind,
                    "fp": fingerprint_id,
                    "a": actor_id,
                    "o": origin,
                    "ul": is_unlabeled,
                    "sh": spec_hash,
                    "inv": invocation_id,
                },
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            # The INSERT conflicted. Re-SELECT by run_key: if found, run the same identity compare (a lost
            # same-key race dedupes / a drift raises). If NOT found, the conflict was runs_experiment_variant_uq
            # under a DIFFERENT run_key — two run_keys claiming one experiment variant — an identity violation.
            raced = conn.execute(text(select_by_key), {"rk": run_key}).mappings().one_or_none()
            if raced is None:
                raise IdentityMismatchError(
                    f"run_key {run_key!r} could not be created: its (campaign, work_slug, variant_digest, "
                    "invocation_id) experiment variant is already claimed by a DIFFERENT run_key "
                    "(runs_experiment_variant_uq) — refusing to bind one experiment variant to two run_keys "
                    "(register-first, fail-loud)."
                )
            return _verify_or_raise(raced)

    def get_or_create_storage_backend(
        self,
        backend_key: str,
        *,
        driver: str,
        lane: str,
        base_uri: str,
        is_cloud: bool,
    ) -> int:
        """Register-first / IMMUTABLE storage-backend identity (FIX #8 + binding row L-SB). This was the ONLY
        registry that MUTATED on conflict (`DO UPDATE SET driver`, ignoring lane/base_uri/is_cloud) — a silent
        repoint of the lake driver/URI under a stable backend_key = split-brain blob storage. It now matches
        the sibling registries: INSERT-only, raise IdentityMismatchError on ANY drift of
        (driver, lane, base_uri, is_cloud); identical re-register is idempotent."""
        # A2-4 (GO §5): a cloud DRIVER must carry is_cloud=True. `driver` and `is_cloud` are otherwise
        # decoupled (no CHECK links them), so a cost gate keying on the cloud driver / is_cloud could be
        # fooled by an azure_blob row registered is_cloud=False. Refuse it at the registration choke point
        # (the sibling raise-on-drift idiom) so no consumer of is_cloud can be misled by a decoupled row.
        if driver == "azure_blob" and not is_cloud:
            raise IdentityMismatchError(
                f"storage_backend {backend_key!r} declares driver='azure_blob' but is_cloud=False — a cloud "
                "driver must carry is_cloud=True (fail closed; register the cloud backend with is_cloud=True)."
            )
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.storage_backends (backend_key, driver, lane, base_uri, is_cloud) "
                    "VALUES (:k, :d, :l, :u, :c) "
                    "ON CONFLICT (backend_key) DO NOTHING RETURNING backend_id"
                ),
                {"k": backend_key, "d": driver, "l": lane, "u": base_uri, "c": is_cloud},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text(
                        "SELECT backend_id, driver, lane, base_uri, is_cloud FROM neuro.storage_backends "
                        "WHERE backend_key = :k"
                    ),
                    {"k": backend_key},
                )
                .mappings()
                .one()
            )
            want = {"driver": driver, "lane": lane, "base_uri": base_uri, "is_cloud": is_cloud}
            for field, value in want.items():
                if existing[field] != value:
                    raise IdentityMismatchError(
                        f"storage_backend {backend_key!r} already registered with {field}="
                        f"{existing[field]!r}, not {value!r} — refusing to repoint the lake under a stable "
                        "backend_key (FIX #8 / L-SB: register-first, raise-on-drift)."
                    )
            return existing["backend_id"]

    # --- register-first, fail-loud identity (ADR-0005; registrar role) --------------------------
    def register_tokenizer_identity(
        self,
        *,
        tokenizer_hash: bytes,
        hf_repo: str | None = None,
        hf_revision: str | None = None,
        note: str | None = None,
    ) -> int:
        """INSERT-only by tokenizer_hash (the durable identity); idempotent return on conflict. On a
        tokenizer_hash conflict the recorded hf_repo/hf_revision must MATCH when both sides specify them —
        a same-hash re-register with conflicting (non-NULL) provenance raises (consistency with the model/
        fingerprint conflict path; ADR-0005). A NULL on either side is 'unspecified', not a conflict.

        BOTH DIRECTIONS RAISE (the second added 2026-07-22). The one above is the HARMLESS direction —
        right FILE, wrong LABEL — and it is cosmetic: the durable identity is the hash, and the hash is
        correct. The DAMAGING direction is its mirror image, right LABEL, wrong FILE: a wrong tokenizer.json
        registered under an already-known (hf_repo, hf_revision) mints a SECOND tokenizer identity, and from
        it a second model_identity (register_model_identity hashes over tokenizer_HASH) and a second
        fingerprint (model_identity is folded into the semantic config) — a tokenizer -> model -> fingerprint
        fork that every row COUNT reads green through. `cli/capture.py:22-25`'s 2026-07-02 audit correction
        already named this exact end state "a silent identity split" that "raise-on-drift could never
        reconcile", but it fixed only the INPUT SHAPE (hex validity + 32 bytes) — so a WELL-FORMED sha256 of
        the wrong file still landed in precisely the hazard that correction names.

        ⚠ THE LABEL SCAN THEREFORE RUNS ON BOTH BRANCHES, ABOVE THE INSERTED/CONFLICT SPLIT — not only when
        a row was minted. A first draft ran it on the INSERTED branch alone, which was a checkable falsehood
        (post-build vet, 2026-07-22): the wrong file's hash may ALREADY be registered — a NULL-labelled row
        is enough — in which case the call lands on the CONFLICT branch, which mints nothing but still hands
        back a tokenizer_id bound to the wrong bytes, and the caller forks model + fingerprint from it just
        the same. The stored-NULL drift loop below cannot catch it either: a stored NULL is 'unspecified' and
        passes. "Nothing was inserted" is NOT "nothing was bound".

        It takes an EXPLICIT SECOND LOOKUP, not a wider conflict branch: `tokenizer_identities` is UNIQUE on
        `tokenizer_hash` ALONE (phase3-ddl.sql:79-81 — hf_repo/hf_revision are bare nullable text carrying no
        unique), so NO `ON CONFLICT` arbiter can ever route a label collision into the conflict branch. That
        structural dead end is why the gap outlived earlier vets: the upsert idiom cannot express it.

        ⚠ It cannot false-fire on an honest re-capture: `tokenizer_hash` is a raw sha256 over
        tokenizer.json's BYTES, carrying no tokenizer-library version, so it is stable across re-reads of the
        same file. Two things DO make it fire on a re-read, and neither is a defect in the guard — both are
        the label failing to name one immutable artifact. (a) Sourcing tokenizer.json from a git checkout
        under `core.autocrlf`: CRLF and LF bytes hash differently at the same commit — the ESTELA corpus trap
        (env-gotchas) reappearing on a different file; hash the file as FETCHED, never as checked out.
        (b) Passing a MUTABLE `hf_revision` (a branch name like `main` rather than a commit sha): nothing at
        HEAD constrains the column to an immutable ref, so the same label legitimately covers different bytes
        over time. In both cases the raise is the guard correctly refusing to let two files hide under one
        label — pin the revision."""
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.tokenizer_identities (tokenizer_hash, hf_repo, hf_revision, note) "
                    "VALUES (:h, :r, :rev, :n) ON CONFLICT (tokenizer_hash) DO NOTHING RETURNING tokenizer_id"
                ),
                {"h": tokenizer_hash, "r": hf_repo, "rev": hf_revision, "n": note},
            ).scalar_one_or_none()
            self._assert_no_tokenizer_label_collision(conn, tokenizer_hash, hf_repo, hf_revision)
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text(
                        "SELECT tokenizer_id, hf_repo, hf_revision FROM neuro.tokenizer_identities "
                        "WHERE tokenizer_hash = :h"
                    ),
                    {"h": tokenizer_hash},
                )
                .mappings()
                .one()
            )
            for field, want in (("hf_repo", hf_repo), ("hf_revision", hf_revision)):
                if want is not None and existing[field] is not None and existing[field] != want:
                    raise IdentityMismatchError(
                        f"tokenizer {tokenizer_hash.hex()[:12]} already exists with {field}="
                        f"{existing[field]!r}, not {want!r} (ADR-0005 register-first, raise-on-mismatch)."
                    )
            return existing["tokenizer_id"]

    @staticmethod
    def _assert_no_tokenizer_label_collision(
        conn: Connection,
        tokenizer_hash: bytes,
        hf_repo: str | None,
        hf_revision: str | None,
    ) -> None:
        """Raise if a FULLY PINNED (hf_repo, hf_revision) already names a DIFFERENT tokenizer_hash.

        Called ABOVE the inserted/conflict split, inside the CALLER'S transaction, so that (a) on the
        inserted branch the raise rolls the just-minted row back — nothing durable is created — and (b) on
        the conflict branch it still fires, which is the case a first draft missed. That placement is the
        point: an identity split is unrecoverable in practice — there is no delete verb for identity rows
        anywhere in src/ (the only DELETE is bundles/gc.py, on unsealed bundles).

        SCOPE, stated so it can never be mistaken for coverage: it fires only when BOTH incoming labels are
        non-NULL. A pair with a NULL half names no immutable upstream artifact, so it is 'unspecified' —
        the conflict branch's NULL-means-unspecified convention, applied here to the PAIR rather than
        per-field — and is deliberately OUT of scope rather than checked-and-passed. Both asymmetric NULL
        cases are pinned BEHAVIOURALLY by test (the standing both-NULL-cases obligation), as are stored-side
        NULLs, which fall out of the SQL equality for free.

        ⚠ The `hf_repo is None or hf_revision is None` early return is a BELT, not the mechanism, and is
        deliberately NOT counted in the mutation matrix (precedent 11): deleting it is behaviour-preserving,
        because binding NULL to :r or :rev makes the WHERE clause NULL for every row under SQL three-valued
        logic, so `clash` is None and control reaches the same return. It is kept for legibility and so the
        fail-closed intent is stated where a reader meets it — but it is UNPINNABLE BY CONSTRUCTION, and
        claiming otherwise would be the overclaim precedent 11 exists to prevent.

        NOT a substitute for a DB constraint under concurrency: two concurrent first-registrations of
        different hashes under one label can each miss the other under READ COMMITTED. That is the standing
        ADR-0046 row-locking class, unchanged by this guard; the capture path is single-writer today.
        """
        if hf_repo is None or hf_revision is None:
            return
        clash = conn.execute(
            text(
                "SELECT tokenizer_hash FROM neuro.tokenizer_identities "
                "WHERE hf_repo = :r AND hf_revision = :rev AND tokenizer_hash <> :h LIMIT 1"
            ),
            {"r": hf_repo, "rev": hf_revision, "h": tokenizer_hash},
        ).scalar_one_or_none()
        if clash is None:
            return
        raise IdentityMismatchError(
            f"tokenizer label {hf_repo}@{hf_revision} is already registered with tokenizer_hash "
            f"{clash.hex()[:12]}, not {tokenizer_hash.hex()[:12]} — one pinned (hf_repo, hf_revision) names "
            "ONE immutable tokenizer.json, so a second hash under it is a WRONG-FILE read, not a new "
            "identity. Registering it would fork tokenizer -> model_identity -> fingerprint while every row "
            "count reads green (ADR-0005 register-first, raise-on-drift; the 'silent identity split' named "
            "by cli/capture.py's 2026-07-02 audit correction, which fixed only the input SHAPE)."
        )

    def register_model_identity(
        self,
        *,
        hf_repo: str | None,
        hf_revision: str | None,
        dtype_quant: str,
        tokenizer_hash: bytes,
        serving_stack: str,
        serving_version: str,
        arch_family: str,
    ) -> int:
        """INSERT-only by the 7-component identity_hash (ADR-0005). On conflict, verify the recorded
        components MATCH and raise IdentityMismatchError on any drift (never silently adopt).

        FIX #9 (register-first / no identity-split): the identity_hash is computed over tokenizer_HASH, so
        tokenizer_id is DERIVED from tokenizer_hash here (one source of truth) rather than trusted from the
        caller — a caller can no longer bind a model to tokenizer row B while the identity embeds tokenizer
        A's hash (the FK only proves B exists). Register-first: the tokenizer must already be registered, or
        this raises IdentityMismatchError (mirrors the model/tokenizer/method/fingerprint raise-on-drift)."""
        identity_hash = model_identity_hash(
            hf_repo=hf_repo,
            hf_revision=hf_revision,
            dtype_quant=dtype_quant,
            tokenizer_hash=tokenizer_hash,
            serving_stack=serving_stack,
            serving_version=serving_version,
            arch_family=arch_family,
        )
        with self.engine.begin() as conn:
            # FIX #9: resolve tokenizer_id FROM the hash the identity embeds (register-first, fail-loud).
            tokenizer_id = conn.execute(
                text("SELECT tokenizer_id FROM neuro.tokenizer_identities WHERE tokenizer_hash = :h"),
                {"h": tokenizer_hash},
            ).scalar_one_or_none()
            if tokenizer_id is None:
                raise IdentityMismatchError(
                    f"tokenizer {tokenizer_hash.hex()[:12]} is not registered — register the tokenizer "
                    "identity first (FIX #9 derives tokenizer_id FROM tokenizer_hash; register-first)."
                )
            components = {
                "hf_repo": hf_repo,
                "hf_revision": hf_revision,
                "dtype_quant": dtype_quant,
                "tokenizer_id": tokenizer_id,
                "serving_stack": serving_stack,
                "serving_version": serving_version,
                "arch_family": arch_family,
            }
            # BLOCK-1 (concurrent first-registration race): model_identities has TWO non-PK uniques —
            # identity_hash AND model_components_uq (the 7 components). With an `ON CONFLICT (identity_hash)`
            # arbiter, a concurrent same-identity insert that Postgres happens to detect on the NON-arbiter
            # model_components_uq index raised an unhandled IntegrityError (measured ~33% per 2-thread race).
            # A BARE `ON CONFLICT DO NOTHING` arbitrates on EITHER unique index, so the loser of the race
            # falls through to the re-SELECT (raise-on-drift preserved) instead of crashing.
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.model_identities "
                    "(identity_hash, hf_repo, hf_revision, dtype_quant, tokenizer_id, serving_stack, "
                    "serving_version, arch_family) "
                    "VALUES (:ih, :hr, :rev, :dq, :tid, :ss, :sv, :af) "
                    "ON CONFLICT DO NOTHING RETURNING model_id"
                ),
                {
                    "ih": identity_hash,
                    "hr": hf_repo,
                    "rev": hf_revision,
                    "dq": dtype_quant,
                    "tid": tokenizer_id,
                    "ss": serving_stack,
                    "sv": serving_version,
                    "af": arch_family,
                },
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text(
                        "SELECT model_id, hf_repo, hf_revision, dtype_quant, tokenizer_id, serving_stack, "
                        "serving_version, arch_family FROM neuro.model_identities WHERE identity_hash = :ih"
                    ),
                    {"ih": identity_hash},
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                # The bare DO NOTHING swallowed a conflict on model_components_uq under a DIFFERENT
                # identity_hash (same components, different hash) — with a deterministic hash + same args this
                # is unreachable, but stay LOUD rather than crash on .one() or silently return None.
                raise IdentityMismatchError(
                    f"model_identity insert conflicted on the component tuple but no row matches "
                    f"identity_hash {identity_hash.hex()[:12]} — a component-set / identity_hash split "
                    "(register-first, fail-loud; never a silent adopt)."
                )
            for key, want in components.items():
                if existing[key] != want:
                    raise IdentityMismatchError(
                        f"model_identity {identity_hash.hex()[:12]} already exists with {key}="
                        f"{existing[key]!r}, not {want!r} (ADR-0005 register-first, raise-on-mismatch)."
                    )
            return existing["model_id"]

    def register_fingerprint(
        self, *, fingerprint_hash: bytes, model_id: int, declared_mode: str, semantic_config: str
    ) -> int:
        """INSERT-only (the grant REVOKES UPDATE/DELETE; ADR-0005/0007). On conflict, verify the recorded
        semantic_config / model / mode MATCH and raise on drift (force-new-run is an explicit new hash).

        FIX #4 (no trust-the-caller): the fingerprint_hash is RECOMPUTED from semantic_config with the ONE
        canonical hash fn and a mismatch raises IdentityMismatchError BEFORE any write — closing the lone
        trust-the-caller asymmetry (model/tokenizer/method identity already raise-on-drift). The caller can
        no longer bind an arbitrary hash to a config (a fingerprint that does not match its own config)."""
        recomputed = _fingerprint_hash(semantic_config)
        if recomputed != fingerprint_hash:
            raise IdentityMismatchError(
                f"fingerprint_hash {fingerprint_hash.hex()[:12]} does not match "
                f"fingerprint_hash(semantic_config)={recomputed.hex()[:12]} — refusing a caller-supplied hash "
                "that does not match its own config (FIX #4: recompute-and-compare, reuse the one canonical fn)."
            )
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.fingerprints (fingerprint_hash, model_id, declared_mode, semantic_config) "
                    "VALUES (:fh, :mid, :dm, :cfg) ON CONFLICT (fingerprint_hash) DO NOTHING RETURNING fingerprint_id"
                ),
                {"fh": fingerprint_hash, "mid": model_id, "dm": declared_mode, "cfg": semantic_config},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text(
                        "SELECT fingerprint_id, model_id, declared_mode "
                        "FROM neuro.fingerprints WHERE fingerprint_hash = :fh"
                    ),
                    {"fh": fingerprint_hash},
                )
                .mappings()
                .one()
            )
            # Fold-in (d): the `existing["semantic_config"] != semantic_config` sub-clause is now DEAD — FIX #4
            # recomputes fingerprint_hash(semantic_config) and raises BEFORE any write, so reaching this branch
            # means the passed hash equals the recomputed hash, hence (barring a sha256 collision) the configs
            # are equal. model_id and declared_mode are SEPARATE caller params (not in the hashed material), so
            # a same-hash/same-config re-register with a different model or mode is reachable and stays guarded.
            if existing["model_id"] != model_id or existing["declared_mode"] != declared_mode:
                raise IdentityMismatchError(
                    f"fingerprint {fingerprint_hash.hex()[:12]} already exists with a different "
                    "model / declared_mode (ADR-0005 insert-only, raise-on-mismatch)."
                )
            return existing["fingerprint_id"]

    def seed_expected_rule(
        self, *, declared_mode: str, substrate_key: str, expected: str, note: str | None = None
    ) -> int:
        """Idempotent one-time config seed of the EXPECTED heuristic table (ADR-0004; never identity).
        ON CONFLICT (declared_mode, substrate_key) DO NOTHING — a SAME-value re-seed is a no-op.

        Audit correction 2026-07-02: on conflict the stored `expected` is now COMPARED — a re-seed with
        a DIFFERENT level raises (a wrongly-seeded EXPECTED was silently sticky: ABSENCE is loud via
        FIX #2, wrongness was not). Changing a lane's EXPECTED level is an explicit rule-table update
        (an admin act), never a silent re-seed. `note` is commentary, not compared."""
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.expected_reproducibility_rules (declared_mode, substrate_key, expected, note) "
                    "VALUES (:dm, :sk, :e, :n) "
                    "ON CONFLICT (declared_mode, substrate_key) DO NOTHING RETURNING rule_id"
                ),
                {"dm": declared_mode, "sk": substrate_key, "e": expected, "n": note},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text(
                        "SELECT rule_id, expected FROM neuro.expected_reproducibility_rules "
                        "WHERE declared_mode = :dm AND substrate_key = :sk"
                    ),
                    {"dm": declared_mode, "sk": substrate_key},
                )
                .mappings()
                .one()
            )
            if existing["expected"] != expected:
                raise IdentityMismatchError(
                    f"expected rule ({declared_mode!r}, {substrate_key!r}) is already seeded with "
                    f"expected={existing['expected']!r}, not {expected!r} — a re-seed is idempotent only "
                    "for the SAME level; changing EXPECTED is an explicit rule-table update, never a "
                    "silent re-seed (fail closed; audit 2026-07-02)."
                )
            return existing["rule_id"]

    # --- run_metrics vocabulary + writer (ADR-0017; ESTELA §A12 D4b — the FIRST run_metrics writer) -----
    def seed_metric_key(self, *, metric_key: str, value_kind: str, description: str) -> str:
        """Idempotent, FAIL-LOUD seed of a `metric_keys` vocabulary row — the FK target every `run_metrics`
        row references (`run_metrics.metric_key -> metric_keys.metric_key`). Modeled on `seed_expected_rule`
        (NOT `ON CONFLICT DO NOTHING` alone): a SAME-value re-seed is a no-op; a re-seed with a DIFFERENT
        `value_kind`/`description` RAISES, because `metric_keys` is an identity-bearing vocabulary and a
        silently-dropped divergent re-seed is the exact hole the 2026-07-02 seed audit closed. `metric_keys`
        INSERT is registrar/admin-only (grants.sql), so this runs on the control plane."""
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.metric_keys (metric_key, value_kind, description) "
                    "VALUES (:k, :vk, :d) ON CONFLICT (metric_key) DO NOTHING RETURNING metric_key"
                ),
                {"k": metric_key, "vk": value_kind, "d": description},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text("SELECT value_kind, description FROM neuro.metric_keys WHERE metric_key = :k"),
                    {"k": metric_key},
                )
                .mappings()
                .one()
            )
            if existing["value_kind"] != value_kind or existing["description"] != description:
                raise IdentityMismatchError(
                    f"metric_key {metric_key!r} is already seeded with a different value_kind/description — a "
                    "re-seed is idempotent only for the SAME vocabulary row; changing it is an explicit "
                    "vocabulary update, never a silent re-seed (fail closed)."
                )
            return metric_key

    def write_run_metric(
        self,
        *,
        run_id: int,
        metric_key: str,
        value_json: str | None = None,
        value_num: float | None = None,
    ) -> int:
        """Write ONE `run_metrics` row (the FIRST run_metrics writer; ADR-0017). Register-first /
        raise-on-drift on `UNIQUE(run_id, metric_key)`: a re-write of the SAME (run_id, metric_key) with the
        SAME value is idempotent (returns the existing id); a DIFFERENT value RAISES — the projection is
        deterministic given the capture, so a drift genuinely CONTRADICTS (the integrity class, not the
        attribution class). The `metric_key` FK must be seeded first (`seed_metric_key`); the 8 KB
        `run_metrics_valve` (ADR-0017) is enforced by the DB CHECK. run_metrics INSERT is `neuro_writer`."""
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.run_metrics (run_id, metric_key, value_num, value_json) "
                    "VALUES (:r, :k, :vn, :vj) "
                    "ON CONFLICT (run_id, metric_key) DO NOTHING RETURNING run_metric_id"
                ),
                {"r": run_id, "k": metric_key, "vn": value_num, "vj": value_json},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text(
                        "SELECT run_metric_id, value_num, value_json FROM neuro.run_metrics "
                        "WHERE run_id = :r AND metric_key = :k"
                    ),
                    {"r": run_id, "k": metric_key},
                )
                .mappings()
                .one()
            )
            if existing["value_num"] != value_num or existing["value_json"] != value_json:
                raise IdentityMismatchError(
                    f"run_metric (run_id={run_id}, metric_key={metric_key!r}) already exists with a different "
                    "value — the projection is deterministic given the capture, so a drift contradicts (raise, "
                    "never a silent keep-first)."
                )
            return existing["run_metric_id"]

    # --- MEASURED determinism: method registry + replicate links + divergence (ADR-0004/0011) ----
    def register_method_version(
        self, *, method_key: str, semver: str, code_sha: bytes, set_active: bool
    ) -> int:
        """Register-first, fail-loud a method version (ADR-0011 registry/runtime parity). The method is
        ensured first; the version is INSERT-only by (method_id, semver). On conflict the recorded code_sha
        must MATCH — a same-semver re-register with a DIFFERENT implementation hash raises (bump the semver
        when the code changes). A recorded NULL code_sha is UNVERIFIABLE and can never be adopted (see below).

        `set_active` is REQUIRED (no default): it repoints methods.active_version_id, and a defaulted True
        made EVERY call a silent last-write-wins repoint of the registry's active pointer. Callers state
        intent."""
        with self.engine.begin() as conn:
            method_id = conn.execute(
                text(
                    "INSERT INTO neuro.methods (method_key) VALUES (:k) "
                    "ON CONFLICT (method_key) DO NOTHING RETURNING method_id"
                ),
                {"k": method_key},
            ).scalar_one_or_none()
            if method_id is None:
                method_id = conn.execute(
                    text("SELECT method_id FROM neuro.methods WHERE method_key = :k"), {"k": method_key}
                ).scalar_one()
            mv_id = conn.execute(
                text(
                    "INSERT INTO neuro.method_versions (method_id, semver, code_sha) "
                    "VALUES (:m, :s, :c) ON CONFLICT (method_id, semver) DO NOTHING RETURNING method_version_id"
                ),
                {"m": method_id, "s": semver, "c": code_sha},
            ).scalar_one_or_none()
            if mv_id is None:
                existing = (
                    conn.execute(
                        text(
                            "SELECT method_version_id, code_sha FROM neuro.method_versions "
                            "WHERE method_id = :m AND semver = :s"
                        ),
                        {"m": method_id, "s": semver},
                    )
                    .mappings()
                    .one()
                )
                # A recorded NULL code_sha is UNVERIFIABLE: the old guard short-circuited on it
                # (`is not None and ...`), so such a row was silently ADOPTED against ANY incoming hash —
                # permanently defeating ADR-0011 parity for that method_key@semver, and every promotions row
                # FK'd to it would then carry an unverifiable governance stamp. Fail closed instead.
                if existing["code_sha"] is None:
                    raise IdentityMismatchError(
                        f"method {method_key}@{semver} is recorded with a NULL code_sha — an UNVERIFIABLE "
                        "version cannot be adopted (a NULL sha would match ANY implementation, silently "
                        "defeating ADR-0011 registry/runtime parity). Re-register it with its real code_sha "
                        "or bump the semver."
                    )
                if bytes(existing["code_sha"]) != code_sha:
                    raise IdentityMismatchError(
                        f"method {method_key}@{semver} already registered with a different code_sha "
                        f"(ADR-0011 registry/runtime parity) — bump the semver when the implementation changes."
                    )
                mv_id = existing["method_version_id"]
            if set_active:
                # The registry's active-version pointer (registrar-scoped UPDATE; grants.sql). Set AFTER the
                # version row exists so the methods_active_version_fk is satisfied.
                conn.execute(
                    text("UPDATE neuro.methods SET active_version_id = :v WHERE method_id = :m"),
                    {"v": mv_id, "m": method_id},
                )
            return mv_id

    def link_replicate(self, *, original_run_id: int, replicate_run_id: int) -> int:
        """Link a replicate run to its original (ADR-0004 MEASURED). Idempotent on the UNIQUE pair; the
        in-schema replicate_distinct CHECK (original <> replicate) is guarded here for a clear error.

        BLOCK 3: both runs must share ONE non-NULL fingerprint_id (the SAME experiment) — a mismatched or
        unlabeled pair raises ReplicateMismatchError BEFORE any replicate_links row is written (MEASURED
        divergence is meaningless across different experiments)."""
        if original_run_id == replicate_run_id:
            raise ValueError(
                f"replicate link requires two distinct runs (got {original_run_id} for both); a run cannot "
                "be its own replicate (replicate_distinct CHECK)."
            )
        with self.engine.begin() as conn:
            fps = (
                conn.execute(
                    text("SELECT run_id, fingerprint_id FROM neuro.runs WHERE run_id IN (:o, :r)"),
                    {"o": original_run_id, "r": replicate_run_id},
                )
                .mappings()
                .all()
            )
            by_run = {row["run_id"]: row["fingerprint_id"] for row in fps}
            o_fp, r_fp = by_run.get(original_run_id), by_run.get(replicate_run_id)
            if o_fp is None or r_fp is None or o_fp != r_fp:
                raise ReplicateMismatchError(
                    f"replicate link requires both runs to share one non-NULL fingerprint (original "
                    f"fingerprint_id={o_fp}, replicate fingerprint_id={r_fp}); MEASURED divergence is only "
                    "meaningful for two runs of the SAME experiment (ADR-0004)."
                )
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.replicate_links (original_run_id, replicate_run_id) VALUES (:o, :r) "
                    "ON CONFLICT (original_run_id, replicate_run_id) DO NOTHING RETURNING replicate_link_id"
                ),
                {"o": original_run_id, "r": replicate_run_id},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            return conn.execute(
                text(
                    "SELECT replicate_link_id FROM neuro.replicate_links "
                    "WHERE original_run_id = :o AND replicate_run_id = :r"
                ),
                {"o": original_run_id, "r": replicate_run_id},
            ).scalar_one()

    def record_divergence(
        self,
        *,
        replicate_link_id: int,
        method_version_id: int,
        max_abs_diff: float | None,
        max_rel_diff: float | None,
        argmax_flip_rate: float | None,
        answer_letter_flip_rate: float | None,
        near_tie_margin_nats: float | None,
    ) -> int:
        """Persist one MEASURED divergence keyed (replicate_link, method_version) (ADR-0004). Idempotent on
        the UNIQUE — re-measuring the same pair with the same registered method is a no-op (compare() is
        deterministic over the two immutable captures, so the recorded measurement does not drift)."""
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.divergence_measurements "
                    "(replicate_link_id, method_version_id, max_abs_diff, max_rel_diff, argmax_flip_rate, "
                    "answer_letter_flip_rate, near_tie_margin_nats) "
                    "VALUES (:l, :mv, :ma, :mr, :af, :al, :nt) "
                    "ON CONFLICT (replicate_link_id, method_version_id) DO NOTHING RETURNING divergence_id"
                ),
                {
                    "l": replicate_link_id,
                    "mv": method_version_id,
                    "ma": max_abs_diff,
                    "mr": max_rel_diff,
                    "af": argmax_flip_rate,
                    "al": answer_letter_flip_rate,
                    "nt": near_tie_margin_nats,
                },
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            return conn.execute(
                text(
                    "SELECT divergence_id FROM neuro.divergence_measurements "
                    "WHERE replicate_link_id = :l AND method_version_id = :mv"
                ),
                {"l": replicate_link_id, "mv": method_version_id},
            ).scalar_one()

    # --- enqueue (composer sets the component columns; deps -> 'blocked' at enqueue) -------------
    def enqueue(
        self,
        *,
        run_id: int,
        job_key: str,
        job_role: str | None = None,
        shard_key: str | None = None,
        queue: str = "default",
        gpu_class: str | None = None,
        vram_needed_mb: int | None = None,
        depends_on: Sequence[int] = (),
    ) -> int:
        # C20: compute the initial state from the deps' REAL state, never "has deps => blocked forever".
        # ⚠ THE DEP READ, THE CHILD INSERT AND THE EDGE INSERTS MUST STAY IN **THIS ONE** TRANSACTION.
        # The C20 fix is not the `FOR NO KEY UPDATE` token by itself — it is that the row lock the dep
        # read takes is still HELD when the child and its edges COMMIT. Hoisting the dep read into its
        # own `engine.begin()` (a plausible-looking connection-hygiene refactor) releases the lock early
        # and fully restores the TOCTOU while every behavioural probe stays green, which is why
        # `tests/redteam/test_rt_locking.py` pins this envelope by AST containment and not merely by the
        # call existing.
        with self.engine.begin() as conn:
            state = "queued" if not depends_on else self._initial_state_for_deps(conn, depends_on)
            job_id = conn.execute(
                text(
                    "INSERT INTO neuro.jobs (job_key, run_id, job_role, shard_key, queue, gpu_class, vram_needed_mb, state) "
                    "VALUES (:k, :r, :jr, :sk, :q, :gc, :v, :st) RETURNING job_id"
                ),
                {
                    "k": job_key,
                    "r": run_id,
                    "jr": job_role,
                    "sk": shard_key,
                    "q": queue,
                    "gc": gpu_class,
                    "v": vram_needed_mb,
                    "st": state,
                },
            ).scalar_one()
            for dep in depends_on:
                conn.execute(
                    text("INSERT INTO neuro.job_dependencies (job_id, depends_on) VALUES (:j, :d)"),
                    {"j": job_id, "d": dep},
                )
            return job_id

    @staticmethod
    def _initial_state_for_deps(conn, depends_on: Sequence[int]) -> str:
        """C20's dep-state read — the initial state of a job being enqueued against `depends_on`.

        All deps SUCCEEDED -> 'queued' (the unblock fires inside complete(), which already ran). A dep
        that is TERMINAL-FAILED (dead_letter / cancelled) can never become 'succeeded', so the dependent
        can never run -> 'cancelled' ('failed' is transient/retryable, so it is NOT terminal -> the
        dependent waits, 'blocked'). An unknown/missing dep id falls to 'blocked' via the length guard.

        **ADR-0046 (wedge 3) — `FOR NO KEY UPDATE`.** Unlocked, this read was a TOCTOU: a concurrent
        `complete(P)` could commit between the read and the child's INSERT, and its dependent-unblock
        cannot see a child that does not exist yet, so the child committed 'blocked' with NO remaining
        blocked->queued writer. Locking the dep rows makes the concurrent `complete`/`cancel_cascade`
        wait for THIS transaction's commit, after which its own fresh statement snapshot sees the child.

        **Lock strength is deliberately NO KEY UPDATE, not UPDATE.** It is the minimum that conflicts
        with the plain `UPDATE neuro.jobs` those callers issue (which itself takes NO KEY UPDATE, since
        `state` is not a key column), and — unlike `FOR UPDATE` — it does NOT conflict with the implicit
        RI `FOR KEY SHARE` that a concurrent `INSERT INTO neuro.job_dependencies` takes on these very
        rows. `FOR UPDATE` here opened a real jobs<->jobs deadlock against that FK path.

        **`ORDER BY job_id`** keeps acquisition ascending. Every jobs-row acquirer in this file is now
        ascending by construction (a dependent is enqueued after its parents, so a child's job_id always
        exceeds its parents'), which is what makes the whole locking pass cycle-free.

        ⚠ VISIBLE SIDE EFFECT, stated not glossed: `claim()` uses `FOR UPDATE SKIP LOCKED`, so while
        this lock is held a 'queued' dep is SKIPPED by a concurrent claim — `claim()` can return None
        with a claimable job present, and its lowest-queued-id order is best-effort under concurrency.
        Nothing is lost or double-claimed (the row stays 'queued' with no lease and is claimed on the
        next poll), but keep this transaction short: hold time is queue-visibility cost."""
        dep_states = (
            conn.execute(
                text(
                    "SELECT state FROM neuro.jobs WHERE job_id = ANY(:ids) ORDER BY job_id FOR NO KEY UPDATE"
                ),
                {"ids": list(depends_on)},
            )
            .scalars()
            .all()
        )
        if any(s in ("dead_letter", "cancelled") for s in dep_states):
            return "cancelled"
        if len(dep_states) == len(set(depends_on)) and all(s == "succeeded" for s in dep_states):
            return "queued"
        return "blocked"

    def state_of(self, job_id: int) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT state FROM neuro.jobs WHERE job_id = :j"), {"j": job_id}
            ).scalar_one()

    def get_job(self, job_id: int) -> dict:
        with self.engine.connect() as conn:
            return dict(
                conn.execute(text("SELECT * FROM neuro.jobs WHERE job_id = :j"), {"j": job_id})
                .mappings()
                .one()
            )

    # --- claim: SKIP LOCKED + fencing + lease (one transaction) ---------------------------------
    def claim(self, *, actor_id: int, queue: str = "default", gpu_class: str | None = None) -> Claim | None:
        token = uuid.uuid4()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    UPDATE neuro.jobs
                       SET state = 'claimed', claim_token = :tok, claim_seq = claim_seq + 1,
                           claimed_by = :actor, updated_at = now()
                     WHERE job_id = (
                         SELECT job_id FROM neuro.jobs
                          WHERE state = 'queued' AND queue = :queue
                            AND (CAST(:gpu AS text) IS NULL OR gpu_class IS NOT DISTINCT FROM :gpu)
                          ORDER BY job_id
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1
                     )
                    RETURNING job_id, claim_token, claim_seq
                    """
                    ),
                    {"tok": token, "actor": actor_id, "queue": queue, "gpu": gpu_class},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            conn.execute(
                text(
                    "INSERT INTO neuro.work_leases (job_id, claim_token, leased_by, expires_at) "
                    "VALUES (:job, :tok, :actor, now() + make_interval(secs => :lease))"
                ),
                {"job": row["job_id"], "tok": row["claim_token"], "actor": actor_id, "lease": LEASE_SECONDS},
            )
            return Claim(job_id=row["job_id"], claim_token=row["claim_token"], claim_seq=row["claim_seq"])

    # --- CAS-guarded, ownership-checked mutations -----------------------------------------------
    def heartbeat(self, *, job_id: int, claim_token: uuid.UUID) -> bool:
        with self.engine.begin() as conn:
            return self._renew_lease(conn, job_id=job_id, claim_token=claim_token)

    @staticmethod
    def _renew_lease(conn, *, job_id: int, claim_token: uuid.UUID) -> bool:
        """The lease-renewal statement itself (heartbeat's body, on a caller-supplied connection).

        R4: the join on jobs requires the job to still be in-flight, so a CANCELLED job's heartbeat
        returns False (the worker learns it has been preempted). UPDATE..FROM locks only work_leases
        (jobs is read, not locked) — no jobs/leases lock-order conflict with the reaper.

        FIX #6 (a): `l.expires_at >= now()` — a worker can NEVER renew an ALREADY-EXPIRED lease. An
        expired lease belongs to the reaper; renewing it was the wedge trigger (a heartbeat landing
        during the reaper's sweep renewed the lease while the job was requeued -> queued job + active
        lease -> next claim hits work_leases_active_uq -> un-claimable). Now an expired lease's
        heartbeat returns False and the reaper alone requeues it.

        ⚑ Split out from `heartbeat` so the ADR-0046 near-expiry hammer can hold this statement's row
        lock open across a latch while driving the REAL SQL. A hammer that re-implements the statement
        it is proving would be a fabricated green (precedent 14); `_cascade_cancel` is the file's
        shipped precedent for a connection-taking helper."""
        res = conn.execute(
            text(
                "UPDATE neuro.work_leases l SET last_heartbeat = now(), "
                "expires_at = now() + make_interval(secs => :lease) "
                "FROM neuro.jobs j "
                "WHERE l.job_id = :job AND l.claim_token = :tok AND l.released_at IS NULL "
                "AND l.expires_at >= now() "
                "AND j.job_id = l.job_id AND j.state IN ('claimed', 'running')"
            ),
            {"job": job_id, "tok": claim_token, "lease": LEASE_SECONDS},
        )
        return res.rowcount == 1

    def checkpoint(self, *, job_id: int, claim_token: uuid.UUID, checkpoint_ref: str) -> bool:
        with self.engine.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE neuro.jobs SET state = 'running', checkpoint_ref = :ref, updated_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND state IN ('claimed', 'running')"
                ),
                {"job": job_id, "tok": claim_token, "ref": checkpoint_ref},
            )
            return res.rowcount == 1

    def complete(self, *, job_id: int, claim_token: uuid.UUID) -> bool:
        """Succeed a job (CAS + ownership). On success, flip ready dependents 'blocked' -> 'queued'."""
        with self.engine.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE neuro.jobs SET state = 'succeeded', updated_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND state IN ('claimed', 'running')"
                ),
                {"job": job_id, "tok": claim_token},
            )
            if res.rowcount != 1:
                return False  # wrong token / not in-flight / already stolen-and-fenced
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET released_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND released_at IS NULL"
                ),
                {"job": job_id, "tok": claim_token},
            )
            self._unblock_ready_dependents(conn, job_id)
            return True

    @staticmethod
    def _unblock_ready_dependents(conn, job_id: int) -> None:
        """Flip this job's now-ready dependents 'blocked' -> 'queued' (the C2 AND-gate).

        **ADR-0046 §6 — the dependent-unblock WRITE-SKEW.** Two parents of one AND-gated child running
        `complete()` concurrently at READ COMMITTED each evaluated the NOT-EXISTS predicate against a
        snapshot excluding the other's uncommitted 'succeeded', so each SKIPPED the child — and the skip
        path touches no row (the qual is evaluated in the scan Filter below ModifyTable, so a row that
        fails it is never locked), giving no conflict and no EvalPlanQual recheck. The child was
        stranded 'blocked' forever; the only other blocked->queued writer is enqueue-time C20.
        ⚠ An isolation bump does NOT fix write-skew — ADR-0046 §6 says so in its own text.

        THE FIX IS TWO STATEMENTS, AND BOTH PROPERTIES ARE LOAD-BEARING:

        1. **The lock is taken on the CANDIDATE set, never the READY set.** The locking SELECT below
           carries NO readiness predicate on purpose. Move the NOT-EXISTS into it and the losing parent
           selects zero rows, locks NOTHING, and the write-skew survives with a `FOR NO KEY UPDATE`
           sitting right there looking like a fix.
        2. **The lock and the unblock must stay SEPARATE `conn.execute` calls.** Fusing them into one
           data-modifying CTE would put both under ONE snapshot, so the NOT-EXISTS would again be
           evaluated against a pre-commit view of the sibling parent and the skew would survive. (The
           shipped code was a single statement, so "collapse these two" reads as a strict improvement —
           which is exactly why it is written down here and mutation-pinned.)

        Serialisation: whichever parent locks the child first necessarily commits first (the blocked
        contender cannot commit before its blocker), so the LAST holder of the child lock runs the
        UPDATE under a fresh per-statement snapshot that includes every earlier parent's commit plus
        its own write. `ORDER BY j.job_id` keeps acquisition ascending, consistent with every other
        jobs-row acquirer in this file.

        ⚠ The UPDATE deliberately keeps its OWN dep-subquery predicate rather than being narrowed to
        the locked id set: a child that materialises between the two statements must still be unblocked,
        and narrowing would make this fix silently depend on `_initial_state_for_deps`'s lock to be
        correct. Locking is this statement's job; deciding readiness is the UPDATE's."""
        conn.execute(
            text(
                "SELECT j.job_id FROM neuro.jobs j "
                " WHERE j.state = 'blocked' "
                "   AND j.job_id IN (SELECT job_id FROM neuro.job_dependencies WHERE depends_on = :job) "
                " ORDER BY j.job_id FOR NO KEY UPDATE"
            ),
            {"job": job_id},
        )
        conn.execute(
            text(
                """
                UPDATE neuro.jobs j SET state = 'queued', updated_at = now()
                 WHERE j.state = 'blocked'
                   AND j.job_id IN (SELECT job_id FROM neuro.job_dependencies WHERE depends_on = :job)
                   AND NOT EXISTS (
                       SELECT 1 FROM neuro.job_dependencies d
                         JOIN neuro.jobs dep ON dep.job_id = d.depends_on
                        WHERE d.job_id = j.job_id AND dep.state <> 'succeeded')
                """
            ),
            {"job": job_id},
        )

    def fail_permanent(self, *, job_id: int, claim_token: uuid.UUID, detail: str) -> bool:
        """Dead-letter a permanent failure (CAS + ownership), then cascade-cancel its dependents.

        DEFER (Stage 2): there is no TRANSIENT-`failed` producer yet. When the Stage-2 failure path lands,
        define a transient failure's effect on dependents (retry vs cascade) and have the reaper stamp
        error_class='lease_expired' on expiry. Note only — not built now."""
        with self.engine.begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE neuro.jobs SET state = 'dead_letter', error_class = 'permanent', "
                    "error_detail = :d, attempt_count = attempt_count + 1, updated_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND state IN ('claimed', 'running')"
                ),
                {"job": job_id, "tok": claim_token, "d": detail},
            )
            if res.rowcount != 1:
                return False
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET released_at = now() "
                    "WHERE job_id = :job AND claim_token = :tok AND released_at IS NULL"
                ),
                {"job": job_id, "tok": claim_token},
            )
            self._cascade_cancel(conn, job_id)
            return True

    def cancel_cascade(self, job_id: int) -> int:
        with self.engine.begin() as conn:
            # ADR-0046 (wedge 3's SIBLING vector): take the SEED's row lock in its OWN statement first.
            # `_cascade_cancel` is a single statement whose recursive `deps` CTE is materialised under
            # ONE snapshot, and EvalPlanQual re-checks only the TARGET row's qual — never the CTE — so a
            # dependent enqueued concurrently is absent from `deps` and is stranded 'blocked' against a
            # 'cancelled' parent FOREVER (no blocked->queued writer can ever reach it: complete() needs
            # 'succeeded', and enqueue-time C20 only runs for a brand-new job). The seed lock makes the
            # concurrent enqueue commit FIRST, so the cascade statement's snapshot sees the new edge.
            # ⚑ `fail_permanent` needs no such lock for the SAME vector: its cascade is already a SECOND
            # statement after the dead-letter UPDATE, so it takes a fresh snapshot for free.
            # ⚠ SCOPE, STATED NOT GLOSSED: this closes the vector where the concurrent enqueue names the
            # SEED. An enqueue naming an INTERMEDIATE node of the cascade set locks that node instead, and
            # the `deps` CTE is still materialised under a snapshot fixed before LockRows blocks — so a
            # depth>=2 dependent can still be stranded. That hole is PRE-EXISTING (nothing serialised any
            # of this before) and is strictly NARROWED, not introduced, here; closing it needs the whole
            # cascade set locked before the CTE is computed, which is a REGISTERED FOLLOW-ON, not this
            # unit. The same residual applies to `fail_permanent`.
            conn.execute(
                text("SELECT job_id FROM neuro.jobs WHERE job_id = :j FOR NO KEY UPDATE"),
                {"j": job_id},
            )
            return self._cascade_cancel(conn, job_id, include_self=True)

    @staticmethod
    def _cascade_cancel(conn, job_id: int, *, include_self: bool = False) -> int:
        # Recursive cascade over the dependency edge set: cancel every transitive dependent (and,
        # if include_self, the job itself) that is not already in a terminal state.
        # CAST(:job AS bigint), not :job::bigint — SQLAlchemy text() mis-parses a named param adjacent
        # to the :: cast operator (the param goes unbound). CAST(...) keeps the colon unambiguous.
        seed = (
            "CAST(:job AS bigint)"
            if include_self
            else ("job_id FROM neuro.job_dependencies WHERE depends_on = :job")
        )
        # jobs FIRST (consistent jobs-before-leases lock order, R5).
        deps_cte = (
            "WITH RECURSIVE deps AS ("
            + (f"  SELECT {seed} AS job_id" if include_self else f"  SELECT {seed}")
            + "  UNION "
            "  SELECT d.job_id FROM neuro.job_dependencies d JOIN deps ON d.depends_on = deps.job_id"
            ") "
        )
        # ADR-0046: LOCK the target jobs rows in ASCENDING job_id order BEFORE updating them. An UPDATE
        # cannot carry an ORDER BY, so the shipped single statement acquired its row locks in PLAN order
        # (heap order under the measured Seq Scan) — the ONE jobs-row acquirer in this file with no
        # deterministic order. Harmless while nothing else locked jobs rows; once `_initial_state_for_deps`
        # takes an ascending lock (wedge 3) that un-ordered acquisition closes a real jobs<->jobs cycle,
        # REPRODUCED as a PostgreSQL `DeadlockDetected` in the pre-build vet against a live postgres:18,
        # with a clean pre-fix control on the identical schedule. The pre-lock makes EVERY jobs-row
        # acquirer here ascending, so no cycle can form.
        locked = (
            conn.execute(
                text(
                    deps_cte + "SELECT j.job_id FROM neuro.jobs j "
                    "WHERE j.job_id IN (SELECT job_id FROM deps) "
                    "  AND j.state NOT IN ('succeeded', 'cancelled', 'dead_letter') "
                    "ORDER BY j.job_id FOR NO KEY UPDATE"
                ),
                {"job": job_id},
            )
            .scalars()
            .all()
        )
        if not locked:
            return 0
        # The state predicate is re-asserted as a belt: the rows are already locked, so it can only be a
        # no-op here — but a locked-set UPDATE with no qual would silently widen if the lock ever moved.
        sql = (
            "UPDATE neuro.jobs SET state = 'cancelled', updated_at = now() "
            "WHERE job_id = ANY(:ids) "
            "  AND state NOT IN ('succeeded', 'cancelled', 'dead_letter') "
            "RETURNING job_id"
        )
        cancelled = [r[0] for r in conn.execute(text(sql), {"ids": locked}).all()]
        # R4: release any active lease the cancelled (possibly in-flight) jobs held — otherwise the
        # cancelled worker heartbeats forever and the lease never frees. Leases AFTER jobs.
        if cancelled:
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET released_at = now() "
                    "WHERE job_id = ANY(:ids) AND released_at IS NULL"
                ),
                {"ids": cancelled},
            )
        return len(cancelled)

    # --- reaper: requeue expired leases, fencing the original claimant --------------------------
    def reap_expired(self) -> int:
        """Requeue jobs whose lease expired. Clearing claim_token + bumping claim_seq fences the
        original (now-dead) claimant: its stale token no longer matches, so its complete() CAS fails.

        FIX #6 (b) IS SUPERSEDED BY THE ROW LOCK BELOW — recorded because it is the derivation, not because
        it still describes this method. It made the requeue and the lease-release ONE atomic statement (a
        data-modifying CTE chain); the shipped body is now FOUR statements over one locked set, which gives
        the same guarantee structurally instead of relying on statement atomicity.

        BLOCK-2 finding (2026-06-28), CORRECTED by Panel #2 (2026-06-29): #6a (the heartbeat
        `expires_at >= now()` guard) is the LOAD-BEARING fix and closes the ORIGINAL vector — a dead worker's
        heartbeat renewing an ALREADY-EXPIRED lease mid-reap, which #6a unconditionally refuses. A hand-driven
        heartbeat-at-the-midpoint of the two-statement reaper does NOT wedge with #6a present and DOES wedge
        with #6a removed (verified). #6a REMAINS in `_renew_lease` and remains load-bearing.

        ★ THE RESIDUAL IS CLOSED (ADR-0046 wedge 1, this session) — the history is kept because it is the
        derivation. WAS: a heartbeat renewing a NOT-yet-expired lease at its OWN txn `now()` (so #6a ALLOWS
        it) could commit AFTER the reaper's LATER `now()` had read the same lease as expired. The two-CTE
        form's `requeued` CTE saw the lease expired at the statement snapshot and set the job `queued`,
        while the `released` CTE re-evaluated the now-renewed lease under EvalPlanQual as no-longer-expired
        and SKIPPED the release -> `queued` job + an active, unreleased lease -> the next `claim()` hits
        `work_leases_active_uq` -> an un-claimable wedge. Both CTE forms wedged identically, so #6b did not
        close it; it needed the row lock. Reachable only under a concurrent renewal LOOP, which is why the
        fix lands with B-4 (`workers/runtime.py`) and not before — the hammer had no concurrent target.

        THE FIX — ONE LOCKED READ DECIDES BOTH HALVES. Four statements in one transaction:
          1. lock the candidate JOBS rows (`ORDER BY j.job_id FOR NO KEY UPDATE`);
          2. lock the LEASE rows and RE-READ expiry UNDER that lock — `FOR NO KEY UPDATE` makes
             EvalPlanQual re-apply the WHERE to the NEWEST row version, so a lease renewed in the interim
             is EXCLUDED HERE rather than requeued now and skipped later;
          3. requeue exactly step 2's set;  4. release exactly step 2's set.
        Requeue-set == release-set == step 2's set, and both relations are locked before either write, so
        the two halves can never disagree and the wedge signature is unreachable.

        ⚠ STEP 1 IS NOT OPTIONAL. A lease-only lock would make the reaper leases-then-jobs against
        complete()/fail_permanent()/_cascade_cancel()'s jobs-then-leases — the plan's named "lock-order
        deadlock from a piecemeal lease-only FOR UPDATE" risk. `heartbeat` stays leases-only (its
        `UPDATE..FROM` locks only its target), so it cannot enter a cycle.

        ⚠ LOCK STRENGTH IS `NO KEY UPDATE`, NOT `UPDATE`. That is exactly what the shipped UPDATEs already
        took (none of the columns written here is a key column), so it is strength-preserving; `FOR UPDATE`
        would newly conflict with the implicit RI `FOR KEY SHARE` that `INSERT INTO neuro.job_dependencies`
        takes on these same jobs rows, opening a deadlock this method never had.

        ⚠ STEPS 3 AND 4 KEEP THEIR FULL PREDICATES. Under the lock they can only be no-ops — but they are
        what makes dropping step 2's lock OBSERVABLE (without them, a mutated step 2 still releases the
        renewed lease and the wedge never appears, so the fix's central lock would be unpinned).

        R5 (deadlock-free): jobs-before-leases lock order is preserved — steps 1+3 touch `jobs`, steps 2+4
        touch `work_leases`. complete() / fail_permanent() / _cascade_cancel() / enqueue lock jobs first
        too, and every jobs-row acquirer in this file is now ASCENDING by job_id: no lock cycle."""
        with self.engine.begin() as conn:
            candidates = (
                conn.execute(
                    text(
                        "SELECT j.job_id FROM neuro.jobs j "
                        " WHERE j.state IN ('claimed', 'running') AND EXISTS ("
                        "   SELECT 1 FROM neuro.work_leases l "
                        "    WHERE l.job_id = j.job_id AND l.released_at IS NULL AND l.expires_at < now()) "
                        " ORDER BY j.job_id FOR NO KEY UPDATE"
                    )
                )
                .scalars()
                .all()
            )
            if not candidates:
                return 0  # the common sweep: nothing expired, no further statements
            expired = (
                conn.execute(
                    text(
                        "SELECT l.job_id FROM neuro.work_leases l "
                        " WHERE l.job_id = ANY(:ids) AND l.released_at IS NULL AND l.expires_at < now() "
                        " ORDER BY l.job_id FOR NO KEY UPDATE"
                    ),
                    {"ids": list(candidates)},
                )
                .scalars()
                .all()
            )
            if not expired:
                return 0  # every candidate's lease was renewed under our nose — nothing to reap
            requeued = conn.execute(
                text(
                    "UPDATE neuro.jobs j SET state = 'queued', claim_token = NULL, claimed_by = NULL, "
                    "  claim_seq = claim_seq + 1, expiry_count = expiry_count + 1, updated_at = now() "
                    " WHERE j.job_id = ANY(:ids) AND j.state IN ('claimed', 'running')"
                ),
                {"ids": list(expired)},
            ).rowcount
            conn.execute(
                text(
                    "UPDATE neuro.work_leases l SET released_at = now() "
                    " WHERE l.job_id = ANY(:ids) AND l.released_at IS NULL AND l.expires_at < now()"
                ),
                {"ids": list(expired)},
            )
            return requeued

    def expire_lease_now(self, job_id: int) -> None:
        """Test hook: force a lease to be already-expired so the reaper picks it up deterministically
        (the golden harness drives the state machine without real-time waits)."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE neuro.work_leases SET expires_at = now() - make_interval(secs => 1) "
                    "WHERE job_id = :job AND released_at IS NULL"
                ),
                {"job": job_id},
            )

    # --- spend governance (A2-15: the ledger + rate cards + run plans + reconcile) ----------------
    def get_or_create_rate_card(
        self,
        *,
        backend_or_lane: str,
        unit: str,
        rate: str | int | float | Decimal,
        effective_from: _dt.datetime,
    ) -> int:
        """Register-first / idempotent rate card on the natural key UNIQUE(backend_or_lane, effective_from)
        (the sibling raise-on-drift idiom, FIX #8 family): re-seeding the SAME (backend_or_lane,
        effective_from) with a DIFFERENT unit/rate raises IdentityMismatchError — a rate card is an auditable
        price of record, never silently repriced under a stable natural key (reprice = a NEW effective_from).
        INPUT #2 storage default = azure-blob-storage / usd_per_gb / 0.0184; the PRODUCTION seed's
        effective_from = the A2-1 provisioning date is applied at A2-1 deploy time (this helper is the
        mechanism, unit-tested here). Registrar-role op (grants.sql)."""
        rate_s = str(rate)
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO neuro.rate_cards (backend_or_lane, unit, rate, effective_from) "
                    "VALUES (:b, :u, :r, :ef) "
                    "ON CONFLICT (backend_or_lane, effective_from) DO NOTHING RETURNING rate_card_id"
                ),
                {"b": backend_or_lane, "u": unit, "r": rate_s, "ef": effective_from},
            ).scalar_one_or_none()
            if inserted is not None:
                return inserted
            existing = (
                conn.execute(
                    text(
                        "SELECT rate_card_id, unit, rate FROM neuro.rate_cards "
                        "WHERE backend_or_lane = :b AND effective_from = :ef"
                    ),
                    {"b": backend_or_lane, "ef": effective_from},
                )
                .mappings()
                .one()
            )
            if existing["unit"] != unit or Decimal(str(existing["rate"])) != Decimal(rate_s):
                raise IdentityMismatchError(
                    f"rate_card ({backend_or_lane!r}, effective_from={effective_from.isoformat()}) already "
                    f"registered with unit={existing['unit']!r} rate={existing['rate']}, not unit={unit!r} "
                    f"rate={rate_s} — a rate card is an auditable price of record; reprice with a NEW "
                    "effective_from (register-first, raise-on-drift)."
                )
            return existing["rate_card_id"]

    def record_spend(
        self,
        *,
        run_id: int | None,
        rate_card_id: int,
        quantity: str | int | float | Decimal,
        amount: str | int | float | Decimal,
        is_standing: bool,
    ) -> int:
        """INSERT one spend_entries row — the ledger (A2-15). run_id is nullable (a STANDING infra charge,
        e.g. monthly storage, has no run); rate_card_id is a NOT NULL FK so a cost never lands without a
        priced basis, and a row is written ONLY when there IS spend (a free run writes none). is_standing
        marks a recurring standing charge (storage) vs a per-run charge. Writer-role op (grants.sql)."""
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.spend_entries (run_id, rate_card_id, quantity, amount, is_standing) "
                    "VALUES (:r, :rc, :q, :a, :s) RETURNING spend_entry_id"
                ),
                {"r": run_id, "rc": rate_card_id, "q": str(quantity), "a": str(amount), "s": is_standing},
            ).scalar_one()

    def record_run_plan(
        self,
        *,
        run_id: int | None,
        justification: str,
        est_usd: str | int | float | Decimal | None = None,
        est_su: str | int | float | Decimal | None = None,
        est_gpu_hours: str | int | float | Decimal | None = None,
    ) -> int:
        """INSERT a run_plans justification row (A2-15): a plan justifies a run's spend above the budget
        threshold and doubles as Jetstream2-renewal evidence. justification is NOT NULL; the est_* figures
        (GPU-hours / USD / SU) are optional estimates."""
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO neuro.run_plans (run_id, justification, est_usd, est_su, est_gpu_hours) "
                    "VALUES (:r, :j, :u, :su, :gh) RETURNING run_plan_id"
                ),
                {
                    "r": run_id,
                    "j": justification,
                    "u": None if est_usd is None else str(est_usd),
                    "su": None if est_su is None else str(est_su),
                    "gh": None if est_gpu_hours is None else str(est_gpu_hours),
                },
            ).scalar_one()

    def spend_report(self) -> SpendReport:
        """Per-run non-standing spend (run_id -> USD) + the standing total + the UNATTRIBUTED total
        (non-standing spend with NO run_id) + the grand total, from the ledger — the SU/$ accounting that
        doubles as Jetstream2-renewal evidence (A2-15). The three buckets PARTITION every ledger row
        (per_run = non-standing WITH run_id; standing = is_standing; unattributed = non-standing WITHOUT
        run_id), so no spend is hidden: sum(per_run) + standing + unattributed == total."""
        with self.engine.begin() as conn:
            per_run_rows = (
                conn.execute(
                    text(
                        "SELECT run_id, COALESCE(SUM(amount), 0) AS amt FROM neuro.spend_entries "
                        "WHERE is_standing = false AND run_id IS NOT NULL GROUP BY run_id ORDER BY run_id"
                    )
                )
                .mappings()
                .all()
            )
            standing = conn.execute(
                text("SELECT COALESCE(SUM(amount), 0) FROM neuro.spend_entries WHERE is_standing = true")
            ).scalar_one()
            unattributed = conn.execute(
                text(
                    "SELECT COALESCE(SUM(amount), 0) FROM neuro.spend_entries "
                    "WHERE is_standing = false AND run_id IS NULL"
                )
            ).scalar_one()
            total = conn.execute(
                text("SELECT COALESCE(SUM(amount), 0) FROM neuro.spend_entries")
            ).scalar_one()
        per_run = tuple((int(r["run_id"]), Decimal(str(r["amt"]))) for r in per_run_rows)
        return SpendReport(
            per_run=per_run,
            standing_usd=Decimal(str(standing)),
            unattributed_usd=Decimal(str(unattributed)),
            total_usd=Decimal(str(total)),
        )

    def reconcile_spend(
        self,
        *,
        billed_usd: str | int | float | Decimal,
        threshold_pct: str | int | float | Decimal = 20,
        backend_or_lane: str | None = None,
    ) -> ReconcileResult:
        """Reconcile the ACTUAL billed spend (passed IN — there is NO live Cost-Management query until A2-1
        provisioning) against the LEDGER-predicted total (SUM(spend_entries.amount), optionally restricted to
        one backend_or_lane's rate cards), flagging a BREACH when |divergence| exceeds threshold_pct (default
        20%, owner-adjustable — matches the pin's 20-30% headroom). R4(c). Signed divergence percent: a
        positive value = billed OVER predicted."""
        with self.engine.begin() as conn:
            if backend_or_lane is None:
                predicted_raw = conn.execute(
                    text("SELECT COALESCE(SUM(amount), 0) FROM neuro.spend_entries")
                ).scalar_one()
            else:
                predicted_raw = conn.execute(
                    text(
                        "SELECT COALESCE(SUM(se.amount), 0) FROM neuro.spend_entries se "
                        "JOIN neuro.rate_cards rc ON rc.rate_card_id = se.rate_card_id "
                        "WHERE rc.backend_or_lane = :b"
                    ),
                    {"b": backend_or_lane},
                ).scalar_one()
        predicted = Decimal(str(predicted_raw))
        billed = Decimal(str(billed_usd))
        threshold = Decimal(str(threshold_pct))
        if predicted == 0:
            # nothing predicted: any nonzero billing is an unbounded divergence -> a breach (>=100%).
            divergence = Decimal(0) if billed == 0 else Decimal(100)
        else:
            divergence = (billed - predicted) / predicted * 100
        return ReconcileResult(
            predicted_usd=predicted,
            billed_usd=billed,
            divergence_pct=divergence,
            threshold_pct=threshold,
            breach=abs(divergence) > threshold,
        )
