"""The repo3 (Backblaze B2) NOTIFY-ONLY durability arm (§A·72, 2026-08-28) + the gate-basis pin.

RED before this unit: `governance/repo3_freshness.py`, `repo3_probe.py`, `repo3_escalation.py`,
`block_escalation.py`, `provisioning_invariants.GATE_BASIS_REPOS` and `neuro probe repo3-escalate` did not
exist, and `backup_driver._repo_freshness_from_info` derived its repo set FROM the pgbackrest info JSON — so
adding `repo3-*` to the conf would have required a fresh FULL on repo3 at the first driver run and CLOSED the
ADR-0020 gate on an arm that had never been proven.

Each acceptance criterion of the unit has a falsifying fixture HERE (precedent 20: a criterion without one is
prose). The per-arm COPY claims are asserted on a whole rendered message in one place, deliberately, so the
negative containments cannot pass vacuously on a gutted string — the `test_escalation.py::_assert_backup_copy`
shape, carried.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from neuromancer_llm.cli.app import app
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance import provisioning_invariants as _pi
from neuromancer_llm.governance.alert_triage import (
    ESCALATION_ARMS,
    OBJECT_STORE_REPO_TRIAGE,
    OFF_CLOUD_MIRROR_TRIAGE,
)
from neuromancer_llm.governance.backup_driver import (
    CommandResult,
    InfoUnreadableError,
    _repo_freshness_from_info,
    newest_full_per_repo,
)
from neuromancer_llm.governance.durability import DURABILITY_KEYS, seed_all
from neuromancer_llm.governance.freshness import BACKUP_FRESHNESS_KEY
from neuromancer_llm.governance.health import GATE_CONSULTED_KEYS, assert_durability_ok
from neuromancer_llm.governance.probe_registry import PROBE_RUNNERS, ProbeContext
from neuromancer_llm.governance.repo3_escalation import evaluate_repo3_block_escalation
from neuromancer_llm.governance.repo3_freshness import (
    REPO3_FRESHNESS_KEY,
    REPO3_REPO_KEY,
    resolve_repo3_block_escalate_after,
    resolve_repo3_stale_after,
)
from neuromancer_llm.governance.repo3_probe import (
    Repo3Outcome,
    Repo3ProbeError,
    make_repo3_recency_driver,
    run_repo3_probe,
)

_runner = CliRunner()
_NOW = _dt.datetime(2026, 8, 28, 12, 0, 0, tzinfo=_dt.UTC)
_BOUND = _dt.timedelta(days=4)  # BASE_BACKUP_INTERVAL(2d) + PROVISIONING_MARGIN(2d), asserted below


def _info_json(*, ages_days: dict[int, float | None]) -> str:
    """A pgbackrest info --output=json body; per repo-key, a full backup finished `age` days before _NOW
    (None = the repo is CONFIGURED but carries no full backup)."""
    backups = [
        {
            "type": "full",
            "label": f"20260828-full-r{key}",
            "database": {"repo-key": key},
            "timestamp": {
                "start": (_NOW - _dt.timedelta(days=age)).timestamp() - 60,
                "stop": (_NOW - _dt.timedelta(days=age)).timestamp(),
            },
        }
        for key, age in ages_days.items()
        if age is not None
    ]
    return json.dumps([{"name": "neuro", "repo": [{"key": k} for k in ages_days], "backup": backups}])


def _seam(info: str, *, rc: int = 0, stderr: str = ""):
    def runner(argv: list[str], *, timeout_s: float) -> CommandResult:
        assert timeout_s > 0
        assert argv[0] == "pgbackrest" and "info" in argv
        return CommandResult(rc, info, stderr)

    return runner


# ============ CRITERION 1: the gate-basis pin ==============================================================


def test_the_pinned_basis_is_todays_configured_set() -> None:
    """The value itself, pinned. `{1, 2}` is TODAY'S configured set, which is what makes the landing
    behaviour-identical for the proven repos; promotion (§A·72, "armed, not dated") is this one line."""
    # (written variable-first because ruff SIM300 reads a dotted ALL-CAPS name as the constant side)
    expected = frozenset({1, 2})
    assert expected == _pi.GATE_BASIS_REPOS
    assert _pi.resolve_gate_basis_repos() == expected


@pytest.mark.parametrize("empty", [None, frozenset()])
def test_gate_basis_resolver_fails_closed_on_absent_or_EMPTY_pin(monkeypatch, empty) -> None:
    """★ BOTH poles, and the EMPTY one is the load-bearing half.

    An `is None`-only guard returns `frozenset()` happily, and a recency loop over an empty basis makes NO
    requirement and certifies FRESH against zero repositories — byte-for-byte the vacuous pass that
    `_repo_freshness_from_info`'s empty-repo-array branch exists to close, relocated from the info JSON onto
    the pin. Reverting the resolver to `is None` reddens the `frozenset()` case only, which is exactly why
    both are parametrized rather than one being assumed to imply the other."""
    monkeypatch.setattr(_pi, "GATE_BASIS_REPOS", empty)
    with pytest.raises(ConfigurationError, match="gate basis"):
        _pi.resolve_gate_basis_repos()


def test_an_empty_basis_cannot_certify_freshness_through_the_driver(monkeypatch) -> None:
    """The behavioural companion: the resolver's fail-closed must be LIVE on the recency path, not merely
    importable. An emptied pin must never produce ok=True over an all-fresh info JSON."""
    monkeypatch.setattr(_pi, "GATE_BASIS_REPOS", frozenset())
    with pytest.raises(ConfigurationError):
        _repo_freshness_from_info(
            _info_json(ages_days={1: 1.0, 2: 1.0}), stanza="neuro", bound=_BOUND, now=_NOW
        )


def test_a_stale_repo3_does_NOT_fail_the_gating_recency() -> None:
    """★ CRITERION 1a — THE WHOLE POINT OF THE UNIT. repo3 present and 90 days stale; the gating check still
    passes because repo3 is not in the basis. Mutating the pin to include 3 reddens this."""
    ok, detail = _repo_freshness_from_info(
        _info_json(ages_days={1: 1.0, 2: 1.0, 3: 90.0}), stanza="neuro", bound=_BOUND, now=_NOW
    )
    assert ok is True
    # ...and it is REPORTED rather than silently dropped — a non-gating repo that vanished from the operator's
    # view would be a different failure (an unmonitored copy) wearing the same green.
    assert "non-gating, reported only" in detail and "repo3:20260828-full-r3 STALE 90d" in detail


def test_a_repo3_with_no_full_backup_is_reported_not_gating() -> None:
    """The other non-gating shape: configured, but nothing in it yet — the state the conf edit creates and
    the first repo3 backup clears."""
    ok, detail = _repo_freshness_from_info(
        _info_json(ages_days={1: 1.0, 2: 1.0, 3: None}), stanza="neuro", bound=_BOUND, now=_NOW
    )
    assert ok is True and "repo3:NO FULL BACKUP" in detail


def test_a_pinned_repo_absent_from_the_conf_DOES_fail_closed() -> None:
    """★ CRITERION 1b, and the direction the owner ruled deliberately on 2026-08-28. Before the pin, a repo
    REMOVED from the conf silently left the gate basis: pulling the `repo2-*` lines (the A2-7 §7 rollback
    lever) left backup_freshness reading GREEN on repo1 alone, with nothing to say the cloud copy had gone.
    Dropping `2` from the pin reddens this."""
    ok, detail = _repo_freshness_from_info(
        _info_json(ages_days={1: 1.0}), stanza="neuro", bound=_BOUND, now=_NOW
    )
    assert ok is False
    assert "repo2 is in the pinned gate basis" in detail and "NOT configured" in detail


def test_a_pinned_repo_that_is_stale_DOES_fail_closed() -> None:
    """★ CRITERION 1c — the pre-existing guarantee, unchanged. A2-7 §4.4: a stalled repo2 cadence must go
    loud even though the mirror reads repo1."""
    ok, detail = _repo_freshness_from_info(
        _info_json(ages_days={1: 1.0, 2: 20.0}), stanza="neuro", bound=_BOUND, now=_NOW
    )
    assert ok is False and "recency: repo2" in detail and "20d old" in detail


def test_the_two_repo_detail_string_is_BYTE_IDENTICAL_to_the_pre_pin_shape() -> None:
    """★ CRITERION 1d — THE NO-CHURN PROOF, asserted as a LITERAL rather than a substring.

    With today's `{1, 2}` conf there are no non-gating repos, so the reported suffix must be ABSENT
    ENTIRELY — not empty-but-present, not a trailing separator. A substring assertion would survive a stray
    " (non-gating, reported only: )" being appended; equality will not. This is what lets the unit claim the
    landing is behaviour-identical for the proven repos."""
    ok, detail = _repo_freshness_from_info(
        _info_json(ages_days={1: 1.0, 2: 1.5}), stanza="neuro", bound=_BOUND, now=_NOW
    )
    assert ok is True
    assert detail == "repo1:20260828-full-r1, repo2:20260828-full-r2"


def test_the_empty_repo_array_branch_stays_REACHABLE_and_keeps_its_own_message() -> None:
    """The vet-M2 guard must not become dead code behind the pin — a dead check is a false green. It is
    evaluated BEFORE the per-repo basis loop and keeps a message about the JSON rather than about the pin,
    so deleting it changes the observable text and reddens here."""
    ok, detail = _repo_freshness_from_info(_info_json(ages_days={}), stanza="neuro", bound=_BOUND, now=_NOW)
    assert ok is False and detail == "recency: pgbackrest info lists NO repositories (fail closed)"


# ============ the shared parser (one implementation, two callers) ==========================================


def test_newest_full_per_repo_returns_configured_keys_and_the_newest_full() -> None:
    keys, newest = newest_full_per_repo(_info_json(ages_days={1: 1.0, 3: 2.0}), stanza="neuro")
    assert set(keys) == {1, 3}
    assert newest[3][1] == "20260828-full-r3"


@pytest.mark.parametrize("bad", ["not json", "[]", '[{"name": "other", "repo": []}]', '[{"name":"neuro"}]'])
def test_newest_full_per_repo_raises_on_any_parse_surprise(bad: str) -> None:
    """Both callers convert this to a fail-CLOSED outcome; a parser that returned a partial result on
    malformed input would let a recency check pass on evidence it could not read."""
    with pytest.raises(InfoUnreadableError):
        newest_full_per_repo(bad, stanza="neuro")


def test_the_unreadable_error_carries_only_the_exception_TYPE_not_content() -> None:
    """The info JSON derives from a conf holding account-wide cloud keys, and this text reaches
    system_health.detail and a phone. The redaction contract, one layer downstream."""
    try:
        newest_full_per_repo('{"secret-looking": "AccountKey=hunter2"}', stanza="neuro")
    except InfoUnreadableError as exc:
        assert "hunter2" not in str(exc) and "AccountKey" not in str(exc)
        assert str(exc) in {"TypeError", "KeyError", "ValueError", "StopIteration", "AttributeError"}
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected InfoUnreadableError")


# ============ the leaf contract ===========================================================================


def test_the_repo3_bound_is_derived_by_reference_never_a_third_number(monkeypatch) -> None:
    """One implementation per concept: the bound IS BASE_BACKUP_INTERVAL + PROVISIONING_MARGIN, so a cadence
    change moves it automatically and there is no third constant to forget.

    ⚠⚠ THE DERIVATION IS PINNED BY MOVING AN INPUT, NOT BY COMPARING VALUES, AND THAT MATTERS. A first
    version asserted only `resolve_repo3_stale_after() == resolve_base_backup_interval() +
    resolve_provisioning_margin()` and `== 4 days`. The mutation matrix MEASURED that replacing the whole
    body with a hardcoded `timedelta(days=4)` SURVIVED both assertions — because 2d + 2d happens to equal 4d,
    so a value comparison cannot tell a derivation from a coincidence. It was a false green in the one probe
    written for that exact mutation. Moving a pin and requiring the output to follow is what actually
    distinguishes the two."""
    # the coincidence-free direction FIRST: move an input, the output must move with it
    monkeypatch.setattr(_pi, "BASE_BACKUP_INTERVAL", _dt.timedelta(days=5))
    assert resolve_repo3_stale_after() == _dt.timedelta(days=7)  # 5d + the unchanged 2d margin
    monkeypatch.setattr(_pi, "PROVISIONING_MARGIN", _dt.timedelta(days=1))
    assert resolve_repo3_stale_after() == _dt.timedelta(days=6)  # BOTH inputs, not a representative one
    monkeypatch.undo()
    # ...and then the shipped values, so a silent drift in the pins themselves is still visible
    assert resolve_repo3_stale_after() == (
        _pi.resolve_base_backup_interval() + _pi.resolve_provisioning_margin()
    )
    assert resolve_repo3_stale_after() == _BOUND
    assert resolve_repo3_block_escalate_after() == resolve_repo3_stale_after()


@pytest.mark.parametrize("pin", ["BASE_BACKUP_INTERVAL", "PROVISIONING_MARGIN"])
def test_the_repo3_bound_fails_closed_on_either_absent_pin(monkeypatch, pin: str) -> None:
    """BOTH inherited pins, not a representative one: the bound is a SUM, so either half going None must
    raise. Testing one would leave the other's fail-open unprobed."""
    monkeypatch.setattr(_pi, pin, None)
    with pytest.raises(ConfigurationError):
        resolve_repo3_stale_after()
    with pytest.raises(ConfigurationError):
        resolve_repo3_block_escalate_after()


