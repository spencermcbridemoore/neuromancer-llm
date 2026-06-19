"""Marker detection hooks + expected-skips golden.

The detection hooks (conftest) decide whether gpu/api/network/pg/seam tests run; absent capabilities
skip VISIBLY. This pins (a) the declared marker set, (b) the no-capability baseline against a committed
golden, and (c) that the hooks actually respond to their capability env — so a marker can never silently
swallow the suite, and detection can't drift unnoticed.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

EXPECTED_MARKERS = {"gpu", "api", "network", "pg", "seam"}
GOLDEN = json.loads((Path(__file__).parent / "expected_skips.json").read_text(encoding="utf-8"))


def test_declared_markers_match_expected():
    cfg = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {entry.split(":", 1)[0].strip() for entry in cfg["tool"]["pytest"]["ini_options"]["markers"]}
    assert declared == EXPECTED_MARKERS, f"declared markers {declared} != {EXPECTED_MARKERS}"


def test_no_capability_baseline_matches_golden(markers_api, monkeypatch):
    # Force every capability ABSENT: clear the env flags and make docker/nvidia undiscoverable.
    for var in (
        "NEURO_TEST_GPU",
        "NEURO_TEST_API",
        "NEURO_TEST_NETWORK",
        "NEURO_TEST_DATABASE_URL",
        "NEURO_TEST_AZURITE",
        "AZURE_STORAGE_CONNECTION_STRING",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    status = markers_api["status"]()
    expected = {k: v for k, v in GOLDEN.items() if not k.startswith("_")}
    assert status == expected, f"detection baseline {status} drifted from golden {expected}"


def test_detection_hooks_respond_to_capability_env(markers_api, monkeypatch):
    detectors = markers_api["detectors"]
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # isolate from ambient docker/nvidia

    monkeypatch.setenv("NEURO_TEST_GPU", "1")
    assert detectors["gpu"]() is True
    monkeypatch.setenv("NEURO_TEST_API", "1")
    assert detectors["api"]() is True
    monkeypatch.setenv("NEURO_TEST_NETWORK", "1")
    assert detectors["network"]() is True
    monkeypatch.setenv("NEURO_TEST_DATABASE_URL", "postgresql+psycopg://x/y")
    assert detectors["pg"]() is True
