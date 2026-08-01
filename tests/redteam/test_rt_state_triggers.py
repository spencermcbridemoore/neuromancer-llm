"""Red-team: the migration-0003 in-schema state guards (ADR-0046 P-4 / P-5).

P-5's obligation is that an out-of-ladder write is rejected BY THE SCHEMA, not by Python and not by a
missing grant. Every NEGATIVE probe here therefore drives RAW SQL on the session engine (the container's
owning role, which holds every privilege) so the ONLY thing that can reject it is the trigger — and
asserts on the trigger's own message, so a rejection coming from somewhere else cannot masquerade as
coverage. The POSITIVES drive either raw SQL or the production registrar and assert the write LANDS.

Organised per illegal-edge CLASS, with the POSITIVE ladder beside each negative: a guard pinned only by
its refusals cannot tell "the edge is forbidden" from "the trigger rejects everything" (precedent 20 —
mutation-verify the criterion, not just the module).

⚠ THE RESIDUAL IS PINNED AS A POSITIVE, DELIBERATELY (test_rt_ladder_is_walkable_statement_by_statement).
0003 forbids any SINGLE update that skips a rung; it does NOT stop a writer WALKING the rungs. That is
not a defect to fix here — every column a stronger predicate could test is itself granted to the writer,
so it is forgeable in the same transaction, and rejecting the app's own claim()/complete() pair would
violate P-4's no-stricter arm. The closure is the worker/registrar role split. The probe exists so the
limitation is a measured, red-able fact rather than something a later reader mis-remembers as closed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.registrar import BundleRegistrar
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg

SHARDS = {"shard-0000.bin": b"first shard payload", "shard-0001.bin": b"second shard payload"}


def _job(conn, run_id: int, *, key: str, state: str) -> int:
    """Seed a job DIRECTLY at `state`. jobs INSERT carries no trigger (INSERT on jobs is registrar-only,
    so it is not a writer-bypass surface), which is what lets a probe stage a source state the ladder
    itself can never reach — 'failed' has no producer in shipped code but IS a legal cascade source."""
    return conn.execute(
        text(
            "INSERT INTO neuro.jobs (job_key, run_id, job_role, queue, state) "
            "VALUES (:k, :r, 'p', 'default', :st) RETURNING job_id"
        ),
        {"k": key, "r": run_id, "st": state},
    ).scalar_one()


def _set_state(conn, job_id: int, state: str) -> None:
    conn.execute(text("UPDATE neuro.jobs SET state = :st WHERE job_id = :j"), {"st": state, "j": job_id})


def _backend_id(repo, tmp_path) -> int:
    return repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )


def _bundle(conn, run_id: int, backend_id: int, *, state: str = "unsealed") -> int:
    return conn.execute(
        text(
            "INSERT INTO neuro.bundles (bundle_uuid, run_id, backend_id, state) "
            "VALUES (gen_random_uuid(), :r, :b, :st) RETURNING bundle_id"
        ),
        {"r": run_id, "b": backend_id, "st": state},
    ).scalar_one()


# --- CLASS 1: jobs, the forward-skip (the "mark work done without doing it" single-statement edge) ----
def test_rt_job_forward_skip_queued_to_succeeded_rejected(seeded):
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#skip", state="queued")
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _set_state(conn, j, "succeeded")
    assert "illegal job_state transition" in str(exc.value).lower()
    assert "queued -> succeeded" in str(exc.value)


def test_rt_job_queued_to_running_rejected(seeded):
    """Skipping `claimed` is refused too — the ladder is the whole predicate, not just its endpoints."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#qr", state="queued")
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _set_state(conn, j, "running")
    assert "queued -> running" in str(exc.value)


# --- CLASS 2: jobs, the terminal escape (anti-resurrection; the property that IS multi-hop-proof) ------
@pytest.mark.parametrize("terminal", ["succeeded", "cancelled", "dead_letter"])
@pytest.mark.parametrize("target", ["queued", "claimed", "running"])
def test_rt_job_terminal_states_are_absorbing(seeded, terminal, target):
    """succeeded / cancelled / dead_letter have ZERO out-edges. Unlike the rung-skip guards this one
    cannot be walked around: no legal path leaves a terminal state, so a writer can never un-succeed,
    un-cancel or un-dead-letter a job."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key=f"k#term-{terminal}-{target}", state=terminal)
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _set_state(conn, j, target)
    assert f"{terminal} -> {target}" in str(exc.value)


# --- CLASS 3: jobs, targets with no UPDATE writer / no producer at all --------------------------------
def test_rt_job_update_into_blocked_rejected(seeded):
    """`blocked` is an INSERT-only state: enqueue computes it from the deps, and NO shipped statement
    ever UPDATEs a job INTO it. Re-blocking a queued job would strand it behind the AND-gate."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#reblock", state="queued")
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _set_state(conn, j, "blocked")
    assert "queued -> blocked" in str(exc.value)


