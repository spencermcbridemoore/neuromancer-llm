"""The /pgdata disk-pressure alarm (A2-8 follow-on) — pure evaluate + fail-closed threshold pin.

evaluate_disk_pressure is READ-ONLY (a statvfs, no DB): at/over the threshold -> an actionable message; under
-> None; an unpinned threshold or an unstattable path -> ConfigurationError (fail loud). Mirrors
escalation.py's pure-evaluate shape; the CLI (`neuro probe disk`) notify()s the message. disk_usage is
scripted so the tests are deterministic (a real statvfs of the test host would be nondeterministic).
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.disk_pressure import (
    evaluate_disk_pressure,
    resolve_disk_pressure_threshold,
)


def _usage(used_frac: float, total_gib: float = 100.0) -> SimpleNamespace:
    total = int(total_gib * 1024**3)
    used = int(total * used_frac)
    return SimpleNamespace(total=total, used=used, free=total - used)


@pytest.fixture
def at(monkeypatch):
    """Script shutil.disk_usage to a given used/total fraction (deterministic; no real statvfs)."""

    def _set(used_frac: float) -> None:
        monkeypatch.setattr(shutil, "disk_usage", lambda _path: _usage(used_frac))

    return _set


def test_over_threshold_returns_actionable_message(at):
    at(0.92)
    msg = evaluate_disk_pressure("/pgdata")
    assert msg is not None
    assert "DISK PRESSURE" in msg and "92%" in msg and "/pgdata" in msg
    assert "ACTION" in msg  # the message carries the operator action, not just the symptom


def test_under_threshold_returns_none(at):
    at(0.50)
    assert evaluate_disk_pressure("/pgdata") is None


def test_at_threshold_fires_not_strictly_over(at):
    # the alarm fires at/past the threshold (used_frac >= bound), never strictly over — a `>` regression would
    # return None here. used==total (frac 1.0) vs threshold 1.0 is a float-EXACT boundary (no rounding).
    at(1.0)
    assert evaluate_disk_pressure("/pgdata", threshold=1.0) is not None


def test_threshold_override_forces_alarm(at):
    # the induced-failure knob (the daily timer never passes it): threshold 0.0 -> any occupancy fires.
    at(0.01)
    assert evaluate_disk_pressure("/pgdata", threshold=0.0) is not None


def test_default_threshold_resolves():
    assert resolve_disk_pressure_threshold() == 0.85


def test_threshold_pin_absent_fails_closed(monkeypatch):
    monkeypatch.setattr("neuromancer_llm.governance.disk_pressure.DISK_PRESSURE_THRESHOLD", None)
    with pytest.raises(ConfigurationError, match="threshold pin is absent"):
        resolve_disk_pressure_threshold()


def test_evaluate_fails_closed_when_pin_absent(at, monkeypatch):
    at(0.90)  # over any real threshold, but the pin is gone -> fail closed, never a silent default
    monkeypatch.setattr("neuromancer_llm.governance.disk_pressure.DISK_PRESSURE_THRESHOLD", None)
    with pytest.raises(ConfigurationError, match="threshold pin is absent"):
        evaluate_disk_pressure("/pgdata")


def test_unstattable_path_fails_loud(monkeypatch):
    # a missing/unreadable /pgdata is itself an alertable condition — never a silent skip.
    def _raise(_path):
        raise FileNotFoundError("no such path")

    monkeypatch.setattr(shutil, "disk_usage", _raise)
    with pytest.raises(ConfigurationError, match="cannot stat"):
        evaluate_disk_pressure("/nonexistent")
