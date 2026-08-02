"""The substrate-axis fail-closed belt + DERIVE cross-check + serving_version_tag COMPOSE (Unit A1).

`serving_stack`/`serving_version` are caller-asserted model-identity grades that fold into the MODEL IDENTITY
(`model_identity_hash`'s 5th/6th components) AND the fingerprint's `serving_version_tag`, yet — like
`dtype_quant` — they are NOT on the capture wire (`capture/adapters/vllm.py`). Before this belt both DEFAULTED
(`"vllm"`, `"0.23.0"`) and `serving_version_tag` was a SEPARATE defaulted kwarg, so a wave-2 capture on a
different runtime that forgot to override them would silently carry the wave-1 substrate identity — a false
MERGE the tokenizer_hash cannot backstop (a version/stack change keeps the same tokenizer; state-doc §D
vet-surfaced follow-on (a)(i)).

Three mechanisms, three test groups:
  * REQUIRE  (Layer 1) — `serving_stack`/`serving_version` REQUIRED + non-defaulted at every capture entry
    that CONSUMES them (`capture_logprob`, `run_campaign` — NOT `replicate_and_measure`, which does not take
    them: the substrate identity is already baked into its `original`/`replicate` results) AND the operator
    surface, with a shared `require_substrate` guard (the D1 "no default-clean member" idiom; sibling of the
    `require_dtype_quant` belt, log:245).
  * DERIVE   (Layer 2) — the declared substrate is cross-checked against reality: `serving_stack` against the
    adapter's own self-report, `serving_version` against the server's wire `/version`; declared != derived
    RAISES (the substrate analogue of the tokenizer collision-guard, `d2324a1`).
  * COMPOSE  — `serving_version_tag` is DERIVED from the two components (one implementation), never a free
    third assertion that could contradict the identity.

NO-CHURN guardrail (owner ruling, precedent 20): the pinned wave-1 tuple (vllm / 0.23.0 / vllm-0.23.0) must
resolve to a BYTE-IDENTICAL model_identity_hash + serving_version_tag before/after, else model_id=1 (shared by
every ESTELA + a2-17 run) would re-mint and break recompute-from-pinned-coordinates.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from typer.testing import CliRunner

from neuromancer_llm.capture.campaign import run_campaign
from neuromancer_llm.capture.events import (
    assert_substrate_matches_wire,
    capture_logprob,
    compose_serving_version_tag,
    require_substrate,
)
from neuromancer_llm.cli.capture import app
from neuromancer_llm.db.identity import model_identity_hash
from neuromancer_llm.db.lanes import ConfigurationError

_runner = CliRunner()

# The pinned wave-1 substrate tuple (the E6 launch recipe, 2026-06-20 / D4a).
PINNED_STACK = "vllm"
PINNED_VERSION = "0.23.0"
PINNED_TAG = "vllm-0.23.0"


# --- the library belt: REQUIRED, NO default at every CONSUMING capture entry --------------------------
# replicate_and_measure is DELIBERATELY EXCLUDED: it takes no substrate param (the identity is already baked
# into its original/replicate results), so a required serving_* kwarg there would be a fake belt — an unused
# required param the caller has no meaningful source for (precedent E-5: the dtype-belt shape does not fully
# transfer; dtype_quant WAS consumed there via the tolerance, serving_* are not).
@pytest.mark.parametrize("fn", [capture_logprob, run_campaign], ids=lambda f: f.__name__)
@pytest.mark.parametrize("param", ["serving_stack", "serving_version"])
def test_substrate_param_is_required_no_default(fn, param):
    """Every consuming capture entry must take `serving_stack`/`serving_version` as REQUIRED keywords with NO
    default — re-adding a default is exactly the fail-open this belt closes (a default silently folds a wrong
    substrate into the model identity), so this reddens if any entry regains `serving_*=<default>`."""
    sig = inspect.signature(fn).parameters[param]
    assert sig.default is inspect.Parameter.empty, (
        f"{fn.__name__}: {param} must have NO default; got default={sig.default!r}"
    )


def test_serving_version_tag_is_no_longer_a_free_param():
    """`serving_version_tag` must NOT be a capture_logprob kwarg anymore — it is COMPOSED from the components.
    A re-added `serving_version_tag=` param is the free-third-assertion fail-open (the tag could contradict
    the identity), so this reddens if it comes back."""
    assert "serving_version_tag" not in inspect.signature(capture_logprob).parameters


# --- the wiring: each entry must actually INVOKE the guard + the cross-check (AST, not prose) ----------
def _body_invokes(fn, callee: str) -> bool:
    """True iff fn's body contains a Call to `callee` (AST, so a docstring/comment mention does NOT count —
    the repo's AST-scan idiom; a string scan is satisfied by prose)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee
        for n in ast.walk(tree)
    )