# ============ the driver ==================================================================================


def _driver(info: str, **kw):
    return make_repo3_recency_driver(runner=_seam(info, **kw), now=_NOW)


def test_driver_ok_on_a_fresh_repo3() -> None:
    out = _driver(_info_json(ages_days={1: 1.0, 2: 1.0, 3: 0.5}))()
    assert out.ok is True and out.detail == "repo3:20260828-full-r3"


def test_driver_blocks_with_a_recency_label_on_a_stale_repo3() -> None:
    out = _driver(_info_json(ages_days={1: 1.0, 3: 9.0}))()
    assert out.ok is False and out.detail.startswith("recency:") and "9d old" in out.detail


def test_driver_blocks_with_a_recency_label_when_repo3_has_no_full() -> None:
    out = _driver(_info_json(ages_days={1: 1.0, 3: None}))()
    assert out.ok is False and out.detail == "recency: repo3 has NO full backup (fail closed)"


def test_driver_blocks_with_an_info_read_label_when_repo3_is_not_configured() -> None:
    """The coordinate the health_key is NAMED for, CONFIRMED rather than trusted — this is both the
    pre-conf-edit state and what a pgbackrest repo RENUMBER would look like. ⚠ The label is `info-read:`,
    not `recency:`, and the triage discriminates on exactly that: a missing repo is a config fault, not a
    stalled cadence, and routing it to the cadence branch would send an operator to the wrong place."""
    out = _driver(_info_json(ages_days={1: 1.0, 2: 1.0}))()
    assert out.ok is False and out.detail.startswith("info-read:") and "NOT configured" in out.detail


