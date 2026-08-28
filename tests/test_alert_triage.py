"""The alert-copy repair (2026-08-28; the log:271 registered finding) — the shared triage + message shape.

RED before this unit: `governance/alert_triage.py` did not exist, and each escalation arm hardcoded
"ACTION: check the desktop sshd endpoint (Get-Service sshd)" — a remedy that was WRONG in two of the three
recorded multi-day blocks (the desktop sshd was healthy in both). The repair is not a better guess: the copy
now says the cause is NOT established and hands over a discriminating procedure.

The per-ARM copy claims are asserted in `test_escalation.py` / `test_lake_escalation.py`, co-located on one
rendered message so a negative containment (`"sshd" not in msg`) can never pass vacuously on a gutted string.
This file holds the STRUCTURAL claims: the registry containment, the composer's contract, and the two
source-wide scans that make "one implementation" and "which modules may carry this copy" falsifiable.
"""

from __future__ import annotations

import ast
import datetime as _dt
import inspect
import pathlib

import pytest

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.alert_triage import (
    ESCALATION_ARMS,
    OBJECT_STORE_REPO_TRIAGE,
    OFF_CLOUD_MIRROR_TRIAGE,
    BlockAlertArm,
    compose_block_alert,
    require_triage,
)
from neuromancer_llm.governance.durability import DURABILITY_KEYS
from neuromancer_llm.governance.freshness import BACKUP_FRESHNESS_KEY
from neuromancer_llm.governance.lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
from neuromancer_llm.governance.repo3_freshness import REPO3_FRESHNESS_KEY
from neuromancer_llm.governance.wal_freshness import WAL_LAG_KEY

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "neuromancer_llm"

#: Single unbroken tokens drawn from each triage's own text. ⚠ NEVER a phrase: the constants wrap at the
#: 110-col limit, so a sentinel copied from the RENDERED string would straddle an implicit-concatenation
#: boundary and match nothing in every file forever — a permanent clean green (the false-green family).
#: ⚠ THIS IS THE EXACT INVERSE OF THE RULE FOR RENDERED-MESSAGE ASSERTIONS, and both are deliberate: a
#: SOURCE scan must use unbroken TOKENS (the source is wrapped), while an assertion against a COMPOSED
#: message must use PHRASES (two tokens are not a contrast — the log:274 measurement). Inverting either
#: produces a permanently green probe.
_TRIAGE_SENTINELS = ("neuromirror", "preauth", "netmap")
#: The same, for the object-store triage (repo3). Kept as a SEPARATE tuple so the home-module non-vacuity
#: check below can prove each triage is present on its own rather than in aggregate.
#: ⚠ MEASURED, NOT GUESSED. A first draft used ("journalctl", "narrow", "widen") and the scan reddened on
#: NINE unrelated modules — "narrow"/"widen" are ordinary English that appears all over `src/`. A sentinel
#: must be a token that is DISTINCTIVE to the triage, or the probe reports a fork that does not exist. Each
#: of these three was checked against the rest of `src/` and appears in no other module.
_OBJECT_STORE_SENTINELS = ("journalctl", "P00", "403")


# ---- the arm registry: a new escalating arm is a PURE APPEND ---------------------------------------------


def test_escalation_arms_are_a_subset_of_the_provisioned_durability_rows() -> None:
    """The `PROBE_RUNNERS == DURABILITY_KEYS` idiom (probe_registry / test_probe_cli), one relation weaker.

    ⚠ `<=`, NOT `==`, and the asymmetry is deliberate: `wal_lag` is a provisioned row with NO escalation arm
    (its interim policy is a boolean archiver check, not a staleness block), so equality would force a
    fabricated arm for it. Every escalating arm must be a provisioned row; not every provisioned row
    escalates."""
    assert frozenset(ESCALATION_ARMS) <= DURABILITY_KEYS
    assert frozenset(ESCALATION_ARMS) == {
        BACKUP_FRESHNESS_KEY,
        LAKE_MIRROR_FRESHNESS_KEY,
        REPO3_FRESHNESS_KEY,
    }
    # Pin the PROPERNESS itself, so the `<=` above is not silently an `==` a future reader would tighten:
    # wal_lag is provisioned and deliberately un-escalated.
    assert WAL_LAG_KEY in DURABILITY_KEYS and WAL_LAG_KEY not in ESCALATION_ARMS


