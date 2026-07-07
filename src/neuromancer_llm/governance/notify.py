"""Alert channel seam — ntfy.sh push; the topic is a SECRET (ADR-0019), read from env, never git or
generated docs. Swappable (msmtp/webhook) behind this one seam.

The seam shape is fixed so the durability-staleness gate (ADR-0020) and desktop agent (ADR-0015) share one
notifier. The send is a stdlib-only (urllib) ntfy.sh POST (go-remote Stage A A2-12). It is fail-LOUD, never a
silent no-op that would let an alert vanish (an ADR-0019 blocking precondition for canonical writes): a
missing topic raises ConfigurationError BEFORE any network call (no default-topic fallback), and BOTH a
non-2xx HTTP response and a transport error raise NotifyError.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

from ..db.lanes import ConfigurationError

ENV_NTFY_TOPIC = "NEURO_NTFY_TOPIC"  # secret: the topic name IS the credential
ENV_NTFY_BASE_URL = "NEURO_NTFY_BASE_URL"  # infra config: the ntfy endpoint (default ntfy.sh)
DEFAULT_BASE_URL = "https://ntfy.sh"
_TIMEOUT_S = 10.0


class NotifyError(RuntimeError):
    """The ntfy push failed (a non-2xx HTTP response or a transport error). Fail loud — a swallowed alert
    would defeat ADR-0019."""


def notify(message: str, *, topic: str | None = None) -> None:
    """Push an alert via an ntfy.sh POST (ADR-0019). FAIL LOUD, never a silent no-op:

    - a missing topic (arg ``None`` AND ``NEURO_NTFY_TOPIC`` unset/empty) raises ``ConfigurationError``
      BEFORE any network call — there is NO default-topic fallback (a default would publish alerts to a
      wrong/guessable topic);
    - a non-2xx HTTP response raises ``NotifyError`` (the server error body is surfaced, never swallowed);
    - a transport failure (``URLError``) raises ``NotifyError``.

    The topic is a SECRET (the topic name IS the credential) read from ``NEURO_NTFY_TOPIC``; the endpoint
    defaults to ``https://ntfy.sh`` and is overridable via ``NEURO_NTFY_BASE_URL`` (a self-hosted server).
    """
    topic = (topic or os.environ.get(ENV_NTFY_TOPIC) or "").strip()  # a whitespace topic is a MISSING topic
    if not topic:
        raise ConfigurationError(
            f"notify(): no ntfy topic configured — set {ENV_NTFY_TOPIC} (the topic IS the credential; "
            "there is no default-topic fallback)."
        )
    base_url = (os.environ.get(ENV_NTFY_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    req = urllib.request.Request(f"{base_url}/{topic}", data=message.encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 — infra endpoint, POST body
            resp.read()  # drain; urlopen raises HTTPError on non-2xx, so reaching here is a 2xx success
    except urllib.error.HTTPError as exc:  # non-2xx — surface the server body, never swallow it
        detail = exc.read().decode("utf-8", "replace")
        raise NotifyError(f"ntfy push -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:  # transport failure (DNS, connection refused, TLS)
        # A bare read-timeout is a TimeoutError (NOT a URLError subclass) -> propagates uncaught: still
        # fail-LOUD, never swallowed, but untyped. Deferred precedent-wide timeout-typing pass (with vllm.py).
        raise NotifyError(f"ntfy push to {base_url} failed: {exc}") from exc
