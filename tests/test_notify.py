"""GO-D-notify (A2-12) probes — the ntfy.sh send is fail-LOUD (ADR-0019): a missing topic raises BEFORE any
network call (no default-topic fallback), and BOTH a non-2xx HTTP response and a transport error raise. Pure
unit tests — `urllib.request.urlopen` is mocked, no network, no marker."""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance import notify as notify_mod
from neuromancer_llm.governance.notify import NotifyError, notify


class _FakeResp:
    """Minimal context-manager stand-in for the urlopen response (2xx success path)."""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_notify_missing_topic_raises_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) no topic (arg None AND env unset) -> ConfigurationError, and urlopen is NEVER called — there is
    no default-topic fallback that could publish an alert to a wrong/guessable topic."""
    monkeypatch.delenv(notify_mod.ENV_NTFY_TOPIC, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ConfigurationError):
        notify("hello")
    assert calls == [], "urlopen must NOT be called when no topic is configured"


def test_notify_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) a non-2xx HTTP response -> NotifyError (the server error body is surfaced, never swallowed)."""

    def _raise_http(req: urllib.request.Request, timeout: float | None = None) -> object:
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", None, io.BytesIO(b"boom"))

    monkeypatch.setattr(urllib.request, "urlopen", _raise_http)
    with pytest.raises(NotifyError) as excinfo:
        notify("hello", topic="sekret-topic")
    # pin that the status + server body are SURFACED (a broad-except / swallowing handler would drop these)
    assert "500" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_notify_transport_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) a transport failure (URLError — DNS/refused/timeout/TLS) -> NotifyError."""

    def _raise_url(req: urllib.request.Request, timeout: float | None = None) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise_url)
    with pytest.raises(NotifyError, match="refused"):  # the transport detail is surfaced, not swallowed
        notify("hello", topic="sekret-topic")


def test_notify_2xx_posts_exact_url_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) a 2xx posts to the EXACT topic-derived URL with the message as the POST body (pinned, not
    merely assert-called-once)."""
    monkeypatch.delenv(notify_mod.ENV_NTFY_BASE_URL, raising=False)  # default endpoint
    captured: dict[str, object] = {}

    def _ok(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["method"] = req.get_method()
        return _FakeResp(200)

    monkeypatch.setattr(urllib.request, "urlopen", _ok)
    notify("the alert body", topic="my-topic")
    assert captured["url"] == "https://ntfy.sh/my-topic"
    assert captured["data"] == b"the alert body"
    assert captured["method"] == "POST"


def test_notify_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """(disclosed) NEURO_NTFY_BASE_URL redirects the push to a self-hosted endpoint (trailing / stripped)."""
    monkeypatch.setenv(notify_mod.ENV_NTFY_BASE_URL, "https://ntfy.example.org/")
    captured: dict[str, object] = {}

    def _ok(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        captured["url"] = req.full_url
        return _FakeResp(200)

    monkeypatch.setattr(urllib.request, "urlopen", _ok)
    notify("x", topic="t")
    assert captured["url"] == "https://ntfy.example.org/t"


def test_notify_whitespace_topic_raises_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """(fold, review MINOR-2) a whitespace-only topic (arg or env) is treated as MISSING -> ConfigurationError
    before any network call — it must never POST to `https://ntfy.sh/%20`, where no operator is subscribed."""
    monkeypatch.setenv(notify_mod.ENV_NTFY_TOPIC, "\n")  # a sloppily-populated secret
    calls: list[object] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ConfigurationError):
        notify("hello", topic="   ")
    assert calls == [], "urlopen must NOT be called for a whitespace-only topic"
