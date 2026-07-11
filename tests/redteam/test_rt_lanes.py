"""Red-team: fail-closed lane identity (L1) + no-behavior-by-env-flag / one-implementation (L13).

  * L1 — the SOLE software defense (lanes.py) refuses an invalid expected_lane, a stored 'unknown' lane (the
    enum ADMITS it), a lane mismatch, a uuid mismatch, an unprovisioned DB, and a non-singleton identity;
    provision() refuses re-provision / an unknown lane.
  * L13 — the identity / queue / determinism core reads NO os.environ to switch BEHAVIOR, and there is
    exactly ONE claim path + ONE registrar (no parallel implementation).

Identity-mutating probes run inside a transaction that is ROLLED BACK so the shared session identity row
(the provisioned 'test' lane) is restored.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest
from sqlalchemy import text

from neuromancer_llm.db.lanes import ConfigurationError, LaneAssertionError, assert_lane
from neuromancer_llm.db.provision import provision

pytestmark = pytest.mark.pg

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "neuromancer_llm"


# --- L1: the lane guard fails closed --------------------------------------------------------------
@pytest.mark.parametrize("bad_lane", ["unknown", "prod", "Canonical", ""])
def test_rt_invalid_expected_lane_arg_fails_closed(engine, bad_lane):
    """L1: an expected_lane argument that is not a usable lane raises BEFORE any identity read (the
    expected_lane is a mandatory, validated arg — never an env default)."""
    with engine.connect() as conn, pytest.raises(LaneAssertionError):
        assert_lane(conn, expected_lane=bad_lane)


def test_rt_lane_mismatch_and_uuid_pin_fail_closed(engine):
    """L1: the connected DB is the provisioned 'test' lane — requiring a DIFFERENT lane, or pinning a wrong
    instance_uuid, both fail closed."""
    with engine.connect() as conn:
        with pytest.raises(LaneAssertionError):
            assert_lane(conn, expected_lane="canonical")  # lane mismatch
        with pytest.raises(LaneAssertionError):
            assert_lane(conn, expected_lane="test", expected_uuid=uuid.uuid4())  # uuid pin mismatch


def test_rt_stored_unknown_lane_fails_closed(engine):
    """L1 (the headline inversion case): the lane_kind enum ADMITS 'unknown', so the schema does NOT reject a
    stored lane='unknown' — the software branch is the sole defense. A stored 'unknown' fails closed at the
    stored-not-in-VALID_LANES branch (NOT the lane!=expected branch)."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("UPDATE neuro.database_identity SET lane = 'unknown'"))
            with pytest.raises(LaneAssertionError, match="UNKNOWN/invalid"):
                assert_lane(conn, expected_lane="test")
        finally:
            trans.rollback()  # restore the shared 'test' identity


