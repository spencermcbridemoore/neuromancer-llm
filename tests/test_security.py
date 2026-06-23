"""R11: the privilege boundary (ADR-0007 / C3 / B2) and the assign-once trigger, behaviorally on real PG.

* neuro_writer may NOT insert identity/registry rows (fingerprints, model_identities); neuro_registrar may.
* neuro_reader is SELECT-only — every consumption surface uses it (phase0 Q12); no write grant at all.
* runs.fingerprint_id is assign-once: NULL->value succeeds once (adhoc adoption); value->different raises.

The role-provisioning fixtures (provisioned_roles / role_url / role_pw) live in conftest.py and are shared
with the reader read-path tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

pytestmark = pytest.mark.pg


def _exec_as(role_url: URL, sql: str, params: dict | None = None) -> bool:
    """Run a statement as `role`. Return True if it committed, False ONLY on a permission denial; any other
    error (e.g. a constraint/FK problem) re-raises so a mis-set-up test can't masquerade as 'denied'."""
    eng = create_engine(role_url, future=True)
    try:
        with eng.begin() as conn:
            conn.execute(text(sql), params or {})
        return True
    except Exception as exc:  # noqa: BLE001 — classify permission-denied vs everything else
        if "permission denied" in str(exc).lower():
            return False
        raise
    finally:
        eng.dispose()


def _seed_model(engine) -> int:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.tokenizer_identities (tokenizer_hash) VALUES (:h) ON CONFLICT DO NOTHING"
            ),
            {"h": b"\x01"},
        )
        tid = conn.execute(
            text("SELECT tokenizer_id FROM neuro.tokenizer_identities ORDER BY tokenizer_id LIMIT 1")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO neuro.model_identities "
                "(identity_hash, dtype_quant, tokenizer_id, serving_stack, serving_version, arch_family) "
                "VALUES (:h, 'bf16', :t, 'vllm', '1', 'llama') ON CONFLICT DO NOTHING"
            ),
            {"h": b"\x02", "t": tid},
        )
        return conn.execute(
            text("SELECT model_id FROM neuro.model_identities ORDER BY model_id LIMIT 1")
        ).scalar_one()


def test_role_boundary(engine, provisioned_roles, role_url):
    base = provisioned_roles
    mid = _seed_model(engine)
    writer = role_url(base, "neuro_writer")
    registrar = role_url(base, "neuro_registrar")
    fp_sql = (
        "INSERT INTO neuro.fingerprints (fingerprint_hash, model_id, declared_mode, semantic_config) "
        "VALUES (:h, :m, 'greedy', 'cfg')"
    )
    model_sql = (
        "INSERT INTO neuro.model_identities "
        "(identity_hash, dtype_quant, tokenizer_id, serving_stack, serving_version, arch_family) "
        "VALUES (:h, 'bf16', 1, 'vllm', '1', 'llama')"
    )

    # writer is DENIED identity/registry INSERT (C3)
    assert _exec_as(writer, fp_sql, {"h": b"\x03", "m": mid}) is False
    assert _exec_as(writer, model_sql, {"h": b"\xaa"}) is False
    # registrar is ALLOWED identity/registry INSERT (VM-local orchestrator)
    assert _exec_as(registrar, fp_sql, {"h": b"\x04", "m": mid}) is True


def test_reader_role_is_select_only(engine, provisioned_roles, role_url):
    """R11 mirror: neuro_reader may SELECT every table but holds NO write grant — INSERT on a registry table
    AND UPDATE on an operational table are both DENIED (export discipline; phase0 Q12)."""
    base = provisioned_roles
    mid = _seed_model(engine)
    reader = role_url(base, "neuro_reader")

    # SELECT is allowed (the consumption surface)
    eng = create_engine(reader, future=True)
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM neuro.model_identities")).scalar_one() >= 1
    finally:
        eng.dispose()

    # INSERT on a registry/identity table is DENIED
    fp_sql = (
        "INSERT INTO neuro.fingerprints (fingerprint_hash, model_id, declared_mode, semantic_config) "
        "VALUES (:h, :m, 'greedy', 'cfg')"
    )
    assert _exec_as(reader, fp_sql, {"h": b"\x07", "m": mid}) is False
    # INSERT on an operational/output table is DENIED (the writer has this; the reader does not)
    ce_sql = (
        "INSERT INTO neuro.capture_events (run_id, event_key, model_id, actor_id, origin, request_text) "
        "VALUES (1, 'k', :m, 1, 'o', 'x')"
    )
    assert _exec_as(reader, ce_sql, {"m": mid}) is False
    # UPDATE on an operational table is DENIED (lifecycle UPDATE is the writer's, never the reader's)
    assert _exec_as(reader, "UPDATE neuro.jobs SET state = 'queued' WHERE job_id = -1") is False


