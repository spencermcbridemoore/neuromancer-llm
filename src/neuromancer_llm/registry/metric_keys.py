"""metric_keys vocabulary — run_metrics.metric_key FKs here; the run_metrics valve closes the
'metadata_json reborn' hole (ADR-0017). STAGE 2.

The FIRST minted metric_key is the ESTELA answer-letter projection (owner rulings §A12 D4b + the Unit-B
GO, 2026-07-20). Provenance shape A (owner nod): the metric_key is VERSION-BEARING —
``answer_letter_logprobs@<semver>`` where ``<semver>`` equals the ``method_versions`` semver of the
extraction method that produced it. This keeps the version FK-enforced and reader-surfaced
(``db/run_report.py`` selects ``metric_key``) with zero DDL, since ``run_metrics`` has no
``method_version_id`` column. A future re-derivation registers a new semver and mints
``answer_letter_logprobs@2.0.0``, coexisting cleanly under ``UNIQUE(run_id, metric_key)``.

Seed these through ``Repository.seed_metric_key`` (fail-loud on a divergent re-seed; registrar/admin-only
INSERT per grants.sql). ``value_kind`` is 'scalar' | 'json'; the answer-letter projection is a k-entry
``{letter: logprob}`` dict, so 'json'.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricKeySpec:
    """One row of the metric_keys vocabulary: the FK target a run_metrics row references."""

    metric_key: str
    value_kind: str  # 'scalar' (value_num) | 'json' (value_json)
    description: str


#: The ESTELA answer-letter projection metric_key (shape A, version-bearing; @1.0.0 == the method semver).
ANSWER_LETTER_LOGPROBS_V1 = MetricKeySpec(
    metric_key="answer_letter_logprobs@1.0.0",
    value_kind="json",
    description=(
        "per-answer-letter next-token logprob projection over the injected A-E labels; a k-entry "
        "{letter: logprob} dict (a letter absent from the captured top-N logprobs -> null); derived via "
        "the versioned answer-letter-extraction method; the @<semver> suffix == the method_versions semver "
        "(provenance shape A)."
    ),
)