def test_driver_blocks_and_truncates_stderr_on_a_nonzero_info(monkeypatch) -> None:
    out = _driver(_info_json(ages_days={}), rc=3, stderr="x" * 500)()
    assert out.ok is False and out.detail.startswith("info-read: pgbackrest info failed (rc=3)")
    assert len(out.detail) < 300  # truncated: this string reaches a phone


def test_driver_blocks_on_unparseable_info() -> None:
    out = _driver("not json")()
    assert out.ok is False and out.detail.startswith("info-read: could not read pgbackrest info json")


def test_driver_defaults_are_the_committed_coordinates() -> None:
    """Every coordinate is a committed default, which is WHY the registry runner may default-construct it
    (repo3 has no operator-supplied coordinate at all). A change to any of these is an auditable commit."""
    import inspect

    sig = inspect.signature(make_repo3_recency_driver)
    assert sig.parameters["stanza"].default == "neuro"
    assert sig.parameters["repo"].default == REPO3_REPO_KEY == 3
    assert sig.parameters["runner"].default.__name__ == "run_subprocess"


# ============ CRITERION 4: the keyset pins ================================================================


def test_repo3_is_provisioned_and_runnable_but_NOT_gate_consulted() -> None:
    """★ CRITERION 4c, DECLARATIVELY. The behavioural half is below, against a real gate."""
    assert REPO3_FRESHNESS_KEY in DURABILITY_KEYS
    assert REPO3_FRESHNESS_KEY in PROBE_RUNNERS
    assert REPO3_FRESHNESS_KEY not in GATE_CONSULTED_KEYS
    assert GATE_CONSULTED_KEYS < DURABILITY_KEYS  # still a PROPER subset, now by two keys


