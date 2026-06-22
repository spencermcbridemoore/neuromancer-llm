"""`neuro capture` — logprob | replay | show. The Phase 4 VERTICAL SLICE is `capture logprob`.

`logprob` is a THIN delegate: it parses arguments and makes ONE call into capture.events.capture_logprob
(orchestration lives in the library, not the CLI — one implementation per concept). `replay`/`show` are
the DEFERRED read/verify half (later gate) and remain Stage-2 stubs.
"""

from __future__ import annotations

from pathlib import Path

import typer

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
def replay(
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
    """Replicate a capture for divergence measurement (ADR-0004 MEASURED): capture the experiment, capture a
    DISTINCT re-invocation, measure divergence with the registered method, and assert MEASURED meets the
    EXPECTED reproducibility rule (a divergence on a bitwise lane fails loud, never passes silently)."""
    import socket

    from ..capture.adapters.vllm import VLLMAdapterError, VLLMClient
    from ..capture.determinism import DivergenceVerdictError
    from ..capture.events import capture_logprob, replicate_and_measure
    from ..composer import new_invocation_id
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
        stamp = origin or socket.gethostname()

        def _capture(invocation_id):
            # the original (invocation_id=None) and the replicate (a fresh re-invocation) share everything
            # except the run identity — same fingerprint, distinct run (ADR-0005).
            return capture_logprob(
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
                origin=stamp,
                n_logprobs=n_logprobs,
                seed=seed,
                dataset_name=dataset_name,
                invocation_id=invocation_id,
            )

        original = _capture(None)
        replicate = _capture(new_invocation_id())
        measured = replicate_and_measure(repo=repo, original=original, replicate=replicate)
    except (
        ConfigurationError,
        LaneAssertionError,
        VLLMAdapterError,
        IdentityMismatchError,
        DivergenceVerdictError,
    ) as exc:
        typer.secho(f"replay failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"replicated original run_id={measured.original_run_id} -> replicate run_id={measured.replicate_run_id}"
    )
    typer.echo(
        f"  replicate_link_id={measured.replicate_link_id} method_version_id={measured.method_version_id}"
    )
    typer.echo(
        f"  divergence: max_abs_diff={measured.max_abs_diff} max_rel_diff={measured.max_rel_diff} "
        f"argmax_flip_rate={measured.argmax_flip_rate} near_tie_margin_nats={measured.near_tie_margin_nats}"
    )
    typer.echo(
        f"  EXPECTED={measured.expected_level} bitwise_identical={measured.bitwise_identical} "
        f"meets_expected={measured.meets_expected}"
    )


@app.command()
def show(
    run_id: int = typer.Option(..., help="run_id whose captured logprobs to read back from the lake"),
    reader_url: str | None = typer.Option(
        None,
        envvar="NEURO_READER_DATABASE_URL",
        help="SELECT-only (neuro_reader) DSN; falls back to NEURO_DATABASE_URL",
    ),
    lake_root: str = typer.Option("./_lake", help="local lake root the parquet was written to"),
    dataset_name: str = typer.Option("logprobs", help="dataset to read back"),
    lane: str | None = typer.Option(
        None, envvar="NEURO_EXPECTED_LANE", help="optional: positively confirm the DB lane before reading"
    ),
) -> None:
    """Read a captured run's logprobs from the lake AS A SELECT-ONLY CONSUMER: locate the parquet via
    table_manifests/artifacts, integrity-verify (sha256+size) against the manifest, and DuckDB-read it."""
    from ..capture.reader import IntegrityError, read_run_logprobs
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.session import make_reader_engine
    from ..storage.backends import LocalFsBackend

    try:
        engine = make_reader_engine(reader_url, expected_lane=lane)
        backend = LocalFsBackend(lake_root)
        result = read_run_logprobs(engine, backend, run_id=run_id, dataset_name=dataset_name)
    except (ConfigurationError, LaneAssertionError, IntegrityError) as exc:
        typer.secho(f"read failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"run_id={result.run_id} dataset={result.dataset_name}")
    typer.echo(
        f"  integrity-verified {result.integrity_verified}/{len(result.artifacts)} artifact(s) (sha256+size)"
    )
    for a in result.artifacts:
        typer.echo(f"    {a.kind} {a.uri} ({a.size_bytes}B, rows={a.row_count})")
    typer.echo(
        f"  generated token via DuckDB: token_id={result.generated_token_id} "
        f"logprob={result.generated_logprob} (of {result.total_rows} candidates) from {result.queried_uri}"
    )
