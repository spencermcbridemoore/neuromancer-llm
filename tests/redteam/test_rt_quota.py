"""Red-team (Unit C1 headline, cost-safety GO §3): the storage quota guard fails CLOSED on a usage-sizing
error — a broken sizing query DENIES the write, never a best-effort under-counted ALLOW. This is the direct
closer for the predecessor's fail-OPEN-on-listing-error sin (go-remote plan §1.3 #9).

Pure (a fake connection), so it can NEVER skip: the fail-closed contract is always exercised, GPU/PG/Azurite
present or not (the same discipline as tests/test_audit_guards.py).
"""

from __future__ import annotations

import pytest

from neuromancer_llm.storage import quota


class _RaisingConn:
    """A stand-in DB connection whose sizing query always raises (a transient DB failure)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def execute(self, *args: object, **kwargs: object) -> object:
        raise self._exc


def test_rt_quota_fail_closed_on_usage_error():
    """The usage-sizing query raises -> QuotaDeniedError (DENY), never the raw error and never a silent
    ALLOW. A fail-OPEN guard (swallow the error and return None) would let the write through — the exact
    vulnerability this unit closes. The price pin is the real committed one, so the ceiling resolves and the
    ONLY failure is the sizing query, isolating the fail-closed-on-usage-error path."""
    conn = _RaisingConn(RuntimeError("simulated: the SUM(size_bytes) sizing query failed"))
    with pytest.raises(quota.QuotaDeniedError):
        quota.check_quota(conn, prefix="artifacts-prod", backend_ids=[1])


class _ZeroResult:
    def scalar_one(self) -> int:
        return 0


class _ZeroConn:
    """A fake connection whose sizing query SUCCEEDS and reports ZERO usage — exactly what a genuinely
    empty/absent backend yields. A fail-OPEN guard would read this 0 as 'within budget' and ALLOW."""

    def execute(self, *args: object, **kwargs: object) -> _ZeroResult:
        return _ZeroResult()


def test_rt_quota_fail_closed_on_empty_backend_set():
    """An EMPTY backend_ids set is absence-of-evidence, not proof of zero usage -> DENY (fail closed). The
    zero-usage conn would let a fail-OPEN guard ALLOW; a NON-empty set that truly measures 0 is legitimately
    within budget and allowed, so the deny is specifically due to the empty SET, not the (real) zero usage."""
    zero = _ZeroConn()
    with pytest.raises(quota.QuotaDeniedError):
        quota.check_quota(zero, prefix="artifacts-prod", backend_ids=[])
    # contrast: a real backend that measures 0 bytes may take its first write
    assert quota.check_quota(zero, prefix="artifacts-prod", backend_ids=[1]) is None