def test_repo3_runner_is_not_destination_bearing() -> None:
    """repo3 takes NO operator coordinate, so its runner must not refuse for a missing one — a fail-closed
    refusal here would have to name a flag that does not exist, which is the log:242 defect (a refusal an
    operator cannot act on). It default-constructs instead, exactly as `_run_wal` needs nothing.

    Asserted through the REGISTRY, not by reading the source, and with a driver deliberately NOT supplied."""
    with pytest.raises(Exception) as exc:  # noqa: B017 - the point is WHICH type it is NOT
        PROBE_RUNNERS[REPO3_FRESHNESS_KEY](None, ProbeContext())
    assert not isinstance(exc.value, ConfigurationError)


# ============ CRITERION 3 + 6: the alert copy =============================================================


def _assert_repo3_copy(msg: str | None, *, measured_at) -> None:
    """★ EVERY copy claim for this arm on ONE rendered message — the `_assert_backup_copy` shape. Negative
    containments pass on "" and on None, so they are co-located with the positive claims and a substance
    floor rather than split into separate probes."""
    assert msg is not None
    # THE FLOOR IS ON THE CONSEQUENCE, NOT THE WHOLE MESSAGE. A `len(msg) > 800` floor was MEASURED vacuous
    # by the post-build mutation pass: the ~2.4 KB triage satisfies it single-handedly, so the consequence
    # could be replaced by an ungrammatical bag of the asserted phrases and still score a clean pass.
    arm = ESCALATION_ARMS[REPO3_FRESHNESS_KEY]
    assert len(arm.consequence) > 400, "the consequence lost its substance; the claims below go vacuous"

    # (1) it does not name a cause it cannot establish
    assert "CAUSE NOT ESTABLISHED" in msg
    # (1b) the DISJUNCTION disclosure -- the specific claim the 2026-08-28 alert-copy unit exists to add.
    #      Both sibling arms pin theirs; this arm shipped it unguarded until the post-build pass measured it.
    assert "The third, deletion-resistant copy" in msg
    assert "folds SEVERAL checks into one boolean" in msg
    assert "whether pgbackrest can report at all" in msg
    assert "so it does NOT tell you which failed" in msg
    # (2) ★ the divergence that this arm alone has, as a POSITIVE sentence rather than an omission. The lake
    #     arm's bare "NOTIFY-ONLY" sentence would be true-but-misdirecting here: a failing repo3 CAN refuse
    #     writes, through wal_lag. An operator holding this alert during exactly that incident must be sent
    #     to the right row.
    assert "not consulted by the ADR-0020 durability gate" in msg
    assert "pgbackrest pushes WAL to EVERY configured repository" in msg
    assert "CAN close the gate through the wal_lag row" in msg
    assert "If canonical writes are being refused right now, read wal_lag, not this row" in msg
    # (3) step 0 names the command that can ACTUALLY surface this arm's reason. ⚠ Assert the FLAG: without
    #     it the default limit shows ~2 hours of wal_lag rows and never reaches a days-old repo3 reason, so
    #     a bare `neuro probe report` here would be a discriminator that does not discriminate.
    assert "`neuro probe report --key repo3_freshness`" in msg
    # (3b) ** `recency:` IS TWO DISJUNCTS AND STEP (0) MUST NOT PICK ONE. The first draft said a `recency:`
    #      reason "means repo3 HAS a full backup but an old one" -- but the probe emits that same label for
    #      "has NO full backup", so the copy asserted a third copy EXISTS in the one state where it does
    #      not. Both members are asserted as PHRASES; deleting either discriminator reddens here.
    assert "names WHICH of two states" in msg
    assert "one containing 'NO full backup' means repo3 has NEVER carried one" in msg
    assert "one naming a backup label and an age means repo3 HAS a full backup but an old one" in msg
    # (4) the born-blocked disjunct, which records NO probe_reports row and would otherwise make step 0 come
    #     back empty and send an operator to the provider over a provisioning step.
    assert "'seeded; awaiting first repo3 backup'" in msg
    assert "born fail-closed provisioning state, not an outage" in msg
    # (5) the ignore-failure prefix hides repo3 behind a green unit — said in terms.
    assert "A GREEN neuro-backup.service DOES NOT MEAN repo3 SUCCEEDED" in msg
    # (6) ★ THE FOUR FAMILIES AS CONTRASTING PHRASES, NOT TOKENS. The log:274 measurement: a token pair
    #     survives a mutation that destroys the sentence binding them, so each discriminator is asserted as
    #     the clause that does the discriminating. Note 401/403 collide, which is why the lock case is
    #     stated FIRST and the credential case is stated as the residual.
    assert "A 403 whose text NAMES object lock or retention is the compliance lock" in msg
    assert "retention-SIZING fault, not an outage and not a credential" in msg
    assert "A 401, or a 403 that does NOT name object lock or retention, is a credential or key-scope" in msg
    assert "the message alone does not narrow this further" in msg
    assert "A 400 naming the endpoint or host, or a DNS resolution failure" in msg
    assert "A connection timeout carrying NO HTTP status at all is reachability" in msg
    # (7) the spend cap — the one failure this alert would otherwise mis-route entirely
    assert "rejects WRITES while READS keep succeeding" in msg
    # (8) what the PROVEN repos did, so this is not read as a general backup failure
    assert "each repository mints its OWN backup label seconds apart" in msg
    # (9) the age + last-good timestamp, read back from the DB rather than from the message that made it
    assert f"(last good: {measured_at})" in msg
    # (10) arm coordinates — an arm SWAP is the likeliest refactor error, and the `not in` half makes it RED
    assert msg.startswith("neuromancer IMMUTABLE THIRD COPY (pgbackrest repo3) BLOCKED")
    assert "no confirmed repo3 backup" in msg
    assert "THEN re-run neuro-backup.service." in msg
    assert "neuro-lake-mirror.service" not in msg
    # (11) the desktop-sftp procedure must not have leaked in
    assert "sshd" not in msg and "Get-WinEvent" not in msg and "tailscale" not in msg


