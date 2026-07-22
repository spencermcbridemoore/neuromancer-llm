"""Unit runs-show — db/run_report.py (the first read module) + the `neuro runs show` thin delegate.

Two probe classes, matching the CLI-unit precedent (test_cli_importer.py) and BEATING it:
* CliRunner NEGATIVES with NO database — the selector usage error resolves before any DB is opened.
* CliRunner END-TO-END on real Postgres (pg) — not-found -> exit 1 and a full render, which the importer CLI
  unit did not have (it disclosed a Repository-layer-only residual). Here the whole read path runs through the CLI.
Plus library-module probes on a fixture DB: the found/unlabeled/NULL-fingerprint/labeled paths, the counts
DE-BLINDED by a second run with a DIFFERENT count, run_key lookup, RunNotFoundError, and the payload discipline
(a seeded restricted payload must appear NOWHERE in the report — events are counts, value_json is a byte pointer).
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from neuromancer_llm.cli.runs import app as runs_app
from neuromancer_llm.db.run_report import RunNotFoundError, build_run_report

_runner = CliRunner()

# The render bound on an identity digest (db/run_report.py: `.hex()[:16]`).
_HEX_BOUND = 16


def _tokenizer_hash(tag: str) -> bytes:
    """A REAL 32-byte sha256-shaped tokenizer digest = 64 hex chars.

    THE LENGTH IS LOAD-BEARING (precedent 20). The original stand-in `b"tok-m1"` is 6 bytes = 12 hex chars,
    SHORTER than the 16-char render bound, so `.hex()[:16]` was a no-op on it and a mutation deleting the bound
    could not have reddened — the 'renders a BOUNDED pointer' criterion would have been prose."""
    return hashlib.sha256(f"tokenizer-{tag}".encode()).digest()


# --- seed helpers (raw SQL; the run_report SQL reads what these write) ------------------------------
def _seed_model(conn, *, tag: str = "m1") -> int:
    """The model_id alone, for probes that do not care which tokenizer row it bound."""
    return _seed_model_and_tokenizer(conn, tag=tag)[0]


def _seed_model_and_tokenizer(conn, *, tag: str = "m1") -> tuple[int, int]:
    """(model_id, tokenizer_id) — both ids RETURNED by the DB, never inferred from insertion order."""
    tok = conn.execute(
        text("INSERT INTO neuro.tokenizer_identities (tokenizer_hash) VALUES (:h) RETURNING tokenizer_id"),
        {"h": _tokenizer_hash(tag)},
    ).scalar_one()
    model_id = conn.execute(
        text(
            "INSERT INTO neuro.model_identities (identity_hash, hf_repo, hf_revision, dtype_quant, "
            "tokenizer_id, serving_stack, serving_version, arch_family) "
            "VALUES (:ih, :repo, :rev, :dq, :tok, :ss, :sv, :arch) RETURNING model_id"
        ),
        {
            "ih": f"model-{tag}".encode(),
            "repo": "mistralai/Mistral-7B-v0.3",
            "rev": "caa1feb0",
            "dq": "bf16",
            "tok": tok,
            "ss": "vllm",
            "sv": "0.23.0",
            "arch": "llama",
        },
    ).scalar_one()
    return int(model_id), int(tok)


def _label_run(conn, run_id: int, model_id: int, *, tag: str = "01") -> int:
    fp = conn.execute(
        text(
            "INSERT INTO neuro.fingerprints (fingerprint_hash, model_id, declared_mode, semantic_config) "
            "VALUES (:h, :m, 'greedy', :sc) RETURNING fingerprint_id"
        ),
        {"h": f"fp-hash-{tag}".encode(), "m": model_id, "sc": "canonical-semantic-config"},
    ).scalar_one()
    conn.execute(text("UPDATE neuro.runs SET fingerprint_id = :f WHERE run_id = :r"), {"f": fp, "r": run_id})
    return fp


def _seed_decoy_identities(conn, *, tokenizers: int, models: int, fingerprints: int) -> None:
    """Advance tokenizer_identities / model_identities / fingerprints by DIFFERENT amounts.

    `TRUNCATE ... RESTART IDENTITY` makes the first row of every table pk=1, and these three advance in
    LOCKSTEP when a helper mints one of each — so equal decoy counts re-collide at the next integer and the
    probe goes blind to which id landed in which field (env-gotchas, three consecutive ranks). Different counts
    per table is the only shape that separates them."""
    for i in range(tokenizers):
        conn.execute(
            text("INSERT INTO neuro.tokenizer_identities (tokenizer_hash) VALUES (:h)"),
            {"h": _tokenizer_hash(f"decoy-tok-{i}")},
        )
    for i in range(models):
        decoy_model = _seed_model(conn, tag=f"decoy-model-{i}")
        if i < fingerprints:
            conn.execute(
                text(
                    "INSERT INTO neuro.fingerprints (fingerprint_hash, model_id, declared_mode, "
                    "semantic_config) VALUES (:h, :m, 'greedy', 'decoy-config')"
                ),
                {"h": f"decoy-fp-{i}".encode(), "m": decoy_model},
            )


def _mark_unlabeled(conn, run_id: int) -> None:
    conn.execute(text("UPDATE neuro.runs SET is_unlabeled = true WHERE run_id = :r"), {"r": run_id})


def _seed_backend(conn) -> int:
    return conn.execute(
        text(
            "INSERT INTO neuro.storage_backends (backend_key, driver, lane, base_uri, is_cloud) "
            "VALUES ('desktop-nvme', 'local_fs', 'artifacts', 'file://lake', false) RETURNING backend_id"
        )
    ).scalar_one()


def _seed_artifact(conn, backend_id: int, uri: str) -> int:
    return conn.execute(
        text(
            "INSERT INTO neuro.artifacts (kind, backend_id, uri, sha256, size_bytes) "
            "VALUES ('export', :b, :u, :sha, 4096) RETURNING artifact_id"
        ),
        {"b": backend_id, "u": uri, "sha": b"sha-1"},
    ).scalar_one()


def _seed_event(
    conn,
    run_id: int,
    model_id: int,
    actor_id: int,
    event_key: str,
    *,
    request_text: str | None = None,
    response_text: str | None = None,
    request_spill_artifact_id: int | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO neuro.capture_events (run_id, event_key, model_id, actor_id, origin, "
            "request_text, response_text, request_spill_artifact_id) "
            "VALUES (:r, :ek, :m, :a, 'test-origin', :rq, :rs, :rqs)"
        ),
        {
            "r": run_id,
            "ek": event_key,
            "m": model_id,
            "a": actor_id,
            "rq": request_text,
            "rs": response_text,
            "rqs": request_spill_artifact_id,
        },
    )


def _seed_input(conn, run_id: int, content_hash: bytes) -> int:
    ps = conn.execute(
        text(
            "INSERT INTO neuro.prompt_sets (content_hash, set_kind) VALUES (:h, 'authored') "
            "RETURNING prompt_set_id"
        ),
        {"h": content_hash},
    ).scalar_one()
    conn.execute(
        text("INSERT INTO neuro.run_inputs (run_id, role, prompt_set_id) VALUES (:r, 'stimulus', :p)"),
        {"r": run_id, "p": ps},
    )
    return ps


def _seed_metric(conn, run_id: int, key: str, *, value_num=None, value_json: str | None = None) -> None:
    conn.execute(
        text(
            "INSERT INTO neuro.metric_keys (metric_key, value_kind, description) "
            "VALUES (:k, :vk, :d) ON CONFLICT DO NOTHING"
        ),
        {"k": key, "vk": "scalar" if value_num is not None else "json", "d": f"{key} description"},
    )
    conn.execute(
        text(
            "INSERT INTO neuro.run_metrics (run_id, metric_key, value_num, value_json) "
            "VALUES (:r, :k, :vn, :vj)"
        ),
        {"r": run_id, "k": key, "vn": value_num, "vj": value_json},
    )


def _seed_manifest(conn, run_id: int, model_id: int, artifact_id: int, dataset: str, row_count: int) -> None:
    conn.execute(
        text(
            "INSERT INTO neuro.table_manifests (dataset_name, run_id, model_id, schema_major, "
            "partition_path, row_count, artifact_id) VALUES (:ds, :r, :m, 1, :pp, :rc, :a)"
        ),
        {
            "ds": dataset,
            "r": run_id,
            "m": model_id,
            "pp": f"{dataset}/run={run_id}",
            "rc": row_count,
            "a": artifact_id,
        },
    )


def _seed_asset_input(conn, run_id: int, asset_key: str) -> int:
    """A run_inputs row with role='asset' (asset_id referent) — drives the _referent 'asset' branch."""
    asset_id = conn.execute(
        text(
            "INSERT INTO neuro.assets (asset_key, asset_type, loader_format) "
            "VALUES (:k, 'sae', 'saelens') RETURNING asset_id"
        ),
        {"k": asset_key},
    ).scalar_one()
    conn.execute(
        text("INSERT INTO neuro.run_inputs (run_id, role, asset_id) VALUES (:r, 'asset', :a)"),
        {"r": run_id, "a": asset_id},
    )
    return asset_id


def _seed_intervention_input(conn, run_id: int, tag: str) -> int:
    """A run_inputs row with role='intervention' (intervention_spec_id referent) — drives the _referent
    'intervention_spec' branch. intervention_specs.method_version_id is NOT NULL, so seed the method chain."""
    method_id = conn.execute(
        text("INSERT INTO neuro.methods (method_key) VALUES (:k) RETURNING method_id"),
        {"k": f"method-{tag}"},
    ).scalar_one()
    mv_id = conn.execute(
        text(
            "INSERT INTO neuro.method_versions (method_id, semver) VALUES (:m, '1.0.0') "
            "RETURNING method_version_id"
        ),
        {"m": method_id},
    ).scalar_one()
    ispec_id = conn.execute(
        text(
            "INSERT INTO neuro.intervention_specs (spec_hash, method_version_id, spec_text) "
            "VALUES (:h, :mv, 'canonical-spec') RETURNING intervention_spec_id"
        ),
        {"h": f"ispec-{tag}".encode(), "mv": mv_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO neuro.run_inputs (run_id, role, intervention_spec_id) "
            "VALUES (:r, 'intervention', :i)"
        ),
        {"r": run_id, "i": ispec_id},
    )
    return ispec_id


# --- library-module probes (real Postgres) ---------------------------------------------------------
@pytest.mark.pg
def test_show_seeded_run_identity(seeded):
    """A plain seeded run reports its identity + campaign + actor; fingerprint NULL -> model None, is_unlabeled
    False, status in-progress, invocation canonical. This is also the NULL-fingerprint honest-render case."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    report = build_run_report(repo.engine, run_id=run_id, lane="test")
    assert report.run_id == run_id
    assert report.run_key == "c-test/slug/dig"
    assert report.campaign_key == "c-test"
    assert report.actor_key == "worker:test"
    assert report.actor_kind == "scheduled_worker"
    assert report.work_slug == "slug"
    assert report.run_kind == "experiment"
    assert report.lane == "test"
    assert report.model is None  # no fingerprint -> honest None, never a crash
    assert report.is_unlabeled is False
    assert report.status == "in-progress"
    assert report.finalized_at is None
    assert report.invocation_id is None  # canonical run


