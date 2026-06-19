"""Example capability-gated tests — they run only when the capability is present, else SKIP VISIBLY
via the conftest detection hooks. In the Stage-1 scaffold these are placeholders demonstrating the
skip machinery; Stage 2 replaces them with real GPU/API/network exercises.
"""

from __future__ import annotations

import pytest


@pytest.mark.gpu
def test_gpu_capability(markers_api):
    assert markers_api["detectors"]["gpu"](), "gpu marker ran but no GPU detected"


@pytest.mark.api
def test_api_capability(markers_api):
    assert markers_api["detectors"]["api"](), "api marker ran but no API credentials detected"


@pytest.mark.network
def test_network_capability(markers_api):
    assert markers_api["detectors"]["network"](), "network marker ran but no network detected"