def test_the_repo3_triage_is_not_the_off_cloud_one() -> None:
    """★ CRITERION 3. `require_triage` makes the choice EXPLICIT and cannot make it CORRECT — its own
    docstring says so — so this is where correctness is pinned."""
    assert ESCALATION_ARMS[REPO3_FRESHNESS_KEY].triage is OBJECT_STORE_REPO_TRIAGE
    assert ESCALATION_ARMS[REPO3_FRESHNESS_KEY].triage is not OFF_CLOUD_MIRROR_TRIAGE
    assert OBJECT_STORE_REPO_TRIAGE != OFF_CLOUD_MIRROR_TRIAGE


def test_the_repo3_triage_carries_no_expiring_fact() -> None:
    """No bucket name, no endpoint, no region, no price, no version, no retention number — those live in the
    root-only conf and the provider console, and a copy that hardcoded one would rot silently. ⚠ Digits are
    checked as WORD-BOUNDED tokens so the HTTP status codes the triage legitimately names do not trip it."""
    import re

    allowed = {"0", "1", "2", "3", "4", "8", "401", "403", "400"}  # step numbers, repo3, -8 days, statuses
    found = set(re.findall(r"\b\d+\b", OBJECT_STORE_REPO_TRIAGE))
    assert found <= allowed, (
        f"an unexplained number in the triage (expiring fact?): {sorted(found - allowed)}"
    )
    for token in ("backblazeb2.com", "us-west", "us-east", "$", "GiB", "2.59"):
        assert token not in OBJECT_STORE_REPO_TRIAGE


def test_the_repo3_message_is_pure_ascii_and_fits_the_notify_budget() -> None:
    """★ CRITERION 6, MEASURED rather than estimated. This is the longest alert the system emits; the budget
    is ntfy's documented 4096-byte default, and crossing it makes notify() raise, which downgrades the
    operator to the generic unit-name ping — the delivered-but-unactionable failure this lineage exists to
    escape. The headroom is printed on failure so a future author sees how much is left."""
    msg = _render(days=99, measured_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.UTC))
    assert msg.isascii(), f"non-ASCII: {[c for c in msg if not c.isascii()]}"
    size = len(msg.encode("utf-8"))
    assert size < 4096, f"repo3 alert is {size} B, over the 4096 B notify budget"


def _render(*, days: int, measured_at: _dt.datetime) -> str:
    from neuromancer_llm.governance.alert_triage import compose_block_alert

    return compose_block_alert(arm=ESCALATION_ARMS[REPO3_FRESHNESS_KEY], days=days, measured_at=measured_at)


# ============ pg: the row, the probe, and the notify-only PROOF ===========================================


def _seed(engine) -> None:
    with engine.connect() as conn:
        seed_all(conn)


def _row(engine, key: str):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT status, detail, measured_at, stale_after FROM neuro.system_health "
                    "WHERE health_key = :k"
                ),
                {"k": key},
            )
            .mappings()
            .one_or_none()
        )


@pytest.mark.pg
def test_seed_all_seeds_repo3_born_blocked_with_its_bound(repo) -> None:
    _seed(repo.engine)
    row = _row(repo.engine, REPO3_FRESHNESS_KEY)
    assert row is not None
    assert row["status"] == "blocked"  # born fail-closed
    assert row["measured_at"] == _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
    assert row["stale_after"] == _BOUND
    assert row["detail"] == "seeded; awaiting first repo3 backup"


@pytest.mark.pg
def test_probe_refuses_to_run_against_an_unseeded_row(repo) -> None:
    """The writer cannot self-seed, so a real read must never be orphaned with nowhere to record it."""
    with pytest.raises(Repo3ProbeError, match="not seeded"):
        run_repo3_probe(repo.engine, repo3_driver=lambda: Repo3Outcome(ok=True, detail="x"))


@pytest.mark.pg
def test_a_stale_repo3_flips_ONLY_its_own_row(repo) -> None:
    """★ CRITERION 2, DE-BLINDED PER E-18. The decoy rows are put into states that DIFFER from each other and
    from repo3's, and BOTH poles are asserted — a fixture where every row started identical would go green
    on a runner that flipped the wrong key, which is precisely the blindness E-18 names."""
    _seed(repo.engine)
    with repo.engine.begin() as conn:  # differing decoy states, so a mis-keyed write is visible
        conn.execute(
            text(
                "UPDATE neuro.system_health SET status='ok', detail='decoy-backup', measured_at=now() "
                "WHERE health_key=:k"
            ),
            {"k": BACKUP_FRESHNESS_KEY},
        )
    with pytest.raises(Repo3ProbeError):
        run_repo3_probe(
            repo.engine,
            repo3_driver=lambda: Repo3Outcome(ok=False, detail="recency: repo3 stalled"),
        )
    r3 = _row(repo.engine, REPO3_FRESHNESS_KEY)
    backup = _row(repo.engine, BACKUP_FRESHNESS_KEY)
    assert r3 is not None and backup is not None
    assert r3["status"] == "blocked" and r3["detail"] == "recency: repo3 stalled"
    assert backup["status"] == "ok" and backup["detail"] == "decoy-backup"  # UNTOUCHED, both fields


@pytest.mark.pg
def test_an_ok_probe_advances_measured_at_and_records_an_audit_row(repo) -> None:
    _seed(repo.engine)
    run_repo3_probe(repo.engine, repo3_driver=lambda: Repo3Outcome(ok=True, detail="repo3:LABEL"))
    row = _row(repo.engine, REPO3_FRESHNESS_KEY)
    assert row is not None and row["status"] == "ok"
    assert row["measured_at"] > _dt.datetime(2020, 1, 1, tzinfo=_dt.UTC)
    with repo.engine.connect() as conn:
        rep = conn.execute(
            text(
                "SELECT probe_key, status, report_text FROM neuro.probe_reports "
                "WHERE probe_key = :k ORDER BY probe_report_id DESC LIMIT 1"
            ),
            {"k": REPO3_FRESHNESS_KEY},
        ).one()
    assert rep.status == "ok" and rep.report_text == "repo3:LABEL"