@pytest.mark.pg
def test_show_labeled_run_resolves_model_identity(seeded):
    """A run carrying a fingerprint resolves model identity through fingerprints -> model_identities."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        model_id = _seed_model(conn)
        _label_run(conn, run_id, model_id)
    report = build_run_report(repo.engine, run_id=run_id, lane="test")
    assert report.model is not None
    assert report.model.declared_mode == "greedy"
    assert report.model.hf_repo == "mistralai/Mistral-7B-v0.3"
    assert report.model.hf_revision == "caa1feb0"
    assert report.model.dtype_quant == "bf16"
    assert report.model.arch_family == "llama"
    assert all(c in "0123456789abcdef" for c in report.model.fingerprint_hash_hex)  # hex pointer, not config


@pytest.mark.pg
def test_model_identity_carries_model_id_and_bounded_tokenizer_pointer(seeded):
    """CRITERION (b) at the library grain: the tokenizer digest reaches the report as a BOUNDED hex pointer.

    The seeded digest is a full 32-byte sha256 (64 hex chars), so the 16-char bound is load-bearing: deleting
    `[:16]` renders 64 chars and this reddens. `model_id` is asserted against the id the fixture actually
    minted, never against a positional guess (precedent 10 — measure, don't reason)."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        model_id = _seed_model(conn, tag="bound")
        _label_run(conn, run_id, model_id)
    full_hex = _tokenizer_hash("bound").hex()
    assert len(full_hex) == 64  # the fixture itself is checked: a short digest could not falsify the bound

    report = build_run_report(repo.engine, run_id=run_id, lane="test")
    assert report.model is not None
    assert report.model.model_id == model_id
    assert report.model.tokenizer_hash_hex == full_hex[:_HEX_BOUND]
    assert len(report.model.tokenizer_hash_hex) == _HEX_BOUND
    assert report.model.tokenizer_hash_hex != full_hex  # the bound actually removed something
    # A digest is a POINTER, not tokenizer content — nothing but lowercase hex ever reaches the render.
    assert all(c in "0123456789abcdef" for c in report.model.tokenizer_hash_hex)


@pytest.mark.pg
def test_model_id_and_tokenizer_are_the_runs_own_not_another_runs(seeded):
    """CRITERION (a), E-18 de-blind: two runs, DIFFERENT models and DIFFERENT tokenizers, each report showing
    ITS OWN pair.

    Breaking either join in the head query — `m.model_id = f.model_id` or
    `t.tokenizer_id = m.tokenizer_id` — leaks the other entity's identity, which is exactly the cross-run
    provenance blindness this surface exists to close.

    The decoy CALL is (tokenizers=3, models=4, fingerprints=2); the resulting ADVANCE differs per table and is
    not the same as those arguments — `_seed_model` mints a tokenizer of its own, so tokenizer_identities moves
    by 3+4. All EIGHT ids (run / fingerprint / model / tokenizer, for A and B) are captured from what the DB
    RETURNED, PRINTED, and asserted pairwise distinct. The tokenizer ids are in that set deliberately: the
    mis-keyed-join mutation `t.tokenizer_id = m.model_id` is detectable only while a run's tokenizer_id differs
    from its model_id, and E-18 exists so a later fixture edit that collapses that offset reddens here instead
    of going quietly blind."""
    repo, run_a, actor_id, campaign_id = (
        seeded["repo"],
        seeded["run_id"],
        seeded["actor_id"],
        seeded["campaign_id"],
    )
    run_b = repo.create_run(
        "c-test/slug/dig-B",
        campaign_id=campaign_id,
        work_slug="slug",
        variant_digest="digB",
        actor_id=actor_id,
    )
    with repo.engine.begin() as conn:
        _seed_decoy_identities(conn, tokenizers=3, models=4, fingerprints=2)
        model_a, tok_a = _seed_model_and_tokenizer(conn, tag="A")
        fp_a = _label_run(conn, run_a, model_a, tag="A")
        model_b, tok_b = _seed_model_and_tokenizer(conn, tag="B")
        fp_b = _label_run(conn, run_b, model_b, tag="B")

    ids = {
        "run_a": run_a,
        "fp_a": fp_a,
        "model_a": model_a,
        "tok_a": tok_a,
        "run_b": run_b,
        "fp_b": fp_b,
        "model_b": model_b,
        "tok_b": tok_b,
    }
    print(f"de-blind ids: {ids}")  # printed, per the banked rule — never inferred from insertion order
    assert len(set(ids.values())) == len(ids), f"decoys failed to separate the ids: {ids}"

    a = build_run_report(repo.engine, run_id=run_a, lane="test")
    b = build_run_report(repo.engine, run_id=run_b, lane="test")
    assert a.model is not None and b.model is not None

    assert a.model.model_id == model_a
    assert b.model.model_id == model_b
    assert a.model.model_id != b.model.model_id

    assert a.model.tokenizer_hash_hex == _tokenizer_hash("A").hex()[:_HEX_BOUND]
    assert b.model.tokenizer_hash_hex == _tokenizer_hash("B").hex()[:_HEX_BOUND]
    assert a.model.tokenizer_hash_hex != b.model.tokenizer_hash_hex


@pytest.mark.pg
def test_show_unlabeled_run_is_honest(seeded):
    """ADR-0036: an unlabeled adhoc run (is_unlabeled True, fingerprint NULL) renders honestly — the flag and the
    absent model are FACTS, never a crash."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        _mark_unlabeled(conn, run_id)
    report = build_run_report(repo.engine, run_id=run_id, lane="test")
    assert report.is_unlabeled is True
    assert report.model is None


@pytest.mark.pg
def test_counts_inputs_metrics_pointers(seeded):
    """Counts + detail rows: 2 events (one spilled), 1 input, 1 scalar + 1 json metric, 1 lake manifest. The json
    metric surfaces as a byte-length pointer with value_num None; the scalar carries value_num."""
    repo, run_id, actor_id = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    with repo.engine.begin() as conn:
        model_id = _seed_model(conn)
        backend_id = _seed_backend(conn)
        spill_art = _seed_artifact(conn, backend_id, "file://lake/spill/req.txt")
        parquet_art = _seed_artifact(conn, backend_id, "file://lake/logprobs/part-0.parquet")
        _seed_event(conn, run_id, model_id, actor_id, "e1", response_text="inline-ok")
        _seed_event(conn, run_id, model_id, actor_id, "e2", request_spill_artifact_id=spill_art)
        _seed_input(conn, run_id, b"prompt-set-hash-1")
        _seed_metric(conn, run_id, "answer_accuracy", value_num=0.83)
        _seed_metric(conn, run_id, "logit_summary", value_json='{"top":[1,2,3]}')
        _seed_manifest(conn, run_id, model_id, parquet_art, "logprobs", 1000)

    report = build_run_report(repo.engine, run_id=run_id, lane="test")
    assert report.event_count == 2
    assert report.spilled_event_count == 1
    assert report.input_count == 1
    assert report.metric_count == 2
    assert report.manifest_count == 1

    assert len(report.inputs) == 1
    assert report.inputs[0].role == "stimulus"
    assert report.inputs[0].referent_kind == "prompt_set"

    by_key = {m.metric_key: m for m in report.metrics}
    assert by_key["answer_accuracy"].value_num == pytest.approx(0.83)
    assert by_key["answer_accuracy"].value_json_bytes is None
    assert by_key["logit_summary"].value_num is None
    assert by_key["logit_summary"].value_json_bytes == len('{"top":[1,2,3]}')  # octet_length, not content

    assert len(report.storage_pointers) == 1
    assert report.storage_pointers[0].backend_key == "desktop-nvme"
    assert report.storage_pointers[0].dataset_name == "logprobs"
    assert report.storage_pointers[0].row_count == 1000


@pytest.mark.pg
def test_all_counts_and_details_filtered_by_run(seeded):
    """DE-BLIND EVERY per-run filter, not just event_count: a SECOND run carries DIFFERENT non-zero events,
    spilled events, inputs, metrics, and manifests, with DIFFERENT identities (metric keys, referent kinds,
    dataset names). A `WHERE run_id = :id`-drop mutation on ANY of the five count subqueries or the three
    detail queries then reddens — instead of silently leaking run B's rows into run A's report (cross-run
    provenance leakage the charter forbids). Run B's inputs also exercise the asset + intervention_spec
    branches of _referent, so a mis-mapped referent_kind reddens too.

    Run A: 2 events (1 spilled), 1 prompt_set input, 2 metrics, 1 manifest.
    Run B: 1 event (0 spilled), 2 inputs (asset + intervention_spec), 3 metrics, 2 manifests.
    """
    repo, run_a, actor_id, campaign_id = (
        seeded["repo"],
        seeded["run_id"],
        seeded["actor_id"],
        seeded["campaign_id"],
    )
    run_b = repo.create_run(
        "c-test/slug/dig-B",
        campaign_id=campaign_id,
        work_slug="slug",
        variant_digest="digB",
        actor_id=actor_id,
    )
    with repo.engine.begin() as conn:
        model_id = _seed_model(conn)
        backend_id = _seed_backend(conn)
        a_spill = _seed_artifact(conn, backend_id, "file://lake/a/spill.txt")
        a_parq = _seed_artifact(conn, backend_id, "file://lake/a/part.parquet")
        b_parq1 = _seed_artifact(conn, backend_id, "file://lake/b/part1.parquet")
        b_parq2 = _seed_artifact(conn, backend_id, "file://lake/b/part2.parquet")
        # run A
        _seed_event(conn, run_a, model_id, actor_id, "a1", response_text="x")
        _seed_event(conn, run_a, model_id, actor_id, "a2", request_spill_artifact_id=a_spill)
        _seed_input(conn, run_a, b"a-prompt-set")
        _seed_metric(conn, run_a, "a_accuracy", value_num=0.5)
        _seed_metric(conn, run_a, "a_flip_rate", value_num=0.1)
        _seed_manifest(conn, run_a, model_id, a_parq, "a_logprobs", 10)
        # run B — different counts AND identities
        _seed_event(conn, run_b, model_id, actor_id, "b1", response_text="z")
        _seed_asset_input(conn, run_b, "b-asset")
        _seed_intervention_input(conn, run_b, "b")
        _seed_metric(conn, run_b, "b_m1", value_num=0.9)
        _seed_metric(conn, run_b, "b_m2", value_num=0.8)
        _seed_metric(conn, run_b, "b_m3", value_num=0.7)
        _seed_manifest(conn, run_b, model_id, b_parq1, "b_ds1", 20)
        _seed_manifest(conn, run_b, model_id, b_parq2, "b_ds2", 30)

    a = build_run_report(repo.engine, run_id=run_a, lane="test")
    b = build_run_report(repo.engine, run_id=run_b, lane="test")

    # counts: every subquery de-blinded (A's values differ from B's and from the global totals)
    assert (a.event_count, a.spilled_event_count, a.input_count, a.metric_count, a.manifest_count) == (
        2,
        1,
        1,
        2,
        1,
    )
    assert (b.event_count, b.spilled_event_count, b.input_count, b.metric_count, b.manifest_count) == (
        1,
        0,
        2,
        3,
        2,
    )

    # detail-row identity de-blind: A's rows are A's, never B's
    assert {m.metric_key for m in a.metrics} == {"a_accuracy", "a_flip_rate"}
    assert {p.dataset_name for p in a.storage_pointers} == {"a_logprobs"}
    assert {r.referent_kind for r in a.inputs} == {"prompt_set"}
    # B's inputs cover the other two _referent branches
    assert {r.referent_kind for r in b.inputs} == {"asset", "intervention_spec"}
    assert {p.dataset_name for p in b.storage_pointers} == {"b_ds1", "b_ds2"}


@pytest.mark.pg
def test_finalized_run_renders_finalized_status(seeded):
    """The 'finalized' side of the status derivation + the non-NULL finalized_at pass-through (every other probe
    leaves finalized_at NULL, so only the 'in-progress' branch was exercised)."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        conn.execute(text("UPDATE neuro.runs SET finalized_at = now() WHERE run_id = :r"), {"r": run_id})
    report = build_run_report(repo.engine, run_id=run_id, lane="test")
    assert report.status == "finalized"
    assert report.finalized_at is not None


@pytest.mark.pg
def test_lookup_by_run_key_matches_run_id(seeded):
    """run_key selects the same run as run_id."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    by_id = build_run_report(repo.engine, run_id=run_id, lane="test")
    by_key = build_run_report(repo.engine, run_key="c-test/slug/dig", lane="test")
    assert by_key.run_id == by_id.run_id == run_id


@pytest.mark.pg
def test_missing_run_raises(seeded):
    """A well-formed request for a run that does not exist raises RunNotFoundError (the CLI maps it to exit 1),
    by id and by key."""
    repo = seeded["repo"]
    with pytest.raises(RunNotFoundError):
        build_run_report(repo.engine, run_id=999999, lane="test")
    with pytest.raises(RunNotFoundError):
        build_run_report(repo.engine, run_key="no/such/run", lane="test")


@pytest.mark.pg
def test_payload_discipline_no_restricted_content(seeded):
    """EXAM_RESTRICTED: a seeded restricted wire payload + a restricted metric json must appear NOWHERE in the
    report — events are counts, value_json is a byte-length pointer. The report repr is the whole rendered surface,
    so this reddens the moment any payload column enters a select list."""
    repo, run_id, actor_id = seeded["repo"], seeded["run_id"], seeded["actor_id"]
    secret_req = "RESTRICTED_REQ_A7F3"
    secret_resp = "RESTRICTED_RESP_B8E2"
    secret_json = '{"restricted":"C9D1_SECRET"}'
    with repo.engine.begin() as conn:
        model_id = _seed_model(conn)
        _seed_event(
            conn, run_id, model_id, actor_id, "e1", request_text=secret_req, response_text=secret_resp
        )
        _seed_metric(conn, run_id, "restricted_metric", value_json=secret_json)

    report = build_run_report(repo.engine, run_id=run_id, lane="test")
    blob = repr(report)
    assert secret_req not in blob
    assert secret_resp not in blob
    assert "C9D1_SECRET" not in blob
    assert report.event_count == 1
    assert report.metrics[0].value_json_bytes == len(secret_json)  # the pointer, not the content


@pytest.mark.pg
def test_exactly_one_selector_required(seeded):
    """The library guard: exactly one of run_id / run_key (belt under the CLI's usage check)."""
    repo = seeded["repo"]
    with pytest.raises(ValueError, match="exactly one"):
        build_run_report(repo.engine, lane="test")
    with pytest.raises(ValueError, match="exactly one"):
        build_run_report(repo.engine, run_id=1, run_key="x", lane="test")


# --- CliRunner negatives with NO database (the importer-CLI precedent) ------------------------------
def test_cli_requires_a_selector_no_db():
    """`neuro runs show` with no selector is a usage error (exit 2) resolved BEFORE any DB is opened."""
    res = _runner.invoke(runs_app, ["show"])
    assert res.exit_code == 2, res.output
    assert "exactly one" in res.output


def test_cli_rejects_both_selectors_no_db():
    """Both a positional run_id AND --run-key is a usage error (exit 2), before any DB is opened."""
    res = _runner.invoke(runs_app, ["show", "5", "--run-key", "c/s/d"])
    assert res.exit_code == 2, res.output
    assert "exactly one" in res.output


# --- CliRunner end-to-end on real Postgres (BEATS the importer-CLI unit) ----------------------------
@pytest.mark.pg
def test_cli_not_found_exits_1(repo):
    """End-to-end through the CLI: a well-formed request for a missing run fails loud with exit 1 (not a
    traceback). `repo` guarantees the schema is built + NEURO_DATABASE_URL points at the test DB."""
    res = _runner.invoke(runs_app, ["show", "999999", "--lane", "test"])
    assert res.exit_code == 1, res.output
    assert "runs show failed" in res.output
    assert "no run matches" in res.output


@pytest.mark.pg
def test_cli_renders_a_seeded_run(seeded):
    """End-to-end render through the CLI: a real seeded run prints its identity, the unlabeled line, and counts —
    the first time the platform shows a human its own provenance."""
    run_id = seeded["run_id"]
    res = _runner.invoke(runs_app, ["show", str(run_id), "--lane", "test"])
    assert res.exit_code == 0, res.output
    assert "c-test/slug/dig" in res.output
    assert "campaign:" in res.output
    assert "worker:test" in res.output
    assert "unlabeled:" in res.output
    assert "counts:" in res.output


@pytest.mark.pg
def test_cli_renders_model_line_for_labeled_run(seeded):
    """The model-present render branch (never exercised by the bare seeded run) prints the resolved identity."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        model_id = _seed_model(conn)
        _label_run(conn, run_id, model_id)
    res = _runner.invoke(runs_app, ["show", str(run_id), "--lane", "test"])
    assert res.exit_code == 0, res.output
    assert "model:" in res.output
    assert "arch=llama" in res.output
    assert "serving=vllm@0.23.0" in res.output


@pytest.mark.pg
def test_cli_renders_model_id_and_a_bounded_tokenizer_pointer(seeded):
    """CRITERION (a)+(b) at the RENDER grain: `model_id` and the tokenizer digest reach the human, the id is
    the MODEL's, and the digest is BOUNDED.

    DE-BLINDED (E-18). Without decoys this probe is vacuous: `_truncate_all` restarts every identity, so a
    lone seeded run has run_id == fingerprint_id == model_id == tokenizer_id == 1, and `model_id:    1` is
    equally satisfied by a render of the run id or the fingerprint id. Decoys with per-table-differing counts
    separate them; the ids are PRINTED and asserted pairwise distinct, and the `fingerprint:` line is pinned
    with its own id so the label-to-column binding is de-blinded in both directions.

    Asserted as EXACT LINES, not substrings: `model_id:    1` is a substring of `model_id:    12`, so a
    substring check cannot tell a correct id from a wrong one that shares a prefix.

    The full 64-char hex must appear NOWHERE in the output, so replacing the bounded pointer with the whole
    digest reddens (payload discipline: a hex POINTER, never the content it identifies)."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    with repo.engine.begin() as conn:
        _seed_decoy_identities(conn, tokenizers=2, models=3, fingerprints=1)
        model_id, tok_id = _seed_model_and_tokenizer(conn, tag="render")
        fp_id = _label_run(conn, run_id, model_id, tag="render")
    full_hex = _tokenizer_hash("render").hex()

    ids = {"run_id": run_id, "fp_id": fp_id, "model_id": model_id, "tok_id": tok_id}
    print(f"render de-blind ids: {ids}")
    assert len(set(ids.values())) == len(ids), f"decoys failed to separate the ids: {ids}"

    res = _runner.invoke(runs_app, ["show", str(run_id), "--lane", "test"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert f"  model_id:    {model_id}" in lines
    # The fingerprint line carries a DIFFERENT id, so rendering it under the model_id label reddens.
    assert any(ln.startswith(f"  fingerprint: {fp_id} hash=") for ln in lines), res.output
    assert f"  tokenizer:   {full_hex[:_HEX_BOUND]} " in res.output
    assert full_hex not in res.output  # the bound is real: the whole digest is never rendered
    # The line names the column the digest came FROM, so it cannot be misread as the model's hf label.
    assert "sha256 prefix of tokenizer_identities.tokenizer_hash" in res.output


@pytest.mark.pg
def test_cli_absent_model_states_absence_and_asserts_nothing_else(seeded):
    """CRITERION (c): with no fingerprint, the two new lines state their OWN absence and NOTHING else.

    Asserted as EXACT LINES (E-19 as extended at log:238). Any added clause — a cause ('no model identity
    yet'), a consequence, or a cross-reference to the other line — reddens, because the durability lesson is
    that a coherent false explanation is worse than none: `model_id` and the tokenizer digest are read through
    two different registry rows, and a line keyed on one must not speak for the other. They are also asserted
    PRESENT, not merely absent-of-noise: omitting them entirely is the blindness this unit closes."""
    run_id = seeded["run_id"]  # plain seeded run: fingerprint NULL, is_unlabeled False
    res = _runner.invoke(runs_app, ["show", str(run_id), "--lane", "test"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert "  model_id:    none" in lines
    assert "  tokenizer:   none" in lines


@pytest.mark.pg
def test_cli_fingerprint_none_never_claims_unlabeled(seeded):
    """Render honesty: a run with fingerprint NULL + is_unlabeled=False (exactly what repo.create_run makes) must
    NOT contradict itself — the fingerprint line states only the absent model identity; the labeling claim is the
    `unlabeled:` line's alone. This pins the fix for the vet's checkable-falsehood finding."""
    run_id = seeded["run_id"]  # plain seeded run: fingerprint NULL, is_unlabeled False
    res = _runner.invoke(runs_app, ["show", str(run_id), "--lane", "test"])
    assert res.exit_code == 0, res.output
    assert "unlabeled:   no" in res.output
    assert "fingerprint: none (no model identity yet)" in res.output
    assert "none (unlabeled" not in res.output  # the self-contradiction is gone
