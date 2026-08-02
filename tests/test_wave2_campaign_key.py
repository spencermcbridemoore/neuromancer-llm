"""The wave-2 CAMPAIGN-COORDINATE belt: `campaign_key` is a REQUIRED caller argument, not a module constant.

`capture/campaign.py` hardcoded `CAMPAIGN_KEY = "estela-order-bias"` into every `capture_logprob` call, and
`run_campaign` / `neuro capture campaign` exposed no way to change it. Since `compose_run_key` derives
`<campaign_key>/<work_slug>/<variant_digest>`, wave-1's 6,000 canonical runs permanently occupy those keys — so
the wave-2 fp16 re-capture (a NEW dtype = a NEW model identity, by design) was IMPOSSIBLE on the shipped code:
re-using the key makes `get_or_create_run` raise `IdentityMismatchError` on the `fingerprint_id` compare.

Two things are pinned here, and they are different:
  * REQUIRED + non-defaulted at the library entries (`run_campaign`, `capture_logprob`) and on the `campaign`
    CLI verb (the D1 "no default-clean member" idiom), because `campaigns.campaign_key` is `text NOT NULL
    UNIQUE` with NO non-empty CHECK — a blank key mints a campaign keyed `""` and 6,000 fresh run_keys,
    colliding with nothing and raising nothing. ⚠ Scope, stated: the `logprob`/`replay` verbs still DEFAULT
    `--campaign-key` to `"phase4-capture"`. That is not a hole in the guard — `require_campaign_key` sits in
    `capture_logprob`, which both verbs call, so a blank is refused there too; only the CLI *default* differs,
    and requiring it on two ad-hoc single-capture verbs is an unrelated CLI-contract change (their `work_slug`
    and `variant_digest` are defaulted too, so tightening one of three would be an inconsistent surface).
  * THREADED, not merely accepted — the runs must actually land under the caller's key (a parameter that is
    accepted and then ignored is the defect a signature probe alone cannot see).

⚠ The blank guard closes the BLANK case only; a typo'd NOVEL key also collides with nothing and completes
silently. That is not mechanically detectable (a closed vocab is the wrong instrument, for the same reason
`dtype_quant` is a free string — log:245) and is caught by post-run verification of the counts under the
intended key. Stated here so no reader mistakes REQUIRED for typo-proof.
"""

from __future__ import annotations

import ast
import inspect
from inspect import Parameter
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

import neuromancer_llm.capture.campaign as campaign_mod
from neuromancer_llm.capture.campaign import EstelaQuestion, run_campaign
from neuromancer_llm.capture.events import capture_logprob, require_campaign_key
from neuromancer_llm.cli.capture import app  # the CAPTURE sub-app: `campaign` is its subcommand
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.storage.backends import LocalFsBackend
from test_estela_campaign import _FakeCampaignClient, _sample_with_letters

_runner = CliRunner()


# --- REQUIRED + non-defaulted at the library entries -------------------------------------------------
@pytest.mark.parametrize("fn", [run_campaign, capture_logprob], ids=["run_campaign", "capture_logprob"])
def test_campaign_key_is_required_and_non_defaulted(fn) -> None:
    """A default here is the whole fail-open: it re-creates the hardcode one layer up, and (at
    `run_campaign`) the default would inevitably be wave-1's key — the exact value wave-2 cannot use."""
    param = inspect.signature(fn).parameters["campaign_key"]
    assert param.default is Parameter.empty, f"{fn.__name__} defaults campaign_key to {param.default!r}"


def test_campaign_key_constant_is_gone() -> None:
    """The module constant must NOT come back. A surviving `CAMPAIGN_KEY` is read by nothing, and exists only
    to be imported by a future caller as "the" campaign key — the hardcode returning through the back door."""
    assert not hasattr(campaign_mod, "CAMPAIGN_KEY")


# --- the fail-closed guard ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n"])
def test_require_campaign_key_refuses_blank(bad) -> None:
    with pytest.raises(ConfigurationError, match="campaign_key is REQUIRED"):
        require_campaign_key(bad)


def test_require_campaign_key_accepts_a_real_key() -> None:
    require_campaign_key("estela-order-bias-fp16")  # must not raise


