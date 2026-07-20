"""GO-D-wal signal-staleness follow-on: the wal_lag freshness contract module (the carved neutral home).

Pure probes for governance/wal_freshness.py: the staleness-bound pin resolves; an absent pin fails closed
(the freshness.py::resolve_backup_stale_after idiom); and the bound clears the archiver-probe cadence + margin
(the mis-pin floor — a bound below the cadence would false-flip a merely-late probe). The gate-side branch
that CONSUMES this contract is probed in tests/test_health_gate.py; the row's provisioning in
tests/test_durability_seed.py.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.wal_freshness import (
    ARCHIVER_PROBE_INTERVAL,
    WAL_LAG_KEY,
    WAL_LAG_MARGIN,
    resolve_archiver_probe_interval,
    resolve_wal_lag_stale_after,
)


def test_wal_lag_key_value():
    assert WAL_LAG_KEY == "wal_lag"


def test_wal_lag_bound_resolves_to_one_hour():
    assert resolve_wal_lag_stale_after() == _dt.timedelta(hours=1)


def test_wal_lag_bound_absent_fails_closed(monkeypatch):
    # the resolve idiom: an unpinned (None) bound raises ConfigurationError, never a silent unpinned pass.
    monkeypatch.setattr("neuromancer_llm.governance.wal_freshness.WAL_LAG_STALE_AFTER", None)
    with pytest.raises(ConfigurationError, match="unpinned|absent"):
        resolve_wal_lag_stale_after()


def test_wal_lag_bound_clears_cadence_plus_margin():
    # the mis-pin floor (the PROVISIONING_MARGIN inequality analog): the staleness bound must exceed the
    # archiver-probe cadence + the slack margin, so a merely-late probe never false-flips the gate.
    assert resolve_wal_lag_stale_after() >= resolve_archiver_probe_interval() + WAL_LAG_MARGIN


def test_archiver_probe_interval_resolves():
    assert resolve_archiver_probe_interval() == _dt.timedelta(minutes=15) == ARCHIVER_PROBE_INTERVAL


def test_archiver_probe_interval_absent_fails_closed(monkeypatch):
    # graduated to a GATING pin by wal D4 (verify_pgbackrest_config assertion 7): an unpinned cadence raises
    # rather than silently defaulting (the resolve_wal_lag_stale_after / resolve_base_backup_interval idiom).
    monkeypatch.setattr("neuromancer_llm.governance.wal_freshness.ARCHIVER_PROBE_INTERVAL", None)
    with pytest.raises(ConfigurationError, match="pin is absent"):
        resolve_archiver_probe_interval()