def test_rt_job_update_into_failed_rejected(seeded):
    """`failed` (transient) has NO producer in shipped code — fail_permanent writes 'dead_letter', and its
    own docstring defers the transient path to Stage 2. Fail-closed per the D1 no-default-clean idiom: the
    unit that builds the transient-failure path extends this ladder in its own migration."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#failed", state="claimed")
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _set_state(conn, j, "failed")
    assert "claimed -> failed" in str(exc.value)


# --- CLASS 4: jobs POSITIVES — the ladder the app actually walks -------------------------------------
def test_rt_job_legal_ladder_passes(seeded):
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#ladder", state="queued")
        for nxt in ("claimed", "running", "succeeded"):
            _set_state(conn, j, nxt)
        assert (
            conn.execute(text("SELECT state FROM neuro.jobs WHERE job_id = :j"), {"j": j}).scalar_one()
            == "succeeded"
        )


def test_rt_job_running_self_edge_passes(seeded):
    """checkpoint() writes state='running' WHERE state IN ('claimed','running'), so running->running is a
    REAL app self-edge. `BEFORE UPDATE OF state` fires on a same-value write (measured), so this must be
    an explicit member of the legal set — it is not made safe by the column-scoping."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#self", state="claimed")
        _set_state(conn, j, "running")
        _set_state(conn, j, "running")


def test_rt_job_failed_is_a_legal_cancel_source(seeded):
    """_cascade_cancel's shipped predicate is `state NOT IN ('succeeded','cancelled','dead_letter')`, which
    ADMITS 'failed'. Excluding it would be STRICTER than the app even though nothing produces it today."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#failcancel", state="failed")
        _set_state(conn, j, "cancelled")


def test_rt_job_null_state_is_rejected_by_the_guard(seeded):
    """The guard must be the thing that refuses, not the column's NOT NULL. With `IF NOT (...)` a NULL
    collapses the OR-chain to NULL and the THEN branch is NOT taken (measured), so the write slipped past
    the guard and died on the constraint with an error naming no transition — and, worse, the SAME
    statement raised correctly when OLD.state happened to be terminal. `IS NOT TRUE` makes it consistent."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#null", state="running")
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.jobs SET state = NULL WHERE job_id = :j"), {"j": j})
    assert "illegal job_state transition" in str(exc.value).lower()


# --- CLASS 5: the DISCLOSED RESIDUAL — a POSITIVE that pins what 0003 does NOT do ---------------------
def test_rt_ladder_is_walkable_statement_by_statement(seeded):
    """★ THE HONEST-SCOPE PIN. queued -> claimed -> succeeded is TWO individually-legal hops, so a role
    holding GRANT UPDATE (state, ...) marks a job succeeded with no claim_token, no lease and no work.
    0003 does NOT close that; the role split does. This probe asserts the residual EXISTS so the guarantee
    can never be silently over-claimed — if a future unit really closes it, this test reddens and its
    author is forced to update the claim rather than quietly inherit a stale one.

    ★ STILL TRUE AFTER ADR-0046 SESSION C3, AND DELIBERATELY SO — the probe is unchanged, its SCOPE is now
    stated. C3 revoked `UPDATE (state, ...) ON jobs` from `neuro_writer`, so no untrusted worker can walk
    this ladder any more. It runs on `repo.engine`, i.e. as ADMIN, which holds the grant **by nature** and
    always will: an admin can rewrite any row in the schema, and pretending otherwise would be the
    over-claim this pin exists to prevent. So the residual it names is now ROLE-SCOPED — walkable as admin,
    closed for the writer — and that is the strongest true statement available.
    The writer-side half is `tests/redteam/test_rt_role_split.py`, which proves BOTH that the raw ladder is
    permission-denied AND that a narrower residual survives there (claim_job + complete_job still reaches
    'succeeded' with no work, attributably)."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#walk", state="queued")
        _set_state(conn, j, "claimed")
        _set_state(conn, j, "succeeded")
        row = (
            conn.execute(
                text("SELECT state, claim_token, claim_seq FROM neuro.jobs WHERE job_id = :j"), {"j": j}
            )
            .mappings()
            .one()
        )
        lease = conn.execute(
            text("SELECT count(*) FROM neuro.work_leases WHERE job_id = :j"), {"j": j}
        ).scalar_one()
    assert row["state"] == "succeeded" and row["claim_token"] is None and lease == 0


# --- CLASS 6: bundles, the backward / reserved / terminal edges ---------------------------------------
def test_rt_bundle_backward_registered_to_sealed_rejected(seeded, tmp_path):
    """L9 seam-state immutability: a registered bundle can never be re-opened for registration."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, _backend_id(repo, tmp_path))
        conn.execute(text("UPDATE neuro.bundles SET state='sealed' WHERE bundle_id=:b"), {"b": b})
        conn.execute(text("UPDATE neuro.bundles SET state='registered' WHERE bundle_id=:b"), {"b": b})
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state='sealed' WHERE bundle_id=:b"), {"b": b})
    assert "registered -> sealed" in str(exc.value)


