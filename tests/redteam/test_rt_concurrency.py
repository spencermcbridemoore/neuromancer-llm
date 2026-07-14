"""Red-team: the pervasive CONCURRENCY gap (cross-cutting finding 1) — the precise races.

Nothing but the single 2-thread queue claim was tested under TRUE concurrency. These probes land the real
races with deterministic interleavings (a gated backend, or two-connection hand-driven statements):

  * FIX #7 — seam concurrent divergent-bytes blob CLOBBER (content-addressed shard keys close it).
  * FIX #10 — GC select->delete TOCTOU (single-statement delete-time recheck closes it).
  * L4 — concurrent same-event_key capture writers RE-VERIFY (never blind-adopt).
  * L5 — concurrent first-registration of one identity -> exactly one row, both callers agree.
  * L8 (#3 ADR-accept) — record_divergence keep-first under contention (NOT last-wins).
  * L3 — resurrected claimant vs live successor (the dead worker is fenced).

`pg` (real Postgres); the seam clobber probe uses LocalFs (no Azurite needed) so it runs on the pg lane.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.gc import collect_unsealed
from neuromancer_llm.bundles.registrar import BundleRegistrar, SeamIntegrityError, SeamStateError
from neuromancer_llm.capture.events import INLINE_CAP, write_capture_event
from neuromancer_llm.db.identity import sha256_bytes
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.storage.backends import LocalFsBackend
from neuromancer_llm.workers.runtime import reap

pytestmark = pytest.mark.pg

_WAIT = 15.0  # generous per-step timeout so a wedge surfaces as a loud failure, never a hang


def _race_two(call):
    """Release two threads SIMULTANEOUSLY on a Barrier, each running `call`; return both results (an
    exception in either propagates out of `.result()`). The deterministic-interleave primitive for the
    same-identity / same-key first-write races."""
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait(timeout=_WAIT)
        return call()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(_run), ex.submit(_run)
        return [f1.result(timeout=30), f2.result(timeout=30)]


def _race_two_calls(call_a, call_b):
    """Like `_race_two`, but releases two DISTINCT calls simultaneously on one Barrier. The same-content
    spill race (FIX-A) needs two threads writing DIFFERENT event_keys, so they cannot run an identical
    call. An exception in either propagates out of `.result()`."""
    barrier = threading.Barrier(2)

    def _run(call):
        barrier.wait(timeout=_WAIT)
        return call()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(_run, call_a), ex.submit(_run, call_b)
        return [f1.result(timeout=30), f2.result(timeout=30)]


def _wait_until_blocked(engine, *, timeout=10.0) -> None:
    """Poll until a backend is BLOCKED on a lock while running a DELETE on bundles (the GC sweep). Used to
    sequence a concurrent seal into the precise SELECT->DELETE window — deterministic, no fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            n = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND query ILIKE '%delete%' AND query ILIKE '%bundles%'"
                )
            ).scalar_one()
        if n >= 1:
            return
        time.sleep(0.02)
    raise AssertionError("the GC DELETE never blocked on the bundle row lock within the timeout")


class _GatedBackend:
    """A LocalFs wrapper that gates the FIRST shard put so two concurrent registers interleave
    deterministically: both pass _assert_resume_consistent (manifest NULL) at the barrier, then the
    'loser' writes only AFTER the 'winner' has fully registered — reproducing the put-after-commit clobber
    window on coordinate-derived keys. Content-addressed keys (FIX #7) make the loser write elsewhere."""

    def __init__(self, root, *, role, barrier, winner_done):
        self._inner = LocalFsBackend(root)
        self.driver = self._inner.driver  # GO-D-cost C1: choke points classify by driver (missing = refused)
        self._role = role
        self._barrier = barrier
        self._winner_done = winner_done
        self._gated = False

    def put(self, key, data):
        if not self._gated:
            self._gated = True
            self._barrier.wait(timeout=_WAIT)  # both registers passed the resume-consistency read
            if self._role == "loser":
                assert self._winner_done.wait(timeout=_WAIT)  # write AFTER the winner committed
        return self._inner.put(key, data)

    def get(self, key):
        return self._inner.get(key)

    def exists(self, key):
        return self._inner.exists(key)

    def delete(self, key):
        self._inner.delete(key)


