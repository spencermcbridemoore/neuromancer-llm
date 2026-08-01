"""Red-team: the ADR-0046 C3 worker/registrar ROLE SPLIT — the closure, and the residual it does NOT close.

This file is the positive half of the split. `test_rt_roles.py` proves the walking kit is GONE from
`neuro_writer`; this proves the capability came back as five named entry points, that the internals stayed
out of reach, and — deliberately — that the split's guarantee is NARROWER than "a worker cannot mark a job
succeeded with no work".

★ WHY THE HONEST PROBE IS HERE. Migration 0003 shipped `test_rt_ladder_is_walkable_statement_by_statement`
as a POSITIVE probe of its own residual, "so it can never be silently mis-remembered as closed". C3 closes
that residual for `neuro_writer` — and opens a narrower one in its place: `claim_job` then `complete_job`
reaches 'succeeded' with no work in two GRANTED calls. Re-scoping the 0003 probe to admin without pinning
the new residual would have REMOVED the honesty mechanism instead of updating it, so
`test_rt_writer_can_still_claim_and_complete_without_work` inherits that job.

⚠ Every probe here runs as the REAL `neuro_writer` login (or the real admin), never as the session
superuser — a permission boundary asserted from a superuser connection proves nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.pg

_DEFINER_ENTRY_POINTS = (
    "neuro.claim_job(bigint, text, text, integer)",
    "neuro.renew_lease(bigint, uuid, integer)",
    "neuro.checkpoint_job(bigint, uuid, text)",
    "neuro.complete_job(bigint, uuid)",
    "neuro.fail_job_permanent(bigint, uuid, text)",
)
_INTERNALS = (
    "neuro.unblock_ready_dependents(bigint)",
    "neuro.cascade_cancel_jobs(bigint, boolean)",
)


def _writer_engine(provisioned_roles, role_url):
    return create_engine(role_url(provisioned_roles, "neuro_writer"), future=True)


def test_rt_writer_cannot_walk_the_ladder_only_the_functions_can(seeded, provisioned_roles, role_url):
    """★ THE CLOSURE, PROVEN BOTH WAYS IN ONE PROBE — this is what session C3 exists to make true.

    NEGATIVE: as the real `neuro_writer`, the two hops of 0003's walkable ladder are now refused by the
    GRANT, before any trigger is consulted — `queued -> claimed` is permission denied, and so is forging the
    lease that would make the claim look real.
    POSITIVE: the SAME role, in the SAME connection, claims the SAME job through `neuro.claim_job` and gets
    a real token and a real lease. Both halves matter: without the positive, this is indistinguishable from
    a role that simply cannot work; without the negative, nothing is closed."""
    repo, run_id, actor = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#c3-closure")
    assert repo.state_of(job) == "queued"

    weng = _writer_engine(provisioned_roles, role_url)
    try:
        # NEGATIVE — the raw ladder is gone
        for sql in (
            "UPDATE neuro.jobs SET state = 'claimed' WHERE job_id = :j",
            "UPDATE neuro.jobs SET state = 'succeeded' WHERE job_id = :j",
            "UPDATE neuro.jobs SET claim_token = gen_random_uuid(), claim_seq = claim_seq + 1 "
            "WHERE job_id = :j",
        ):
            with pytest.raises(Exception) as exc, weng.begin() as conn:  # noqa: PT011 — message asserted
                conn.execute(text(sql), {"j": job})
            assert "permission denied" in str(exc.value).lower(), (
                f"the walking kit must be refused by the GRANT, not by a trigger: {sql!r}"
            )
        assert repo.state_of(job) == "queued", "no refused statement may have taken effect"

        # POSITIVE — the same role, through the front door
        with weng.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT out_job_id, out_claim_token, out_claim_seq "
                        "FROM neuro.claim_job(:a, 'default', NULL, 120)"
                    ),
                    {"a": actor},
                )
                .mappings()
                .one()
            )
    finally:
        weng.dispose()

    assert row["out_job_id"] == job
    assert row["out_claim_token"] is not None
    assert row["out_claim_seq"] == 1, "the claim must ADVANCE the fence — that is what makes it auditable"
    assert repo.state_of(job) == "claimed"
    with repo.engine.connect() as conn:
        leases = conn.execute(
            text("SELECT count(*) FROM neuro.work_leases WHERE job_id = :j AND released_at IS NULL"),
            {"j": job},
        ).scalar_one()
        claimed_by = conn.execute(
            text("SELECT claimed_by FROM neuro.jobs WHERE job_id = :j"), {"j": job}
        ).scalar_one()
    assert leases == 1, "claim_job must open the lease atomically with the claim"
    assert claimed_by == actor, "the claim must be ATTRIBUTED — the audit trail is the whole trade"


def test_rt_writer_can_still_claim_and_complete_without_work(seeded, provisioned_roles, role_url):
    """★ THE HONEST-SCOPE PIN, C3's own. The split does NOT stop a worker marking a job succeeded with no
    work — it stops it doing so SILENTLY.

    `claim_job` + `complete_job` are both granted, so this sequence is legal and reaches 'succeeded' having
    performed nothing. What C3 removes is the anonymous path: the claim bumps `claim_seq` and stamps
    `claimed_by`, so the act is attributable to an actor and countable, where the bare
    `UPDATE ... SET state='succeeded'` it replaces left no trace at all.

    This probe asserts the residual EXISTS, exactly as 0003's walkable-ladder probe did for its own. If a
    future unit really closes it (per-worker logins binding `claimed_by` to `current_user`, P-7, is the
    plausible route), this test reddens and its author is FORCED to update the claim rather than quietly
    inherit a stale one."""
    repo, run_id, actor = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    job = repo.enqueue(run_id=run_id, job_key="k#c3-residual")

    weng = _writer_engine(provisioned_roles, role_url)
    try:
        with weng.begin() as conn:
            row = (
                conn.execute(
                    text("SELECT out_job_id, out_claim_token FROM neuro.claim_job(:a, 'default', NULL, 120)"),
                    {"a": actor},
                )
                .mappings()
                .one()
            )
            ok = conn.execute(
                text("SELECT neuro.complete_job(:j, :t)"),
                {"j": row["out_job_id"], "t": row["out_claim_token"]},
            ).scalar_one()
    finally:
        weng.dispose()

    assert ok is True
    assert repo.state_of(job) == "succeeded", (
        "RESIDUAL: a writer still reaches 'succeeded' with no work, via two GRANTED calls"
    )
    # ...but it is ATTRIBUTED, which the pre-C3 bare UPDATE was not. That is the delta, and it is the only
    # thing this unit may claim.
    with repo.engine.connect() as conn:
        row2 = (
            conn.execute(text("SELECT claimed_by, claim_seq FROM neuro.jobs WHERE job_id = :j"), {"j": job})
            .mappings()
            .one()
        )
    assert row2["claimed_by"] == actor and row2["claim_seq"] == 1


def test_rt_writer_cannot_call_the_internal_functions(engine, seeded, provisioned_roles, role_url, rt):
    """The two INVOKER internals are reachable only THROUGH the DEFINER entry points. A bare
    `cascade_cancel_jobs(x, true)` would let a compromised worker cancel an arbitrary dependency subtree it
    holds no token for; `unblock_ready_dependents` would let it flip 'blocked' jobs queued at will.

    ★ THE PRIVILEGE IS ASSERTED DIRECTLY, NOT ONLY BEHAVIOURALLY — and the mutation matrix is why.
    Granting `neuro_writer` EXECUTE on `cascade_cancel_jobs` did NOT redden the behavioural assertions
    below, because the internals are protected TWICE: they are SECURITY **INVOKER**, so even with EXECUTE
    the function body runs as the writer, whose `UPDATE ON jobs` C3 revoked — and the body's own
    `FOR NO KEY UPDATE` is then permission-denied. Real defense in depth, but it made the EXECUTE
    revocation itself unpinnable, i.e. it could be dropped silently. `has_function_privilege` pins the
    grant as its own fact so both layers redden independently."""
    w = role_url(provisioned_roles, "neuro_writer")
    with engine.connect() as conn:
        for sig in _INTERNALS:
            assert (
                conn.execute(
                    text("SELECT has_function_privilege('neuro_writer', :sig, 'EXECUTE')"), {"sig": sig}
                ).scalar_one()
                is False
            ), f"neuro_writer must hold NO EXECUTE on the internal {sig}"
        for sig in _DEFINER_ENTRY_POINTS:
            assert (
                conn.execute(
                    text("SELECT has_function_privilege('neuro_writer', :sig, 'EXECUTE')"), {"sig": sig}
                ).scalar_one()
                is True
            ), f"neuro_writer must hold EXECUTE on the entry point {sig}"
    assert rt.exec_as(w, "SELECT neuro.cascade_cancel_jobs(-1, true)") is False
    assert rt.exec_as(w, "SELECT neuro.unblock_ready_dependents(-1)") is False
    # ...while the five entry points ARE granted (a blanket-denial bug would otherwise read as a pass here)
    assert rt.exec_as(w, "SELECT neuro.complete_job(-1, gen_random_uuid())") is True


def test_rt_grants_reprovision_actually_revokes(engine, provisioned_roles):
    """★ THE CANONICAL UPGRADE PATH, WHICH NOTHING ELSE TESTS — found by the mutation matrix.

    Every other probe in this file runs against a FRESH database, where the writer is safe simply because
    the C3 grant was never issued. That proves nothing about the path canonical will actually take: an
    ALREADY-PROVISIONED database where the walking kit IS granted and `neuro db roles` is re-run.
    **GRANT IS ADDITIVE** — deleting a column from a GRANT list changes nothing on such a database — which
    is exactly why `grants.sql` carries explicit REVOKEs, and why removing one did not redden any
    fresh-DB probe.

    So this probe reconstructs the pre-C3 state, re-applies the SHIPPED contract, and asserts the
    privileges are GONE. It is the only mechanical check that the runbook's `neuro db roles` step does what
    the runbook says it does."""
    from neuromancer_llm.db.provision import apply_grants  # noqa: PLC0415

    # ⚠ COLUMN-scoped grants need `has_column_privilege`. MEASURED: `has_table_privilege(role, t, 'UPDATE')`
    # reports False even while column-level UPDATE grants exist on that table, so a table-level check here
    # would make the PRECONDITION unsatisfiable and the whole probe vacuous — the mirror of a check that can
    # never fail. Only `work_leases` INSERT is genuinely table-level.
    checks = {
        "jobs.state": "SELECT has_column_privilege('neuro_writer','neuro.jobs','state','UPDATE')",
        "jobs.claim_token": "SELECT has_column_privilege('neuro_writer','neuro.jobs','claim_token','UPDATE')",
        "work_leases INSERT": "SELECT has_table_privilege('neuro_writer','neuro.work_leases','INSERT')",
        "runs.fingerprint_id": (
            "SELECT has_column_privilege('neuro_writer','neuro.runs','fingerprint_id','UPDATE')"
        ),
    }
    # 1. reconstruct a pre-C3 database
    with engine.begin() as conn:
        conn.execute(text("GRANT UPDATE (state, claim_token, claim_seq) ON neuro.jobs TO neuro_writer"))
        conn.execute(text("GRANT INSERT ON neuro.work_leases TO neuro_writer"))
        conn.execute(text("GRANT UPDATE (fingerprint_id) ON neuro.runs TO neuro_writer"))
    with engine.connect() as conn:  # precondition: the walking kit really IS back, or the test is vacuous
        for name, sql in checks.items():
            assert conn.execute(text(sql)).scalar_one() is True, f"precondition failed: {name} not restored"
    # 2. re-apply the shipped contract, exactly as `neuro db roles` does
    with engine.connect() as conn:
        apply_grants(conn)
    # 3. it must have WITHDRAWN them
    with engine.connect() as conn:
        for name, sql in checks.items():
            assert conn.execute(text(sql)).scalar_one() is False, (
                f"re-provisioning did NOT revoke {name} — a GRANT-only file cannot un-grant, so the "
                f"explicit REVOKE for it is missing or mis-ordered in grants.sql"
            )


def test_rt_definer_functions_are_owned_by_neuro_admin(engine, provisioned_roles):
    """★ THE OWNER **IS** THE PRIVILEGE for a SECURITY DEFINER function.

    The migration cannot set it — a migration must not reference provisioned roles — so on canonical these
    functions are created by `neuro_boot`, a SUPERUSER, and `neuro db roles` re-owns them to `neuro_admin`.
    If that provisioning step is ever dropped, the entire queue surface silently becomes superuser-owned
    while every behavioural test stays green. Nothing else in the tree would notice."""
    with engine.connect() as conn:
        for sig in (*_DEFINER_ENTRY_POINTS, *_INTERNALS, "neuro.assert_job_claim_fencing()"):
            owner = conn.execute(
                text(
                    "SELECT r.rolname FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    "WHERE p.oid = CAST(:sig AS regprocedure)"
                ),
                {"sig": sig},
            ).scalar_one()
            assert owner == "neuro_admin", f"{sig} is owned by {owner!r}, not neuro_admin"


def test_rt_writer_cannot_forge_a_bundle_manifest_at_insert(seeded, provisioned_roles, role_url, tmp_path):
    """★ 0003's LESSON, REPEATING ONE COLUMN OVER — and the reason the assign-once arm is not UPDATE-only.

    The writer holds full-row INSERT on `bundles`, so an UPDATE-only assign-once trigger is WALKABLE AROUND:
    MEASURED before the fix, `INSERT ... (state, manifest_sha256) VALUES ('unsealed', <forged>)` landed
    unopposed, because 0003's `bundles_state_insert` constrained only `state`. The forged row then WEDGES
    its identity forever — `_seal` reads a non-NULL manifest that does not match and raises
    SeamIntegrityError on every subsequent registration.

    Exactly the shape 0003 had to close with its fourth arm, one column over. Caught by the C3 pre-build
    vet's privilege lens and confirmed by measurement, not by reasoning."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    backend_id = repo.get_or_create_storage_backend(
        "rt-c3", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    weng = _writer_engine(provisioned_roles, role_url)
    try:
        with pytest.raises(Exception) as exc, weng.begin() as conn:  # noqa: PT011 — message asserted below
            conn.execute(
                text(
                    "INSERT INTO neuro.bundles (run_id, backend_id, state, manifest_sha256) "
                    "VALUES (:r, :b, 'unsealed', :m)"
                ),
                {"r": run_id, "b": backend_id, "m": b"\xde\xad"},
            )
        assert "illegal bundle_state at INSERT" in str(exc.value)
        # the LEGAL birth still works for the same role — this is a guard, not a blanket denial
        with weng.begin() as conn:
            conn.execute(
                text("INSERT INTO neuro.bundles (run_id, backend_id) VALUES (:r, :b)"),
                {"r": run_id, "b": backend_id},
            )
    finally:
        weng.dispose()