def test_role_password_with_quote_roundtrips(provisioned_roles, role_url, role_pw):
    """R3: a password containing a single quote was created safely (sql.Literal), and a role can connect with
    that EXACT password and run a query. The old f-string interpolation broke on the quote."""
    assert "'" in role_pw  # guard: this test is only meaningful with a quote in the password
    eng = create_engine(role_url(provisioned_roles, "neuro_reader"), future=True)
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        eng.dispose()


def test_assign_once_trigger(seeded):
    """NULL->value succeeds once (adhoc adoption); value->different is refused by the in-schema trigger."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    eng = repo.engine
    with eng.begin() as conn:
        conn.execute(
            text("INSERT INTO neuro.tokenizer_identities (tokenizer_hash) VALUES (:h)"), {"h": b"\x10"}
        )
        tid = conn.execute(
            text("SELECT tokenizer_id FROM neuro.tokenizer_identities ORDER BY tokenizer_id DESC LIMIT 1")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO neuro.model_identities "
                "(identity_hash, dtype_quant, tokenizer_id, serving_stack, serving_version, arch_family) "
                "VALUES (:h, 'bf16', :t, 'vllm', '1', 'llama')"
            ),
            {"h": b"\x11", "t": tid},
        )
        mid = conn.execute(
            text("SELECT model_id FROM neuro.model_identities ORDER BY model_id DESC LIMIT 1")
        ).scalar_one()
        for hh, cfg in ((b"\x12", "cfg1"), (b"\x13", "cfg2")):
            conn.execute(
                text(
                    "INSERT INTO neuro.fingerprints (fingerprint_hash, model_id, declared_mode, semantic_config) "
                    "VALUES (:h, :m, 'greedy', :c)"
                ),
                {"h": hh, "m": mid, "c": cfg},
            )
        fps = (
            conn.execute(text("SELECT fingerprint_id FROM neuro.fingerprints ORDER BY fingerprint_id"))
            .scalars()
            .all()
        )
    fp1, fp2 = fps[0], fps[1]

    # NULL -> value succeeds once (adhoc adoption)
    with eng.begin() as conn:
        conn.execute(
            text("UPDATE neuro.runs SET fingerprint_id = :f WHERE run_id = :r"), {"f": fp1, "r": run_id}
        )
    with eng.connect() as conn:
        assert (
            conn.execute(
                text("SELECT fingerprint_id FROM neuro.runs WHERE run_id = :r"), {"r": run_id}
            ).scalar_one()
            == fp1
        )

    # value -> different is refused by the assign-once trigger
    with pytest.raises(Exception) as excinfo, eng.begin() as conn:  # noqa: PT011 — assert on the message below
        conn.execute(
            text("UPDATE neuro.runs SET fingerprint_id = :f WHERE run_id = :r"), {"f": fp2, "r": run_id}
        )
    assert "assign-once" in str(excinfo.value).lower()


def test_registrar_active_version_grant_is_column_scoped(engine, provisioned_roles, role_url):
    """C8: the deviation grant is exactly as narrow as claimed. As neuro_registrar, UPDATE methods SET
    active_version_id SUCCEEDS, but UPDATE of any identity column (methods.method_key, method_versions.semver
    /code_sha) is permission-denied; the reader is denied UPDATE on divergence_measurements."""
    base = provisioned_roles
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO neuro.methods (method_key) VALUES ('c8_probe') ON CONFLICT DO NOTHING")
        )
        mid = conn.execute(
            text("SELECT method_id FROM neuro.methods WHERE method_key = 'c8_probe'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO neuro.method_versions (method_id, semver, code_sha) VALUES (:m, '1.0.0', :c) "
                "ON CONFLICT DO NOTHING"
            ),
            {"m": mid, "c": b"\x01"},
        )
        mvid = conn.execute(
            text("SELECT method_version_id FROM neuro.method_versions WHERE method_id = :m"), {"m": mid}
        ).scalar_one()
    registrar = role_url(base, "neuro_registrar")
    reader = role_url(base, "neuro_reader")

    # registrar CAN set the active-version pointer (the column-scoped deviation grant)
    assert (
        _exec_as(
            registrar,
            "UPDATE neuro.methods SET active_version_id = :v WHERE method_id = :m",
            {"v": mvid, "m": mid},
        )
        is True
    )
    # registrar CANNOT touch identity columns (column-scoped: not method_key / semver / code_sha)
    assert (
        _exec_as(registrar, "UPDATE neuro.methods SET method_key = 'x' WHERE method_id = :m", {"m": mid})
        is False
    )
    assert (
        _exec_as(
            registrar,
            "UPDATE neuro.method_versions SET semver = '9' WHERE method_version_id = :v",
            {"v": mvid},
        )
        is False
    )
    assert (
        _exec_as(
            registrar,
            "UPDATE neuro.method_versions SET code_sha = :c WHERE method_version_id = :v",
            {"v": mvid, "c": b"\x02"},
        )
        is False
    )
    # reader is denied UPDATE on a derived/operational table (SELECT-only consumption surface)
    assert (
        _exec_as(reader, "UPDATE neuro.divergence_measurements SET max_abs_diff = 1 WHERE divergence_id = -1")
        is False
    )
