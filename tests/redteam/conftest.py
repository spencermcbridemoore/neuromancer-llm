"""Shared helpers for the PERMANENT red-team suite (Phase 5).

Each probe in tests/redteam/ takes ONE binding-lesson row (L1-L13 + L-SB storage-backend identity) or a
concurrency recipe and asserts the system FAILS CLOSED with the exact typed exception (or, for the queue
mutation paths, the owner's quiet-bool contract — KEEP the boolean, ADR/decision of record 2026-06-23).

The fixtures here are thin reuse helpers over the session conftest (repo / seeded / engine /
provisioned_roles / role_url). The pure-function helpers (consistent fingerprint hashing, role-statement
allow/deny) are exposed through the `rt` namespace fixture so every probe shares ONE implementation
(L13: one implementation per concept) instead of re-deriving the canonical hash in each file.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from neuromancer_llm.capture.adapters.vllm import CapturedLogprobs, LogprobSample
from neuromancer_llm.db.identity import fingerprint_hash
from neuromancer_llm.storage.backends import LocalFsBackend


def _register_consistent_fingerprint(
    repo, *, config_text: str, model_id: int, declared_mode: str = "greedy"
) -> int:
    """Register a fingerprint whose hash is the CANONICAL hash of its semantic_config (post-FIX #4 the
    registry recomputes + compares, so a probe must hand it a consistent (hash, config) pair). Reuses the
    ONE canonical fingerprint_hash fn — never a second hash impl (L13)."""
    return repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash(config_text),
        model_id=model_id,
        declared_mode=declared_mode,
        semantic_config=config_text,
    )


def _seed_tok_model(
    repo,
    *,
    tokenizer_hash: bytes = b"rt-tok",
    hf_repo: str = "r",
    hf_revision: str = "rev",
    dtype_quant: str = "bf16",
    serving_stack: str = "vllm",
    serving_version: str = "0.23.0",
    arch_family: str = "llama",
) -> tuple[int, int]:
    """Register-first a tokenizer THEN a model identity (post-FIX #9 the model derives its tokenizer_id
    from the tokenizer_hash, so the tokenizer must exist first). Returns (tokenizer_id, model_id)."""
    tok = repo.register_tokenizer_identity(tokenizer_hash=tokenizer_hash, hf_repo=hf_repo)
    mid = repo.register_model_identity(
        hf_repo=hf_repo,
        hf_revision=hf_revision,
        dtype_quant=dtype_quant,
        tokenizer_hash=tokenizer_hash,
        serving_stack=serving_stack,
        serving_version=serving_version,
        arch_family=arch_family,
    )
    return tok, mid


def _exec_as(role_url, sql: str, params: dict | None = None) -> bool:
    """Run a statement as `role`. Return True if it committed, False ONLY on a permission denial; any other
    error (constraint/FK/etc.) re-raises so a mis-set-up probe can't masquerade as 'denied' (sharpens the
    role tests that asserted only 'a denial happened' without proving WHERE — Panel #1 C12)."""
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
def rt():
    """The shared red-team helper namespace (one implementation per concept; L13)."""
    return SimpleNamespace(
        fingerprint=_register_consistent_fingerprint,
        seed_tok_model=_seed_tok_model,
        exec_as=_exec_as,
        fingerprint_hash=fingerprint_hash,
    )


def rt_sample(n: int = 20, generated: int = 1000, base: int = 1000) -> LogprobSample:
    """A peaked id-keyed distribution (descending logprobs); argmax = `generated`."""
    pairs = tuple((base + i, -0.01 * (i + 1)) for i in range(n))
    return LogprobSample(prompt="p", generated_token_id=generated, top_logprobs=pairs)


class _FakeClient:
    """A vLLM stand-in returning precomputed CapturedLogprobs so the REAL capture + measure path runs
    without a GPU. The wire bytes are synthetic-but-verbatim (stored as captured)."""

    def __init__(self, samples: list[LogprobSample]) -> None:
        self._samples = samples
        self._calls = 0

    def served_model(self) -> str:
        return "fake/Mistral-7B-v0.3"

    def next_token_logprobs_capture(self, prompt, *, model, n_logprobs, seed) -> CapturedLogprobs:
        s = self._samples[self._calls]
        self._calls += 1
        req = json.dumps(
            {"model": model, "prompt": prompt, "max_tokens": 1, "temperature": 0, "seed": seed}
        ).encode("utf-8")
        top = {f"token_id:{t}": lp for t, lp in s.top_logprobs}
        resp = json.dumps(
            {
                "choices": [
                    {"logprobs": {"tokens": [f"token_id:{s.generated_token_id}"], "top_logprobs": [top]}}
                ]
            }
        ).encode("utf-8")
        return CapturedLogprobs(
            sample=s, request_body=req, response_body=resp, http_status=200, content_type="application/json"
        )


@pytest.fixture
def rt_capture():
    """Capture ONE real logprob pass through the REAL capture path with a fake client (no GPU). Returns a
    function (repo, tmp_path, sample, **over) -> LogprobCaptureResult."""
    from neuromancer_llm.capture.events import capture_logprob

    def _cap(repo, tmp_path, sample, **over):
        backend = LocalFsBackend(tmp_path)
        backend_id = repo.get_or_create_storage_backend(
            "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
        )
        kw = dict(
            repo=repo,
            backend=backend,
            backend_id=backend_id,
            client=_FakeClient([sample]),
            expected_lane="test",
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
        kw.update(over)
        return capture_logprob(**kw)

    return _cap
