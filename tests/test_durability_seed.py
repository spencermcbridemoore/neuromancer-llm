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
    assert _seed(repo.engine) == {BACKUP_FRESHNESS_KEY: True}
    row = _read(repo.engine)
    assert row is not None
    assert row["status"] == "blocked"  # born fail-closed
    assert row["measured_at"] == _EPOCH
    assert row["stale_after"] == _EIGHT_DAYS  # the provisioning sentinel == the pin
    assert row["detail"] == "seeded; awaiting first verified backup"  # born detail preserved (fold M4)


@pytest.mark.pg
def test_seed_all_idempotent(repo):
    assert _seed(repo.engine) == {BACKUP_FRESHNESS_KEY: True}
    assert _seed(repo.engine) == {BACKUP_FRESHNESS_KEY: False}  # already present
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
    assert _reconcile(repo.engine) == {BACKUP_FRESHNESS_KEY: (True, 1)}  # present, 1 re-aligned
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
    assert _reconcile(repo.engine) == {BACKUP_FRESHNESS_KEY: (True, 0)}  # present, 0 re-aligned
    row = _read(repo.engine)
    assert row is not None and row["stale_after"] is None  # still NULL
    with pytest.raises(DurabilityGateError):  # gate STAYS fail-closed (branch 2: stale_after IS NULL)
        assert_durability_ok(repo.engine)


# ---- status (report missing / drift; non-zero) ------------------------------------------------------


@pytest.mark.pg
def test_status_reports_missing_then_ok_then_drift(repo):
    st = _status(repo.engine)  # unseeded (repo truncated)
    assert len(st) == 1 and st[0].health_key == BACKUP_FRESHNESS_KEY
    assert st[0].present is False
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
    assert r.exit_code == 0 and "1 inserted" in r.stdout
    r = _runner.invoke(app, ["db", "durability", "status", "--lane", "test"])
    assert r.exit_code == 0 and "provisioned + consistent" in r.stdout
    assert _runner.invoke(app, ["db", "durability", "reconcile", "--lane", "test"]).exit_code == 0


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
