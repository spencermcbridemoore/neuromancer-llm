"""Per-prefix, dollar-calibrated cloud-storage quota guard — fails CLOSED (ADR-0040; A2-3, cost-safety GO §3).

Consulted before every cloud `put` (the wiring is Unit C4; this module is the guard itself). It is the
direct fix for the predecessor's fail-OPEN-on-listing-error sin (go-remote plan §1.3 #9): a listing/usage
error there silently ALLOWED the write.

Fail-closed contract (NEVER a best-effort / under-counted ALLOW):
  * an absent/expired price pin -> ConfigurationError, raised BEFORE any DB sizing (storage/price_pin.py);
  * ANY error computing usage OR the ceiling -> QuotaDeniedError (deny), never a silent allow;
  * an EMPTY backend_ids set (nothing to measure) -> QuotaDeniedError before any sizing (absence of evidence
    is not proof of zero usage);
  * usage (+ the incoming object) AT or OVER the prefix's dollar-calibrated byte ceiling -> QuotaDeniedError.
`check_quota` returns None (allow) only when the write is PROVABLY within budget.

Usage comes from the DB CATALOG, not a blob listing: SUM(neuro.artifacts.size_bytes) per backend_id (the
integrity-bound side — reader.py re-verifies sha256+size on read). The StorageBackend Protocol has no
`list()` (storage/backends.py) and a blob enumeration is the UNTRUSTED side the predecessor fail-opened on.
Each cloud prefix == one storage_backends row, so "per-prefix" == per-backend_id. TOMBSTONED-BUT-UNPURGED
rows are COUNTED: `artifacts.deleted_at` is a *recorded* logical deletion (orm.py "deletion RECORDED") with
NO blob-purge side effect (AzureBlobBackend.delete_blob has zero wired callers), so a tombstoned blob still
incurs real Azure storage cost — excluding `deleted_at IS NOT NULL` rows would under-count the billed
footprint (a cost-safety fail-open). A conservative ceiling counts every not-yet-purged row.

HONEST LIMIT (rulings brief §4): a bytes quota bounds STORED bytes only. At this project's ~2.3 KB objects
the dominant real spend is per-write TRANSACTIONS (~600k events ~= 1.2M PUTs ~= $6 one-time vs ~$0.03/mo for
the bytes stored) — invisible to a stored-bytes ceiling. Transaction spend is bounded OPERATIONALLY (the C2
spend ledger + the monthly Cost-Management reconcile + the pin's 20-30% headroom + the portal Budget alert),
NOT by this guard.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import Connection, text

from .price_pin import resolve_price

# 1 GB == 10^9 bytes (decimal): the price meter's unitOfMeasure is "1 GB/Month", and Azure bills stored
# capacity in decimal GB. This deliberately matches the meter, not a 2^30 GiB.
_GB_BYTES = 10**9

# R3 ($10/mo storage envelope split; pre-a2-rulings-brief §9, RULED 2026-07-03). Each budget group is one or
# more container prefixes SHARING a single dollar ceiling; `artifacts-dev` + `artifacts-stage` share $1.50,
# so the five provisioned containers sum to EXACTLY $10/mo. This is owner-adjustable DATA (edit here + regen)
# — a change is an auditable commit, NEVER an env switch (L13; the module is in the env-free scan). The
# staging container was ruled to the legal name `upload-staging` (the earlier `_staging` is an illegal Azure
# container name).
_BUDGET_GROUPS: tuple[tuple[tuple[str, ...], Decimal], ...] = (
    (("artifacts-prod",), Decimal("6.00")),
    (("artifacts-dev", "artifacts-stage"), Decimal("1.50")),  # dev + stage SHARE this $1.50 bucket
    (("db-backups",), Decimal("2.00")),
    (("upload-staging",), Decimal("0.50")),
)
# container prefix -> its budget group's dollar ceiling / member prefixes (derived; one source of truth).
_CEILING_USD: dict[str, Decimal] = {prefix: usd for members, usd in _BUDGET_GROUPS for prefix in members}
_GROUP_MEMBERS: dict[str, tuple[str, ...]] = {
    prefix: members for members, _usd in _BUDGET_GROUPS for prefix in members
}


class QuotaDeniedError(RuntimeError):
    """The cloud put is DENIED (fail closed): the prefix is at/over its dollar-calibrated byte ceiling, the
    prefix has no configured ceiling, OR usage could not be computed. Never a best-effort under-counted
    ALLOW. Distinct from ConfigurationError (an absent/expired price pin) — that is a deploy-time
    misconfiguration raised before any sizing, not a per-write budget denial."""


def _ceiling_usd(prefix: str) -> Decimal:
    """The dollar ceiling for `prefix`'s R3 budget group. Unknown prefix -> QuotaDeniedError (fail closed:
    never let an un-budgeted cloud prefix write freely)."""
    try:
        return _CEILING_USD[prefix]
    except KeyError:
        raise QuotaDeniedError(
            f"no cost ceiling is configured for storage prefix {prefix!r} — refusing to write to an "
            f"un-budgeted cloud prefix (fail closed; known prefixes: {sorted(_CEILING_USD)})"
        ) from None


def budget_group_for_prefix(prefix: str) -> tuple[str, ...]:
    """The container prefixes that SHARE `prefix`'s dollar ceiling (dev+stage share; the rest are
    singletons). The C4 wiring uses this to resolve every group member's backend_id, so combined usage is
    summed against the shared ceiling. Unknown prefix -> QuotaDeniedError (fail closed)."""
    try:
        return _GROUP_MEMBERS[prefix]
    except KeyError:
        raise QuotaDeniedError(
            f"no budget group is configured for storage prefix {prefix!r} (fail closed; known prefixes: "
            f"{sorted(_GROUP_MEMBERS)})"
        ) from None


def byte_ceiling_for_prefix(prefix: str, *, now: _dt.datetime | None = None) -> int:
    """The pin-resolved byte ceiling for `prefix`'s R3 budget group: floor($ceiling / tier0_price) x 10^9.

    Resolves the price pin FIRST, so an absent/expired pin raises ConfigurationError BEFORE any DB sizing;
    an unknown prefix raises QuotaDeniedError (fail closed). `now` is threaded to the pin's staleness check
    (tests only; defaults to the real UTC clock)."""
    price = resolve_price(now=now)  # ConfigurationError on absent/expired pin -> BEFORE any sizing
    usd = _ceiling_usd(prefix)
    gb = (usd / price).to_integral_value(rounding=ROUND_FLOOR)  # floor the GB count, THEN scale to bytes
    return int(gb) * _GB_BYTES


def usage_bytes(conn: Connection, backend_ids: int | Sequence[int]) -> int:
    """SUM(size_bytes) over the given backend_id(s)' artifact rows, straight from the DB catalog (NOT a blob
    listing). Counts EVERY not-yet-purged row: a logical tombstone (deleted_at) does NOT free billed bytes,
    so tombstoned-but-unpurged rows are INCLUDED (excluding them would under-count the billed footprint — a
    cost-safety fail-open). This is a RAW catalog SUM (0 for a backend with no rows); the fail-closed guard
    against an EMPTY backend SET lives in check_quota, not here."""
    ids = [backend_ids] if isinstance(backend_ids, int) else list(backend_ids)
    total = conn.execute(
        text("SELECT COALESCE(SUM(size_bytes), 0) FROM neuro.artifacts WHERE backend_id = ANY(:ids)"),
        {"ids": ids},
    ).scalar_one()
    return int(total)


def check_quota(
    conn: Connection,
    *,
    prefix: str,
    backend_ids: int | Sequence[int],
    incoming_bytes: int = 0,
) -> None:
    """Consult before every cloud put — fail CLOSED. Raises ConfigurationError if the price pin is
    absent/expired (BEFORE any sizing); raises QuotaDeniedError if the ceiling/usage cannot be computed
    (fail closed), if backend_ids is EMPTY (nothing to measure — absence of evidence, not proof of zero
    usage), or if the group's usage plus the incoming object would be AT or OVER the prefix ceiling.
    Returns None (allow) only when the write is provably within budget.

    `prefix` selects the R3 budget group + ceiling; `backend_ids` are the storage_backends rows in that
    group (pass EVERY member for a shared group, e.g. artifacts-dev + artifacts-stage under one $1.50, so
    their combined footprint is checked). `incoming_bytes` is the size of the object about to be written
    (default 0 = check current footprint only; the C4 wiring passes the pending object's size)."""
    ceiling = byte_ceiling_for_prefix(prefix)  # pin -> ConfigurationError before any sizing; deny if unknown
    ids = [backend_ids] if isinstance(backend_ids, int) else list(backend_ids)
    if not ids:
        raise QuotaDeniedError(
            f"no storage backends to measure for prefix {prefix!r} (backend_ids is empty) — an empty backend "
            "set is absence of evidence, not proof of zero usage; refusing to certify the write within budget "
            "(fail closed). The C4 wiring must resolve every group member's backend_id before consulting."
        )
    try:
        used = usage_bytes(conn, ids)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: ANY usage-sizing failure is a DENY, never an ALLOW
        raise QuotaDeniedError(
            f"could not compute storage usage for prefix {prefix!r} (backend_ids={backend_ids!r}) — DENYING "
            "the write (fail closed; never a best-effort under-count)"
        ) from exc
    projected = used + max(incoming_bytes, 0)
    if projected >= ceiling:
        raise QuotaDeniedError(
            f"storage prefix {prefix!r} at/over its ${_CEILING_USD.get(prefix)} ceiling: usage {used} + "
            f"incoming {max(incoming_bytes, 0)} = {projected} bytes >= {ceiling} bytes (fail closed) — "
            "DENYING the write"
        )
