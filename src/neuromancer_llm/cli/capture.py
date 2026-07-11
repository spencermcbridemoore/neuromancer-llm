"""`neuro capture` — logprob | replay | show. The Phase 4 VERTICAL SLICE is `capture logprob`.

Every command is a THIN delegate: it parses arguments and makes ONE call into the library
(orchestration lives in capture/, not the CLI — one implementation per concept). `logprob` drives one
verbatim capture end to end; `replay` closes the MEASURED divergence loop over an original + a distinct
re-invocation; `show` is the SELECT-only, integrity-verified read-back. All three are built (Stage 2
landed 2026-06-22).
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="Capture (Stage 2 vertical slice): logprob | replay | show.")


def _tokenizer_hash_bytes(tokenizer_hash: str) -> bytes:
    """Validate + decode a --tokenizer-hash value: exactly 64 hex chars -> the 32-byte sha256.

    Audit correction 2026-07-02: `bytes.fromhex` accepted ANY even-length hex — a truncated (e.g.
    2-byte) value became durable register-first tokenizer identity that raise-on-drift could never
    reconcile with the later correct hash (a silent identity split) — and malformed hex escaped the
    command's except-tuple as a raw ValueError traceback. Fail closed with the typed error instead."""
    from ..db.lanes import ConfigurationError

    value = tokenizer_hash.strip()
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ConfigurationError(
            f"--tokenizer-hash is not valid hex: {tokenizer_hash!r} (expected the 64-hex-char sha256 "
            "of tokenizer.json)."
        ) from exc
    if len(decoded) != 32:
        raise ConfigurationError(
            f"--tokenizer-hash must be a 64-hex-char sha256 (32 bytes); got {len(decoded)} byte(s) — a "
            "truncated hash would mint a durable, irreconcilable tokenizer identity (fail closed)."
        )
    return decoded


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
    backend_key: str = typer.Option(
        "local-lake",
        help="registered storage_backends row to resolve the lake through (GO-D-cost: the ONE row-bound "
        "resolver; a cloud key must be provisioned via `neuro storage seed` first)",
    ),
    lane: str = typer.Option(
        "canonical",
        envvar="NEURO_EXPECTED_LANE",
        help="expected DB lane verified before any write (fail closed)",
    ),
    n_logprobs: int = typer.Option(20, help="top-k next-token logprobs to capture"),
    seed: int = typer.Option(1234, help="greedy seed (recorded in the fingerprint)"),
    dataset_name: str = typer.Option("logprobs", help="lake dataset name for the parquet shard"),
) -> None:
    """Capture one real next-token logprob pass end to end (verbatim wire + identity + parquet bundle).

    CONTROL-PLANE op: NEURO_DATABASE_URL must be an admin DSN — the path spans neuro_registrar registry
    INSERTs and neuro_writer bundle-lifecycle UPDATEs, so neither single role suffices (BLOCK 4)."""
    import socket

    from ..capture.adapters.vllm import VLLMAdapterError, VLLMClient
    from ..capture.events import capture_logprob
    from ..db.identity import sha256_bytes
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import IdentityMismatchError, Repository
    from ..db.session import make_verified_engine
    from ..governance.health import DurabilityGateError
    from ..registry.backends import (
        LOCAL_LAKE_BACKEND_KEY,
        LOCAL_LAKE_BASE_URI,
        resolve_capture_backend,
    )
    from ..storage.quota import QuotaDeniedError

    try:
        if tokenizer_file is not None:
            tok_hash = sha256_bytes(Path(tokenizer_file).read_bytes())
        elif tokenizer_hash is not None:
            tok_hash = _tokenizer_hash_bytes(tokenizer_hash)
        else:
            raise ConfigurationError(
                "tokenizer identity required: pass --tokenizer-file <tokenizer.json> or --tokenizer-hash <hex> "
                "(register-first identity; ADR-0005)."
            )
        engine = make_verified_engine(expected_lane=lane)
        repo = Repository(engine, expected_lane=lane)
        # The local-lake row is self-bootstrapping (fold 12: ONLY for the local key — a cloud key must be
        # provisioning-seeded, and the resolver fails closed on its absence, never a side-effect INSERT).
        if backend_key == LOCAL_LAKE_BACKEND_KEY:
            repo.get_or_create_storage_backend(
                LOCAL_LAKE_BACKEND_KEY,
                driver="local_fs",
                lane="artifacts",
                base_uri=LOCAL_LAKE_BASE_URI,
                is_cloud=False,
            )
        backend_id, backend = resolve_capture_backend(repo, backend_key=backend_key, local_root=lake_root)
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
        QuotaDeniedError,
        DurabilityGateError,
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
    backend_key: str = typer.Option(
        "local-lake",
        help="registered storage_backends row to resolve the lake through (GO-D-cost: the ONE row-bound "
        "resolver; a cloud key must be provisioned via `neuro storage seed` first)",
    ),
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
    EXPECTED reproducibility rule (a divergence on a bitwise lane fails loud, never passes silently).

    CONTROL-PLANE op: NEURO_DATABASE_URL must be an admin DSN (spans registrar INSERTs + writer UPDATEs)."""
    import socket

    from ..capture.adapters.vllm import VLLMAdapterError, VLLMClient
    from ..capture.determinism import DivergenceVerdictError
    from ..capture.events import capture_logprob, replicate_and_measure
    from ..composer import new_invocation_id
    from ..db.identity import sha256_bytes
    from ..db.lanes import ConfigurationError, LaneAssertionError
    from ..db.repository import IdentityMismatchError, Repository
    from ..db.session import make_verified_engine
    from ..governance.health import DurabilityGateError
    from ..registry.backends import (
        LOCAL_LAKE_BACKEND_KEY,
        LOCAL_LAKE_BASE_URI,
        resolve_capture_backend,
    )
    from ..storage.quota import QuotaDeniedError

    try:
        if tokenizer_file is not None:
            tok_hash = sha256_bytes(Path(tokenizer_file).read_bytes())
        elif tokenizer_hash is not None:
            tok_hash = _tokenizer_hash_bytes(tokenizer_hash)
        else:
            raise ConfigurationError(
                "tokenizer identity required: pass --tokenizer-file <tokenizer.json> or --tokenizer-hash <hex> "
                "(register-first identity; ADR-0005)."
            )
        engine = make_verified_engine(expected_lane=lane)
        repo = Repository(engine, expected_lane=lane)
        # fold 12: self-bootstrap ONLY the local key; a cloud key is provisioning-seeded (resolver fails
        # closed on absence — never a side-effect INSERT).
        if backend_key == LOCAL_LAKE_BACKEND_KEY:
            repo.get_or_create_storage_backend(
                LOCAL_LAKE_BACKEND_KEY,
                driver="local_fs",
                lane="artifacts",
                base_uri=LOCAL_LAKE_BASE_URI,
                is_cloud=False,
            )
        backend_id, backend = resolve_capture_backend(repo, backend_key=backend_key, local_root=lake_root)
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
        QuotaDeniedError,
        DurabilityGateError,
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

    # C7: a consumption surface should use the SELECT-only role. Warn LOUDLY if neither --reader-url nor
    # NEURO_READER_DATABASE_URL is set, so a reader can't silently fall back to a privileged DSN.
    if reader_url is None:
        typer.secho(
            "warning: no reader DSN (--reader-url / NEURO_READER_DATABASE_URL unset) — falling back to "
            "NEURO_DATABASE_URL, which may connect as a PRIVILEGED role. Set NEURO_READER_DATABASE_URL to a "
            "neuro_reader (SELECT-only) DSN for a least-privilege read (phase0 Q12).",
            fg=typer.colors.YELLOW,
            err=True,
        )

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