@pytest.mark.parametrize("reserved", ["staged", "verified"])
def test_rt_bundle_reserved_members_refuse_entry(seeded, tmp_path, reserved):
    """ADR-0018 reserves the VALUES and defines no EDGES; no code path writes or reads them (gc.py names
    them in prose only). Blessing an invented relay lifecycle in-schema would be a fabricated graph, so
    entry is fail-closed — re-arming the relay extends the ladder in its own migration."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, _backend_id(repo, tmp_path))
        conn.execute(text("UPDATE neuro.bundles SET state='sealed' WHERE bundle_id=:b"), {"b": b})
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state=:s WHERE bundle_id=:b"), {"s": reserved, "b": b})
    assert f"sealed -> {reserved}" in str(exc.value)


def test_rt_bundle_sealed_to_tombstoned_rejected(seeded, tmp_path):
    """tombstoned is reachable ONLY from registered. A sealed bundle is exactly the one a resumed worker
    RE-REGISTERS (ADR-0010's GC exemption is what makes W6/W7 recoverable), and tombstoned is terminal —
    so this edge would be a one-statement, schema-blessed way to permanently strand an in-flight bundle.
    It also has no producer: the only tombstone write anywhere in the repo is registered -> tombstoned."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, _backend_id(repo, tmp_path))
        conn.execute(text("UPDATE neuro.bundles SET state='sealed' WHERE bundle_id=:b"), {"b": b})
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state='tombstoned' WHERE bundle_id=:b"), {"b": b})
    assert "sealed -> tombstoned" in str(exc.value)


def test_rt_bundle_tombstoned_is_absorbing(seeded, tmp_path):
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, _backend_id(repo, tmp_path))
        for s in ("sealed", "registered", "tombstoned"):
            conn.execute(text("UPDATE neuro.bundles SET state=:s WHERE bundle_id=:b"), {"s": s, "b": b})
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state='registered' WHERE bundle_id=:b"), {"b": b})
    assert "tombstoned -> registered" in str(exc.value)


def test_rt_bundle_legal_ladder_passes(seeded, tmp_path):
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, _backend_id(repo, tmp_path))
        for s in ("sealed", "registered", "tombstoned"):
            conn.execute(text("UPDATE neuro.bundles SET state=:s WHERE bundle_id=:b"), {"s": s, "b": b})
        assert (
            conn.execute(text("SELECT state FROM neuro.bundles WHERE bundle_id=:b"), {"b": b}).scalar_one()
            == "tombstoned"
        )


# --- CLASS 7: bundles INSERT — the route an UPDATE-only ladder leaves wide open -----------------------
@pytest.mark.parametrize("state", ["sealed", "registered", "tombstoned", "staged", "verified"])
def test_rt_bundle_insert_at_a_non_birth_state_rejected(seeded, tmp_path, state):
    """neuro_writer holds FULL-ROW INSERT on bundles, so an UPDATE-only ladder is walkable around: a
    direct INSERT at 'registered' produces a row with manifest_sha256 NULL and zero artifacts that is
    permanently GC-exempt (COLLECTIBLE_STATES is ('unsealed',)), unreclaimable in-role (no DELETE grant),
    and that WEDGES its (run, dataset, partition) out of registration forever — _ensure_bundle's
    ON CONFLICT adopts the forged row and _seal then matches zero rows and raises. Measured live before
    the arm existed: all five of these INSERTs succeeded."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    bid = _backend_id(repo, tmp_path)
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _bundle(conn, run_id, bid, state=state)
    assert "illegal bundle_state at INSERT" in str(exc.value)


def test_rt_bundle_insert_at_unsealed_passes(seeded, tmp_path):
    """The app's only INSERT value. Without this positive the INSERT arm is indistinguishable from one
    that rejects every insert."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        assert _bundle(conn, run_id, _backend_id(repo, tmp_path)) > 0