def test_every_arm_carries_every_coordinate_non_blank() -> None:
    """An arm added with a forgotten field would render a message with a hole in it. Assert on the ARM
    objects, not on a rendered string, so this reddens at the registry rather than at one call site."""
    for key, arm in ESCALATION_ARMS.items():
        for field in BlockAlertArm.__dataclass_fields__:
            value = getattr(arm, field)
            assert isinstance(value, str) and value.strip(), f"{key}.{field} is blank"


def test_the_arms_do_not_share_a_headline_or_label_and_share_a_rerun_unit_only_by_design() -> None:
    """The likeliest refactor error when folding hardcoded strings into one composer is an arm SWAP — which
    would tell an operator to re-run the wrong unit, i.e. this unit's own defect one arm over.

    ⚠ `rerun_unit` UNIQUENESS WAS DROPPED AS AN INVARIANT AND REPLACED BY AN EXPLICIT ALLOW-SET, rather than
    quietly deleted (2026-08-28, the repo3 arm). repo3's backup and probe are extra `ExecStart=-` /
    `ExecStartPost=-` lines on `neuro-backup.service`, so that genuinely IS the unit to re-run; inventing a
    distinct name would be a checkable falsehood, and a separate unit would race the main backup for
    pgbackrest's per-stanza lock. Headline and label stay UNIQUE, so an arm swap is still caught — and the
    sharing is pinned as a named pair, so a THIRD arm cannot join the shared unit without editing this."""
    headlines = [a.headline for a in ESCALATION_ARMS.values()]
    labels = [a.no_confirmed_label for a in ESCALATION_ARMS.values()]
    assert len(set(headlines)) == len(headlines)
    assert len(set(labels)) == len(labels)
    shared = {
        unit: sorted(k for k, a in ESCALATION_ARMS.items() if a.rerun_unit == unit)
        for unit in {a.rerun_unit for a in ESCALATION_ARMS.values()}
    }
    assert {u: ks for u, ks in shared.items() if len(ks) > 1} == {
        "neuro-backup.service": sorted([BACKUP_FRESHNESS_KEY, REPO3_FRESHNESS_KEY])
    }


# ---- the composer's contract ------------------------------------------------------------------------------


def test_compose_block_alert_requires_exactly_these_keyword_only_params() -> None:
    """⚠ THE NAME SET IS ASSERTED FIRST, AND THAT IS THE LOAD-BEARING HALF. A bare per-parameter loop is
    VACUOUSLY TRUE on a signature that lost a parameter: deleting `arm` — the whole point of the registry —
    would leave the remaining two keyword-only and non-defaulted and score GREEN. Assert WHICH, not how many
    (the count-based-matrix family)."""
    sig = inspect.signature(compose_block_alert)
    assert set(sig.parameters) == {"arm", "days", "measured_at"}
    for name, p in sig.parameters.items():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} is not keyword-only"
        assert p.default is inspect.Parameter.empty, f"{name} has a default"


def test_compose_block_alert_refuses_positional_args() -> None:
    """The behavioural companion to the signature probe — introspection alone cannot prove the call fails."""
    arm = ESCALATION_ARMS[BACKUP_FRESHNESS_KEY]
    with pytest.raises(TypeError):
        compose_block_alert(arm, 3, _dt.datetime(2026, 8, 1, tzinfo=_dt.UTC))  # type: ignore[misc]


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_require_triage_refuses_a_blank_procedure(blank: str) -> None:
    """The D1 no-default-clean-member idiom. ⚠ ONE leg of a three-leg precedent: it makes a new arm's triage
    choice EXPLICIT, it cannot make it CORRECT — nothing stops an arm on a different off-cloud leg passing
    OFF_CLOUD_MIRROR_TRIAGE. That residual is discipline and the module says so."""
    with pytest.raises(ConfigurationError):
        require_triage(blank)


def test_compose_block_alert_is_live_on_the_blank_guard() -> None:
    """The guard is worthless if the composer does not CALL it — the AST wiring-pin idiom, here reachable
    behaviourally because the composer is pure."""
    holed = BlockAlertArm(
        headline="X", no_confirmed_label="x", consequence="c", triage="", rerun_unit="u.service"
    )
    with pytest.raises(ConfigurationError):
        compose_block_alert(arm=holed, days=1, measured_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.UTC))


