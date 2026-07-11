"""GO-D-consult (A2-11b): the fail-closed durability interlock at the registrar + capture write choke points.

The consult (assert_durability_ok) gates CANONICAL cloud writes on system_health['backup_freshness'] (ADR-0020,
owner-ruled canonical-lane-only 2026-07-07). Probes: a stale/flipped interlock RAISES before ANY registrar blob
put and before the first capture durable write; an already-durable registrar resume is NOT blocked by a flip;
and a NON-canonical (test-lane) write is NOT gated (the ruled lane scope). notify is not exercised here (the
seeded 'blocked' state hits branch 4, which does not flip/notify).
"""

from __future__ import annotations

import json
import uuid as _uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from neuromancer_llm.bundles.registrar import BundleRegistrar
from neuromancer_llm.capture.adapters.vllm import CapturedLogprobs, LogprobSample
from neuromancer_llm.capture.events import capture_logprob
from neuromancer_llm.db.canonical_instance import CANONICAL_INSTANCE_UUID
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.db.provision import provision
from neuromancer_llm.db.repository import Repository
from neuromancer_llm.governance.durability import WAL_LAG_ROW, seed_row
from neuromancer_llm.governance.freshness import BACKUP_FRESHNESS_KEY, seed_backup_freshness
from neuromancer_llm.governance.health import DurabilityGateError
from neuromancer_llm.governance.probes import _bump_freshness
from neuromancer_llm.governance.wal_archiving import _bump_wal_lag
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg


@pytest.fixture
def canonical_url(pg_url):
    """An isolated DB provisioned as the CANONICAL lane (identity uuid pinned to the repo constant) so a
    canonical-lane Repository/BundleRegistrar verifies and the A2-11b consult FIRES. Created + dropped per
    test — the drill needs the canonical lane the interlock actually gates, which the shared 'test' DB is not.
    """
    name = f"canon_a2_11b_{_uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_url, future=True, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')  # noqa: S608 — generated hex, not user input
    admin.dispose()
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    ddl = Path("tests/reference/phase3-ddl.sql").read_text(encoding="utf-8")
    eng = create_engine(url, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(ddl.replace("%", "%%"))  # frozen schema (exec_driver_sql -> escape literal %)
    with eng.connect() as c:
        provision(c, lane="canonical")
        # pin the identity to the repo constant so verify_engine (canonical -> the pin) accepts the engine;
        # there is no immutability trigger on instance_uuid (A2-9 verification).
        c.execute(
            text("UPDATE neuro.database_identity SET instance_uuid = :u"),
            {"u": str(CANONICAL_INSTANCE_UUID)},
        )
        c.commit()
    eng.dispose()
    try:
        yield url
    finally:
        admin = create_engine(pg_url, future=True, isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.exec_driver_sql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"  # noqa: S608 — generated name
            )
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')  # noqa: S608 — generated name
        admin.dispose()


class _RecordingBackend:
    """Wrap a real backend, recording put keys so a probe can assert the interlock blocked BEFORE any put."""

    def __init__(self, inner: LocalFsBackend) -> None:
        self._inner = inner
        self.driver = inner.driver  # GO-D-cost C1: the choke points classify by driver (missing = refused)
        self.puts: list[str] = []

    def put(self, key: str, data: bytes) -> str:
        self.puts.append(key)
        return self._inner.put(key, data)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def delete(self, key: str) -> None:
        self._inner.delete(key)


class _FakeClient:
    """A vLLM stand-in so the capture path runs GPU-free; the consult fires before any durable write."""

    def served_model(self) -> str:
        return "fake/Mistral-7B-v0.3"

    def next_token_logprobs_capture(self, prompt, *, model, n_logprobs, seed) -> CapturedLogprobs:
        pairs = tuple((1000 + i, -0.01 * (i + 1)) for i in range(n_logprobs))
        sample = LogprobSample(prompt=prompt, generated_token_id=1000, top_logprobs=pairs)
        req = json.dumps(
            {"model": model, "prompt": prompt, "max_tokens": 1, "temperature": 0, "seed": seed}
        ).encode("utf-8")
        top = {f"token_id:{t}": lp for t, lp in pairs}
        resp = json.dumps(
            {
                "choices": [
                    {"logprobs": {"tokens": [f"token_id:{sample.generated_token_id}"], "top_logprobs": [top]}}
                ]
            }
        ).encode("utf-8")
        return CapturedLogprobs(
            sample=sample,
            request_body=req,
            response_body=resp,
            http_status=200,
            content_type="application/json",
        )


def _seed_interlock(engine, *, healthy: bool) -> None:
    # BOTH ADR-0020 arms (GO-D-wal §10 MUST-FIX 1): the consult composes backup_freshness AND wal_lag, so a
    # healthy interlock must heal both. The WAL heal is SYNTHETIC (_bump_wal_lag) — the archiving-OFF test PG
    # cannot produce a healthy real archiver read. blocked keeps both born-'blocked' (backup blocks first).
    with engine.connect() as conn:
        seed_backup_freshness(conn)  # fail-closed birth: status='blocked', measured_at=epoch
        seed_row(conn, WAL_LAG_ROW)
    if healthy:
        _bump_freshness(engine, ok=True, detail="test provisioning: healthy backup")
        _bump_wal_lag(engine, ok=True, detail="test provisioning: healthy archiver")


def _flip_blocked(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.system_health SET status='blocked' WHERE health_key=:k"),
            {"k": BACKUP_FRESHNESS_KEY},
        )


def _canonical_repo(url) -> Repository:
    return Repository(create_engine(url, future=True), expected_lane="canonical")


def _run_and_backend(repo, tmp_path):
    actor_id = repo.create_actor("worker:consult", kind="scheduled_worker")
    campaign_id = repo.create_campaign("c-consult", actor_id)
    run_id = repo.create_run(
        "c-consult/slug/dig",
        campaign_id=campaign_id,
        work_slug="slug",
        variant_digest="dig",
        actor_id=actor_id,
    )
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    return run_id, backend_id


# ---- registrar side (canonical): the consult gates the W1-W5 cloud writes ----------------------------


def test_registrar_canonical_consult_blocks_before_any_put(canonical_url, tmp_path):
    repo = _canonical_repo(canonical_url)
    _seed_interlock(repo.engine, healthy=False)  # interlock BLOCKED
    run_id, backend_id = _run_and_backend(repo, tmp_path)
    rec = _RecordingBackend(LocalFsBackend(tmp_path))
    reg = BundleRegistrar(repo.engine, rec, expected_lane="canonical")
    with pytest.raises(DurabilityGateError):
        reg.register(
            run_id=run_id,
            backend_id=backend_id,
            dataset_name="logprobs",
            partition_path="logprobs/run=x/part-0000",
            shards={"s-0000.parquet": b"payload-A"},
        )
    assert rec.puts == []  # BLOCKED before the first cloud BLOB write — no blob put, no seal (Option A gates
    #                        the W1-W5 blob writes; the blob-less 'unsealed' bundles row is GC-reapable)


def test_registrar_canonical_healthy_registers_then_resume_survives_flip(canonical_url, tmp_path):
    repo = _canonical_repo(canonical_url)
    _seed_interlock(repo.engine, healthy=True)
    run_id, backend_id = _run_and_backend(repo, tmp_path)
    rec = _RecordingBackend(LocalFsBackend(tmp_path))
    reg = BundleRegistrar(repo.engine, rec, expected_lane="canonical")
    kw = dict(
        run_id=run_id,
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path="logprobs/run=x/part-0000",
        shards={"s-0000.parquet": b"payload-A"},
    )
    bundle_id = reg.register(**kw)  # healthy -> registers
    assert reg.bundle_state(bundle_id) == "registered"
    assert rec.puts  # blobs were written
    # the interlock now FLIPS blocked; a resume of the SAME already-durable bundle must NOT be gated (Option A)
    _flip_blocked(repo.engine)
    assert reg.register(**kw) == bundle_id  # idempotent resume, not blocked
    assert reg.bundle_state(bundle_id) == "registered"


# ---- capture side (canonical): the consult gates BEFORE the first durable write ----------------------


def test_capture_canonical_consult_blocks_before_first_durable_write(canonical_url, tmp_path):
    repo = _canonical_repo(canonical_url)
    _seed_interlock(repo.engine, healthy=False)  # interlock BLOCKED
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    with pytest.raises(DurabilityGateError):
        capture_logprob(
            repo=repo,
            backend=LocalFsBackend(tmp_path),
            backend_id=backend_id,
            client=_FakeClient(),
            expected_lane="canonical",
            hf_repo="r",
            hf_revision="rev",
            tokenizer_hash=b"T",
            campaign_key="c",
            work_slug="slug",
            variant_digest="v1",
            actor_key="owner",
            origin="test",
            n_logprobs=20,
            seed=1234,
        )
    with repo.engine.connect() as conn:
        # blocked BEFORE the first durable write -> no tokenizer identity was registered
        assert conn.execute(text("SELECT count(*) FROM neuro.tokenizer_identities")).scalar_one() == 0


# ---- lane scope: a NON-canonical (test-lane) write is NOT gated (ADR-0020 governs canonical only) -----


def test_test_lane_register_is_not_gated(seeded, tmp_path):
    repo = seeded["repo"]
    _seed_interlock(repo.engine, healthy=False)  # BLOCKED — but this is the 'test' lane
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    reg = BundleRegistrar(repo.engine, LocalFsBackend(tmp_path), expected_lane="test")
    bundle_id = reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path="logprobs/run=t/part-0000",
        shards={"s-0000.parquet": b"payload-T"},
    )
    assert reg.bundle_state(bundle_id) == "registered"  # NOT gated on the test lane