def test_rt_registrar_resume_survives_the_bundle_triggers(seeded, tmp_path):
    """★ The column-scoping pin. `_ensure_bundle` runs `INSERT ... ON CONFLICT (bundle_uuid) DO UPDATE SET
    run_id = ...` on EVERY register call, including an idempotent resume of an already-'registered'
    bundle. `BEFORE UPDATE OF state` does not fire there (the SET list is {run_id}); a bare BEFORE UPDATE
    would see registered->registered and reject, breaking the W6/W7 crash-recovery path in production.
    Widening the INSERT arm to BEFORE INSERT OR UPDATE breaks the same path. This asserts the resume."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    kw = {
        "run_id": seeded["run_id"],
        "backend_id": _backend_id(repo, tmp_path),
        "dataset_name": "trig_rt",
        "partition_path": "trig_rt/p0",
        "shards": SHARDS,
    }
    first = reg.register(**kw)
    assert reg.bundle_state(first) == "registered"
    assert reg.register(**kw) == first  # the idempotent resume must still land


# --- CLASS 8: the capture_events.model_id bind (C10b), INSERT-side -----------------------------------
def _capture(conn, run_id: int, model_id: int, actor_id: int, key: str) -> None:
    conn.execute(
        text(
            "INSERT INTO neuro.capture_events (run_id, event_key, model_id, actor_id, origin, request_text) "
            "VALUES (:r, :k, :m, :a, 'trig-rt', 'x')"
        ),
        {"r": run_id, "k": key, "m": model_id, "a": actor_id},
    )


def test_rt_capture_model_id_contradicting_the_run_fingerprint_rejected(seeded, rt):
    repo, run_id, actor_id = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    _, mid_a = rt.seed_tok_model(repo, tokenizer_hash=b"trig-a", hf_repo="ra")
    _, mid_b = rt.seed_tok_model(repo, tokenizer_hash=b"trig-b", hf_repo="rb")
    assert mid_a != mid_b
    fp = rt.fingerprint(repo, config_text="cfg-trig", model_id=mid_a)
    with repo.engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.runs SET fingerprint_id = :f WHERE run_id = :r"), {"f": fp, "r": run_id}
        )
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _capture(conn, run_id, mid_b, actor_id, "ev#bad")
    assert "contradicts run" in str(exc.value)


def test_rt_capture_model_id_matching_the_run_fingerprint_passes(seeded, rt):
    repo, run_id, actor_id = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    _, mid = rt.seed_tok_model(repo, tokenizer_hash=b"trig-ok", hf_repo="rok")
    fp = rt.fingerprint(repo, config_text="cfg-ok", model_id=mid)
    with repo.engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.runs SET fingerprint_id = :f WHERE run_id = :r"), {"f": fp, "r": run_id}
        )
        _capture(conn, run_id, mid, actor_id, "ev#ok")


def test_rt_capture_on_an_adhoc_run_accepts_any_model_the_designed_carve_out(seeded, rt):
    """★ A DESIGNED CARVE-OUT (ADR-0036), NOT coverage of the bind. `runs.fingerprint_id` is NULL for an
    adhoc run, the join yields no row, `bound_model` stays NULL and ANY model_id is accepted — exactly
    what the shipped Python check does. The multi-model-adhoc consequence is the separately registered
    'adhoc auto-mint + fingerprint-adoption' obligation, and it is IRREVERSIBLE once adopted, because
    runs_fingerprint_assign_once then refuses any correction of fingerprint_id. Nobody may cite this file
    as 'the capture bind is enforced in-schema' without that asterisk."""
    repo, run_id, actor_id = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    _, mid_a = rt.seed_tok_model(repo, tokenizer_hash=b"trig-h1", hf_repo="rh1")
    _, mid_b = rt.seed_tok_model(repo, tokenizer_hash=b"trig-h2", hf_repo="rh2")
    with repo.engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT fingerprint_id FROM neuro.runs WHERE run_id = :r"), {"r": run_id}
            ).scalar_one()
            is None
        )
        _capture(conn, run_id, mid_a, actor_id, "ev#adhoc-a")
        _capture(conn, run_id, mid_b, actor_id, "ev#adhoc-b")


# --- CLASS 9: the gaps a post-build vet MEASURED as unpinned ------------------------------------------
@pytest.mark.parametrize("state", ["queued", "claimed", "succeeded", "cancelled"])
def test_rt_job_non_running_self_edge_rejected(seeded, state):
    """★ THE `OF state` PIN. The migration calls column-scoping load-bearing and explicitly rules out the
    tempting alternatives — a same-value early-out, or a `WHEN (OLD.state IS DISTINCT FROM NEW.state)`
    clause — because those legalize EVERY self-edge, which is looser than the app. That claim was PROSE:
    a post-build vet added the WHEN clause and measured 82 tests passing, because `running -> running` is
    the only self-edge any probe touched and it is the one the mutant keeps green. These four turn the
    claim into a measured fact. `running` is excluded on purpose — it is checkpoint's REAL self-edge."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key=f"k#selfneg-{state}", state=state)
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _set_state(conn, j, state)
    assert f"{state} -> {state}" in str(exc.value)


