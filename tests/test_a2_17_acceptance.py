"""GO-D-cost C3: the A2-17 acceptance gate — ONE composed cloud write proving every invariant (vs Azurite).

The composed path this unit exists to make real: `neuro storage seed` (the SHIPPED provisioning surface,
GO-input 5) -> resolve_capture_backend (row-bound, rate-bound, guarded, stamped) -> capture_logprob on the
CANONICAL lane (both ADR-0020 durability arms consulted) -> quota consult + spend row per put -> blobs on
the cloud backend -> row-bound read-back. Rehearsed here against Azurite (ADR-0040); the real A2-17 first
write re-runs this shape at the cliff with the un-quarantined credential.

Invariants (GO §3e): (i) a spend row per put with quantity == bytes/10^9 (exact Decimal at the
Numeric(18,6) grain — the payload is sized ≡0 mod 1000 and > 8 KB so it rides the _spill put, fold 7) and
the SEEDED rate_card_id; (ii) the resolved backend IS a QuotaGuardedBackend stamped with its row; (iii) a
blocked durability arm raises DurabilityGateError with ZERO puts/spend (composes GO-D-wal: the wal_lag arm
is the one flipped); (iv) a credential whose endpoint host != the ROW's base_uri host fails the C4
cross-check; (v) no credential -> fail closed, never an Azurite-fallback write; (vi) read-back pointer
coherence through the row-bound backend (sha256 verified); (vii) an over-ceiling write is DENIED with no
blob and no spend row (consult -> record -> upload); (viii) the raw-backend choke-point refusals live in
tests/test_cloud_put_guard.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid as _uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from neuromancer_llm.capture.adapters.vllm import CapturedLogprobs, LogprobSample
from neuromancer_llm.capture.events import capture_logprob
from neuromancer_llm.cli.app import app as cli_app
from neuromancer_llm.db.canonical_instance import CANONICAL_INSTANCE_UUID
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.db.provision import provision
from neuromancer_llm.db.repository import Repository
from neuromancer_llm.governance.durability import WAL_LAG_ROW, seed_row
from neuromancer_llm.governance.freshness import seed_backup_freshness
from neuromancer_llm.governance.health import DurabilityGateError
from neuromancer_llm.governance.probes import _bump_freshness
from neuromancer_llm.governance.wal_archiving import _bump_wal_lag
from neuromancer_llm.registry.backends import QuotaGuardedBackend, resolve_capture_backend
from neuromancer_llm.storage.backends import AZURITE_DEFAULT_CONNECTION_STRING
from neuromancer_llm.storage.quota import QuotaDeniedError, byte_ceiling_for_prefix

pytestmark = pytest.mark.seam  # real Azurite AND real Postgres

_runner = CliRunner()
_AZURITE_CONN = os.environ.get("NEURO_TEST_AZURITE") or AZURITE_DEFAULT_CONNECTION_STRING
_BASE_URI = "http://127.0.0.1:10000/devstoreaccount1/a217accept"
_RESPONSE_BYTES = 16_000  # > 8 KB (rides the _spill put) and ≡ 0 (mod 1000) -> exactly 0.000016 GB (fold 7)


@pytest.fixture
def canon(pg_url, monkeypatch):
    """A canonical-lane scratch DB (the consult FIRES), interlock healed on BOTH ADR-0020 arms, provisioned
    through the SHIPPED `neuro storage seed` path (GO-input 5: the acceptance exercises the command A2-17
    runs, never hand-SQL). Yields the verified Repository."""
    name = f"a217_accept_{_uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_url, future=True, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')  # noqa: S608 — generated hex, not user input
    admin.dispose()
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    ddl = Path("tests/reference/phase3-ddl.sql").read_text(encoding="utf-8")
    eng = create_engine(url, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(ddl.replace("%", "%%"))
    with eng.connect() as c:
        provision(c, lane="canonical")
        c.execute(
            text("UPDATE neuro.database_identity SET instance_uuid = :u"),
            {"u": str(CANONICAL_INSTANCE_UUID)},
        )
        c.commit()
    eng.dispose()
    repo = Repository(create_engine(url, future=True), expected_lane="canonical")
    # heal BOTH durability arms (backup via its producer; wal synthetically — archiving-off test PG)
    with repo.engine.connect() as conn:
        seed_backup_freshness(conn)
        seed_row(conn, WAL_LAG_ROW)
    _bump_freshness(repo.engine, ok=True, detail="acceptance: healthy backup")
    _bump_wal_lag(repo.engine, ok=True, detail="acceptance: healthy archiver")
    # provision the cloud row + the pin-derived rate card through the SHIPPED seed command
    monkeypatch.setenv("NEURO_DATABASE_URL", url)
    r = _runner.invoke(
        cli_app,
        [
            "storage",
            "seed",
            "--backend-key",
            "artifacts-dev",
            "--driver",
            "azure_blob",
            "--base-uri",
            _BASE_URI,
            "--lane",
            "canonical",
        ],
    )
    assert r.exit_code == 0, r.output
    try:
        yield repo
    finally:
        repo.engine.dispose()
        admin = create_engine(pg_url, future=True, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.exec_driver_sql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"  # noqa: S608 — generated name
            )
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')  # noqa: S608 — generated name
        admin.dispose()


class _SizedClient:
    """A vLLM stand-in whose response body is EXACTLY _RESPONSE_BYTES long (fold 7: the spill put's spend
    quantity is then an exact Decimal at the ledger's Numeric(18,6) grain)."""

    def served_model(self) -> str:
        return "org/model"

    def next_token_logprobs_capture(self, prompt, *, model, n_logprobs, seed):
        pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(n_logprobs))
        sample = LogprobSample(prompt=prompt, generated_token_id=1000, top_logprobs=pairs)
        req = json.dumps({"model": model, "prompt": prompt, "seed": seed}).encode()
        body = {"choices": [{"logprobs": {"tokens": ["token_id:1000"]}}], "pad": ""}
        pad = _RESPONSE_BYTES - len(json.dumps(body).encode())
        body["pad"] = "x" * pad
        resp = json.dumps(body).encode()
        assert len(resp) == _RESPONSE_BYTES
        return CapturedLogprobs(
            sample=sample,
            request_body=req,
            response_body=resp,
            http_status=200,
            content_type="application/json",
        )


