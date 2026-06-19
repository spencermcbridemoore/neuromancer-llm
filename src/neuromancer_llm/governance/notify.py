"""Alert channel seam — ntfy.sh push; the topic is a SECRET (ADR-0019), read from env/connections,
never git or generated docs. Swappable (msmtp/webhook) behind this one seam. STAGE 2 (wiring).

The seam shape is fixed now so the durability-staleness gate (ADR-0020) and desktop agent (ADR-0015)
share one notifier; the actual HTTP send lands in Stage 2.
"""

from __future__ import annotations

ENV_NTFY_TOPIC = "NEURO_NTFY_TOPIC"  # secret: the topic name IS the credential


def notify(message: str, *, topic: str | None = None) -> None:  # noqa: ARG001 — seam, wired in Stage 2
    """Push an alert. STAGE 2: curl ntfy.sh/<topic> on OnFailure. No-op placeholder for now."""
    raise NotImplementedError("notify() is wired in Stage 2 (ntfy.sh push; topic is a secret).")