def test_compose_block_alert_is_pure_and_renders_its_arm() -> None:
    """No I/O, no clock: the same inputs render the same bytes, and every arm coordinate reaches the string."""
    arm = ESCALATION_ARMS[BACKUP_FRESHNESS_KEY]
    ts = _dt.datetime(2026, 8, 1, 2, 3, 4, tzinfo=_dt.UTC)
    first = compose_block_alert(arm=arm, days=7, measured_at=ts)
    assert first == compose_block_alert(arm=arm, days=7, measured_at=ts)
    assert arm.headline in first and arm.consequence in first and arm.rerun_unit in first
    assert arm.no_confirmed_label in first and "~7d" in first and str(ts) in first


# ---- one implementation: the copy lives in exactly one module, and exactly two arms may carry it ----------


def test_the_triage_text_lives_in_exactly_one_module() -> None:
    """★ THE ONE-IMPLEMENTATION PROBE, and it is a SOURCE scan on purpose.

    A containment check (`OFF_CLOUD_MIRROR_TRIAGE in msg`) is f(x)-vs-f(x): it imports the constant and finds
    it inside a message built by interpolating that same constant, so an escalation module that inlined its
    own byte-identical copy would leave it GREEN. Only a source scan sees the fork this unit exists to
    prevent — specifically the dangerous one, a copy identical on landing day that drifts a year later.

    NON-VACUITY PIN FIRST (the D3 empty-glob idiom): assert the sentinels ARE present in the home module, so
    a mis-resolved `_SRC` or a re-worded constant reddens instead of passing green while matching nothing.

    ⚠ BOTH triages are scanned SEPARATELY (2026-08-28). Checking them in aggregate would let a re-worded or
    deleted second triage hide behind the first one's hits, which is the same aggregation blindness the
    per-target mutation rule exists to stop."""
    home = "governance/alert_triage.py"
    all_sentinels = _TRIAGE_SENTINELS + _OBJECT_STORE_SENTINELS
    hits: dict[str, set[str]] = {}
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        text = py.read_text(encoding="utf-8")
        found = {s for s in all_sentinels if s in text}
        if found:
            hits[rel] = found
    for group, label in ((_TRIAGE_SENTINELS, "off-cloud"), (_OBJECT_STORE_SENTINELS, "object-store")):
        assert set(group) <= hits.get(home, set()), (
            f"the scan did not find every {label} sentinel in {home} (found {hits.get(home)}) — a re-worded "
            "triage or a mis-resolved src root would make the assertion below vacuously GREEN"
        )
    assert hits.get(home) == set(all_sentinels)
    assert set(hits) == {home}, f"triage copy found outside {home}: {sorted(set(hits) - {home})}"


def test_exactly_the_escalation_arms_and_the_shared_evaluator_consume_the_copy() -> None:
    """★ Makes the `disk_pressure.py` exclusion FALSIFIABLE rather than an in-code assertion, and makes a
    NEW arm's arrival LOUD (the fold-7 one-surface-append idiom).

    ⚠ RENAMED 2026-08-28. It was `..._only_the_two_escalation_arms_...` and promised to make "a third arm's
    arrival LOUD" — the third arm has arrived, so both the name and that phrasing had become false about the
    set the probe actually pins (three arms plus the extracted evaluator). Renaming rather than leaving a
    stale name is the point: a probe whose name misdescribes it is read past.

    `disk_pressure.py` is deliberately NOT a caller: its ACTION (prune retained fulls / expand the volume) is
    deterministic for its cause, it reads no `system_health` row, and it uses no off-cloud transport. Folding
    it in would force a triage on an arm that needs none — the precedent is real, its shape does not
    transfer. An in-code comment saying so is not a fixture; this is."""
    importers: set[str] = set()
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        if rel == "governance/alert_triage.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            from_import = isinstance(node, ast.ImportFrom) and (node.module or "").endswith("alert_triage")
            plain_import = isinstance(node, ast.Import) and any(
                a.name.endswith("alert_triage") for a in node.names
            )
            if from_import or plain_import:
                importers.add(rel)
    assert importers == {
        "governance/escalation.py",
        "governance/lake_escalation.py",
        "governance/repo3_escalation.py",
        # The shared evaluator composes the message, so it imports the copy too. ⚠ THE ARM MODULES ARE STILL
        # IN THIS SET ON PURPOSE: `evaluate_block_escalation` takes `arm=`, so each arm keeps its own
        # `ESCALATION_ARMS[...]` lookup and therefore its own import. An earlier draft of the extraction
        # passed `health_key=` and looked the arm up inside the evaluator — which would have collapsed this
        # set to {block_escalation} and turned the probe into f(x)-vs-f(x): a fourth arm would have imported
        # the evaluator instead and this assertion would have stayed GREEN, losing BOTH of the purposes
        # named above. The signature is what keeps this probe meaningful.
        "governance/block_escalation.py",
    }, (
        f"unexpected consumers of the shared alert copy: {sorted(importers)} — a new arm is a registry "
        "append plus its own delegate; update this probe only with a justification"
    )


