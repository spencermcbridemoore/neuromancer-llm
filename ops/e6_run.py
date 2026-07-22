"""Operator driver for the E6 determinism gate — the FULL N=50 certification shape.

Runs ONE arm of the E6 protocol against a live vLLM server using the package's own harness
(neuromancer_llm.capture.determinism.run_e6). Parameterized by env so the agent can run the test arm
(VLLM_BATCH_INVARIANT=1, with one engine restart) and the control arm (flag unset) by swapping the
server in between — the two 7B servers cannot co-reside in 16 GB, so the arms run sequentially.

Env:
  E6_URL                base url (default http://127.0.0.1:8000)
  E6_ARM                "test" (batch_invariant=True) | "control" (False)   [default test]
  E6_N                  replicate count                                      [default 50]
  E6_BATCH              comma batch sizes                                    [default 1,2,4,8,16]
  E6_NLOGPROBS          top-k logprobs per pass                              [default 20]
  E6_RESTART_CONTAINER  if set AND arm==test, `docker restart <name>` at the midpoint
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from neuromancer_llm.capture.adapters.vllm import VLLMClient
from neuromancer_llm.capture.determinism import (
    E6Config,
    host_compute_capability,
    render_e6_report,
    run_e6,
)

sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252

URL = os.environ.get("E6_URL", "http://127.0.0.1:8000")
ARM = os.environ.get("E6_ARM", "test")
N = int(os.environ.get("E6_N", "50"))
BATCH = tuple(int(x) for x in os.environ.get("E6_BATCH", "1,2,4,8,16").split(","))
NLP = int(os.environ.get("E6_NLOGPROBS", "20"))
RESTART_CONTAINER = os.environ.get("E6_RESTART_CONTAINER", "")


def restart() -> None:
    print(f"[restart] docker restart {RESTART_CONTAINER}", flush=True)
    subprocess.run(["docker", "restart", RESTART_CONTAINER], check=True)
    print("[restart] issued; waiting for /health", flush=True)


def main() -> None:
    client = VLLMClient(URL, timeout=180.0)
    cc = host_compute_capability()
    model = client.served_model()
    is_test = ARM == "test"
    restart_cb = restart if (RESTART_CONTAINER and is_test) else None
    print(
        f"arm={ARM} url={URL} cc={cc} model={model} N={N} batch={list(BATCH)} "
        f"nlogprobs={NLP} restart={'yes' if restart_cb else 'no'}",
        flush=True,
    )
    cfg = E6Config(model=model, n_replicates=N, batch_sizes=BATCH, n_logprobs=NLP, seed=1234)
    result = run_e6(client, cfg, batch_invariant=is_test, restart_engine=restart_cb)
    summary = {k: v for k, v in vars(result).items() if k != "samples"}
    print("JSON_SUMMARY " + json.dumps(summary, default=str), flush=True)
    print(render_e6_report(result))


if __name__ == "__main__":
    main()
