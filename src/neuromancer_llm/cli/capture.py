"""`neuro capture` — logprob | replay | show. The Phase 4 VERTICAL SLICE is `capture logprob`.

`logprob` is a THIN delegate: it parses arguments and makes ONE call into capture.events.capture_logprob
(orchestration lives in the library, not the CLI — one implementation per concept). `replay`/`show` are
the DEFERRED read/verify half (later gate) and remain Stage-2 stubs.
"""

from __future__ import annotations

from pathlib import Path

import typer

from . import stage2

app = typer.Typer(no_args_is_help=True, help="Capture (Stage 2 vertical slice): logprob | replay | show.")


@app.command()
def logprob(
    hf_revision: str = typer.Option(..., help="pinned HF revision sha (part of model identity; ADR-0005)"),
    base_url: str = typer.Option(
        "http://127.0.0.1:8000", envvar="NEURO_VLLM_BASE_URL", help="vLLM OpenAI-compatible server base URL"
    ),
    hf_repo: str = typer.Option("mistralai/Mistral-7B-v0.3", help="HF repo (part of model identity)"),
    tokenizer_file: str | None = typer.Option(
        None, help="path to tokenizer.json — sha256'd for the durable tokenizer identity"
    ),
    tokenizer_hash: str | None = typer.Option(
        None, help="tokenizer identity hash as hex (use instead of --tokenizer-file)"
    ),
    campaign_key: str = typer.Option("phase4-capture", help="campaign display key (ADR-0037)"),
    work_slug: str = typer.Option("mcq-next-token", help="run work_slug (human-readable coordinates)"),
    variant_digest: str = typer.Option("v1", help="run variant_digest (short uniqueness suffix)"),
    actor_key: str = typer.Option("owner", help="actor stamped on the run + capture (phase0 Q13)"),
    origin: str | None = typer.Option(None, help="origin stamp (defaults to the hostname)"),
    lake_root: str = typer.Option("./_lake", help="local lake root for parquet + wire spills"),
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before any write (fail closed)",
    ),
    n_logprobs: int = typer.Option(20, help="top-k next-token logprobs to capture"),
    seed: int = typer.Option(1234, help="greedy seed (recorded in the fingerprint)"),
    dataset_name: str = typer.Option("logprobs", help="lake dataset name for the parquet shard"),
) -> None:
    """Capture one real next-token logprob pass end to end (verbatim wire + identity + parquet bundle)."""
    import socket

    from ..capture.adapters.vllm import VLLMAdapterError, VLLMClient
    from ..capture.events import capture_logprob
    from ..db.identity import sha256_bytes
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import IdentityMismatchError, Repository
    from ..db.session import make_writer_engine
    from ..storage.backends import LocalFsBackend

    try:
        if tokenizer_file is not None:
            tok_hash = sha256_bytes(Path(tokenizer_file).read_bytes())
        elif tokenizer_hash is not None:
            tok_hash = bytes.fromhex(tokenizer_hash)
        else:
            raise ConfigurationError(
                "tokenizer identity required: pass --tokenizer-file <tokenizer.json> or --tokenizer-hash <hex> "
                "(register-first identity; ADR-0005)."
            )
        engine = make_writer_engine(expected_lane=lane)
        repo = Repository(engine, expected_lane=lane)
        backend_id = repo.get_or_create_storage_backend(
            "local-lake",
            driver="local_fs",
            lane="artifacts",
            base_uri=str(Path(lake_root).resolve()),
            is_cloud=False,
        )
        backend = LocalFsBackend(lake_root)
        client = VLLMClient(base_url, timeout=120.0)
        result = capture_logprob(
            repo=repo,
            backend=backend,
            backend_id=backend_id,
            client=client,
            expected_lane=lane,
            hf_repo=hf_repo,
            hf_revision=hf_revision,
            tokenizer_hash=tok_hash,
            campaign_key=campaign_key,
            work_slug=work_slug,
            variant_digest=variant_digest,
            actor_key=actor_key,
            origin=origin or socket.gethostname(),
            n_logprobs=n_logprobs,
            seed=seed,
            dataset_name=dataset_name,
        )
    except (
        ConfigurationError,
        LaneAssertionError,
        VLLMAdapterError,
        IdentityMismatchError,
    ) as exc:
        typer.secho(f"capture failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    req_disp = "inline" if result.request_inlined else "SPILLED"
    resp_disp = "inline" if result.response_inlined else "SPILLED"
    typer.echo(f"captured run_key={result.run_key} (run_id={result.run_id})")
    typer.echo(f"  model_id={result.model_id} fingerprint_id={result.fingerprint_id}")
    typer.echo(
        f"  capture_event_id={result.capture_event_id} "
        f"request={req_disp}({result.request_bytes}B) response={resp_disp}({result.response_bytes}B)"
    )
    typer.echo(
        f"  bundle_id={result.bundle_id} parquet_rows={result.parquet_row_count} "
        f"dataset={dataset_name} partition={result.partition_path}"
    )
    typer.echo(
        f"  declared_mode={result.declared_mode} EXPECTED={result.expected_level} ({result.substrate_key})"
    )
    typer.echo(f"  semantic_config={result.semantic_config}")


@app.command()
def replay() -> None:
    """STAGE 2 (later gate) — replicate a captured run for divergence measurement."""
    stage2("capture replay")


@app.command()
def show() -> None:
    """STAGE 2 (later gate) — show a capture_events row + its spilled payloads."""
    stage2("capture show")