def test_rt_bundle_registered_self_edge_rejected(seeded, tmp_path):
    """The bundles mirror of the above: no bundle self-edge is an app path, so none is legal."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, _backend_id(repo, tmp_path))
        for s in ("sealed", "registered"):
            conn.execute(text("UPDATE neuro.bundles SET state=:s WHERE bundle_id=:b"), {"s": s, "b": b})
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state='registered' WHERE bundle_id=:b"), {"b": b})
    assert "registered -> registered" in str(exc.value)


def test_rt_bundle_unsealed_to_registered_rejected(seeded, tmp_path):
    """The BUNDLE forward-skip — the analogue of queued->succeeded, and it had no probe. Registering a
    bundle that was never sealed means manifest_sha256 NULL, zero artifacts, and GC-exempt forever."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, _backend_id(repo, tmp_path))
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state='registered' WHERE bundle_id=:b"), {"b": b})
    assert "unsealed -> registered" in str(exc.value)


@pytest.mark.parametrize("reserved", ["staged", "verified"])
def test_rt_bundle_reserved_members_are_dead_end_sources(seeded, tmp_path, reserved):
    """★ BOTH ROUTES into a reserved member are refused — the UPDATE arm and the INSERT arm — which is
    what makes staged/verified UNREACHABLE rather than merely unused.

    ⚠ Read the assertion carefully: this probe cannot demonstrate "no legal edge LEAVES a reserved
    state", because after this migration there is no way to CREATE such a row to leave from. That is
    the stronger fact and the honest one to assert, and it is exactly what the canonical runbook's
    pre-apply census depends on: any bundle ALREADY sitting in staged/verified when 0003 lands would be
    permanently frozen (no legal edge out), and no new one can ever appear. The census expects zero."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    bid = _backend_id(repo, tmp_path)
    with repo.engine.begin() as conn:
        b = _bundle(conn, run_id, bid)
    # There is NO legal route into a reserved state — that is the guarantee. Both arms refuse.
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.bundles SET state=:s WHERE bundle_id=:b"), {"s": reserved, "b": b})
    assert f"unsealed -> {reserved}" in str(exc.value)
    with pytest.raises(Exception) as exc2, repo.engine.begin() as conn:
        _bundle(conn, run_id, bid, state=reserved)
    assert "illegal bundle_state at INSERT" in str(exc2.value)


def test_rt_job_blocked_cannot_be_claimed(seeded):
    """blocked -> claimed is the dependency AND-gate bypass: claiming a job whose parents have not
    succeeded. `blocked` appeared only as a rejected TARGET; this pins it as a SOURCE."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        j = _job(conn, run_id, key="k#blocked-claim", state="blocked")
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        _set_state(conn, j, "claimed")
    assert "blocked -> claimed" in str(exc.value)


def test_rt_bundle_insert_arm_rejects_null_state_via_the_guard(seeded, tmp_path):
    """The INSERT arm's own NULL direction. `IS NOT TRUE` is what makes a NULL state a rejection here
    too; flipping it back to `IF NOT (...)` would turn this arm fail-OPEN, and until now only the jobs
    UPDATE arm had a probe for that shape."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    bid = _backend_id(repo, tmp_path)
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.bundles (bundle_uuid, run_id, backend_id, state) "
                "VALUES (gen_random_uuid(), :r, :b, NULL)"
            ),
            {"r": run_id, "b": bid},
        )
    assert "illegal bundle_state at INSERT" in str(exc.value)
