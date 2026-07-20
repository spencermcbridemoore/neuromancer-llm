"""Precedent-wide timeout-typing pass (landed WITH governance/notify.py): a bare read-timeout from urllib is a
TimeoutError, NOT a urllib.error.URLError subclass, so without a dedicated arm it escapes _get / _post_capture
UNTYPED (a caller catching VLLMAdapterError would miss it). These pure tests (urllib.request.urlopen mocked; no
server, no GPU, no marker — the test_rt_reader idiom of driving the client internals directly) pin that BOTH
transport methods map a TimeoutError to the typed VLLMAdapterError. socket.timeout is an alias of TimeoutError
since 3.10, so the single arm covers both spellings.
"""

from __future__ import annotations

import urllib.request

import pytest

from neuromancer_llm.capture.adapters.vllm import VLLMAdapterError, VLLMClient


def _raise_timeout(*_a: object, **_k: object) -> object:
    raise TimeoutError("timed out")


def test_get_timeout_maps_to_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _raise_timeout)
    client = VLLMClient("http://127.0.0.1:1", timeout=0.01)
    with pytest.raises(VLLMAdapterError, match="timed out after"):
        client._get("/v1/models")


def test_post_capture_timeout_maps_to_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _raise_timeout)
    client = VLLMClient("http://127.0.0.1:1", timeout=0.01)
    with pytest.raises(VLLMAdapterError, match="timed out after"):
        client._post_capture("/v1/completions", {"prompt": "x"})