@pytest.mark.parametrize("fn", [capture_logprob, run_campaign], ids=lambda f: f.__name__)
def test_every_consuming_entry_invokes_the_guard(fn):
    """Required-with-no-default forces the caller to PASS a grade; it does not by itself refuse a blank one.
    Each consuming entry must also CALL require_substrate in its body — deleting that call is a fail-open (a
    blank grade would reach the model identity), so this reddens if the wiring is dropped even though the
    signature stays required (precedent 20: a criterion needs a falsifying fixture)."""
    assert _body_invokes(fn, "require_substrate"), (
        f"{fn.__name__} must invoke require_substrate(...) in its body"
    )


@pytest.mark.parametrize("fn", [capture_logprob, run_campaign], ids=lambda f: f.__name__)
def test_every_consuming_entry_invokes_the_wire_cross_check(fn):
    """The DERIVE half is only real if each entry actually CALLS the cross-check — a required-and-composed
    belt with no wire check would let a WRONG-but-present version through. Deleting the cross-check call is a
    fail-open, so this reddens if `assert_substrate_matches_wire` is dropped from the body."""
    assert _body_invokes(fn, "assert_substrate_matches_wire"), (
        f"{fn.__name__} must invoke assert_substrate_matches_wire(...) in its body"
    )


# --- the fail-closed guard: an absent/blank grade is refused before any identity decision -------------
@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"], ids=["empty", "spaces", "tab", "newline"])
def test_require_substrate_refuses_blank_stack(blank):
    with pytest.raises(ConfigurationError):
        require_substrate(serving_stack=blank, serving_version=PINNED_VERSION)


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"], ids=["empty", "spaces", "tab", "newline"])
def test_require_substrate_refuses_blank_version(blank):
    with pytest.raises(ConfigurationError):
        require_substrate(serving_stack=PINNED_STACK, serving_version=blank)


def test_require_substrate_accepts_real_grades():
    require_substrate(serving_stack=PINNED_STACK, serving_version=PINNED_VERSION)  # no raise


# --- the DERIVE cross-check: declared != adapter/wire RAISES; declared == derived passes ---------------
class _FakeWireClient:
    """A minimal client stand-in exposing the two DERIVE surfaces: `serving_stack` (self-report) and
    `server_version()` (the wire /version read)."""

    def __init__(self, *, serving_stack: str = PINNED_STACK, version: str = PINNED_VERSION) -> None:
        self.serving_stack = serving_stack
        self._version = version

    def server_version(self) -> str:
        return self._version


def test_wire_cross_check_passes_when_declared_matches_reality():
    assert_substrate_matches_wire(
        _FakeWireClient(), serving_stack=PINNED_STACK, serving_version=PINNED_VERSION
    )  # no raise


def test_wire_cross_check_raises_on_stack_mismatch():
    """A capture declaring a stack the adapter does NOT self-report is the fail-open this closes (declaring
    'nnsight' while driving the vLLM adapter would fold a wrong stack into the model identity)."""
    with pytest.raises(ConfigurationError) as exc:
        assert_substrate_matches_wire(
            _FakeWireClient(serving_stack="vllm"), serving_stack="nnsight", serving_version=PINNED_VERSION
        )
    assert "serving_stack" in str(exc.value)


def test_wire_cross_check_raises_on_version_mismatch():
    """The REAL exposure: a version change keeps the same tokenizer, so only the wire /version cross-check
    catches a capture declaring 0.23.0 against a server actually running a different version."""
    with pytest.raises(ConfigurationError) as exc:
        assert_substrate_matches_wire(
            _FakeWireClient(version="0.24.0"), serving_stack=PINNED_STACK, serving_version=PINNED_VERSION
        )
    assert "serving_version" in str(exc.value) and "0.24.0" in str(exc.value)


def test_wire_cross_check_raises_when_client_hides_its_stack():
    """A client that does not self-report `serving_stack` fails CLOSED (never a silent skip of the DERIVE
    half). Uses an object with a server_version() but no serving_stack attribute."""

    class _NoStack:
        def server_version(self) -> str:
            return PINNED_VERSION

    with pytest.raises(ConfigurationError):
        assert_substrate_matches_wire(_NoStack(), serving_stack=PINNED_STACK, serving_version=PINNED_VERSION)


# --- the COMPOSE: serving_version_tag is derived from the two components -------------------------------
def test_compose_serving_version_tag_shape():
    assert compose_serving_version_tag("vllm", "0.23.0") == "vllm-0.23.0"
    assert compose_serving_version_tag("vllm-lens", "0.24.1") == "vllm-lens-0.24.1"


# --- NO-CHURN: the pinned wave-1 tuple must resolve byte-identically (precedent 20) --------------------
def test_no_churn_pinned_wave1_tag_is_byte_identical():
    """The composed tag for the pinned wave-1 tuple must equal the OLD free default `"vllm-0.23.0"` exactly —
    any normalization drift (e.g. `vllm/0.23.0`, `vllm_0.23.0`) would change every ESTELA + a2-17 fingerprint
    and re-mint model_id=1. This is the falsifying fixture the NO-CHURN acceptance criterion needs."""
    assert compose_serving_version_tag(PINNED_STACK, PINNED_VERSION) == PINNED_TAG