def test_rt_manifest_sha256_is_assign_once(seeded, tmp_path):
    """The UPDATE arm: `bundles.manifest_sha256` is set ONCE, at seal, and can never be re-keyed.

    ⚠ THIS SUPERSEDES the surviving `manifest_sha256` half of ADR-0010's 2026-06-16 closed decision, on its
    own grounds: "hash-verifiable on read" does not stop a writer RE-KEYING the hash BEFORE any read
    happens, and the direct-writer topology is exactly what makes that reachable.

    Driven as admin because the writer legitimately holds `UPDATE (manifest_sha256)` — it must, to seal — so
    unlike the ladder this arm has no revocation under it and the TRIGGER is the whole closure. It reuses
    0001's parameterized `neuro.assert_assign_once`; there is still exactly one assign-once implementation."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    backend_id = repo.get_or_create_storage_backend(
        "rt-c3b", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    with repo.engine.begin() as conn:
        bundle = conn.execute(
            text("INSERT INTO neuro.bundles (run_id, backend_id) VALUES (:r, :b) RETURNING bundle_id"),
            {"r": run_id, "b": backend_id},
        ).scalar_one()
        # NULL -> value once (the seal)
        conn.execute(
            text("UPDATE neuro.bundles SET manifest_sha256 = :m WHERE bundle_id = :b"),
            {"m": b"\x01\x02", "b": bundle},
        )
    # value -> DIFFERENT value is refused in-schema
    with pytest.raises(Exception) as exc, repo.engine.begin() as conn:  # noqa: PT011
        conn.execute(
            text("UPDATE neuro.bundles SET manifest_sha256 = :m WHERE bundle_id = :b"),
            {"m": b"\x03\x04", "b": bundle},
        )
    assert "assign-once" in str(exc.value).lower()
    # value -> SAME value is permitted (the idempotent re-seal shape registrar.py::_seal early-returns on)
    with repo.engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.bundles SET manifest_sha256 = :m WHERE bundle_id = :b"),
            {"m": b"\x01\x02", "b": bundle},
        )


def test_rt_claim_fence_cannot_be_frozen_or_rolled_back(seeded):
    """The fencing belt: any write touching `claim_token`/`claim_seq` must advance the fence by exactly one.

    A BELT UNDER THE REVOCATION, not the closure — grants are provisioning-mutable while triggers are
    schema, so this is 0003's defense-in-depth pattern applied to the fence. It is therefore driven as ADMIN
    (the writer no longer holds the grant at all): the scenario it defends is a MIS-PROVISIONED database
    where the revocation was lost, not the correctly-provisioned one.

    ⚠ WHAT IT DOES NOT DO, stated: it does not stop a STEAL, because a steal that also bumps the fence is
    indistinguishable from a legal re-claim. It makes the fence trustworthy; the revocation is what makes
    the steal unreachable."""
    repo, run_id, actor = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    repo.enqueue(run_id=run_id, job_key="k#fence")
    claim = repo.claim(actor_id=actor)
    assert claim is not None and claim.claim_seq == 1  # the real writer advanced the fence

    for sql, why in (
        ("UPDATE neuro.jobs SET claim_token = gen_random_uuid() WHERE job_id = :j", "silent token swap"),
        ("UPDATE neuro.jobs SET claim_seq = 0 WHERE job_id = :j", "fence rollback"),
        ("UPDATE neuro.jobs SET claim_seq = claim_seq WHERE job_id = :j", "fence freeze"),
    ):
        with pytest.raises(Exception) as exc, repo.engine.begin() as conn:  # noqa: PT011
            conn.execute(text(sql), {"j": claim.job_id})
        assert "claim fence violation" in str(exc.value).lower(), f"{why} must be refused by the guard"

    # ...and a write that does NOT touch the fence must not fire the trigger at all (`OF` is load-bearing:
    # without it every counter and checkpoint UPDATE would need a fence bump it has no business making).
    with repo.engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.jobs SET state = 'running', updated_at = now() WHERE job_id = :j"),
            {"j": claim.job_id},
        )
