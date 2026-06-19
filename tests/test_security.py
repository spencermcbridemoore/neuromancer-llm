"""R11: the privilege boundary (ADR-0007 / C3 / B2) and the assign-once trigger, behaviorally on real PG.

* neuro_writer may NOT insert identity/registry rows (fingerprints, model_identities); neuro_registrar may.
* runs.fingerprint_id is assign-once: NULL->value succeeds once (adhoc adoption); value->different raises.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

pytestmark = pytest.mark.pg

# R3: a password containing a single quote (and a double quote) — the old f-string SQL broke on this
# ('unterminated quoted string'); sql.Literal escaping must round-trip it.
ROLE_PW = "a'b\"c"


def _role_url(base_url: str, role: str) -> URL:
    # Return a URL OBJECT (not a rendered string) so the password reaches psycopg verbatim, with no
    # URL percent-encoding/decoding in the middle to muddy what we're testing.
    return make_url(base_url).set(username=role, password=ROLE_PW)


def _exec_as(role_url: URL, sql: str, params: dict | None = None) -> bool:
    """Run an INSERT as `role`. Return True if it committed, False ONLY on a permission denial; any other
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


@pytest.fixture
def provisioned_roles(engine, pg_url):
    from neuromancer_llm.db.provision import provision_roles

    with engine.connect() as conn:
        provision_roles(conn, password=ROLE_PW)
    return pg_url


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


def test_role_boundary(engine, provisioned_roles):
    base = provisioned_roles
    mid = _seed_model(engine)
    writer = _role_url(base, "neuro_writer")
    registrar = _role_url(base, "neuro_registrar")
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


def test_role_password_with_quote_roundtrips(provisioned_roles):
    """R3: a password containing a single quote was created safely (sql.Literal), and a role can connect
    with that EXACT password and run a query. The old f-string interpolation broke on the quote."""
    assert "'" in ROLE_PW  # guard: this test is only meaningful with a quote in the password
    eng = create_engine(_role_url(provisioned_roles, "neuro_reader"), future=True)
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
