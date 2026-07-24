"""The pinned recompute recipe + estimated cost — ONE repo constant for the manifest block-10 revival.

Before this, the capture recipe (HOW to re-launch the vLLM server to regenerate a derived artifact) lived
ONLY in prose / runbooks / the ``ops/e6_run.py`` driver defaults — never in a manifest, never on
``artifacts.recompute_recipe`` — so ADR-0034's block-10 recompute trigger and the phase0-Q6 "provenance
survives deletion" column (``artifacts.recompute_recipe``, ``orm.py:617`` / ``phase3-ddl.sql:405``) were both
DEAD (``build_manifest`` was never passed ``recompute_recipe`` and hard-coded ``estimated_cost`` None). This
module is the ONE home the capture path records: ``build_manifest`` block 10 + ``artifacts.recompute_recipe``
both carry ``RECOMPUTE_RECIPE_JSON``, and block 10's ``estimated_cost`` carries the D5-MEASURED figure.

⚠ This is the WAVE-1 pinned SERVER-LAUNCH recipe (the vLLM ``v0.23.0`` image + the BI-on sm_89 launch env/args
— the E6 bitwise cert substrate). It is deliberately MODEL-AGNOSTIC: the per-capture MODEL identity
(hf_repo/hf_revision/dtype_quant/serving_stack/serving_version) rides manifest block 2 (run_model_identity, the
authoritative identity), the prompt is captured VERBATIM in capture_events, and the per-request DECODING config
(n_logprobs, the request seed, the batch_invariant/runner FINGERPRINT fields) rides the fingerprint
semantic_config — so this constant records ONLY the launch provenance NOT already stored elsewhere, and never
duplicates (or lies about) a per-capture value. ⚠ Do NOT confuse the two homes: the LAUNCH ``--seed 1234`` +
``VLLM_BATCH_INVARIANT=1`` below ARE server-launch facts (kept here); the fingerprint separately records the
per-request seed + the batch_invariant decoding fact — same words, different layer. Together (block 10 launch +
block 2 identity + captured prompt + fingerprint decoding) = a full recompute. A different SUBSTRATE (a Stage-B
fp32/H100 run, or a vLLM image bump) is a DISTINCT launch recipe.
"""

from __future__ import annotations

from ..db.identity import canonical_bytes

#: The banked E6/capture SERVER-LAUNCH recipe, VERBATIM (a2-17 runbook §4 / env-gotchas). Records EXACTLY the
#: provenance NOT already authoritative elsewhere — the container image + launch env/args — so a GC'd/deleted
#: derived artifact can be regenerated (phase0 Q6; ADR-0034 block-10 recompute trigger). ⚠ DELIBERATELY EXCLUDES
#: the model identity (hf_repo/hf_revision/dtype_quant/serving_stack/serving_version — authoritative in
#: model_identities + manifest block 2 run_model_identity, with its own identity hash), the request prompt
#: (captured VERBATIM in capture_events), and the decoding (n_logprobs/seed/batch_invariant/runner — in the
#: fingerprint semantic_config). Duplicating those here would be a SECOND implementation of one concept AND a
#: latent lie for a capture whose per-call model/dtype differ from a hard-coded literal — the exact "one
#: implementation per concept" the charter names. To recompute: block 10 (this launch recipe) + block 2 (the
#: model identity) + the captured prompt + the fingerprint decoding.
RECOMPUTE_RECIPE: dict = {
    "container_image": "vllm/vllm-openai:v0.23.0",
    "env": {
        "VLLM_BATCH_INVARIANT": "1",
        "VLLM_USE_V2_MODEL_RUNNER": "0",
        "HF_HUB_OFFLINE": "1",
    },
    "server_args": [
        "--gpu-memory-utilization",
        "0.65",
        "--max-model-len",
        "2048",
        "--max-logprobs",
        "128",
        "--logprobs-mode",
        "raw_logprobs",
        "--return-tokens-as-token-ids",
        "--enforce-eager",
        "--no-enable-prefix-caching",
        "--seed",
        "1234",
    ],
    "host_port": 8001,
    "note": (
        "invariant server-LAUNCH recipe (ADR-0034 block 10); the model identity rides manifest block 2 "
        "(run_model_identity, --dtype/--revision derive from it), the prompt is verbatim in capture_events, "
        "the decoding rides the fingerprint. A different substrate (Stage-B fp32/H100, or a vLLM bump) is a "
        "DISTINCT recipe."
    ),
}

#: The D5-MEASURED per-capture recompute cost — explicit UNITS + BASIS (owner-ruled honest value; never a bare
#: guessed number, never a silent None). Measured at the ESTELA Unit-C run (log:231): 6,000 captures in 33m14s
#: over VM loopback, GPU never the bottleneck ("Running: 0 reqs" throughout; log:231/232). Excludes the
#: one-time server launch (weights load). A PROPOSAL shape — ADR-0034 is shape-silent on estimated_cost.
RECOMPUTE_ESTIMATED_COST: dict = {
    "wall_clock_seconds_per_capture": 0.33,
    "substrate": "rtx-4090-sm89",
    "gpu_bound": False,
    "basis": "measured 33m14s / 6000 captures over loopback (ESTELA Unit-C, log:231); excludes one-time launch",
}

#: The canonical-JSON STRING form written to BOTH manifest block 10 ("recipe") and ``artifacts.recompute_recipe``
#: (TEXT) — ONE source, byte-identical in both homes. Uses THE canonicalizer (``db.identity.canonical_bytes``;
#: §B "one implementation per concept"), so the recorded recipe is deterministic + recomputable from the inputs.
RECOMPUTE_RECIPE_JSON: str = canonical_bytes(RECOMPUTE_RECIPE).decode("utf-8")