# --- AST WIRING PINS: the guard must actually be CALLED, not merely defined --------------------------
def _calls_in(func_name: str, module) -> set[str]:
    """Every function called inside `func_name` of `module`'s source (a guard that exists but is never
    invoked is prose; the belt is the CALL, not the def)."""
    tree = ast.parse(Path(inspect.getsourcefile(module)).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func_name)
    return {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_require_campaign_key_is_wired_into_run_campaign() -> None:
    import neuromancer_llm.capture.campaign as m

    assert "require_campaign_key" in _calls_in("run_campaign", m)


def test_require_campaign_key_is_wired_into_capture_logprob() -> None:
    import neuromancer_llm.capture.events as m

    assert "require_campaign_key" in _calls_in("capture_logprob", m)


# --- the operator surface: --campaign-key REQUIRED on `neuro capture campaign` -----------------------
# Every OTHER required option is supplied, so the ONLY missing one is the one under test -> typer refuses
# with a usage error (exit 2) naming it, BEFORE the body runs (no DB, no vLLM). Defaulting the option would
# let typer accept the omission and run the body -> exit != 2, so this reddens.
_CAMPAIGN_ARGV = [
    "campaign",
    "--dtype-quant",
    "fp16",
    "--serving-stack",
    "vllm",
    "--serving-version",
    "0.23.0",
    "--corpus-root",
    "/tmp/nonexistent.jsonl",
    "--corpus-commit",
    "abc",
    "--hf-revision",
    "rev",
]


def test_cli_campaign_requires_campaign_key_non_defaulted() -> None:
    res = _runner.invoke(app, _CAMPAIGN_ARGV)
    assert res.exit_code == 2, res.output
    assert "Missing" in res.output and "--campaign-key" in res.output


# --- THREADED, not ignored: the runs land under the CALLER's key -------------------------------------
@pytest.mark.pg
def test_run_campaign_files_runs_under_the_caller_key(repo, tmp_path) -> None:
    """The parameter must reach the rows. A decoy campaign is minted FIRST so the campaign ids differ (the
    RESTART IDENTITY blind-id trap: without it every id is 1 and a wrong-campaign binding reads clean)."""
    decoy_actor = repo.get_or_create_actor("decoy-actor")
    decoy_id = repo.get_or_create_campaign("decoy-campaign", decoy_actor)
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    client = _FakeCampaignClient(_sample_with_letters({"A": -0.5, "B": -1.5}))
    key = "wave2-fp16-probe"
    result = run_campaign(
        repo=repo,
        backend=backend,
        backend_id=backend_id,
        client=client,
        tokenizer_hash=b"T",
        questions=[EstelaQuestion(uid="q1", stem="S1", choices=("x", "y"), num_choices=2)],
        hf_repo="r",
        hf_revision="rev",
        dtype_quant="fp16",
        serving_stack="vllm",
        serving_version="0.23.0",
        campaign_key=key,
        corpus_commit="c-test",
        expected_lane="test",
        n_logprobs=64,
        origin="test",
    )
    # The result carries the caller's key (a pass-through, NOT a read-back -- so it is not a typo detector).
    # This assert is the ONLY fixture pinning CampaignResult.campaign_key; the row assertions below are what
    # prove the key reached the DB.
    assert result.campaign_key == key

    with repo.engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT r.run_key, c.campaign_key, c.campaign_id FROM neuro.runs r "
                    "JOIN neuro.campaigns c ON c.campaign_id = r.campaign_id ORDER BY r.run_id"
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2  # k=2 -> 2! permutations
    assert {r["campaign_key"] for r in rows} == {key}
    assert all(r["campaign_id"] != decoy_id for r in rows)  # de-blinded: not the decoy's id
    # the key is the run_key's leading segment -- this is what makes wave-1 and wave-2 disjoint
    assert {r["run_key"] for r in rows} == {f"{key}/q1/0", f"{key}/q1/1"}


@pytest.mark.pg
def test_two_campaign_keys_are_disjoint(repo, tmp_path) -> None:
    """The guarantee wave-2 rests on: the SAME questions captured twice under DIFFERENT keys produce two
    campaigns and two disjoint run_key sets. Under the old hardcoded constant the second call would collide
    on every run_key -- which is precisely why the fp16 re-capture was impossible."""
    backend = LocalFsBackend(tmp_path)
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    questions = [EstelaQuestion(uid="q1", stem="S1", choices=("x", "y"), num_choices=2)]

    def _run(key: str, dtype: str):
        # A DIFFERENT dtype per campaign, exactly as wave-1 (bf16) vs wave-2 (fp16), so the assertions below
        # cover the real scenario. ⚠ MEASURED, because the obvious justification is FALSE: two campaigns at the
        # SAME dtype do NOT collide on the fingerprint UNIQUE -- `register_fingerprint` is a get-or-create, so
        # they SHARE fingerprint rows (measured: 4 runs / 2 distinct fingerprints / 1 model identity, no
        # raise). What the dtype difference actually buys is the `models == 2` assert at the end: with one
        # dtype that count is 1, which is precisely the wave-2 silent-merge this belt exists to prevent.
        return run_campaign(
            repo=repo,
            backend=backend,
            backend_id=backend_id,
            client=_FakeCampaignClient(_sample_with_letters({"A": -0.5, "B": -1.5})),
            tokenizer_hash=b"T",
            questions=questions,
            hf_repo="r",
            hf_revision="rev",
            dtype_quant=dtype,
            serving_stack="vllm",
            serving_version="0.23.0",
            campaign_key=key,
            corpus_commit="c-test",
            expected_lane="test",
            n_logprobs=64,
            origin="test",
        )

    _run("wave1-bf16-probe", "bf16")
    _run("wave2-fp16-probe", "fp16")

    with repo.engine.connect() as conn:
        by_key = (
            conn.execute(
                text(
                    "SELECT c.campaign_key AS k, count(*) AS n, count(DISTINCT r.fingerprint_id) AS fps "
                    "FROM neuro.runs r JOIN neuro.campaigns c ON c.campaign_id = r.campaign_id "
                    "GROUP BY c.campaign_key ORDER BY c.campaign_key"
                )
            )
            .mappings()
            .all()
        )
        models = conn.execute(text("SELECT count(*) FROM neuro.model_identities")).scalar_one()

    assert [dict(r) for r in by_key] == [
        {"k": "wave1-bf16-probe", "n": 2, "fps": 2},
        {"k": "wave2-fp16-probe", "n": 2, "fps": 2},
    ]
    # the dtype split is real end-to-end: two runtimes -> two model identities, never pooled into one
    assert models == 2