def _capture_kwargs(repo, backend_id, backend, **over):
    kw = dict(
        repo=repo,
        backend=backend,
        backend_id=backend_id,
        client=_SizedClient(),
        expected_lane="canonical",
        hf_repo="r",
        hf_revision="rev",
        dtype_quant="bf16",
        tokenizer_hash=b"T" * 32,
        campaign_key="a217",
        work_slug="accept",
        variant_digest="v1",
        actor_key="owner",
        origin="test",
    )
    kw.update(over)
    return kw


def _spends(repo):
    with repo.engine.connect() as conn:
        return conn.execute(
            text("SELECT quantity, rate_card_id FROM neuro.spend_entries ORDER BY spend_entry_id")
        ).all()


def test_composed_write_proves_the_invariants(canon):
    repo = canon
    backend_id, backend = resolve_capture_backend(
        repo, backend_key="artifacts-dev", connection_string=_AZURITE_CONN
    )
    # (ii) guarded AND stamped with its row
    assert isinstance(backend, QuotaGuardedBackend) and backend.backend_id == backend_id
    res = capture_logprob(**_capture_kwargs(repo, backend_id, backend))
    assert res is not None
    # (i) one ledger row per put — response spill + parquet shard + manifest = 3 (request inlines at <8KB);
    # the spill row's quantity is the EXACT Decimal 16000/10^9 with the SEEDED rate_card_id
    spends = _spends(repo)
    assert len(spends) == 3
    with repo.engine.connect() as conn:
        seeded_rc = conn.execute(text("SELECT rate_card_id FROM neuro.rate_cards")).scalar_one()
    assert all(s.rate_card_id == seeded_rc for s in spends)
    assert sum(1 for s in spends if Decimal(str(s.quantity)) == Decimal("0.000016")) == 1
    # (vi) pointer coherence: every artifact row points at the RESOLVER's backend row, and the bytes read
    # back through the SAME row-bound backend hash to the recorded sha256
    with repo.engine.connect() as conn:
        artifacts = conn.execute(text("SELECT uri, sha256, backend_id FROM neuro.artifacts")).all()
    assert artifacts and all(a.backend_id == backend_id for a in artifacts)
    for a in artifacts:
        assert hashlib.sha256(backend.get(a.uri)).digest() == bytes(a.sha256)


def test_blocked_durability_arm_stops_the_write_before_any_put(canon):
    # (iii) — composes GO-D-wal: flip the WAL arm (the newer arm) and the composed gate refuses BEFORE any
    # put -> zero spend rows (spend precedes upload, so no-spend == no-put == no blob).
    repo = canon
    backend_id, backend = resolve_capture_backend(
        repo, backend_key="artifacts-dev", connection_string=_AZURITE_CONN
    )
    _bump_wal_lag(repo.engine, ok=False, detail="acceptance: archiver fault injected")
    with pytest.raises(DurabilityGateError):
        capture_logprob(**_capture_kwargs(repo, backend_id, backend))
    assert _spends(repo) == []
    with repo.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM neuro.capture_events")).scalar_one() == 0


def test_endpoint_mismatch_fails_the_row_cross_check(canon):
    # (iv) C4: the credential's account host must match the ROW's base_uri host (10.0.0.9 != 127.0.0.1).
    bad = (
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=x;"
        "BlobEndpoint=http://10.0.0.9:10000/devstoreaccount1;"
    )
    with pytest.raises(ConfigurationError, match="endpoint mismatch"):
        resolve_capture_backend(canon, backend_key="artifacts-dev", connection_string=bad)


def test_no_credential_fails_closed_never_azurite_fallback(canon, monkeypatch):
    # (v) C5: env unset + none passed -> refuse; the deleted fallback chain can never write to localhost.
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    with pytest.raises(ConfigurationError, match="NO connection string"):
        resolve_capture_backend(canon, backend_key="artifacts-dev", connection_string=None)


def test_over_ceiling_write_is_denied_with_no_blob_and_no_spend(canon):
    # (vii) fail-safe-for-cost ordering (consult -> record -> upload): saturate the artifacts-dev group's
    # catalog usage, then the capture's FIRST put is DENIED -> no spend row, no artifact row, no blob.
    repo = canon
    backend_id, backend = resolve_capture_backend(
        repo, backend_key="artifacts-dev", connection_string=_AZURITE_CONN
    )
    ceiling = byte_ceiling_for_prefix("artifacts-dev")
    with repo.engine.begin() as conn:  # a fat catalog row: usage == ceiling (quota reads the DB catalog)
        conn.execute(
            text(
                "INSERT INTO neuro.artifacts (bundle_id, kind, backend_id, uri, sha256, size_bytes, retention) "
                "VALUES (NULL, 'other', :be, 'fat/seed', :sha, :sz, 'ttl')"
            ),
            {"be": backend_id, "sha": b"\x00" * 32, "sz": ceiling},
        )
    with pytest.raises(QuotaDeniedError):
        capture_logprob(**_capture_kwargs(repo, backend_id, backend))
    assert _spends(repo) == []  # denied BEFORE the ledger row — and therefore before any upload
    with repo.engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM neuro.artifacts")).scalar_one()
    assert n == 1  # only the fat seed row — the capture registered nothing
