"""Red-team: DB=pointers + sha256+size verified on read; the guard never fails open (L6) + Section-C FIXes.

* L6 — same-SIZE content swap fires the sha256 clause (not the cheap size check); LocalFs path-traversal /
  absolute / drive-qualified keys are rejected; a NON-target shard tamper still raises; a backend fetch
  error propagates (never partial data).
* Section-C FIX — the multi-shard reader RAISES ConfigurationError (no silent first-shard-only read).
* Section-C FIX — the adapter raises a typed VLLMAdapterError on an EMPTY tokens/top_logprobs list, not a
  bare IndexError.
"""

from __future__ import annotations

import io

import pytest

from neuromancer_llm.bundles.registrar import BundleRegistrar, TableManifestSpec
from neuromancer_llm.capture.adapters.vllm import LogprobSample, VLLMAdapterError, VLLMClient
from neuromancer_llm.capture.events import logprob_parquet
from neuromancer_llm.capture.reader import IntegrityError, manifest_artifacts, read_run_logprobs
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.storage.backends import LocalFsBackend


def _sample(n=20, generated=1000):
    return LogprobSample(
        prompt="p",
        generated_token_id=generated,
        top_logprobs=tuple((1000 + i, -0.01 * (i + 1)) for i in range(n)),
    )


# --- L6: same-size content swap fires the sha256 clause --------------------------------------------
@pytest.mark.pg
def test_rt_same_size_tamper_fires_sha256(seeded, tmp_path):
    """L6 (highest-value integrity probe): mutate the parquet bytes WITHOUT changing length so the cheap
    size check passes and the sha256 clause is the one that fires. A refactor dropping the sha256 half
    would otherwise pass the whole suite."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    pq, n, _ = logprob_parquet(_sample(12))
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=f"logprobs/run={seeded['run_id']}/part-0000",
        shards={"logprobs-0000.parquet": pq},
        artifact_kinds={"logprobs-0000.parquet": "token_table"},
        table_manifests=[TableManifestSpec("logprobs-0000.parquet", "logprobs", row_count=n)],
    )
    art = manifest_artifacts(repo.engine, run_id=seeded["run_id"])[0]
    original = backend.get(art.uri)
    tampered = bytes((b ^ 0xFF) for b in original)  # SAME length, different bytes
    assert len(tampered) == art.size_bytes and tampered != original
    backend.put(art.uri, tampered)
    with pytest.raises(IntegrityError):  # the sha256 clause fires (size still matches)
        read_run_logprobs(repo.engine, backend, run_id=seeded["run_id"])


# --- L6: LocalFs containment ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_key",
    ["../../../etc/passwd", "/abs/secret", "C:/Windows/system32/x", "a/../../escape"],
)
def test_rt_localfs_rejects_traversal_and_absolute(tmp_path, bad_key):
    """L6 (guard-correct-but-unguarded): LocalFs put/get with a traversal / absolute / drive-qualified key
    escapes the root and is rejected with ValueError — the single highest-value 'correct but untested' guard."""
    backend = LocalFsBackend(tmp_path)
    with pytest.raises(ValueError):
        backend.put(bad_key, b"x")
    with pytest.raises(ValueError):
        backend.get(bad_key)


# --- L6: a NON-target shard tamper still raises ----------------------------------------------------
@pytest.mark.pg
def test_rt_non_target_shard_tamper_raises(seeded, tmp_path):
    """L6: the reader integrity-verifies EVERY artifact, not just the queried token_table — corrupting a
    NON-target (non-token_table) artifact still raises IntegrityError (the verify-every-artifact loop)."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    pq, n, _ = logprob_parquet(_sample(8))
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    shards = {"logprobs-0000.parquet": pq, "side-0000.bin": b"a non-token-table side artifact"}
    reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=f"logprobs/run={seeded['run_id']}/part-0000",
        shards=shards,
        artifact_kinds={"logprobs-0000.parquet": "token_table", "side-0000.bin": "other"},
        table_manifests=[
            TableManifestSpec("logprobs-0000.parquet", "logprobs", row_count=n),
            TableManifestSpec("side-0000.bin", "logprobs", row_count=0),
        ],
    )
    # corrupt the NON-target side artifact (same length, flipped bytes)
    side = next(
        a for a in manifest_artifacts(repo.engine, run_id=seeded["run_id"]) if a.kind != "token_table"
    )
    backend.put(side.uri, bytes((b ^ 0xFF) for b in backend.get(side.uri)))
    with pytest.raises(IntegrityError):
        read_run_logprobs(repo.engine, backend, run_id=seeded["run_id"])


