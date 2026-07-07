"""Restore drill + clone-identity scrub (A2-9) — the A1-15 + A2-9 HARD GATE.

A1-15 (`canonical_instance.py`) rejects a restored clone that mints a FRESH `instance_uuid`. The other
half — a VERBATIM clone that copies the identity row byte-for-byte — still verifies as canonical, because
its uuid IS the pin. A2-9 closes it: after a restore, the drill SCRUBS the clone's identity (regenerate
`instance_uuid`, record `cloned_from`) so a verbatim canonical backup verifies as NON-canonical.

The drill is DESTRUCTIVE (it rewrites `database_identity`) and MUST never touch the live canonical. The
guard is TARGET-based, not identity-based: a correctly-restored clone legitimately holds canonical identity
(that is exactly what we scrub), so identity cannot distinguish it from the original — only the connection
target can. So the drill (a) requires an explicit `--confirm-scratch` affirmation and (b) refuses if the
scratch DSN resolves to the canonical DSN (`NEURO_DATABASE_URL`, which it fails closed without). The DSN
match normalizes the loopback family (localhost / 127.0.0.1 / ::1) + the default port, so an alias spelling
of the same box cannot slip it; it does NOT resolve DNS aliases or unix sockets — `--confirm-scratch` is the
primary affirmation for those.

Scope (ruled synthetic-dump-first): this builds the SCRUB LOGIC + the GUARD. The actual restore of a real
backup FROM the desktop-NVMe copy (A2-10) into a scratch cluster is a deferred A2-9-real step, gated on
A2-7 + A2-10; the drill here operates on an already-restored scratch DB. It runs as the connection's admin
role (`grants.sql:65` — no writer/registrar UPDATE on `database_identity`).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, make_url

from .canonical_instance import expected_uuid_for_lane
from .lanes import ConfigurationError, LaneAssertionError, assert_lane, read_identity
from .session import database_url, make_engine


class RestoreDrillError(RuntimeError):
    """The scrub did not close the clone (post-scrub identity still verifies canonical, or no row to scrub).

    A should-never-happen invariant break — fail LOUD rather than report a false 'drill passed'.
    """


# The loopback family — every spelling of "this box". PostgreSQL binds 127.0.0.1 AND ::1, and testcontainers
# hand out `localhost`; a raw string compare would treat these as DISTINCT and let the drill scrub the live
# canonical spelled with a different loopback alias (the fail-open the A2-9 review reproduced live).
_LOOPBACK = frozenset({"", "localhost", "127.0.0.1", "::1", "[::1]"})
_DEFAULT_PG_PORT = 5432


def same_target(a: str, b: str) -> bool:
    """True if two DSNs point at the same (host, port, database) — the target comparison the guard uses to
    refuse the canonical DSN. The loopback family (localhost / 127.0.0.1 / ::1 / omitted) normalizes to one
    sentinel and an omitted port to 5432, so an ALIAS spelling of the same box (the drill's default target)
    cannot slip the guard. It deliberately does NOT resolve DNS names or unix sockets — a distinct hostname
    aliasing the same server, or a socket-vs-TCP path, is outside a target-string guard's reach;
    `--confirm-scratch` is the primary affirmation there (module docstring)."""

    def key(u: URL) -> tuple[str, int, str]:
        host = (u.host or "").lower()
        return ("localhost" if host in _LOOPBACK else host, u.port or _DEFAULT_PG_PORT, u.database or "")

    return key(make_url(a)) == key(make_url(b))


def scrub_identity(conn: Connection) -> dict[str, str]:
    """Atomically rewrite the connected DB's `database_identity`: mint a FRESH `instance_uuid` and record
    the original in `cloned_from`. ONE committed UPDATE (the uuid regeneration is the load-bearing closer;
    `cloned_from` alone does not close the residual). Returns {old_uuid, new_uuid}."""
    row = read_identity(conn)
    if row is None:
        raise RestoreDrillError(
            "scratch has no neuro.database_identity row — nothing to scrub (did the restore run?)."
        )
    old = str(row["instance_uuid"])
    new = conn.execute(
        text(
            "UPDATE neuro.database_identity "
            "SET instance_uuid = gen_random_uuid(), cloned_from = :old "
            "RETURNING instance_uuid"
        ),
        {"old": old},
    ).scalar()
    conn.commit()
    return {"old_uuid": old, "new_uuid": str(new)}


def restore_drill(scratch_url: str, *, confirm_scratch: bool) -> dict[str, str]:
    """Scrub a restored clone's identity so a verbatim canonical backup verifies as NON-canonical (A2-9).

    Fail CLOSED before any write: (1) `confirm_scratch` must be True (a positive affirmation that the target
    is a throwaway — the drill is destructive); (2) the scratch DSN must NOT resolve to the canonical DSN
    (`NEURO_DATABASE_URL`, itself required — the drill refuses to run without knowing which target it must
    protect). Then scrub the scratch identity and ASSERT it now verifies non-canonical (fails the pin).
    """
    if not confirm_scratch:
        raise ConfigurationError(
            "restore-drill rewrites database_identity (destructive) — refusing without --confirm-scratch "
            "(a positive affirmation that the target is a throwaway scratch DB, never the canonical)."
        )
    canonical = database_url()  # NEURO_DATABASE_URL; fail closed if unset (we must know what to refuse)
    if same_target(scratch_url, canonical):
        raise ConfigurationError(
            "--scratch-url resolves to the canonical DSN (NEURO_DATABASE_URL) — refusing to run the "
            "destructive drill against the canonical (fail closed). Point it at a throwaway scratch DB."
        )

    pin = expected_uuid_for_lane("canonical")  # the repo pin (A1-15); absent -> ConfigurationError
    engine = make_engine(
        scratch_url
    )  # raw pre-identity path (sanctioned, like provision) — the scratch target
    try:
        with engine.connect() as conn:
            result = scrub_identity(conn)
            after = read_identity(conn)
            if after is None or str(after["instance_uuid"]) == str(pin) or after["cloned_from"] is None:
                raise RestoreDrillError("post-scrub identity did not change (fail loud) — clone not closed.")
            # prove it through the SAME guard a canonical worker would hit: it MUST now fail the pin
            verified_non_canonical = False
            try:
                assert_lane(conn, expected_lane="canonical", expected_uuid=pin)
            except LaneAssertionError:
                verified_non_canonical = True
            if not verified_non_canonical:
                raise RestoreDrillError(
                    "post-scrub identity STILL verifies canonical — the scrub failed to close the clone."
                )
    finally:
        engine.dispose()
    return result
