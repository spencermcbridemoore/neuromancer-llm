"""E6 determinism gate (capture contract §6) — the gpu-marked driver test. STAGE 2.

This drives the real E6 harness (capture/determinism.py over the vLLM HTTP adapter) against a *running*
containerized vLLM server. It is `gpu`-marked, so it skips visibly off-GPU via the conftest detection
hook; it ALSO skips visibly when no server is reachable, so it never hard-fails in a dev/CI context that
has a GPU but no live server.

Property asserted: under `VLLM_BATCH_INVARIANT=1` the next-token logprob arrays are bitwise-identical
across varied batch sizes (the gate PASS criterion). Point it at the BI-on server with
`NEURO_VLLM_BASE_URL` (default http://127.0.0.1:8000).

Non-vacuity (the control arm) is asserted only when a *second*, no-flag server is provided via
`NEURO_VLLM_CONTROL_BASE_URL` — then this test also confirms divergence APPEARS there, proving the
protocol exercises the batch-nondeterminism. The full N=50 + engine-restart + control protocol (the
authoritative gate run) is driven by the operator using the same `run_e6` harness; this test is the
committed, fast regression driver.
"""

from __future__ import annotations

import os

import pytest

from neuromancer_llm.capture.adapters.vllm import VLLMClient
from neuromancer_llm.capture.determinism import (
    MIN_COMPUTE_CAPABILITY,
    E6Config,
    host_compute_capability,
    run_e6,
)

pytestmark = pytest.mark.gpu

_BASE_URL = os.environ.get("NEURO_VLLM_BASE_URL", "http://127.0.0.1:8000")
_CONTROL_URL = os.environ.get("NEURO_VLLM_CONTROL_BASE_URL")


def _reachable_or_skip(url: str) -> VLLMClient:
    client = VLLMClient(url, timeout=120.0)
    if not client.is_ready():
        pytest.skip(f"no reachable vLLM server at {url} (set NEURO_VLLM_BASE_URL); skipped visibly")
    return client


def test_e6_logprobs_are_batch_invariant():
    client = _reachable_or_skip(_BASE_URL)
    if host_compute_capability() < MIN_COMPUTE_CAPABILITY:
        pytest.skip(f"compute capability < {MIN_COMPUTE_CAPABILITY}; E6 gate not applicable here")

    model = client.served_model()
    # A fast driver shape (12 replicates across 4 batch sizes); the operator run uses the full N=50.
    config = E6Config(model=model, n_replicates=12, batch_sizes=(1, 2, 4, 8))
    result = run_e6(client, config, batch_invariant=True)

    # NON-VACUITY (always asserted): the protocol must have actually exercised the batch-size variation a
    # PASS depends on. Without these guards a bitwise PASS could come from running too few / single-batch
    # samples — vacuously "invariant". (This replaces the old behaviour of only checking non-vacuity when
    # a control server was configured; the regression driver can no longer silently go vacuous.)
    assert result.n_samples == config.n_replicates, (
        f"E6 collected {result.n_samples} samples, expected {config.n_replicates} — protocol under-ran"
    )
    distinct_batch_sizes = {s.batch_size for s in result.samples}
    assert len(distinct_batch_sizes) > 1, (
        f"E6 ran only batch size(s) {sorted(distinct_batch_sizes)} — a bitwise PASS would be vacuous "
        "without varied batches"
    )

    assert result.passed, (
        f"E6 FAIL: {result.distinct_signatures} distinct logprob signatures across batch sizes "
        f"{sorted(distinct_batch_sizes)}; first divergence {result.first_divergence}; "
        f"max|Δlogprob|={result.max_abs_diff:.3e}, max relΔ={result.max_rel_diff:.3e}"
    )
    assert result.argmax_flips == 0, "greedy argmax flipped across batch sizes under BI=1"

    # Stronger non-vacuity when a no-flag control server is provided: it MUST show divergence under the
    # same protocol, proving the protocol can detect the batch-nondeterminism the test arm rules out.
    if _CONTROL_URL:
        control_client = _reachable_or_skip(_CONTROL_URL)
        control = run_e6(control_client, config, batch_invariant=False)
        assert control.divergence_appeared, (
            "control arm (flag unset) showed NO divergence — the protocol is not exercising the "
            "batch-nondeterminism, so the test arm's PASS would be vacuous"
        )