def test_rt_unprovisioned_and_non_singleton_fail_closed(engine):
    """L1: no identity row (unprovisioned) fails closed; and a SECOND identity row is rejected by the
    singleton PK/CHECK (so read_identity can never go nondeterministic)."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("DELETE FROM neuro.database_identity"))
            with pytest.raises(LaneAssertionError, match="not provisioned"):
                assert_lane(conn, expected_lane="test")
        finally:
            trans.rollback()
    # a second identity row violates the singleton (only_row PK + CHECK)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(Exception):  # noqa: B017 — IntegrityError from the singleton constraint
                conn.execute(text("INSERT INTO neuro.database_identity (lane) VALUES ('staging')"))
        finally:
            trans.rollback()


def test_rt_provision_refuses_reprovision_and_unknown(engine):
    """L1: provision() refuses to re-provision a live DB (already 'test') and refuses an unknown lane —
    UNKNOWN is never written."""
    with engine.connect() as conn:
        with pytest.raises(ConfigurationError, match="already provisioned"):
            provision(conn, lane="canonical")
        with pytest.raises(ConfigurationError):
            provision(conn, lane="unknown")


# --- L13: no behavior-by-env-flag; one implementation per concept ---------------------------------
def test_rt_core_modules_read_no_env():
    """L13: the identity / queue / determinism / capture / cost-safety CORE reads NO os.environ / getenv —
    behavior never lives in an env var (env carries infrastructure config only: the DSN + lane, read in
    session.py / migrations/env.py). A behavior-switching env read in these modules would be the SQ_*-style
    sin."""
    for rel in (
        "db/lanes.py",
        "db/canonical_instance.py",  # A1-15: an env-overridable pin would reintroduce rejected option (b)
        "db/repository.py",
        "capture/events.py",
        "capture/determinism.py",
        "bundles/registrar.py",
        "storage/price_pin.py",  # A2-3: an env-overridable price would reintroduce rejected option (b)
        "storage/quota.py",  # A2-3: the R3 $-ceiling split is owner-adjustable DATA, never an env switch
        "storage/backends.py",  # GO-D-cost C5: the adapter classes are credential-EXPLICIT — the one env
        #                         read (the conn string, infra config) lives in registry/backends.make_backend;
        #                         re-adding the Azurite fallback chain here would redden this statically.
    ):
        src = (_SRC / rel).read_text(encoding="utf-8")
        assert "os.environ" not in src and "getenv" not in src, f"{rel} reads an env var (behavior-by-flag?)"


def test_rt_azure_backend_constructed_only_by_the_factory():
    """GO-D-cost C5 (owner-ruled 2026-07-11): `AzureBlobBackend(` is constructed in src/ ONLY inside
    registry/backends.py::make_backend — the factory that enforces the no-conn-string fail-closed and the
    credential-host <-> registered-base_uri cross-check. A direct construction elsewhere would bypass both
    (the 'true only by accident of one callsite' hole the synthesis named); test fixtures construct via the
    factory/resolver or pass the Azurite string explicitly."""
    import re

    callsites = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        src = py.read_text(encoding="utf-8")
        callsites += [rel for _ in re.finditer(r"(?<!class )AzureBlobBackend\(", src)]
    # set-compare: the factory module may mention the constructor in its own docstring; what must never
    # happen is a hit in ANY OTHER module.
    assert set(callsites) == {"registry/backends.py"}, (
        f"AzureBlobBackend( constructed outside make_backend: {sorted(set(callsites))} — construct through "
        "the factory (C5) and update this probe only with a justification"
    )


def test_rt_no_write_path_on_unverified_engine():
    """L1 probe-6 (matrix ~line 39, GAP-TEST-ONLY — chartered in the Phase-5 reconciliation, landed by
    the 2026-07-02 audit correction pass): 'no write path executes on an unverified engine' is a
    CONVENTION — this pins it statically so a new raw-engine write path must show up here and justify
    itself.

      * `create_engine(` exists in src/ ONLY in db/session.py (the one raw-engine factory);
      * `make_engine(` callers outside session.py are ONLY cli/db.py's pre-identity bootstrap
        (provision/roles/verify run before an identity row can exist — sanctioned);
      * both domain-write choke points (Repository / BundleRegistrar) construct THROUGH verify_engine;
      * `write_capture_event`'s single production callsite receives the VERIFIED repo.engine (exactly
        one callsite in src/ besides the def);
      * `collect_unsealed` has no production caller in src/ yet — a future caller appears here and must
        receive a verified engine."""
    import re

    create_engine_files = []
    make_engine_callers = []
    collect_unsealed_callsites = []  # src/-wide, excluding only the def itself (gc.py included)
    wce_callsites = []  # src/-wide write_capture_event callsites, excluding only the def itself
    cli_db_make_engine_count = 0
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        src = py.read_text(encoding="utf-8")
        if "create_engine(" in src and rel != "db/session.py":
            create_engine_files.append(rel)
        # db/restore.py: the A2-9 restore-drill's raw path is SANCTIONED (justified here per the convention) —
        # it scrubs a SCRATCH clone's identity, a pre-identity DESTRUCTIVE-REWRITE path that cannot go through
        # verify_engine (which would require the very canonical identity the drill rewrites). The safety is not
        # identity verification but a TARGET guard: restore_drill refuses without --confirm-scratch AND if the
        # scratch DSN resolves to the canonical NEURO_DATABASE_URL (db/restore.py).
        if "make_engine(" in src and rel not in ("db/session.py", "cli/db.py", "db/restore.py"):
            make_engine_callers.append(rel)
        if rel == "cli/db.py":
            cli_db_make_engine_count = src.count("make_engine(")
        collect_unsealed_callsites += [rel for _ in re.finditer(r"(?<!def )collect_unsealed\(", src)]
        wce_callsites += [rel for _ in re.finditer(r"(?<!def )write_capture_event\(", src)]
    assert create_engine_files == [], f"create_engine( outside db/session.py: {create_engine_files}"
    assert make_engine_callers == [], (
        f"raw make_engine( outside session.py + cli/db.py's pre-identity bootstrap: {make_engine_callers}"
    )
    # the cli/db.py allowance is pinned to its CURRENT three bootstrap calls (provision/roles/verify) —
    # a NEW raw-engine call added there must show up here and justify itself (review hardening).
    assert cli_db_make_engine_count == 3, (
        f"cli/db.py now has {cli_db_make_engine_count} make_engine( occurrences (expected the 3 "
        "pre-identity bootstrap calls) — a new raw-engine caller must justify itself here"
    )
    assert collect_unsealed_callsites == [], (
        f"new collect_unsealed( caller(s) {collect_unsealed_callsites} — route through a VERIFIED engine "
        "and update this probe"
    )
    # the two domain-write choke points verify identity at construction (R1)
    for rel in ("db/repository.py", "bundles/registrar.py"):
        assert "verify_engine(engine" in (_SRC / rel).read_text(encoding="utf-8"), rel
    # write_capture_event: exactly ONE production callsite anywhere in src/, and it gets the verified
    # repo.engine (the scan is src/-wide, so a new callsite in ANY module trips this).
    assert wce_callsites == ["capture/events.py"], (
        f"write_capture_event callsites {wce_callsites} — a new callsite must receive a verified engine "
        "and update this probe"
    )
    events_src = (_SRC / "capture" / "events.py").read_text(encoding="utf-8")
    assert re.search(r"write_capture_event\(\s*repo\.engine", events_src)


def test_rt_one_queue_one_registrar():
    """L13: exactly ONE claim path (the worker delegate is a pass-through to Repository.claim, not a second
    queue) and ONE registrar concept (a single BundleRegistrar class). A parallel implementation would bite."""
    claim_src = (_SRC / "workers" / "claim.py").read_text(encoding="utf-8")
    assert "repo.claim(" in claim_src  # the delegate forwards to the one Repository.claim
    repo_src = (_SRC / "db" / "repository.py").read_text(encoding="utf-8")
    assert repo_src.count("def claim(") == 1  # exactly one claim method (no second queue)
    assert "SKIP LOCKED" in repo_src and "SKIP LOCKED" not in claim_src  # the only SKIP-LOCKED is here
    registrar_src = (_SRC / "bundles" / "registrar.py").read_text(encoding="utf-8")
    assert registrar_src.count("class BundleRegistrar") == 1  # one registrar concept