# ---- the transport budget ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(ESCALATION_ARMS))
def test_every_arm_message_fits_the_notify_body_budget(key: str) -> None:
    """A REGRESSION TRIPWIRE on unbounded copy growth, not a verified server contract.

    `notify()` POSTs the message as the raw body. 4096 is ntfy's DOCUMENTED default `message-size-limit`;
    ⚠ it was NOT verified against the live server from here, and it is recorded as a bound of unknown
    tightness rather than as a measurement. What makes it worth pinning is the failure mode: crossing the
    real limit makes notify() raise, the unit exits non-zero, and the operator receives only the generic
    `OnFailure=neuro-alert@` unit-name ping — precisely the delivered-but-unactionable alert this whole
    lineage exists to escape."""
    msg = compose_block_alert(
        arm=ESCALATION_ARMS[key], days=99, measured_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.UTC)
    )
    assert len(msg.encode("utf-8")) < 4096


@pytest.mark.parametrize("key", sorted(ESCALATION_ARMS))
def test_every_arm_message_is_pure_ascii(key: str) -> None:
    """The rendered alert crosses three renderers — a cp1252 Windows console (where a non-ASCII `print()`
    RAISES on this box), `typer.secho`, and a phone's ntfy client — and needs a glyph for none of them.

    ⚠ This is the one probe that would redden if someone restored the em-dashes or the ⚠ marks the shipped
    copy used to carry. It is deliberate, not an accident of drafting; the module says why."""
    msg = compose_block_alert(
        arm=ESCALATION_ARMS[key], days=3, measured_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.UTC)
    )
    assert msg.isascii(), f"non-ASCII in the {key} alert: {[c for c in msg if not c.isascii()]}"


def test_each_arm_carries_the_triage_for_its_own_failing_leg() -> None:
    """★ AN EXACT PARTITION BY FAILING LEG, ASSERTED BY IDENTITY — the replacement for the blanket
    "every arm carries OFF_CLOUD_MIRROR_TRIAGE" pin, which the repo3 arm legitimately falsifies.

    Identity, not equality: two arms holding equal-but-distinct strings would satisfy `==` while being two
    implementations. Complements the source scan above from the other direction.

    ⚠ RE-KEYED, NOT LOOSENED, and the difference matters. The tempting weakening is
    `arm.triage in {OFF_CLOUD_MIRROR_TRIAGE, OBJECT_STORE_REPO_TRIAGE}` — which would let a FOURTH arm on a
    third kind of leg silently inherit whichever of the two it happened to pick, i.e. ship the exact defect
    this module was built to repair, one arm over. An exact mapping means a new arm must be added HERE, by a
    human deciding which leg it is on. That is the `ENTITY_KINDS` rule (flip the exactness in the same unit;
    never relax it to a containment check) applied to alert copy."""
    assert {k: a.triage for k, a in ESCALATION_ARMS.items()} == {
        BACKUP_FRESHNESS_KEY: OFF_CLOUD_MIRROR_TRIAGE,
        LAKE_MIRROR_FRESHNESS_KEY: OFF_CLOUD_MIRROR_TRIAGE,
        REPO3_FRESHNESS_KEY: OBJECT_STORE_REPO_TRIAGE,
    }
    # identity, arm by arm — `==` on a dict of strings would accept a distinct equal copy
    assert ESCALATION_ARMS[BACKUP_FRESHNESS_KEY].triage is OFF_CLOUD_MIRROR_TRIAGE
    assert ESCALATION_ARMS[LAKE_MIRROR_FRESHNESS_KEY].triage is OFF_CLOUD_MIRROR_TRIAGE
    assert ESCALATION_ARMS[REPO3_FRESHNESS_KEY].triage is OBJECT_STORE_REPO_TRIAGE
    # and the acceptance criterion stated as its own assertion, so it cannot be lost in a refactor of the above
    assert ESCALATION_ARMS[REPO3_FRESHNESS_KEY].triage is not OFF_CLOUD_MIRROR_TRIAGE