def test_no_churn_pinned_wave1_model_identity_hash():
    """The model identity over the pinned wave-1 substrate tuple must hash to a byte-stable value — reddens if
    the component set / order / serialization ever drifts (a fixed tokenizer_hash isolates the substrate
    axis). This is the identity half of the NO-CHURN guardrail: model_id=1 is shared by every ESTELA + a2-17
    run, so a re-mint would orphan that data and break recompute-from-pinned-coordinates."""
    h = model_identity_hash(
        hf_repo="mistralai/Mistral-7B-v0.3",
        hf_revision="caa1feb0e54d415e2df31207e5f4e273e33509b1",
        dtype_quant="bf16",
        tokenizer_hash=bytes.fromhex("11" * 32),
        serving_stack=PINNED_STACK,
        serving_version=PINNED_VERSION,
        arch_family="llama",
    ).hex()
    assert h == "fab3766c94f641e2c5e8275a12cdd916bed22c5d32c00745db03369f4ec91107"


# --- the DTYPE SPLIT: wave-2's fp16 re-capture must be a DISTINCT model identity ----------------------
def test_dtype_quant_splits_the_model_identity_three_ways():
    """Changing ONLY `dtype_quant` must change `model_identity_hash` — the guarantee the whole wave-2 fp16
    re-capture rests on. If dtype did NOT fold into the identity, an fp16 campaign would silently reuse
    wave-1's `model_id=1`, pooling two different runtimes under one identity and making the 8x-finer logprob
    grid unattributable.

    Three grades, pairwise distinct, not two: a two-grade inequality can pass on an unrelated change, whereas
    dropping `dtype_quant` from the hash entirely collapses ALL THREE to one value and reddens here.

    Deliberately does NOT restate the wave-1 pinned literal — that lives once, in the NO-CHURN sibling above,
    which is the churn detector and must not be re-authored (a second copy of a pin is a second home, and
    re-deriving it would fabricate a green). This probe pins the SPLIT; that one pins the ANCHOR."""
    common = dict(
        hf_repo="mistralai/Mistral-7B-v0.3",
        hf_revision="caa1feb0e54d415e2df31207e5f4e273e33509b1",
        tokenizer_hash=bytes.fromhex("11" * 32),  # fixed: isolates the DTYPE axis
        serving_stack=PINNED_STACK,
        serving_version=PINNED_VERSION,
        arch_family="llama",
    )
    hashes = {g: model_identity_hash(dtype_quant=g, **common).hex() for g in ("bf16", "fp16", "fp32")}
    assert len(set(hashes.values())) == 3, f"dtype_quant does not split the model identity: {hashes}"


# --- the operator surface: --serving-stack / --serving-version REQUIRED on every capture command -------
# Each command is invoked with all its OTHER required options provided, so the ONLY missing required option
# is the one under test -> typer refuses with a usage error (exit 2) naming it, BEFORE the body runs (no
# DB/vLLM touched). Defaulting the option (the fail-open) would let typer accept the omission and run the
# body -> exit != 2, so this reddens. Mirrors test_dtype_quant_belt.py / test_cli_importer.py's D1.
_REQUIRED = {
    "logprob": [
        "--dtype-quant",
        "bf16",
        "--serving-stack",
        "vllm",
        "--serving-version",
        "0.23.0",
        "--hf-revision",
        "rev",
    ],
    "replay": [
        "--dtype-quant",
        "bf16",
        "--serving-stack",
        "vllm",
        "--serving-version",
        "0.23.0",
        "--hf-revision",
        "rev",
    ],
    "campaign": [
        "--dtype-quant",
        "bf16",
        "--serving-stack",
        "vllm",
        "--serving-version",
        "0.23.0",
        "--corpus-root",
        "/tmp",
        "--corpus-commit",
        "abc",
        "--hf-revision",
        "rev",
        # `campaign` also requires --campaign-key (the wave-2 coordinate belt). It is listed here so these
        # probes stay PLACEMENT-INDEPENDENT: click names only the EARLIEST-DECLARED missing required option,
        # so an unlisted required option declared ahead of the one under test would be reported INSTEAD of it
        # and redden this fixture — a failure mode invisible in a diff, since it is a property of parameter
        # ORDER, not of any line's content.
        "--campaign-key",
        "ck",
    ],
}


def _argv_without(cmd: str, opt: str) -> list[str]:
    """The full required-option argv for `cmd` with the `opt` flag (and its value) removed."""
    full = _REQUIRED[cmd]
    i = full.index(opt)
    return [cmd] + full[:i] + full[i + 2 :]


@pytest.mark.parametrize("cmd", ["logprob", "replay", "campaign"])
@pytest.mark.parametrize("opt", ["--serving-stack", "--serving-version"])
def test_cli_capture_requires_serving_option_non_defaulted(cmd, opt):
    res = _runner.invoke(app, _argv_without(cmd, opt))
    assert res.exit_code == 2, res.output
    assert "Missing" in res.output and opt in res.output
