"""GO-D-seed (pre-cliff item 1): the durability-row provisioning surface — seed / reconcile / status.

RED@cd45a4c = ModuleNotFoundError (neuromancer_llm.governance.durability absent) + `neuro db durability`
does not exist. Probes the folds from the pre-cliff adversarial vet (w610z9b1v):
  - seed born fail-closed + idempotent; the pin-absent path fails closed BEFORE any write (LATE-BOUND);
  - reconcile RE-ALIGNS a non-NULL drifted sentinel but NEVER fills a NULL (the fail-open guard, fold M1) and
    never touches status/measured_at;
  - a BOUND-LESS row (the item-3 wal_lag pattern, fold M3) seeds NULL, reconciles as a no-op, reports non-drift;
  - status reports missing/drift and exits non-zero (the CI / A2-16-provisioning check);
  - the delegation seed_backup_freshness -> seed_row is behavior-preserving (born detail + commit, fold M4);
  - the registry covers every health_key the gate consults.
Grant-boundary (seed=registrar|admin, reconcile=admin-ONLY) is the L7 probe in
tests/redteam/test_rt_roles.py::test_rt_backup_freshness_grant_boundary.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from neuromancer_llm.cli.app import app
from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.durability import (
    BACKUP_FRESHNESS_ROW,
    DURABILITY_KEYS,
    DurabilityRow,
    reconcile_all,
    seed_all,
    seed_row,
    status_all,
)
from neuromancer_llm.governance.freshness import BACKUP_FRESHNESS_KEY, seed_backup_freshness
from neuromancer_llm.governance.health import (
    GATE_CONSULTED_KEYS,
    DurabilityGateError,
    assert_durability_ok,
)
from neuromancer_llm.governance.lake_freshness import LAKE_MIRROR_FRESHNESS_KEY
from neuromancer_llm.governance.repo3_freshness import REPO3_FRESHNESS_KEY
from neuromancer_llm.governance.wal_freshness import WAL_LAG_KEY

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
_EIGHT_DAYS = _dt.timedelta(days=8)
_runner = CliRunner()


def _read(engine, key: str = BACKUP_FRESHNESS_KEY):
    with engine.begin() as conn:
        return (
            conn.execute(
                text(
                    "SELECT status, measured_at, stale_after, detail "
                    "FROM neuro.system_health WHERE health_key = :k"
                ),
                {"k": key},
            )
            .mappings()
            .one_or_none()
        )


def _seed(engine, rows=None):
    with engine.connect() as conn:
        return seed_all(conn) if rows is None else seed_all(conn, rows)


def _reconcile(engine, rows=None):
    with engine.connect() as conn:
        return reconcile_all(conn) if rows is None else reconcile_all(conn, rows)


def _status(engine, rows=None):
    with engine.connect() as conn:
        return status_all(conn) if rows is None else status_all(conn, rows)


# ---- seed (born fail-closed, idempotent) ------------------------------------------------------------


@pytest.mark.pg
def test_seed_all_seeds_backup_freshness_born_blocked(repo):
    assert _seed(repo.engine) == {
        BACKUP_FRESHNESS_KEY: True,
        WAL_LAG_KEY: True,
        LAKE_MIRROR_FRESHNESS_KEY: True,
        REPO3_FRESHNESS_KEY: True,
    }
    row = _read(repo.engine)
    assert row is not None
    assert row["status"] == "blocked"  # born fail-closed
    assert row["measured_at"] == _EPOCH
    assert row["stale_after"] == _EIGHT_DAYS  # the provisioning sentinel == the pin
    assert row["detail"] == "seeded; awaiting first verified backup"  # born detail preserved (fold M4)


@pytest.mark.pg
def test_seed_all_seeds_lake_mirror_freshness_born_blocked_and_non_gating(repo):
    # B-7: the blob-lake mirror is a durability SIGNAL row (bound-carrying, 3d) provisioned via the ONE seed
    # surface — but DELIBERATELY NON-GATING (the owner fork ruling): its key is in DURABILITY_KEYS yet NOT in
    # GATE_CONSULTED_KEYS, so a stale lake mirror alarms but never blocks a canonical write.
    _seed(repo.engine)
    row = _read(repo.engine, LAKE_MIRROR_FRESHNESS_KEY)
    assert row is not None
    assert row["status"] == "blocked" and row["measured_at"] == _EPOCH
    assert row["stale_after"] == _dt.timedelta(days=3)  # the pinned bound (provisioning sentinel)
    assert row["detail"] == "seeded; awaiting first verified lake mirror"
    assert LAKE_MIRROR_FRESHNESS_KEY in DURABILITY_KEYS
    assert LAKE_MIRROR_FRESHNESS_KEY not in GATE_CONSULTED_KEYS  # notify-only, never gates (fork ruling)


@pytest.mark.pg
def test_seed_all_seeds_wal_lag_boundless_born_blocked(repo):
    # GO-D-wal: the real item-3 row — the ONE seed surface covers it with zero CLI change (the owner's
    # "one surface" constraint, realized; promotes the synthetic bound-less proof below to a real member).
    _seed(repo.engine)
    row = _read(repo.engine, WAL_LAG_KEY)
    assert row is not None
    assert row["status"] == "blocked"  # born fail-closed: the gate BLOCKs until the first healthy probe
    assert row["measured_at"] == _EPOCH
    assert row["stale_after"] is None  # bound-less: row PRESENCE is the provisioning proof
    assert row["detail"] == "seeded; awaiting first WAL-archiver probe"
    st = {s.health_key: s for s in _status(repo.engine)}[WAL_LAG_KEY]
    assert st.present is True and st.has_bound is False and st.drift is False


@pytest.mark.pg
def test_seed_all_idempotent(repo):
    assert _seed(repo.engine) == {
        BACKUP_FRESHNESS_KEY: True,
        WAL_LAG_KEY: True,
        LAKE_MIRROR_FRESHNESS_KEY: True,
        REPO3_FRESHNESS_KEY: True,
    }
    assert _seed(repo.engine) == {
        BACKUP_FRESHNESS_KEY: False,
        WAL_LAG_KEY: False,
        LAKE_MIRROR_FRESHNESS_KEY: False,
        REPO3_FRESHNESS_KEY: False,
    }  # already present
    row = _read(repo.engine)
    assert row is not None and row["measured_at"] == _EPOCH  # undisturbed


def test_seed_row_fails_closed_before_write_when_pin_absent(monkeypatch):
    # fold M4c: the bound is a LATE-BOUND callable (resolve_backup_stale_after), re-resolved each seed — so a
    # monkeypatched-absent pin raises BEFORE any DB statement. Baking the resolved value at import would
    # silently pass here.
    monkeypatch.setattr("neuromancer_llm.governance.freshness.BACKUP_STALE_AFTER", None)

    class _NoExec:
        def execute(self, *a, **k):
            raise AssertionError("seed_row must not touch the DB when the pin is absent")

        def commit(self):
            raise AssertionError("seed_row must not commit when the pin is absent")

    with pytest.raises(ConfigurationError):
        seed_row(_NoExec(), BACKUP_FRESHNESS_ROW)


# ---- reconcile (re-align the sentinel; NEVER fill a NULL) --------------------------------------------


@pytest.mark.pg
def test_reconcile_realigns_drift_but_leaves_status_and_measured_at(repo):
    _seed(repo.engine)
    marker = _dt.datetime(2020, 6, 15, 12, 0, 0, tzinfo=_dt.UTC)  # a fixed, distinguishable non-epoch stamp
    with repo.engine.begin() as conn:  # hand-drift the sentinel + set status/measured_at to prove scoping
        conn.execute(
            text(
                "UPDATE neuro.system_health SET stale_after='7 days', status='ok', measured_at=:m "
                "WHERE health_key = :k"
            ),
            {"m": marker, "k": BACKUP_FRESHNESS_KEY},
        )
    # all rows present; only the hand-drifted backup row re-aligns. wal_lag is bound-less -> (True, 0); the
    # lake row is bound-carrying but was seeded AT the pin (not drifted) -> (True, 0).
    assert _reconcile(repo.engine) == {
        BACKUP_FRESHNESS_KEY: (True, 1),
        WAL_LAG_KEY: (True, 0),
        LAKE_MIRROR_FRESHNESS_KEY: (True, 0),
        REPO3_FRESHNESS_KEY: (True, 0),
    }
    row = _read(repo.engine)
    assert row is not None
    assert row["stale_after"] == _EIGHT_DAYS  # re-aligned to the pin
    assert row["status"] == "ok"  # NOT touched (reconcile fixes only the sentinel)
    # NOT touched — EXACT equality: a reconcile that re-ran now() would be a stale->fresh fail-open at the
    # gate ((now() - measured_at) > bound), and a later timestamp would redden this equality.
    assert row["measured_at"] == marker


@pytest.mark.pg
def test_reconcile_never_fills_null_sentinel_gate_stays_blocked(repo):
    # fold M1 (the headline fail-open): a BARE row (reachable via a non-seed INSERT) with stale_after NULL.
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO neuro.system_health (health_key, status, measured_at) VALUES (:k, 'ok', now())"
            ),
            {"k": BACKUP_FRESHNESS_KEY},
        )
    # reconcile must NOT fill the NULL — filling it would flip the gate to FRESH with no backup recorded.
    # (wal_lag + lake_mirror_freshness were never inserted here -> (False, 0); the loud-missing report is the
    # CLI's job.)
    assert _reconcile(repo.engine) == {
        BACKUP_FRESHNESS_KEY: (True, 0),
        WAL_LAG_KEY: (False, 0),
        LAKE_MIRROR_FRESHNESS_KEY: (False, 0),
        REPO3_FRESHNESS_KEY: (False, 0),
    }
    row = _read(repo.engine)
    assert row is not None and row["stale_after"] is None  # still NULL
    with pytest.raises(DurabilityGateError):  # gate STAYS fail-closed (branch 2: stale_after IS NULL)
        assert_durability_ok(repo.engine)


# ---- status (report missing / drift; non-zero) ------------------------------------------------------


@pytest.mark.pg
def test_status_reports_missing_then_ok_then_drift(repo):
    st = _status(repo.engine)  # unseeded (repo truncated)
    # ⚠ A KEYSET PIN, NOT A COUNT (converted 2026-08-28 with the repo3 arm). `len(st) == 3` was the fourth
    # literal in this file encoding "how many durability rows exist", and a count says nothing about WHICH —
    # it would stay green if an arm were swapped for another. Keying on DURABILITY_KEYS makes the next arm a
    # pure append here, which is the property the whole one-surface registry exists to have. The registry
    # ORDER is pinned separately, because status_all's output order is what the CLI renders.
    assert {s.health_key for s in st} == DURABILITY_KEYS
    assert st[0].health_key == BACKUP_FRESHNESS_KEY  # registry order: backup first
    assert all(s.present is False for s in st)
    _seed(repo.engine)
    st0 = _status(repo.engine)[0]
    assert st0.present is True and st0.drift is False and st0.status == "blocked"
    with repo.engine.begin() as conn:  # hand-drift the sentinel
        conn.execute(
            text("UPDATE neuro.system_health SET stale_after='7 days' WHERE health_key = :k"),
            {"k": BACKUP_FRESHNESS_KEY},
        )
    assert _status(repo.engine)[0].drift is True


# ---- the item-3 (wal_lag) pattern: a bound-less row is a PURE APPEND ---------------------------------


@pytest.mark.pg
def test_boundless_row_seeds_null_reconciles_noop_reports_nondrift(repo):
    # fold M3: the item-3 wal_lag row carries NO stale_after bound. This synthetic bound-less row proves the
    # generic ops treat it as a genuine zero-op append (never a false-drift the operator learns to ignore).
    walish = (
        DurabilityRow(health_key="__test_boundless__", born_detail="synthetic", stale_after_bound=None),
    )
    assert _seed(repo.engine, walish) == {"__test_boundless__": True}
    row = _read(repo.engine, "__test_boundless__")
    assert row is not None
    assert row["status"] == "blocked"
    assert row["stale_after"] is None  # bound-less -> NULL; ROW PRESENCE is its provisioning proof
    assert _reconcile(repo.engine, walish) == {"__test_boundless__": (True, 0)}  # no sentinel to re-align
    st = _status(repo.engine, walish)[0]
    assert st.present is True and st.has_bound is False and st.drift is False  # never "drifted"


# ---- delegation + registry coherence ----------------------------------------------------------------


@pytest.mark.pg
def test_seed_backup_freshness_delegation_is_behavior_preserving(repo):
    with repo.engine.connect() as conn:
        assert (
            seed_backup_freshness(conn) is True
        )  # commits on a bare Connection (test_backup_probe contract)
    row = _read(repo.engine)
    assert row is not None
    assert row["status"] == "blocked" and row["measured_at"] == _EPOCH and row["stale_after"] == _EIGHT_DAYS
    assert row["detail"] == "seeded; awaiting first verified backup"
    with repo.engine.connect() as conn:
        assert seed_backup_freshness(conn) is False  # idempotent, same as seed_row(BACKUP_FRESHNESS_ROW)


def test_every_gate_consulted_key_has_a_registry_row():
    # ENFORCEABLE coherence (not a tautology): every health_key the gate consults (health.GATE_CONSULTED_KEYS)
    # MUST have a provisioning row (durability.DURABILITY_KEYS). So item 3 is a genuine TWO-edit change — adding
    # a wal_lag gate branch (-> GATE_CONSULTED_KEYS) without its registry row reddens this.
    assert GATE_CONSULTED_KEYS <= DURABILITY_KEYS


# ---- CLI (lane-verified; happy path on --lane test, mismatch fails closed) ---------------------------


@pytest.mark.pg
def test_cli_seed_status_reconcile_on_test_lane(repo):
    # repo truncates system_health; the CLI hits the SAME session DB (lane='test') via NEURO_DATABASE_URL.
    assert _runner.invoke(app, ["db", "durability", "status", "--lane", "test"]).exit_code == 1  # unseeded
    r = _runner.invoke(app, ["db", "durability", "seed", "--lane", "test"])
    # backup_freshness + wal_lag + lake_mirror_freshness (B-7) + repo3_freshness (§A·72), ONE surface —
    # derived from the registry so a future arm does not need a fifth edit of a bare literal here.
    assert r.exit_code == 0 and f"{len(DURABILITY_KEYS)} inserted" in r.stdout
    r = _runner.invoke(app, ["db", "durability", "status", "--lane", "test"])
    assert r.exit_code == 0 and "provisioned + consistent" in r.stdout
    assert _runner.invoke(app, ["db", "durability", "reconcile", "--lane", "test"]).exit_code == 0


@pytest.mark.pg
def test_cli_status_never_calls_a_blocked_row_ok(repo):
    """RENDER-HONESTY (§A-62 / precedent 19) in a SAFETY surface. Rows are BORN status='blocked', so a
    freshly seeded DB is exactly the case where this command used to print `ok status=blocked` — the word
    `ok` asserting a health verdict the command never established.

    The provisioning verdict must STAY GREEN here (exit 0): born-blocked is correct fail-closed
    provisioning, and gating on status would break the A2-16 seed-then-green sequence pinned above. So this
    pins four facts together — green exit, real status visible, no `ok` in front of it, and the operator
    pointed at the operational read."""
    _runner.invoke(app, ["db", "durability", "seed", "--lane", "test"])
    r = _runner.invoke(app, ["db", "durability", "status", "--lane", "test"])
    assert r.exit_code == 0  # provisioning green: a born-blocked row is BY DESIGN, never a provisioning fault
    assert "status=blocked" in r.stdout  # the real status is rendered, not suppressed
    assert "ok status=blocked" not in r.stdout  # ...and is no longer prefixed by a bare `ok` (the falsehood)
    assert "probe report" in r.stdout  # the operator is pointed at the OPERATIONAL read


@pytest.mark.pg
def test_cli_status_never_explains_a_blocked_row_as_merely_born_blocked(repo):
    """The post-build vet's CONFIRMED finding: the first fix traded a false VERDICT for a false CAUSE.

    The note is keyed on `status != 'ok'`, but born-blocked is only ONE route there — a probed-then-FAILED
    row is written 'blocked' by the probe producers, and health.py's drift/staleness CAS flips 'ok'->'blocked'
    long after the first probe. A parenthetical saying "a row is born blocked until its first probe" therefore
    explained a LIVE alarm away as a fresh seed. The seeded-only test above cannot catch this, because there
    the benign cause happens to be true — so this fixture builds the DIVERGENT case explicitly: a row whose
    measured_at is seconds old, i.e. unambiguously probed, and still blocked."""
    _runner.invoke(app, ["db", "durability", "seed", "--lane", "test"])
    with repo.engine.begin() as conn:  # a probe RAN and FAILED: blocked, but measured_at is now, not epoch
        conn.execute(
            text(
                "UPDATE neuro.system_health SET status='blocked', detail='backup FAILED', "
                "measured_at=now() WHERE health_key = :k"
            ),
            {"k": BACKUP_FRESHNESS_KEY},
        )
    r = _runner.invoke(app, ["db", "durability", "status", "--lane", "test"])
    assert r.exit_code == 0  # still a PROVISIONING pass — the row is present and its sentinel matches
    assert "status=blocked" in r.stdout
    assert "born blocked until its first probe" not in r.stdout  # the exact causal falsehood, gone
    assert "does not say WHY" in r.stdout  # ...replaced by an explicit disclaimer of the cause


@pytest.mark.pg
def test_cli_lane_mismatch_fails_closed_no_write(repo):
    # default --lane canonical against the lane='test' session DB -> fail closed at assert_lane, NO write.
    r = _runner.invoke(app, ["db", "durability", "seed"])  # default lane=canonical
    assert r.exit_code == 1
    assert _read(repo.engine) is None  # nothing seeded


@pytest.mark.pg
def test_cli_reconcile_missing_row_fails_loud(repo):
    # reconcile on an unseeded DB runs (0 re-aligned) then exits non-zero on the missing row (directs to seed).
    r = _runner.invoke(app, ["db", "durability", "reconcile", "--lane", "test"])
    assert r.exit_code == 1
    assert "re-aligned" in r.stdout  # it ran before failing loud


@pytest.mark.pg
def test_cli_status_drift_fails_loud(repo):
    _runner.invoke(app, ["db", "durability", "seed", "--lane", "test"])
    with repo.engine.begin() as conn:  # hand-drift the sentinel (non-NULL)
        conn.execute(
            text("UPDATE neuro.system_health SET stale_after='7 days' WHERE health_key = :k"),
            {"k": BACKUP_FRESHNESS_KEY},
        )
    r = _runner.invoke(app, ["db", "durability", "status", "--lane", "test"])
    assert r.exit_code == 1 and "DRIFT" in r.stdout