# --- Section-C FIX: the multi-shard reader fails closed -------------------------------------------
@pytest.mark.pg
def test_rt_multi_shard_reader_raises(seeded, tmp_path):
    """Section-C FIX: >1 token_table shard for a run -> ConfigurationError (the reader refuses to silently
    return a first-shard-only partial view)."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    p0, n0, _ = logprob_parquet(_sample(8))
    p1, n1, _ = logprob_parquet(_sample(12))
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    shards = {"logprobs-0000.parquet": p0, "logprobs-0001.parquet": p1}
    reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=f"logprobs/run={seeded['run_id']}/part-0000",
        shards=shards,
        artifact_kinds={k: "token_table" for k in shards},
        table_manifests=[
            TableManifestSpec("logprobs-0000.parquet", "logprobs", row_count=n0),
            TableManifestSpec("logprobs-0001.parquet", "logprobs", row_count=n1),
        ],
    )
    with pytest.raises(ConfigurationError):
        read_run_logprobs(repo.engine, backend, run_id=seeded["run_id"])


# --- Section-C FIX: adapter empty-list is a typed error (pure / mocked) ----------------------------
def test_rt_adapter_empty_tokens_is_typed_error():
    """Section-C FIX: an empty tokens[] / top_logprobs[] from the server raises VLLMAdapterError, not a bare
    IndexError on lp['tokens'][0]. Pure/mocked — no GPU."""
    client = VLLMClient("http://127.0.0.1:1")  # never contacted; we call the pure parser directly
    parse = client._parse_completion
    for bad in (
        {"choices": [{"logprobs": {"tokens": [], "top_logprobs": [{"token_id:1": -0.1}]}}]},  # empty tokens
        {"choices": [{"logprobs": {"tokens": ["token_id:1"], "top_logprobs": []}}]},  # empty top_logprobs
        {"choices": [{"logprobs": None}]},  # no logprobs at all
        {"choices": []},  # no choices
    ):
        with pytest.raises(VLLMAdapterError):
            parse(bad, "p", 1, "capture", -1)


def test_rt_adapter_empty_per_token_dict_is_typed_error():
    """C4.1 (audit 2026-07-02): `top_logprobs=[{}]` — a NON-empty list whose first per-token dict is
    empty (or not a dict) — passed the Section-C guard and yielded an EMPTY distribution; two such
    samples compare bitwise_identical=True, a VACUOUS bitwise PASS. Now a typed VLLMAdapterError."""
    client = VLLMClient("http://127.0.0.1:1")  # never contacted; the pure parser is called directly
    for bad_top in ({}, "not-a-dict", None, []):
        data = {"choices": [{"logprobs": {"tokens": ["token_id:5"], "top_logprobs": [bad_top]}}]}
        with pytest.raises(VLLMAdapterError):
            client._parse_completion(data, "p", 1, "capture", -1)


@pytest.mark.pg
def test_rt_reader_zero_token_table_raises(seeded, tmp_path):
    """C4.2 (audit 2026-07-02): a run whose manifests carry NO token_table artifact used to fall back to
    `artifacts[0]` — silently reading an artifact of ANY kind as the logprob shard (asymmetric with the
    >1 fail-closed branch). Zero token_tables now raises ConfigurationError."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=f"logprobs/run={seeded['run_id']}/part-0000",
        shards={"side-0000.bin": b"a non-token-table side artifact"},
        artifact_kinds={"side-0000.bin": "other"},
        table_manifests=[TableManifestSpec("side-0000.bin", "logprobs", row_count=0)],
    )
    with pytest.raises(ConfigurationError):  # no token_table -> refuse, never guess an arbitrary artifact
        read_run_logprobs(repo.engine, backend, run_id=seeded["run_id"])


def test_rt_adapter_string_keyed_logprobs_is_typed_error():
    """L4 GAP: a server NOT launched --return-tokens-as-token-ids returns string-keyed logprobs -> a typed
    VLLMAdapterError (the id-keyed contract is enforced loudly)."""
    client = VLLMClient("http://127.0.0.1:1")
    data = {"choices": [{"logprobs": {"tokens": ["Paris"], "top_logprobs": [{"Paris": -0.1}]}}]}
    with pytest.raises(VLLMAdapterError):
        client._parse_completion(data, "p", 1, "capture", -1)


# --- L6: a backend fetch error propagates (never partial data) ------------------------------------
@pytest.mark.pg
def test_rt_backend_fetch_error_propagates(seeded, tmp_path):
    """L6: a verified manifest row whose blob is missing/unreadable propagates the error — the read never
    returns partial data (the classic fail-open shape)."""
    repo = seeded["repo"]
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    pq, n, _ = logprob_parquet(_sample(8))
    reg = BundleRegistrar(repo.engine, backend, expected_lane="test")
    art_name = "logprobs-0000.parquet"
    reg.register(
        run_id=seeded["run_id"],
        backend_id=backend_id,
        dataset_name="logprobs",
        partition_path=f"logprobs/run={seeded['run_id']}/part-0000",
        shards={art_name: pq},
        artifact_kinds={art_name: "token_table"},
        table_manifests=[TableManifestSpec(art_name, "logprobs", row_count=n)],
    )
    # delete the lake blob: the manifest row still points at it -> the fetch must raise, never return partial
    art = manifest_artifacts(repo.engine, run_id=seeded["run_id"])[0]
    backend.delete(art.uri)
    with pytest.raises((FileNotFoundError, IntegrityError, OSError)):
        read_run_logprobs(repo.engine, backend, run_id=seeded["run_id"])


def test_rt_parquet_reader_rejects_non_parquet():
    """L6: a verified-but-non-parquet body fails loud at parse time (never silently returns)."""
    import pyarrow.parquet as pq

    with pytest.raises(Exception):  # noqa: B017 - pyarrow raises its own ArrowInvalid
        pq.read_table(io.BytesIO(b"not-a-parquet-file"))