def test_test_lane_capture_is_not_gated(repo, tmp_path, rt_capture):
    _seed_interlock(repo.engine, healthy=False)  # BLOCKED — but rt_capture runs on the 'test' lane
    sample = LogprobSample(
        prompt="p",
        generated_token_id=1000,
        top_logprobs=tuple((1000 + i, -0.01 * (i + 1)) for i in range(20)),
    )
    res = rt_capture(repo, tmp_path, sample)  # test lane -> consult skipped -> capture completes
    assert res is not None


# ---- capture side: the consult trusts the VERIFIED lane, not the free expected_lane param -------------


def _canonical_capture_kwargs(repo, tmp_path, *, expected_lane):
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    return dict(
        repo=repo,
        backend=LocalFsBackend(tmp_path),
        backend_id=backend_id,
        client=_FakeClient(),
        expected_lane=expected_lane,
        hf_repo="r",
        hf_revision="rev",
        tokenizer_hash=b"T",
        campaign_key="c",
        work_slug="slug",
        variant_digest="v1",
        actor_key="owner",
        origin="test",
        n_logprobs=20,
        seed=1234,
    )


def test_capture_canonical_healthy_completes(canonical_url, tmp_path):
    # a HEALTHY canonical capture is NOT wrongly blocked — both the capture consult and the registrar consult
    # pass, and the full slice completes (the symmetric positive to the blocked probe).
    repo = _canonical_repo(canonical_url)
    _seed_interlock(repo.engine, healthy=True)
    res = capture_logprob(**_canonical_capture_kwargs(repo, tmp_path, expected_lane="canonical"))
    assert res is not None


def test_capture_lane_param_mismatch_fails_closed_before_any_write(canonical_url, tmp_path):
    # the consult trusts the VERIFIED lane, not the free param: a CANONICAL repo called with
    # expected_lane='test' must fail CLOSED before ANY durable write — else it skips the canonical consult and
    # writes canonical identity/fingerprint rows ungated (the internal registrar re-verify only errors later,
    # AFTER those writes). RED without the fold: events.py would trust the param, skip the consult, write a
    # tokenizer row, then raise a LaneAssertionError at the internal registrar (wrong type, count == 1).
    repo = _canonical_repo(canonical_url)
    _seed_interlock(repo.engine, healthy=True)  # healthy: the lane MISMATCH itself is the refusal
    with pytest.raises(ConfigurationError):
        capture_logprob(**_canonical_capture_kwargs(repo, tmp_path, expected_lane="test"))
    with repo.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM neuro.tokenizer_identities")).scalar_one() == 0