@pytest.mark.pg
def test_a_blocked_probe_PERSISTS_BEFORE_it_raises(repo) -> None:
    """The probes.py contract: step (0) of the triage has something to read only if the reason is written
    before the raise. A raise-then-persist ordering would leave the operator with an alert and no reason."""
    _seed(repo.engine)
    with pytest.raises(Repo3ProbeError):
        run_repo3_probe(repo.engine, repo3_driver=lambda: Repo3Outcome(ok=False, detail="info-read: boom"))
    with repo.engine.connect() as conn:
        rep = conn.execute(
            text("SELECT status, report_text FROM neuro.probe_reports WHERE probe_key = :k"),
            {"k": REPO3_FRESHNESS_KEY},
        ).one()
    assert rep.status == "blocked" and rep.report_text == "info-read: boom"


@pytest.mark.pg
def test_a_raising_driver_is_recorded_then_re_raised(repo) -> None:
    _seed(repo.engine)

    def _boom() -> Repo3Outcome:
        raise RuntimeError("pgbackrest vanished")

    with pytest.raises(Repo3ProbeError):
        run_repo3_probe(repo.engine, repo3_driver=_boom)
    row = _row(repo.engine, REPO3_FRESHNESS_KEY)
    assert row is not None and row["status"] == "blocked" and "driver raised" in row["detail"]


@pytest.mark.pg
def test_the_durability_gate_STAYS_OPEN_with_repo3_blocked(repo) -> None:
    """★★ CRITERION 4c, BEHAVIOURALLY — THIS IS WHAT "NOTIFY-ONLY" MEANS, and it is the same shape as the
    B-7 lake pin (third instance). Both GATING arms healthy, repo3 in its maximally-stale born state, and a
    REAL `assert_durability_ok` consult must still pass. Inserting the repo3 key into GATE_CONSULTED_KEYS
    reddens this — which is the falsification the declarative pin above cannot provide."""
    _seed(repo.engine)
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE neuro.system_health SET status='ok', measured_at=now() "
                "WHERE health_key IN ('backup_freshness', 'wal_lag')"
            )
        )
    assert _row(repo.engine, REPO3_FRESHNESS_KEY)["status"] == "blocked"  # type: ignore[index]
    assert assert_durability_ok(repo.engine) is None  # the gate does NOT block


# ============ the escalation + the CLI verb ===============================================================


def _block_repo3(engine, *, age_days: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE neuro.system_health SET status='blocked', "
                "measured_at=now() - (:d * interval '1 day') WHERE health_key=:k"
            ),
            {"d": age_days, "k": REPO3_FRESHNESS_KEY},
        )


@pytest.mark.pg
def test_escalation_fires_only_past_the_onset_and_carries_every_copy_claim(repo) -> None:
    _seed(repo.engine)
    _block_repo3(repo.engine, age_days=1)
    assert evaluate_repo3_block_escalation(repo.engine) is None  # inside the 4d onset -> silent
    _block_repo3(repo.engine, age_days=10)
    _assert_repo3_copy(
        evaluate_repo3_block_escalation(repo.engine),
        measured_at=_row(repo.engine, REPO3_FRESHNESS_KEY)["measured_at"],  # type: ignore[index]
    )


@pytest.mark.pg
def test_escalation_is_silent_when_ok_or_missing(repo) -> None:
    assert evaluate_repo3_block_escalation(repo.engine) is None  # row missing entirely
    _seed(repo.engine)
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE neuro.system_health SET status='ok', measured_at=now() - interval '30 days' "
                "WHERE health_key=:k"
            ),
            {"k": REPO3_FRESHNESS_KEY},
        )
    assert evaluate_repo3_block_escalation(repo.engine) is None  # old but ok -> nothing to escalate


@pytest.mark.pg
def test_cli_repo3_escalate_notifies_exactly_the_evaluator_message(repo, monkeypatch) -> None:
    """★ EQUALITY on the notify() argument, not containment. Containment is MONOTONE: a CLI that APPENDED
    its own remedy would still contain the evaluator's message and pass — the log:242 two-layer defect."""
    _seed(repo.engine)
    _block_repo3(repo.engine, age_days=10)
    calls: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m: calls.append(m))
    expected = evaluate_repo3_block_escalation(repo.engine)  # an INDEPENDENT call, not the captured output
    r = _runner.invoke(app, ["probe", "repo3-escalate", "--lane", "test"])
    assert r.exit_code == 0, r.output
    assert "ESCALATED" in r.output and calls == [expected]


@pytest.mark.pg
def test_cli_repo3_escalate_is_a_silent_no_op_when_not_blocked(repo, monkeypatch) -> None:
    _seed(repo.engine)
    with repo.engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.system_health SET status='ok', measured_at=now() WHERE health_key=:k"),
            {"k": REPO3_FRESHNESS_KEY},
        )
    calls: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m: calls.append(m))
    r = _runner.invoke(app, ["probe", "repo3-escalate", "--lane", "test"])
    assert r.exit_code == 0 and "no alert" in r.output and calls == []


@pytest.mark.pg
def test_cli_repo3_escalate_override_fires_on_a_current_block(repo, monkeypatch) -> None:
    """The `--escalate-after-hours 0` knob is how the deploy runbook lands the E·16 proof without waiting
    four days for the pinned onset."""
    _seed(repo.engine)
    _block_repo3(repo.engine, age_days=0)
    calls: list[str] = []
    monkeypatch.setattr("neuromancer_llm.governance.notify.notify", lambda m: calls.append(m))
    assert _runner.invoke(app, ["probe", "repo3-escalate", "--lane", "test"]).output.count("no alert") == 1
    r = _runner.invoke(app, ["probe", "repo3-escalate", "--lane", "test", "--escalate-after-hours", "0"])
    assert r.exit_code == 0 and "ESCALATED" in r.output and len(calls) == 1


