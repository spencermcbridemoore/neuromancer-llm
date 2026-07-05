"""The repo-pinned Azure Blob Storage price (A2-3 / R4 companion; cost-safety GO §0 INPUT #1, RULED 2026-07-05).

The pin is a COMMITTED CONSTANT — versioned, docs-cannot-rot, and a re-price (Azure changes the Hot LRS
tier-0 rate) is an auditable commit, exactly the event that should be loud. This is the exact shape of the
canonical instance pin (db/canonical_instance.py): one constant + one single-resolution-point function that
FAILS CLOSED when the pin is absent. It is deliberately NOT an env var (an env-overridable price would
reintroduce the rejected option-(b) behavior-by-flag pattern; the L13 red-team probe holds this module
env-free) and NEVER DB-resident (a cost guard cannot trust the very DB it protects to hold its own ceiling
basis).

Read-only pull from the Azure Retail Prices API for the East US 2 Hot LRS TIER-0 "data stored" meter — the
`--kind StorageV2`, non-hierarchical product we provision (`General Block Blob v2` / `Blob Storage` share
this meterId). tier-0 is the HIGHEST per-GB rate of the three storage tiers, so pinning it is the
conservative choice for a cost ceiling (the two higher tiers price lower per GB). The Retail Prices API is
the pin's OUT-OF-BAND refresh/drift-check (a CI/cron job, never the guard's hot path — a fail-closed guard
on a live endpoint would wedge every write on a network hiccup); Cost Management actuals are the monthly
reconciler (Unit C2). The price has been unchanged since effectiveStartDate 2017-02-03.

Values verbatim from the 2026-07-05 read-only pull (cost-safety GO §0 INPUT #1). The numeric fields are
stored as strings so the price reconstructs to an EXACT Decimal (never a binary-float rounding of $0.0184).
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ..db.lanes import ConfigurationError

# The Azure Blob Storage price of record. `None` means "no pin recorded" (a fresh fork / mid-rotation
# state) — price resolution FAILS CLOSED on it, never an unpinned sizing pass (the canonical_instance.py
# CANONICAL_INSTANCE_UUID idiom). `retailPrice` is USD per GB-Month (unitOfMeasure "1 GB/Month"); the quota
# guard treats "1 GB" as decimal 10^9 bytes (how Azure bills stored capacity).
STORAGE_PRICE_PIN: dict[str, str] | None = {
    "retailPrice": "0.0184",  # USD, tier-0 (tierMinimumUnits = 0) — the highest per-GB tier
    "meterId": "10b97fb2-5db4-4ad2-a4e0-69b858d6b52d",
    "meterName": "Hot LRS Data Stored",
    "productName": "General Block Blob v2",  # 'Blob Storage' shares this meterId
    "skuName": "Hot LRS",
    "armRegionName": "eastus2",
    "unitOfMeasure": "1 GB/Month",
    "currencyCode": "USD",
    "effectiveStartDate": "2017-02-03T00:00:00Z",
    "retrieved_at": "2026-07-05",  # date of the read-only Retail Prices API pull
    "source_url": (
        "https://prices.azure.com/api/retail/prices?$filter=serviceName eq 'Storage' and "
        "armRegionName eq 'eastus2' and skuName eq 'Hot LRS' and meterName eq 'Hot LRS Data Stored'"
    ),
    "not_after": "2027-07-05T00:00:00Z",  # +12mo staleness bound; owner-adjustable; expired -> fail closed
}


def _parse_ts(value: str) -> _dt.datetime:
    # Tolerate the trailing 'Z' on 3.11 (fromisoformat gained 'Z' support in 3.11, but be explicit).
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve_price(*, now: _dt.datetime | None = None) -> Decimal:
    """The single price-resolution point (db/canonical_instance.py::expected_uuid_for_lane idiom): return the
    pinned USD/GB-Month rate as an EXACT Decimal, or raise ConfigurationError if the pin is absent OR stale
    (now >= not_after) — refusing to size a cost quota on an absent/expired basis (fail closed). This runs
    BEFORE any DB sizing, so an unpinned/expired deploy can never silently under-count usage into an ALLOW.
    `now` is injectable for tests; it defaults to the real UTC clock."""
    pin = STORAGE_PRICE_PIN
    if pin is None:
        raise ConfigurationError(
            "Azure storage price pin is absent (storage/price_pin.py STORAGE_PRICE_PIN is None) — refusing "
            "to size a cost quota on an unpinned price (fail closed). Record the Retail-Prices-API pull in "
            "the pin; a re-price is an auditable commit (cost-safety GO §0 INPUT #1)."
        )
    now = now or _dt.datetime.now(_dt.UTC)
    not_after = _parse_ts(pin["not_after"])
    if now >= not_after:
        raise ConfigurationError(
            f"Azure storage price pin is STALE (not_after={pin['not_after']!r}, now={now.isoformat()}) — "
            "refusing to size a cost quota on an expired price basis (fail closed). Re-pull the Retail "
            "Prices API and re-pin (an auditable commit); the price has not changed since 2017-02-03."
        )
    return Decimal(pin["retailPrice"])
