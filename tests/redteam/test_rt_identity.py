"""Red-team: register-first / fail-closed identity (L5, L11, L-SB).

Probes that the identity registries refuse to adopt a divergent or caller-trusted identity:
  * FIX #4 — register_fingerprint recomputes the hash from semantic_config (no trust-the-caller).
  * FIX #8 / L-SB — storage_backend identity is register-first/immutable (no DO UPDATE repoint).
  * FIX #9 — register_model_identity derives tokenizer_id FROM tokenizer_hash (no id/hash split).
  * L5 / L11 GAP probes — drift on the other identity dimensions raises (guards present, untested).
  * ADR-0048 layer (a) — get_or_create_actor/campaign key-drift now RAISES (2026-07-14). These probes
    formerly DOCUMENTED the accepted-now bug; the recorded trigger ("multi-user creds OR the importer") is
    arriving, layer (a) is BUILT, and they now assert the FIX. Layer (b) (owner-scoped keys) stays deferred.
  * ADR-0011 — a recorded NULL code_sha is UNVERIFIABLE and must never be adopted; set_active is explicit.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from neuromancer_llm.db.identity import fingerprint_hash
from neuromancer_llm.db.repository import IdentityMismatchError

pytestmark = pytest.mark.pg


# --- FIX #4: register_fingerprint recomputes the hash from the config (L5) --------------------------
def test_rt_register_fingerprint_recomputes_hash(repo, rt):
    """FIX #4: register_fingerprint must recompute fingerprint_hash(semantic_config) and REFUSE a caller
    hash that does not match it (the lone trust-the-caller asymmetry — model/tokenizer/method already
    raise-on-drift). A consistent (hash, config) pair is accepted + idempotent."""
    _tok, mid = rt.seed_tok_model(repo, tokenizer_hash=b"fp4-tok")
    cfg = '{"declared_mode":"greedy","v":1}'
    good = repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash(cfg), model_id=mid, declared_mode="greedy", semantic_config=cfg
    )
    assert good == repo.register_fingerprint(
        fingerprint_hash=fingerprint_hash(cfg), model_id=mid, declared_mode="greedy", semantic_config=cfg
    )
    # a hash that does NOT equal fingerprint_hash(cfg) is rejected at register time (recompute-and-compare)
    with pytest.raises(IdentityMismatchError):
        repo.register_fingerprint(
            fingerprint_hash=b"\x00" * 32, model_id=mid, declared_mode="greedy", semantic_config=cfg
        )


def test_rt_fingerprint_drift_on_model_or_mode(repo, rt):
    """L5 GAP: the conflict-compare OR-clause — same fingerprint_hash but a different model_id OR
    declared_mode raises (only the semantic_config branch was covered)."""
    _tok, mid = rt.seed_tok_model(repo, tokenizer_hash=b"fp-drift-tok")
    cfg = '{"declared_mode":"greedy","x":1}'
    h = fingerprint_hash(cfg)
    repo.register_fingerprint(fingerprint_hash=h, model_id=mid, declared_mode="greedy", semantic_config=cfg)
    # same hash + same config, but a different declared_mode -> drift
    with pytest.raises(IdentityMismatchError):
        repo.register_fingerprint(
            fingerprint_hash=h, model_id=mid, declared_mode="seeded_sampling", semantic_config=cfg
        )


# --- FIX #8 / L-SB: storage-backend identity is register-first / immutable --------------------------
def test_rt_storage_backend_identity_immutable(repo):
    """FIX #8 + new binding row L-SB: get_or_create_storage_backend is the ONLY registry that mutated on
    conflict (DO UPDATE SET driver, ignoring lane/base_uri/is_cloud) — a silent repoint of the lake driver
    /URI under a stable backend_key (split-brain blob storage). It must now raise IdentityMismatchError on
    ANY drift of (driver, lane, base_uri, is_cloud), matching the sibling registries; identical re-register
    is idempotent."""
    bid = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri="file:///lake", is_cloud=False
    )
    assert bid == repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri="file:///lake", is_cloud=False
    )
    for drift in (
        dict(driver="azure_blob", lane="artifacts", base_uri="file:///lake", is_cloud=False),  # driver
        dict(driver="local_fs", lane="scratch", base_uri="file:///lake", is_cloud=False),  # lane
        dict(driver="local_fs", lane="artifacts", base_uri="azure://other", is_cloud=False),  # base_uri
        dict(driver="local_fs", lane="artifacts", base_uri="file:///lake", is_cloud=True),  # is_cloud
    ):
        with pytest.raises(IdentityMismatchError):
            repo.get_or_create_storage_backend("lake", **drift)


# --- FIX #9: register_model_identity derives tokenizer_id FROM tokenizer_hash -----------------------
def test_rt_model_identity_no_tokenizer_split(repo):
    """FIX #9: register_model_identity hashes tokenizer_HASH but used to ALSO take a tokenizer_id the
    caller could set to a DIFFERENT tokenizer row (the FK only proves it exists) — an identity-split
    vector. The fix derives tokenizer_id FROM tokenizer_hash (one source of truth) and is register-first:
    it raises if the tokenizer_hash is not registered yet."""
    # register-first: an unregistered tokenizer_hash refuses (raise, never a dangling/auto-bind)
    with pytest.raises(IdentityMismatchError):
        repo.register_model_identity(
            hf_repo="r",
            hf_revision="rev",
            dtype_quant="bf16",
            tokenizer_hash=b"never-registered",
            serving_stack="vllm",
            serving_version="0.23.0",
            arch_family="llama",
        )
    # register the tokenizer, then the model binds to EXACTLY that tokenizer (no split possible)
    tok = repo.register_tokenizer_identity(tokenizer_hash=b"split-tok")
    mid = repo.register_model_identity(
        hf_repo="r",
        hf_revision="rev",
        dtype_quant="bf16",
        tokenizer_hash=b"split-tok",
        serving_stack="vllm",
        serving_version="0.23.0",
        arch_family="llama",
    )
    with repo.engine.connect() as conn:
        bound_hash = conn.execute(
            text(
                "SELECT t.tokenizer_hash FROM neuro.model_identities m "
                "JOIN neuro.tokenizer_identities t ON t.tokenizer_id = m.tokenizer_id "
                "WHERE m.model_id = :m"
            ),
            {"m": mid},
        ).scalar_one()
    # the model's tokenizer_id resolves to the SAME hash the identity_hash embeds (no A-vs-B split)
    assert bytes(bound_hash) == b"split-tok"
    with repo.engine.connect() as conn:
        stored_tid = conn.execute(
            text("SELECT tokenizer_id FROM neuro.model_identities WHERE model_id = :m"), {"m": mid}
        ).scalar_one()
    assert stored_tid == tok


# --- L5 GAP: method version code_sha drift ----------------------------------------------------------
def test_rt_method_version_code_sha_drift(repo):
    """L5 GAP: same (method_key, semver) re-registered with a DIFFERENT code_sha raises (bump the semver
    when the implementation changes; ADR-0011 registry/runtime parity)."""
    repo.register_method_version(method_key="m_probe", semver="1.0.0", code_sha=b"\xaa" * 32, set_active=True)
    with pytest.raises(IdentityMismatchError):
        repo.register_method_version(
            method_key="m_probe", semver="1.0.0", code_sha=b"\xbb" * 32, set_active=True
        )


# --- L11 GAP: get_or_create_run drift on the OTHER immutable fields ---------------------------------
@pytest.mark.parametrize(
    "field,bad",
    [
        ("work_slug", "OTHER"),
        ("variant_digest", "OTHER"),
        ("run_kind", "adhoc"),
    ],
)
def test_rt_get_or_create_run_drift_other_fields(seeded, field, bad):
    """L11 GAP: a run_key conflict is idempotent ONLY when EVERY immutable field matches — drift on
    work_slug / variant_digest / run_kind (not just fingerprint) raises (composer note D1)."""
    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    base = dict(
        campaign_id=cid, work_slug="ws", variant_digest="vd", actor_id=aid, origin="o", run_kind="experiment"
    )
    rid = repo.get_or_create_run("c-test/ws/vd", **base)
    assert repo.get_or_create_run("c-test/ws/vd", **base) == rid  # idempotent
    drifted = dict(base, **{field: bad})
    with pytest.raises(IdentityMismatchError):
        repo.get_or_create_run("c-test/ws/vd", **drifted)


def test_rt_get_or_create_run_invocation_id_drift(seeded):
    """L11 GAP: invocation_id has bespoke str-normalization (NULL-vs-set entirely untested). A run created
    canonical (invocation_id NULL) then re-requested with a set invocation_id under the same run_key raises."""
    import uuid as _uuid

    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    base = dict(campaign_id=cid, work_slug="iv", variant_digest="v1", actor_id=aid, origin="o")
    rid = repo.get_or_create_run("c-test/iv/v1", **base, invocation_id=None)
    assert repo.get_or_create_run("c-test/iv/v1", **base, invocation_id=None) == rid
    with pytest.raises(IdentityMismatchError):
        repo.get_or_create_run("c-test/iv/v1", **base, invocation_id=_uuid.uuid4())


# --- ADR-0048 layer (a): actor/campaign key-drift now RAISES (the importer trigger is arriving) -----
# ⚠ HISTORY (2026-07-14): this probe formerly ASSERTED the BROKEN behavior ("the deferred guard would raise
# here"). Its recorded trigger — "multi-user creds OR the importer" — is arriving; layer (a) is now BUILT and
# the probe asserts the RAISE. Do NOT "fix" the code to make an accepted-now assertion pass again: that would
# RE-INTRODUCE the silent ownership/lineage reassignment. Layer (b) (owner-scoped keys) remains deferred.
def test_rt_campaign_owner_drift_raises(seeded):
    """ADR-0048 layer (a) — THE OWNERSHIP BITE. A campaign re-created under the SAME campaign_key but a
    DIFFERENT actor_id previously returned the existing row and SILENTLY kept the old owner. It now raises,
    and ownership is provably unchanged."""
    repo, aid = seeded["repo"], seeded["actor_id"]
    other = repo.get_or_create_actor("other-owner", kind="agent")
    assert other != aid
    cid = repo.get_or_create_campaign("shared-key", aid)
    assert repo.get_or_create_campaign("shared-key", aid) == cid  # idempotent for the SAME owner
    with pytest.raises(IdentityMismatchError, match="already owned by"):
        repo.get_or_create_campaign("shared-key", other)
    with repo.engine.connect() as conn:
        owner = conn.execute(
            text("SELECT actor_id FROM neuro.campaigns WHERE campaign_id = :c"), {"c": cid}
        ).scalar_one()
    assert owner == aid  # ownership NOT reassigned


def test_rt_actor_kind_drift_raises(seeded):
    """ADR-0048 layer (a): identity is (actor_key, kind). One actor_key bound to two kinds — e.g. an
    'importer' key colliding with an existing 'agent' — is the misattribution this closes."""
    repo = seeded["repo"]
    a1 = repo.get_or_create_actor("drift-key", kind="agent")
    assert repo.get_or_create_actor("drift-key", kind="agent") == a1  # idempotent
    with pytest.raises(IdentityMismatchError, match="already registered with kind"):
        repo.get_or_create_actor("drift-key", kind="human")


def test_rt_actor_display_name_is_not_identity(seeded):
    """display_name is a mutable LABEL, not identity, and is deliberately NOT compared: the column is
    NOT NULL and coerced to actor_key on INSERT (so there is no 'unspecified' state the C10 convention could
    key on), and the registrar holds NO UPDATE on actors — a raise here would be unrecoverable in-role. A
    re-register with a different label must NOT raise, and the EXISTING label is KEPT (no last-writer-wins)."""
    repo = seeded["repo"]
    aid = repo.get_or_create_actor("label-key", kind="agent", display_name="First Label")
    assert repo.get_or_create_actor("label-key", kind="agent", display_name="Second Label") == aid
    assert repo.get_or_create_actor("label-key", kind="agent") == aid  # omitted label: no false drift
    with repo.engine.connect() as conn:
        label = conn.execute(
            text("SELECT display_name FROM neuro.actors WHERE actor_id = :a"), {"a": aid}
        ).scalar_one()
    assert label == "First Label"  # kept, not overwritten


_RACE_WAIT = 15.0
# A single 2-thread attempt only LOSES the race ~1/3 of the time (the BLOCK-1 200-trial hammer finding), so
# one round would be a FLAKY revert-RED. Loop it (the FIX-A looped-probe idiom) so the non-vacuity is reliable.
_RACE_ROUNDS = 16


def _race_two(call):
    """Release two threads SIMULTANEOUSLY on a Barrier (the house primitive — test_rt_concurrency._race_two).
    An exception in EITHER thread propagates out of .result(), so a lost race fails the test loudly."""
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait(timeout=_RACE_WAIT)
        return call()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(_run), ex.submit(_run)
        return [f1.result(timeout=30), f2.result(timeout=30)]


def test_rt_actor_concurrent_first_creation_one_row(seeded):
    """Bare ON CONFLICT + re-SELECT also closes the concurrent-first-creation race (the BLOCK-1/1b class):
    two threads released SIMULTANEOUSLY on the SAME new key yield ONE row and no IntegrityError — the old
    SELECT-then-INSERT loses this race (both SELECT empty, both INSERT, one gets a unique violation)."""
    repo = seeded["repo"]
    for i in range(_RACE_ROUNDS):
        key = f"race-key-{i}"
        got = _race_two(lambda k=key: repo.get_or_create_actor(k, kind="agent"))
        assert got[0] == got[1]  # both threads agree on ONE row; an IntegrityError would have propagated


# --- ADR-0011: a recorded NULL code_sha is UNVERIFIABLE and must never be adopted -------------------
def test_rt_method_version_null_code_sha_not_adopted(repo):
    """The old guard read `if existing["code_sha"] is not None and ...`, so a recorded NULL sha
    SHORT-CIRCUITED it and was silently adopted against ANY incoming hash — permanently defeating ADR-0011
    parity for that method_key@semver (and every promotions row FK'd to it would then carry an unverifiable
    governance stamp). A NULL sha must fail CLOSED."""
    with repo.engine.begin() as conn:
        mid = conn.execute(
            text("INSERT INTO neuro.methods (method_key) VALUES ('m_null') RETURNING method_id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO neuro.method_versions (method_id, semver, code_sha) VALUES (:m, '1.0.0', NULL)"
            ),
            {"m": mid},
        )
    with pytest.raises(IdentityMismatchError, match="NULL code_sha"):
        repo.register_method_version(
            method_key="m_null", semver="1.0.0", code_sha=b"\xaa" * 32, set_active=False
        )


def test_rt_method_version_set_active_is_explicit(repo):
    """set_active is REQUIRED (no default) AND mechanically load-bearing: with set_active=False the
    registry's active pointer must NOT move (the defaulted True made every call a silent last-write-wins
    repoint of methods.active_version_id)."""
    v1 = repo.register_method_version(
        method_key="m_active", semver="1.0.0", code_sha=b"\x11" * 32, set_active=True
    )
    v2 = repo.register_method_version(
        method_key="m_active", semver="2.0.0", code_sha=b"\x22" * 32, set_active=False
    )
    assert v1 != v2
    with repo.engine.connect() as conn:
        active = conn.execute(
            text("SELECT active_version_id FROM neuro.methods WHERE method_key = 'm_active'")
        ).scalar_one()
    assert active == v1  # NOT repointed by the set_active=False registration
    with pytest.raises(TypeError):  # the default is gone — callers must state intent
        repo.register_method_version(method_key="m_active", semver="3.0.0", code_sha=b"\x33" * 32)  # type: ignore[call-arg]
