"""Red-team: least-privilege roles (L7), per-statement allow/deny (sharpens Panel #1 C12).

The base role tests asserted "a denial happened" without proving WHERE and never ran AS neuro_registrar.
These probe the EXACT column-scoped boundary — the C3 compromised-worker surface: a worker that connects as
neuro_writer must not be able to mutate identity / integrity / wire bytes, DELETE, or write a registry row;
the registrar must not touch the worker's lifecycle columns (role separation is bidirectional).

A column-UPDATE grant is checked BEFORE the row is touched, so `WHERE <id> = -1` (0 rows) still exercises
the grant: a denied column raises permission-denied; a granted column commits (0 rows). No seeding needed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.pg


def test_rt_writer_column_update_boundary(provisioned_roles, role_url, rt):
    """L7 (C3): as neuro_writer, the GRANTED lifecycle columns UPDATE; the identity / integrity / wire
    columns are DENIED. A grant widened to all columns on artifacts would let a worker rewrite sha256/uri
    (an L6 sin) — this pins the exact boundary.

    ★ THREE ASSERTIONS INVERTED AT ADR-0046 C3, and the inversion IS the unit's headline. `neuro.jobs` and
    `neuro.work_leases` were in the GRANTED half here, which is exactly what made 0003's state ladder
    WALKABLE — `queued -> claimed -> succeeded` in two legal hops with no token, no lease and no work. The
    role split revokes the whole jobs UPDATE and the whole work_leases INSERT+UPDATE, and returns that
    capability as five SECURITY DEFINER entry points instead. `runs.fingerprint_id` goes the same way (the
    identity-adjacent adoption column, which nothing in src/ writes).

    These are the assertions that would have gone stale SILENTLY if the grants had changed without them:
    they asserted a positive, so a revocation reddens them loudly rather than leaving a weaker gate green.
    The positive half now lives in `test_rt_role_split.py`, against the functions."""
    w = role_url(provisioned_roles, "neuro_writer")
    # GRANTED (operational lifecycle / tombstone-state) — the surface the writer legitimately still owns
    assert rt.exec_as(w, "UPDATE neuro.artifacts SET deleted_at=now() WHERE artifact_id=-1") is True
    assert rt.exec_as(w, "UPDATE neuro.bundles SET state='sealed' WHERE bundle_id=-1") is True
    assert rt.exec_as(w, "UPDATE neuro.runs SET finalized_at=now() WHERE run_id=-1") is True
    # ★ REVOKED at C3 — the walking kit and the lease surface, now reachable only through the functions
    assert rt.exec_as(w, "UPDATE neuro.jobs SET state='queued' WHERE job_id=-1") is False
    assert rt.exec_as(w, "UPDATE neuro.jobs SET claim_token=claim_token WHERE job_id=-1") is False
    assert rt.exec_as(w, "UPDATE neuro.work_leases SET last_heartbeat=now() WHERE job_id=-1") is False
    assert (
        rt.exec_as(
            w,
            "INSERT INTO neuro.work_leases (job_id, claim_token, leased_by) "
            "VALUES (-1, gen_random_uuid(), -1)",
        )
        is False
    )
    assert rt.exec_as(w, "UPDATE neuro.runs SET fingerprint_id=fingerprint_id WHERE run_id=-1") is False
    # DENIED (identity / integrity / wire bytes / non-granted columns). Self-reference (SET col=col) keeps
    # the value well-typed so the PERMISSION check is what fails, not a type cast.
    assert rt.exec_as(w, "UPDATE neuro.artifacts SET sha256=sha256 WHERE artifact_id=-1") is False
    assert (
        rt.exec_as(w, "UPDATE neuro.capture_events SET response_text='x' WHERE capture_event_id=-1") is False
    )
    assert rt.exec_as(w, "UPDATE neuro.jobs SET run_id=1 WHERE job_id=-1") is False  # identity-adjacent col
    assert rt.exec_as(w, "UPDATE neuro.model_identities SET dtype_quant='x' WHERE model_id=-1") is False


def test_rt_writer_no_delete_no_registry_insert(provisioned_roles, role_url, rt):
    """L7: neuro_writer has NO DELETE on any table, and NO INSERT on the registry/identity tables (the
    'NO DELETE' / default-deny surface was entirely untested). The operational INSERTs it DOES hold succeed."""
    w = role_url(provisioned_roles, "neuro_writer")
    # NO DELETE anywhere
    assert rt.exec_as(w, "DELETE FROM neuro.artifacts WHERE artifact_id=-1") is False
    assert rt.exec_as(w, "DELETE FROM neuro.jobs WHERE job_id=-1") is False
    # NO registry/identity INSERT (a denied INSERT is permission-checked before FK evaluation)
    assert rt.exec_as(w, "INSERT INTO neuro.methods (method_key) VALUES ('x')") is False
    assert rt.exec_as(w, "INSERT INTO neuro.campaigns (campaign_key, actor_id) VALUES ('x', 1)") is False
    assert (
        rt.exec_as(
            w,
            "INSERT INTO neuro.runs (run_key, campaign_id, work_slug, variant_digest, actor_id, origin) "
            "VALUES ('x',1,'s','d',1,'o')",
        )
        is False
    )


def test_rt_registrar_denied_worker_lifecycle(provisioned_roles, role_url, rt):
    """L7 (bidirectional separation): the registrar may INSERT registries but is DENIED the worker's lifecycle
    UPDATEs (jobs.state, work_leases.last_heartbeat) — its only UPDATE is the methods.active_version_id pointer."""
    r = role_url(provisioned_roles, "neuro_registrar")
    assert (
        rt.exec_as(r, "INSERT INTO neuro.methods (method_key) VALUES ('rt_reg_probe') ON CONFLICT DO NOTHING")
        is True
    )
    assert rt.exec_as(r, "UPDATE neuro.jobs SET state='queued' WHERE job_id=-1") is False
    assert rt.exec_as(r, "UPDATE neuro.work_leases SET last_heartbeat=now() WHERE job_id=-1") is False
    assert rt.exec_as(r, "UPDATE neuro.bundles SET state='sealed' WHERE bundle_id=-1") is False


def test_rt_backup_freshness_grant_boundary(provisioned_roles, role_url, rt):
    """L7 (A2-10 durability): the backup-freshness signal leans on a grant ASYMMETRY — the writer bumps the
    operational health columns but can neither seed the row nor forge the `stale_after` sentinel; the row is
    seeded ONCE by registrar/admin. A grant widened to stale_after or INSERT would let a compromised worker
    fake 'backup fresh' and slip a stale-DB cloud write past the ADR-0020 gate. Corollary (GO-D-seed): the
    `neuro db durability reconcile` (stale_after UPDATE) is ADMIN-ONLY — registrar holds INSERT-all but NO
    system_health UPDATE, so it is denied the sentinel re-align exactly like the writer (adding a registrar
    stale_after grant to 'fix' that would reopen this exact forgery surface)."""
    writer = role_url(provisioned_roles, "neuro_writer")
    registrar = role_url(provisioned_roles, "neuro_registrar")
    reader = role_url(provisioned_roles, "neuro_reader")
    nope = {"k": "__rt_nope__"}  # 0-row WHERE: the column grant is checked before a row is touched
    # writer GRANTED the three operational columns (grants.sql:48)
    assert (
        rt.exec_as(writer, "UPDATE neuro.system_health SET status=status WHERE health_key=:k", nope) is True
    )
    assert (
        rt.exec_as(writer, "UPDATE neuro.system_health SET measured_at=measured_at WHERE health_key=:k", nope)
        is True
    )
    assert (
        rt.exec_as(writer, "UPDATE neuro.system_health SET detail=detail WHERE health_key=:k", nope) is True
    )
    # GO-D-wal: the SAME table grant carries the wal_lag producer — exercised on the new arm's real key
    # (0-row WHERE in the unseeded roles DB: grant-legality only; grants are column- not row-scoped, so this
    # pins that the archiver producer runs as plain neuro_writer with NO grant change).
    wal = {"k": "wal_lag"}
    assert rt.exec_as(writer, "UPDATE neuro.system_health SET status=status WHERE health_key=:k", wal) is True
    assert (
        rt.exec_as(writer, "UPDATE neuro.system_health SET measured_at=measured_at WHERE health_key=:k", wal)
        is True
    )
    # writer DENIED the sentinel column and the seed INSERT (un-forgeable freshness contract)
    assert (
        rt.exec_as(writer, "UPDATE neuro.system_health SET stale_after=stale_after WHERE health_key=:k", nope)
        is False
    )
    assert (
        rt.exec_as(writer, "UPDATE neuro.system_health SET stale_after=stale_after WHERE health_key=:k", wal)
        is False
    )  # the wal_lag sentinel (NULL for life on a bound-less row) is exactly as un-forgeable
    assert (
        rt.exec_as(
            writer,
            "INSERT INTO neuro.system_health (health_key, status) VALUES ('__rt_w__','blocked') "
            "ON CONFLICT (health_key) DO NOTHING",
        )
        is False
    )
    # writer GRANTED the probe_reports audit INSERT (grants.sql:30)
    assert (
        rt.exec_as(writer, "INSERT INTO neuro.probe_reports (probe_key, status) VALUES ('__rt_w__','ok')")
        is True
    )
    # registrar GRANTED the seed INSERT; reader DENIED any system_health write
    assert (
        rt.exec_as(
            registrar,
            "INSERT INTO neuro.system_health (health_key, status) VALUES ('__rt_seed__','blocked') "
            "ON CONFLICT (health_key) DO NOTHING",
        )
        is True
    )
    assert (
        rt.exec_as(reader, "UPDATE neuro.system_health SET status=status WHERE health_key=:k", nope) is False
    )
    assert (
        rt.exec_as(
            reader,
            "INSERT INTO neuro.system_health (health_key, status) VALUES ('__rt_r__','blocked') "
            "ON CONFLICT (health_key) DO NOTHING",
        )
        is False
    )  # reader cannot seed either — pin the reader seed-forgery denial alongside the writer's
    # GO-D-seed reconcile (stale_after UPDATE) is ADMIN-ONLY: registrar is DENIED it (INSERT != UPDATE), and
    # only neuro_admin (GRANT ALL) may re-align the sentinel — pins reconcile's grade to admin, not registrar.
    admin = role_url(provisioned_roles, "neuro_admin")
    assert (
        rt.exec_as(
            registrar, "UPDATE neuro.system_health SET stale_after=stale_after WHERE health_key=:k", nope
        )
        is False
    )
    assert (
        rt.exec_as(admin, "UPDATE neuro.system_health SET stale_after=stale_after WHERE health_key=:k", nope)
        is True
    )


def test_rt_reader_is_strictly_select_only(provisioned_roles, role_url, rt):
    """L7: neuro_reader holds NO write grant — INSERT on a registry table, UPDATE on an operational table,
    and DELETE are all DENIED (every consumption surface uses this role)."""
    rd = role_url(provisioned_roles, "neuro_reader")
    assert rt.exec_as(rd, "INSERT INTO neuro.methods (method_key) VALUES ('x')") is False
    assert rt.exec_as(rd, "UPDATE neuro.jobs SET state='queued' WHERE job_id=-1") is False
    assert (
        rt.exec_as(rd, "UPDATE neuro.divergence_measurements SET max_abs_diff=1 WHERE divergence_id=-1")
        is False
    )
    assert rt.exec_as(rd, "DELETE FROM neuro.artifacts WHERE artifact_id=-1") is False


def test_rt_adversarial_role_password_roundtrips(provisioned_roles, role_url, role_pw):
    """L7: an adversarial-shaped password (the fixture's a'b\"c) round-trips through provisioning (sql.Literal)
    and the role connects + runs a query — injection-safety shown by an actual quote payload, not just escaping."""
    from sqlalchemy import create_engine, text

    assert "'" in role_pw and '"' in role_pw
    eng = create_engine(role_url(provisioned_roles, "neuro_writer"), future=True)
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        eng.dispose()