def test_rt_seam_concurrent_divergent_register_no_clobber(seeded, tmp_path):
    """FIX #7: two concurrent registers on the SAME bundle_uuid with DIVERGENT bytes both pass the
    (manifest-NULL) resume check; on coordinate-derived keys the loser's put clobbered the winner's blob
    AFTER it committed -> committed artifacts.sha256 != blob on disk (a torn write). Content-addressed shard
    keys make the loser write to a DIFFERENT key, so every committed artifact still matches its blob."""
    repo = seeded["repo"]
    engine = repo.engine
    run_id = seeded["run_id"]
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    barrier = threading.Barrier(2)
    winner_done = threading.Event()
    shard = "shard-0000.bin"
    ds, pp = "seam_race", "seam_race/p0"

    def _register(role, payload):
        backend = _GatedBackend(tmp_path, role=role, barrier=barrier, winner_done=winner_done)
        reg = BundleRegistrar(engine, backend, expected_lane="test")
        try:
            reg.register(
                run_id=run_id,
                backend_id=backend_id,
                dataset_name=ds,
                partition_path=pp,
                shards={shard: payload},
            )
            return None
        except (SeamIntegrityError, SeamStateError) as exc:
            return type(exc).__name__
        finally:
            if role == "winner":
                winner_done.set()

    with ThreadPoolExecutor(max_workers=2) as ex:
        fw = ex.submit(_register, "winner", b"WINNER-bytes-AAAA")
        fl = ex.submit(_register, "loser", b"LOSER-bytes-BBBBBB")
        outcomes = [fw.result(timeout=30), fl.result(timeout=30)]

    # exactly one register survived (the loser is fenced by the immutable sealed manifest)
    assert outcomes.count(None) == 1, f"expected exactly one winner, got {outcomes}"

    # the binding: EVERY committed artifact's blob on disk matches its committed sha256 (no torn write)
    inner = LocalFsBackend(tmp_path)
    with engine.connect() as conn:
        arts = conn.execute(
            text(
                "SELECT a.uri, a.sha256 FROM neuro.artifacts a JOIN neuro.bundles b ON b.bundle_id = a.bundle_id "
                "WHERE b.run_id = :r AND a.kind <> 'wire_payload'"
            ),
            {"r": run_id},
        ).all()
    assert arts, "the winner must have committed at least one artifact"
    for uri, sha in arts:
        assert sha256_bytes(inner.get(uri)) == bytes(sha), f"torn write: blob at {uri} != committed sha256"


# --- FIX #10: GC select->delete TOCTOU ------------------------------------------------------------
def test_rt_gc_vs_register_toctou(seeded, tmp_path):
    """FIX #10: collect_unsealed used a separate SELECT (ids WHERE unsealed+aged) then DELETE WHERE id=ANY
    with NO state recheck — a bundle SEALED between the SELECT and the DELETE was deleted by stale id. The
    single-statement DELETE re-checks state='unsealed' at delete time (EvalPlanQual), so a bundle that is no
    longer unsealed when the delete runs is never collected.

    Deterministic interleave: conn S locks the bundle row (UPDATE to 'sealed', uncommitted); collect_unsealed
    runs in a thread — its DELETE blocks on that lock; once it is blocking we commit S (bundle -> 'sealed')
    and the DELETE re-evaluates."""
    repo = seeded["repo"]
    engine = repo.engine
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    with engine.begin() as conn:
        bid = conn.execute(
            text(
                "INSERT INTO neuro.bundles (bundle_uuid, run_id, backend_id, state, created_at) "
                "VALUES (:u, :r, :be, 'unsealed', now() - interval '2 hours') RETURNING bundle_id"
            ),
            {"u": uuid.uuid4(), "r": seeded["run_id"], "be": backend_id},
        ).scalar_one()

    s_conn = engine.connect()
    s_tx = s_conn.begin()
    s_conn.execute(
        text("UPDATE neuro.bundles SET state = 'sealed', sealed_at = now() WHERE bundle_id = :b"), {"b": bid}
    )  # the row is now locked by S, NOT yet committed

    collected = {}

    def _gc():
        collected["n"] = collect_unsealed(engine, grace=timedelta(0))

    t = threading.Thread(target=_gc)
    t.start()
    try:
        _wait_until_blocked(engine)  # the GC DELETE is now waiting on S's row lock
        s_tx.commit()  # the bundle becomes 'sealed' under the GC's feet
        s_conn.close()
        t.join(timeout=30)
    finally:
        if t.is_alive():
            s_tx.rollback()
            s_conn.close()
            t.join(timeout=5)

    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM neuro.bundles WHERE bundle_id = :b"), {"b": bid}
        ).scalar_one_or_none()
    assert state == "sealed", "a bundle sealed during the sweep must NOT be collected (FIX #10 recheck)"


