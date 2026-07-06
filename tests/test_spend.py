"""Unit C2 (A2-15 cost-safety GO §4) — the spend ledger: record_spend + the idempotent rate-card seed +
run_plans + reconcile. These pin the ledger's contract: a spend row lands with a resolved NOT NULL
rate_card FK, the rate-card seed is register-first / idempotent / raise-on-drift on the natural key,
`plan` writes a run_plans justification row, and `reconcile` flags a billed-vs-predicted divergence over the
threshold. All need real Postgres (`pg`); the CLI is a thin delegate over these Repository methods.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from neuromancer_llm.db.repository import IdentityMismatchError

# The INPUT #2 storage rate card (GO §0), seeded with a TEST effective_from — the production seed's
# effective_from = the A2-1 provisioning date is applied at A2-1 deploy time (deferred), not here.
_EF = dt.datetime(2026, 7, 5, tzinfo=dt.UTC)


def _seed_storage_card(repo, rate: str = "0.0184") -> int:
    return repo.get_or_create_rate_card(
        backend_or_lane="azure-blob-storage", unit="usd_per_gb", rate=rate, effective_from=_EF
    )


@pytest.mark.pg
def test_record_spend_inserts_row(seeded):
    """record_spend writes a correct spend_entries row with the resolved NOT NULL rate_card FK."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    rate_card_id = _seed_storage_card(repo)
    sid = repo.record_spend(
        run_id=run_id, rate_card_id=rate_card_id, quantity="10", amount="0.184", is_standing=True
    )
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT run_id, rate_card_id, quantity, amount, is_standing "
                    "FROM neuro.spend_entries WHERE spend_entry_id = :i"
                ),
                {"i": sid},
            )
            .mappings()
            .one()
        )
    assert row["run_id"] == run_id
    assert row["rate_card_id"] == rate_card_id
    assert row["is_standing"] is True
    assert Decimal(str(row["amount"])) == Decimal("0.184")
    assert Decimal(str(row["quantity"])) == Decimal("10")


@pytest.mark.pg
def test_record_spend_seeds_rate_card_idempotent(repo):
    """The rate-card seed is register-first / idempotent on UNIQUE(backend_or_lane, effective_from): a second
    identical call reuses the row (no duplicate); the SAME natural key with a DIFFERENT rate raises
    IdentityMismatchError (a rate card is an auditable price of record — reprice with a NEW effective_from)."""
    first = _seed_storage_card(repo)
    second = _seed_storage_card(repo)
    assert first == second, "idempotent on the natural key — no duplicate rate card"
    with repo.engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM neuro.rate_cards WHERE backend_or_lane = 'azure-blob-storage'")
        ).scalar_one()
    assert n == 1
    with pytest.raises(IdentityMismatchError):
        _seed_storage_card(repo, rate="0.0200")  # same natural key, different rate -> raise-on-drift


@pytest.mark.pg
def test_spend_plan_writes_run_plan(seeded):
    """`plan` (record_run_plan) writes a run_plans justification row with its estimates."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    pid = repo.record_run_plan(
        run_id=run_id,
        justification="renewal evidence: E6 determinism sweep",
        est_usd="12.50",
        est_su="500",
    )
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT run_id, justification, est_usd, est_su FROM neuro.run_plans "
                    "WHERE run_plan_id = :i"
                ),
                {"i": pid},
            )
            .mappings()
            .one()
        )
    assert row["run_id"] == run_id
    assert "renewal evidence" in row["justification"]
    assert Decimal(str(row["est_usd"])) == Decimal("12.50")
    assert Decimal(str(row["est_su"])) == Decimal("500")


@pytest.mark.pg
def test_spend_reconcile_flags_divergence(repo):
    """reconcile compares ACTUAL billed spend (passed in) vs the LEDGER-predicted total, flagging a breach
    when |divergence| exceeds the threshold — in EITHER direction. Non-vacuous across every branch: the
    empty-ledger (predicted==0) case, within-threshold, over-billing, UNDER-billing (the abs() branch — a
    signed-only comparison would miss it), and the backend_or_lane-scoped JOIN."""
    # empty ledger (predicted 0): 0 billed -> no breach; ANY nonzero billing is an unbounded divergence -> breach
    assert repo.reconcile_spend(billed_usd="0", threshold_pct="20").breach is False
    assert repo.reconcile_spend(billed_usd="0.50", threshold_pct="20").breach is True

    rate_card_id = _seed_storage_card(repo)
    repo.record_spend(
        run_id=None, rate_card_id=rate_card_id, quantity="100", amount="1.84", is_standing=True
    )  # predicted total = 1.84
    within = repo.reconcile_spend(billed_usd="2.00", threshold_pct="20")  # +8.7% divergence < 20
    assert within.breach is False
    assert within.predicted_usd == Decimal("1.84")
    over = repo.reconcile_spend(billed_usd="3.00", threshold_pct="20")  # +63% divergence > 20
    assert over.breach is True
    assert over.divergence_pct > Decimal("20")
    under = repo.reconcile_spend(billed_usd="1.00", threshold_pct="20")  # -45.7% -> abs > 20 -> breach
    assert under.breach is True
    assert under.divergence_pct < Decimal("0"), "signed divergence: billed UNDER predicted is negative"

    # backend_or_lane scoping: the JOIN restricts predicted to that lane's rate cards
    scoped = repo.reconcile_spend(billed_usd="1.84", backend_or_lane="azure-blob-storage")
    assert scoped.predicted_usd == Decimal("1.84")
    assert scoped.breach is False
    other = repo.reconcile_spend(billed_usd="0.50", backend_or_lane="some-other-lane")
    assert other.predicted_usd == Decimal("0"), "no spend joins that lane -> predicted 0"
    assert other.breach is True


@pytest.mark.pg
def test_spend_report_renders(seeded):
    """spend_report partitions ALL ledger rows into per-run (non-standing WITH run) + standing + unattributed
    (non-standing WITHOUT run), so no spend is hidden: sum(per_run) + standing + unattributed == total."""
    repo, run_id = seeded["repo"], seeded["run_id"]
    rate_card_id = _seed_storage_card(repo)
    repo.record_spend(
        run_id=run_id, rate_card_id=rate_card_id, quantity="10", amount="0.184", is_standing=False
    )  # per-run
    repo.record_spend(
        run_id=None, rate_card_id=rate_card_id, quantity="100", amount="1.84", is_standing=True
    )  # standing
    repo.record_spend(
        run_id=None, rate_card_id=rate_card_id, quantity="5", amount="0.05", is_standing=False
    )  # unattributed: non-standing with NO run_id (would be hidden by a naive per_run+standing report)
    report = repo.spend_report()
    assert report.standing_usd == Decimal("1.84")
    assert (run_id, Decimal("0.184")) in list(report.per_run)
    assert report.unattributed_usd == Decimal("0.05")
    assert report.total_usd == Decimal("2.074")  # 0.184 + 1.84 + 0.05
    per_run_total = sum((amt for _, amt in report.per_run), Decimal("0"))
    assert per_run_total + report.standing_usd + report.unattributed_usd == report.total_usd
