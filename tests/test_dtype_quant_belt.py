"""The dtype_quant fail-closed belt (Layer 1).

`dtype_quant` is a caller-asserted model-identity grade that folds into the MODEL IDENTITY + fingerprint
(`capture_logprob`) and the E6 divergence TOLERANCE (`replicate_and_measure`), yet it is NOT on the wire
(`capture/adapters/vllm.py`). Before this belt it defaulted to `"bf16"`, so a future fp16/fp32 capture that
forgot to override it would silently carry a BF16 identity — a "right label, wrong runtime dtype" lie that
nothing caught (E6 is a separate cert, not a per-capture check; the tokenizer analogue was closed at
`d2324a1`).

Layer 1 (here) makes `dtype_quant` REQUIRED and non-defaulted at every capture entry — the library entries
(`capture_logprob`, `replicate_and_measure`, `run_campaign`) AND the operator surface (`neuro capture
logprob | replay | campaign`) — mirroring the D1 "no default-clean member" idiom (`importer/ingress.py`;
`tests/test_cli_importer.py`). It forbids the SILENT default and the EMPTY grade only. A WRONG-but-present
grade (a valid label on the wrong runtime) is `capture/gridcheck.py`'s job (§D Layer 2, BUILT; tested in
tests/test_gridcheck.py), NOT here — it hard-raises the ONE reliably detectable lie (a declared full-depth
`fp32` whose captured logprobs carry a coarse quantization grid); bf16<->fp16 is not identifiable from
log-softmax (bf16-depth, log:244).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from typer.testing import CliRunner

from neuromancer_llm.capture.campaign import run_campaign
from neuromancer_llm.capture.events import (
    capture_logprob,
    replicate_and_measure,
    require_dtype_quant,
)
from neuromancer_llm.cli.capture import app
from neuromancer_llm.db.lanes import ConfigurationError

_runner = CliRunner()


# --- the library belt: REQUIRED, NO default at every capture entry ------------------------------------
@pytest.mark.parametrize(
    "fn", [capture_logprob, replicate_and_measure, run_campaign], ids=lambda f: f.__name__
)
def test_dtype_quant_is_required_no_default(fn):
    """Every capture entry must take `dtype_quant` as a REQUIRED keyword with NO default — re-adding a
    default is exactly the fail-open this belt closes (a default silently folds a wrong dtype into the model
    identity), so this reddens if any entry regains `dtype_quant=<default>`."""
    param = inspect.signature(fn).parameters["dtype_quant"]
    assert param.default is inspect.Parameter.empty, (
        f"{fn.__name__}: dtype_quant must have NO default; got default={param.default!r}"
    )


# --- the wiring: each entry must actually INVOKE the guard (not merely declare the param required) -------
def _body_invokes(fn, callee: str) -> bool:
    """True iff fn's body contains a Call to `callee` (AST, so a docstring/comment mention does NOT count —
    the repo's AST-scan idiom; a string scan is satisfied by prose)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee
        for n in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "fn", [capture_logprob, replicate_and_measure, run_campaign], ids=lambda f: f.__name__
)
def test_every_capture_entry_invokes_the_guard(fn):
    """Required-with-no-default forces the caller to PASS a grade; it does not by itself refuse a blank one.
    Each entry must also CALL require_dtype_quant in its body — deleting that call is a fail-open (a blank
    grade would reach the model identity / the divergence tolerance), so this reddens if the wiring is
    dropped even though the signature stays required (precedent 20: a criterion needs a falsifying fixture)."""
    assert _body_invokes(fn, "require_dtype_quant"), (
        f"{fn.__name__} must invoke require_dtype_quant(dtype_quant) in its body"
    )


# --- the fail-closed guard: an absent/blank grade is refused before any identity/tolerance decision -----
@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"], ids=["empty", "spaces", "tab", "newline"])
def test_require_dtype_quant_refuses_blank(blank):
    with pytest.raises(ConfigurationError):
        require_dtype_quant(blank)


@pytest.mark.parametrize("grade", ["bf16", "fp16", "fp32"])
def test_require_dtype_quant_accepts_a_grade(grade):
    require_dtype_quant(grade)  # a real grade passes (no raise; the belt does not validate the vocabulary)


# --- the operator surface: --dtype-quant is REQUIRED on every capture command --------------------------
# Each command is invoked with all its OTHER required options provided, so the ONLY missing required option
# is --dtype-quant -> typer refuses with a usage error (exit 2) naming it, BEFORE the command body runs (no
# DB/vLLM touched). Defaulting the option (the fail-open) would let typer accept the omission and run the
# body -> exit != 2 and no "--dtype-quant" mention, so this test reddens. Mirrors test_cli_importer.py's D1.
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["logprob", "--hf-revision", "rev"], id="logprob"),
        pytest.param(["replay", "--hf-revision", "rev"], id="replay"),
        # This row lists EVERY required option of `campaign` except --dtype-quant -- all SIX (measured off
        # typer.main.get_command: campaign requires 7, logprob/replay 4). Not a delta: --serving-stack and
        # --serving-version are SHARED with logprob/replay, so they are not "extra", yet omitting them breaks
        # this row just as surely as omitting the three that ARE campaign-only (--corpus-root,
        # --corpus-commit, --campaign-key). Listing all six is what makes the comment above TRUE for this row
        # and the probe PLACEMENT-INDEPENDENT: click names only the EARLIEST-DECLARED missing required option,
        # so any unlisted one declared ahead of --dtype-quant is reported INSTEAD of it and reddens this test
        # -- a failure mode invisible in a diff, because it is a property of parameter ORDER, not of any
        # line's content. (Before the wave-2 --campaign-key option this row passed only because --dtype-quant
        # happens to be declared first. ⚠ The logprob/replay rows below are still minus-three and so remain
        # placement-dependent -- a PRE-EXISTING gap, not introduced here; registered, not fixed in this unit.)
        pytest.param(
            [
                "campaign",
                "--serving-stack",
                "vllm",
                "--serving-version",
                "0.23.0",
                "--campaign-key",
                "ck",
                "--corpus-root",
                "/tmp",
                "--corpus-commit",
                "abc",
                "--hf-revision",
                "rev",
            ],
            id="campaign",
        ),
    ],
)
def test_cli_capture_requires_dtype_quant_non_defaulted(argv):
    res = _runner.invoke(app, argv)
    assert res.exit_code == 2, res.output
    assert "Missing" in res.output and "--dtype-quant" in res.output