# ============ `neuro probe report --key` — the flag step (0) depends on ====================================


@pytest.mark.pg
def test_probe_report_key_filter_surfaces_a_buried_repo3_reason(repo) -> None:
    """★ THE FIXTURE FOR THE FLAG, and it reproduces the real burial rather than asserting the SQL.

    The repo3 reason is written FIRST and then buried under 40 wal_lag rows — the shape the live system
    produces, where the archiver probe writes a row every 15 minutes and repo3's cadence is two days. At the
    default limit the repo3 line is NOT reachable; with `--key` it is. Dropping the predicate reddens the
    second half, and dropping the burial would make the first half pass vacuously."""
    _seed(repo.engine)
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.probe_reports (probe_key, status, report_text) "
                "VALUES (:k, 'blocked', 'recency: repo3 needle')"
            ),
            {"k": REPO3_FRESHNESS_KEY},
        )
        for _ in range(40):
            conn.execute(
                text(
                    "INSERT INTO neuro.probe_reports (probe_key, status, report_text) "
                    "VALUES ('wal_lag', 'ok', 'archiver healthy')"
                )
            )
    unfiltered = _runner.invoke(app, ["probe", "report", "--lane", "test"])
    assert unfiltered.exit_code == 0
    assert "repo3 needle" not in unfiltered.output  # buried, exactly as in production
    filtered = _runner.invoke(app, ["probe", "report", "--lane", "test", "--key", REPO3_FRESHNESS_KEY])
    assert filtered.exit_code == 0 and "repo3 needle" in filtered.output
    assert "archiver healthy" not in filtered.output  # and the filter is exclusive, not merely additive


# ============ the post-vet folds: each behaviour added during the fold gets its own fixture ================


@pytest.mark.pg
def test_probe_report_renders_the_row_DETAIL_so_step_0_works_for_an_unprobed_row(repo) -> None:
    """★ THE FOLD THAT MADE THE TRIAGE'S OWN STEP (0) RUNNABLE.

    The triage tells the operator that if the detail still reads 'seeded; awaiting first repo3 backup', the
    row has never been probed — that is a provisioning state, not an outage. But `probe report` rendered only
    `status` and `measured_at`, and a freshly seeded row writes NO probe_reports row at all, so the operator
    following the copy literally would see nothing and go check the provider. The copy asserted a discriminator
    the command could not show — which is the defect this whole lineage is about, committed inside the repair.

    Removing `detail` from the render reddens this; so does dropping it from `RowStatus`/`status_all`."""
    _seed(repo.engine)
    out = _runner.invoke(app, ["probe", "report", "--lane", "test"]).output
    assert "repo3_freshness: status=blocked" in out
    assert "seeded; awaiting first repo3 backup" in out


@pytest.mark.pg
def test_probe_report_detail_also_exposes_a_gate_origin_flip_which_writes_no_probe_row(repo) -> None:
    """The OTHER state that records no probe_reports row: a gate-origin flip (drift/staleness) writes only
    `system_health.detail`. `alert_triage.py` had registered this as an open residual; rendering detail is
    what closes it, and this is the fixture that says so. ⚠ A test that only exercised the seeded row could
    not catch a render that special-cased the born detail — hence the divergent case, built explicitly."""
    _seed(repo.engine)
    with repo.engine.begin() as conn:
        conn.execute(
            text("UPDATE neuro.system_health SET status='blocked', detail=:d WHERE health_key=:k"),
            {"d": "stale_after drift: 9 days != pinned 8 days", "k": BACKUP_FRESHNESS_KEY},
        )
    out = _runner.invoke(app, ["probe", "report", "--lane", "test"]).output
    assert "stale_after drift: 9 days != pinned 8 days" in out


def test_verify_config_PRINTS_the_non_gating_remainder(tmp_path) -> None:
    """★ provisioning_invariants.py claims reporting `non_gating_repos` turns "repo3 is deliberately
    non-gating" into something the daily verify-config run PRINTS. Nothing printed it until this fold — the
    module was asserting an operator-visible consequence it did not produce. Both branches are asserted,
    because a render that only handled the populated case would leave the two-repo run silent about the basis."""
    from neuromancer_llm.governance import provisioning_invariants as pinv

    base = tmp_path / "base.conf"
    common = (
        "[global]\narchive-async=y\narchive-push-queue-max=32GiB\n"
        "repo1-retention-full-type=time\nrepo1-retention-full=30\n"
        "repo2-retention-full-type=time\nrepo2-retention-full=30\n"
    )
    base.write_text(common + "\n[neuro]\npg1-path=/x\n", encoding="utf-8")
    r = _runner.invoke(
        app, ["probe", "verify-config", "--conf", str(base), "--legacy-conf", str(tmp_path / "no")]
    )
    assert r.exit_code == 0, r.output
    assert "gate basis: [1, 2]; every configured repo gates" in r.output

    withrepo3 = tmp_path / "r3.conf"
    withrepo3.write_text(
        common + "repo3-retention-full-type=time\nrepo3-retention-full=30\n\n[neuro]\npg1-path=/x\n",
        encoding="utf-8",
    )
    r3 = _runner.invoke(
        app, ["probe", "verify-config", "--conf", str(withrepo3), "--legacy-conf", str(tmp_path / "no")]
    )
    assert r3.exit_code == 0, r3.output
    assert "gate basis: [1, 2]; NON-GATING (reported only): [3]" in r3.output
    assert pinv.resolve_gate_basis_repos() == frozenset({1, 2})


