"""Alert channel seam — ntfy.sh push; the topic is a SECRET (ADR-0019), read from env/connections,
never git or generated docs. Swappable (msmtp/webhook) behind this one seam.

The seam shape is fixed now so the durability-staleness gate (ADR-0020) and desktop agent (ADR-0015)
share one notifier; the actual HTTP send is NOT built yet (go-remote Stage A item A2-12; an ADR-0019
blocking precondition for canonical writes). Until then notify() raises NotImplementedError — a LOUD
typed refusal, never a silent no-op that would let an alert vanish.
"""

from __future__ import annotations

ENV_NTFY_TOPIC = "NEURO_NTFY_TOPIC"  # secret: the topic name IS the credential


def notify(message: str, *, topic: str | None = None) -> None:  # noqa: ARG001 — seam, send not yet built
    """Push an alert (ntfy.sh push on OnFailure, once built). Raises NotImplementedError until the send
    lands (fail loud — a swallowed alert would defeat ADR-0019)."""
    raise NotImplementedError("notify() send is not built yet (ntfy.sh push; topic is a secret).")