# --- L4: concurrent same-event_key capture writers RE-VERIFY (never blind-adopt) -------------------
def test_rt_concurrent_divergent_capture_loser_re_verifies(seeded, rt, tmp_path):
    """L4: the ON CONFLICT DO NOTHING -> re-SELECT -> _assert_capture_consistent race branch was entirely
    unexercised. Two concurrent writers of the SAME (run, event_key) with DIVERGENT bytes -> exactly ONE
    row, and the loser RE-VERIFIES and raises SeamIntegrityError (it never blind-adopts)."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    _t, mid = rt.seed_tok_model(repo, tokenizer_hash=b"race-tok")  # seeded run has NULL fp -> C10b allows
    barrier = threading.Barrier(2)

    def _write(payload):
        barrier.wait(timeout=_WAIT)
        try:
            write_capture_event(
                repo.engine,
                backend,
                run_id=seeded["run_id"],
                event_key="race-ev",
                model_id=mid,
                actor_id=seeded["actor_id"],
                origin="h",
                request_body=payload,
                response_body=b"{}",
                backend_id=backend_id,
                partition_path="logprobs/run=x/part-0000",
            )
            return "ok"
        except SeamIntegrityError:
            return "fenced"

    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(_write, b'{"who":"A"}')
        fb = ex.submit(_write, b'{"who":"BB"}')
        out = [fa.result(timeout=30), fb.result(timeout=30)]
    assert sorted(out) == ["fenced", "ok"], f"exactly one writer wins; the divergent loser is fenced: {out}"
    with repo.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM neuro.capture_events WHERE run_id = :r AND event_key = 'race-ev'"),
            {"r": seeded["run_id"]},
        ).scalar_one()
    assert rows == 1  # exactly one capture row (no double-insert, no blind adopt)


# --- L5: concurrent first-registration of one identity -> exactly one row, both agree --------------
# BLOCK-1: model_identities has TWO non-PK uniques (identity_hash AND model_components_uq), so a single
# 2-thread attempt catches the dual-unique race only ~⅓ of the time. LOOP it so one probe run exercises
# the race many times and the RED (an unhandled IntegrityError on the non-arbiter index) is deterministic.
_RACE_ROUNDS = 24


def test_rt_concurrent_first_registration_one_row(seeded, rt):
    """L5 (BLOCK-1): two writers registering the SAME identity concurrently land exactly ONE row and BOTH
    callers agree on the id (the bare ON CONFLICT DO NOTHING -> SELECT-existing fallback under contention).

    model_identities is the ONLY registry with two non-PK uniques (identity_hash + model_components_uq); an
    `ON CONFLICT (identity_hash)` arbiter let a clash Postgres detected on the NON-arbiter model_components_uq
    raise an unhandled IntegrityError. The bare ON CONFLICT (arbitrates on either index) closes it. Looped so
    the RED is deterministic, not a ~⅓-per-attempt coin flip."""
    repo = seeded["repo"]
    args = dict(
        hf_repo="r",
        hf_revision="rev",
        dtype_quant="bf16",
        tokenizer_hash=b"concurrent-tok",
        serving_stack="vllm",
        serving_version="0.23.0",
        arch_family="llama",
    )

    for round_idx in range(_RACE_ROUNDS):
        with (
            repo.engine.begin() as conn
        ):  # fresh slate each round so every round is a true first-registration
            conn.execute(
                text("TRUNCATE neuro.model_identities, neuro.tokenizer_identities RESTART IDENTITY CASCADE")
            )
        repo.register_tokenizer_identity(tokenizer_hash=b"concurrent-tok")  # register-first
        ids = _race_two(lambda: repo.register_model_identity(**args))
        assert ids[0] == ids[1], f"round {round_idx}: callers disagree on the model_id {ids}"
        with repo.engine.connect() as conn:
            n = conn.execute(text("SELECT count(*) FROM neuro.model_identities")).scalar_one()
        assert n == 1, f"round {round_idx}: expected exactly one model_identities row, got {n}"


# --- L11 (BLOCK-1b): concurrent first-creation of one run -> exactly one row, both agree ------------
def test_rt_concurrent_first_run_creation(seeded):
    """L11 (BLOCK-1b): get_or_create_run was SELECT-then-INSERT with NO ON CONFLICT, and runs has two non-PK
    uniques (run_key AND the partial runs_experiment_variant_uq). Two concurrent same-run_key creators both
    SELECT-missed then both INSERTed -> the loser raised an unhandled IntegrityError. The bare ON CONFLICT DO
    NOTHING -> re-SELECT dedupes to ONE row, both agree; and a DIFFERENT run_key claiming the SAME experiment
    variant is an identity violation that raises LOUD (never a silent adopt). Looped so the RED is
    deterministic (a single 2-thread attempt is a coin flip on which unique the conflict lands)."""
    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    common = dict(
        campaign_id=cid,
        work_slug="b1b",
        variant_digest="v1",
        actor_id=aid,
        origin="o",
        run_kind="experiment",
    )

    for round_idx in range(_RACE_ROUNDS):
        with repo.engine.begin() as conn:  # fresh slate so every round is a true first-creation race
            conn.execute(text("TRUNCATE neuro.runs RESTART IDENTITY CASCADE"))
        ids = _race_two(lambda: repo.get_or_create_run("c-test/b1b/v1", **common))
        assert ids[0] == ids[1], f"round {round_idx}: callers disagree on the run_id {ids}"
        with repo.engine.connect() as conn:
            n = conn.execute(text("SELECT count(*) FROM neuro.runs")).scalar_one()
        assert n == 1, f"round {round_idx}: expected exactly one runs row, got {n}"

    # a DIFFERENT run_key claiming the SAME (campaign, slug, digest, invocation=NULL) experiment variant is an
    # identity violation, not a dedupe -> raise (runs_experiment_variant_uq under two run_keys, fail-loud).
    with repo.engine.begin() as conn:
        conn.execute(text("TRUNCATE neuro.runs RESTART IDENTITY CASCADE"))
    repo.get_or_create_run("c-test/b1b/v1", **common)
    with pytest.raises(IdentityMismatchError):
        repo.get_or_create_run("c-test/b1b/v1-OTHER-KEY", **common)


# --- L8 (ADR-accept #3): record_divergence is keep-first, NOT last-wins ----------------------------
def test_rt_record_divergence_keep_first_not_last_wins(seeded, rt):
    """ADR-accept #3 (sharpened): a second record_divergence on the same (replicate_link, method_version)
    with DIVERGENT metrics keeps the FIRST values (keep-first), it does NOT overwrite them (last-wins). The
    existing idempotency test reused one kwargs dict and could not tell the two apart."""
    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    _t, mid = rt.seed_tok_model(repo, tokenizer_hash=b"kf-tok")
    fp = rt.fingerprint(repo, config_text='{"x":1}', model_id=mid)
    r1 = repo.get_or_create_run(
        "c-test/kf/v1",
        campaign_id=cid,
        work_slug="kf",
        variant_digest="v1",
        actor_id=aid,
        origin="o",
        fingerprint_id=fp,
    )
    r2 = repo.get_or_create_run(
        "c-test/kf/v2",
        campaign_id=cid,
        work_slug="kf",
        variant_digest="v2",
        actor_id=aid,
        origin="o",
        fingerprint_id=fp,
    )
    link = repo.link_replicate(original_run_id=r1, replicate_run_id=r2)
    mv = repo.register_method_version(
        method_key="logprob_divergence", semver="1.0.0", code_sha=b"\x01" * 32, set_active=True
    )
    first = repo.record_divergence(
        replicate_link_id=link,
        method_version_id=mv,
        max_abs_diff=0.0,
        max_rel_diff=0.0,
        argmax_flip_rate=0.0,
        answer_letter_flip_rate=None,
        near_tie_margin_nats=0.1,
    )
    second = repo.record_divergence(
        replicate_link_id=link,
        method_version_id=mv,
        max_abs_diff=9.9,
        max_rel_diff=9.9,
        argmax_flip_rate=1.0,
        answer_letter_flip_rate=0.5,
        near_tie_margin_nats=0.0,
    )
    assert second == first  # idempotent on the UNIQUE
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT max_abs_diff, argmax_flip_rate FROM neuro.divergence_measurements WHERE divergence_id = :d"
                ),
                {"d": first},
            )
            .mappings()
            .one()
        )
    assert row["max_abs_diff"] == 0.0 and row["argmax_flip_rate"] == 0.0  # the FIRST values persist


# --- L3: resurrected claimant vs live successor ----------------------------------------------------
def test_rt_resurrected_claimant_fenced_no_double_lease(seeded):
    """L3: after worker A's lease expires and the reaper requeues the job, worker B claims it (a fresh
    token). A (resurrected, stale token) is FENCED — its complete() returns False — and there is never a
    second active lease; B is the sole live owner."""
    repo, run_id, wA = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    wB = repo.create_actor("worker:B", kind="scheduled_worker")
    job = repo.enqueue(run_id=run_id, job_key="k#res", job_role="capture")
    cA = repo.claim(actor_id=wA)
    assert cA is not None
    repo.expire_lease_now(job)
    assert reap(repo) == 1
    cB = repo.claim(actor_id=wB)
    assert cB is not None and cB.job_id == job and cB.claim_token != cA.claim_token
    assert repo.complete(job_id=job, claim_token=cA.claim_token) is False  # A fenced
    assert repo.heartbeat(job_id=job, claim_token=cB.claim_token) is True  # B is the live owner
    with repo.engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM neuro.work_leases WHERE job_id = :j AND released_at IS NULL"),
            {"j": job},
        ).scalar_one()
    assert active == 1  # exactly one active lease (no double-occupancy)
    assert repo.complete(job_id=job, claim_token=cB.claim_token) is True


# --- FIX-A: concurrent same-content wire spill from two DIFFERENT event_keys dedupes to ONE artifact ---
def test_rt_spill_concurrent_same_content_one_artifact(seeded, rt, tmp_path):
    """FIX-A (Panel #2 — codex/gpt/opus): FIX #1 made the wire-spill key a FUNCTION of the content sha256
    (`{prefix}/{sha256}.json`), so two CONCURRENT `write_capture_event` calls on the SAME run with DIFFERENT
    `event_key`s but IDENTICAL >8 KB bodies now spill to the SAME artifact key. On `f2758d2` `_spill` does a
    bare SELECT-then-INSERT with no `ON CONFLICT`: both racers SELECT-miss then both INSERT INTO
    neuro.artifacts -> the loser raises `IntegrityError` on the `(backend_id, uri)` unique (the SAME
    SELECT-then-INSERT class as BLOCK-1/1b, newly reachable via content-addressing). The targeted
    `ON CONFLICT (backend_id, uri) DO NOTHING` -> re-SELECT-and-verify dedupes them: both captures succeed,
    EXACTLY ONE `wire_payload` artifact exists at the content key, and both capture rows reference it.

    Looped >=20 rounds (truncating capture_events + artifacts between) so the RED is deterministic — a single
    2-thread attempt under-catches the SELECT-miss overlap (DIAG 1 reproduced it 40/40, but the loop pins it).
    Uses the NULL-fingerprint (adhoc) seeded run so C10b allows the shared model_id."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    _t, mid = rt.seed_tok_model(repo, tokenizer_hash=b"spill-race-tok")  # seeded run NULL fp -> C10b allows
    run_id = seeded["run_id"]
    big = b'{"prompt":"' + b"Z" * (INLINE_CAP + 1024) + b'"}'  # identical >8 KB body -> both threads spill it

    def _write(ek):
        return write_capture_event(
            repo.engine,
            backend,
            run_id=run_id,
            event_key=ek,
            model_id=mid,
            actor_id=seeded["actor_id"],
            origin="h",
            request_body=big,
            response_body=b"{}",
            backend_id=backend_id,
            partition_path="logprobs/run=x/part-0000",
        )

    for round_idx in range(_RACE_ROUNDS):
        with repo.engine.begin() as conn:  # fresh slate so every round is a true first-spill race
            conn.execute(text("TRUNCATE neuro.capture_events, neuro.artifacts RESTART IDENTITY CASCADE"))
        # both DIFFERENT event_keys -> two capture rows; the ONLY contention is the shared content artifact.
        # On f2758d2 the loser's bare INSERT raises IntegrityError, which propagates out of `.result()` (RED).
        ra, rb = _race_two_calls(lambda: _write("ev-A"), lambda: _write("ev-B"))
        assert ra.request_spill_artifact_id is not None, f"round {round_idx}: request must have spilled"
        assert ra.request_spill_artifact_id == rb.request_spill_artifact_id, (
            f"round {round_idx}: same-content concurrent spills must dedupe to ONE artifact id "
            f"({ra.request_spill_artifact_id} != {rb.request_spill_artifact_id})"
        )
        with repo.engine.connect() as conn:
            n_art = conn.execute(
                text("SELECT count(*) FROM neuro.artifacts WHERE kind = 'wire_payload'")
            ).scalar_one()
            n_ref = conn.execute(
                text(
                    "SELECT count(*) FROM neuro.capture_events "
                    "WHERE run_id = :r AND request_spill_artifact_id = :a"
                ),
                {"r": run_id, "a": ra.request_spill_artifact_id},
            ).scalar_one()
        assert n_art == 1, f"round {round_idx}: expected exactly ONE wire_payload artifact, got {n_art}"
        assert n_ref == 2, (
            f"round {round_idx}: both capture rows must reference the one artifact, got {n_ref}"
        )