@pytest.mark.pg
def test_the_shared_evaluator_resolves_the_onset_ONLY_when_no_override_is_given(repo, monkeypatch) -> None:
    """★ block_escalation.py singles this out with a ⚠ as behaviour-preserving, and it had no fixture.

    An EAGER `resolve_onset()` — resolved before the override check — would raise on an absent pin even when
    the caller supplied an explicit override, breaking the operator/diagnostic knob the deploy runbook's E·16
    proof depends on. With the pin absent AND an override supplied, the call must still work."""
    _seed(repo.engine)
    _block_repo3(repo.engine, age_days=10)
    monkeypatch.setattr(_pi, "BASE_BACKUP_INTERVAL", None)
    with pytest.raises(ConfigurationError):  # no override -> the pin is resolved -> fail closed
        evaluate_repo3_block_escalation(repo.engine)
    # with an override the pin is never touched, so the alert still renders
    assert evaluate_repo3_block_escalation(repo.engine, escalate_after=_dt.timedelta(0)) is not None


def test_every_step_label_the_probe_can_emit_is_discriminated_by_the_triage() -> None:
    """★ THE MECHANICAL MAPPING PIN, so step (0) cannot drift away from the producer in prose.

    The probe emits exactly two step labels, and `recency:` covers TWO distinct states. The triage
    discriminates on those labels, so a new producer branch with a new label — or a re-worded state — must
    show up HERE rather than as an operator following a procedure with no branch for what they are holding.
    Derived from the driver's ACTUAL outputs, never from a hand-copied list."""
    infos = {
        "no-full": _info_json(ages_days={1: 1.0, 3: None}),
        "stale": _info_json(ages_days={1: 1.0, 3: 9.0}),
        "not-configured": _info_json(ages_days={1: 1.0, 2: 1.0}),
        "unparseable": "not json",
    }
    labels = {name: _driver(info)().detail.split(":")[0] for name, info in infos.items()}
    assert labels == {
        "no-full": "recency",
        "stale": "recency",
        "not-configured": "info-read",
        "unparseable": "info-read",
    }
    for label in set(labels.values()):
        assert f"`{label}:` reason" in OBJECT_STORE_REPO_TRIAGE, f"{label} is unhandled by step (0)"
    # ...and the TWO recency states are told apart rather than collapsed into one claim
    assert "NO full backup" in OBJECT_STORE_REPO_TRIAGE
    assert "an age means repo3 HAS a full backup" in OBJECT_STORE_REPO_TRIAGE


@pytest.mark.pg
def test_a_blocked_probe_does_NOT_advance_measured_at(repo) -> None:
    """★★ THE ARM'S ONLY ALERT DEPENDS ON THIS, and it survived the ENTIRE suite unpinned until the
    post-build mutation pass measured it.

    The daily escalation asks `(now() - measured_at) > onset` in SQL. If the BLOCKED branch advanced
    measured_at, a repo3 failing every 2-day cycle would reset that clock on every run, the 4-day onset would
    never be crossed, and `repo3-escalate` would return None forever — silently, with the row still reading
    'blocked' and every other test still green. repo3 has no per-cycle OnFailure ping of its own, so that is
    the whole alert gone. BOTH poles are asserted, so a mutation freezing measured_at on every path cannot
    pass either. Mirrors the lake arm's pin, which exists for exactly this reason."""
    _seed(repo.engine)
    before = _row(repo.engine, REPO3_FRESHNESS_KEY)["measured_at"]  # type: ignore[index]
    with pytest.raises(Repo3ProbeError):
        run_repo3_probe(repo.engine, repo3_driver=lambda: Repo3Outcome(ok=False, detail="recency: stalled"))
    assert _row(repo.engine, REPO3_FRESHNESS_KEY)["measured_at"] == before  # type: ignore[index]
    run_repo3_probe(repo.engine, repo3_driver=lambda: Repo3Outcome(ok=True, detail="repo3:L"))
    assert _row(repo.engine, REPO3_FRESHNESS_KEY)["measured_at"] > before  # type: ignore[index]


def test_a_FRESH_non_gating_repo_is_reported_WITHOUT_a_stale_marker() -> None:
    """The FALSE branch of the STALE marker, unpinned until the post-build pass measured it. Making the
    marker unconditional would tell an operator every non-gating repo is stale — a cause the code did not
    establish — and every existing assertion would still have passed."""
    ok, detail = _repo_freshness_from_info(
        _info_json(ages_days={1: 1.0, 2: 1.0, 3: 0.5}), stanza="neuro", bound=_BOUND, now=_NOW
    )
    assert ok is True
    assert "(non-gating, reported only: repo3:20260828-full-r3)" in detail
    assert "STALE" not in detail


@pytest.mark.pg
def test_probe_run_repo3_exits_1_cleanly_instead_of_a_traceback(repo, monkeypatch) -> None:
    """The `Repo3ProbeError` arm in `neuro probe run`'s except tuple, which had no fixture. `OnFailure=`
    keys on the exit code and an unhandled traceback would also be non-zero — so what this pins is the CLEAN
    failure the other three producers give: a one-line reason, not a stack dump."""
    _seed(repo.engine)
    monkeypatch.setattr(
        "neuromancer_llm.governance.probe_registry.make_repo3_recency_driver",
        lambda **kw: lambda: Repo3Outcome(ok=False, detail="recency: repo3 has NO full backup"),
    )
    r = _runner.invoke(app, ["probe", "run", "--key", REPO3_FRESHNESS_KEY, "--lane", "test"])
    assert r.exit_code == 1
    assert "probe repo3_freshness failed" in r.output
    assert "Traceback" not in r.output
