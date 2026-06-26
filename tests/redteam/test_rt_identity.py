"""Red-team: register-first / fail-closed identity (L5, L11, L-SB).

Probes that the identity registries refuse to adopt a divergent or caller-trusted identity:
  * FIX #4 — register_fingerprint recomputes the hash from semantic_config (no trust-the-caller).
  * FIX #8 / L-SB — storage_backend identity is register-first/immutable (no DO UPDATE repoint).
  * FIX #9 — register_model_identity derives tokenizer_id FROM tokenizer_hash (no id/hash split).
  * L5 / L11 GAP probes — drift on the other identity dimensions raises (guards present, untested).
  * #5 (ADR-accept) — get_or_create_actor/campaign key-drift is accepted single-user; this DOCUMENTS the
    current behavior + cites the deferred obligation (it does not assert a fix).
"""

from __future__ import annotations

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
    repo.register_method_version(method_key="m_probe", semver="1.0.0", code_sha=b"\xaa" * 32)
    with pytest.raises(IdentityMismatchError):
        repo.register_method_version(method_key="m_probe", semver="1.0.0", code_sha=b"\xbb" * 32)


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


# --- #5 (ADR-accept): actor/campaign key-drift is accepted single-user (DOCUMENTS, not a fix) -------
def test_rt_actor_campaign_keydrift_accepted_now(seeded):
    """#5 ADR-accept + DEFERRED-OBLIGATION: get_or_create_campaign returns the EXISTING row by key with NO
    comparison — a campaign re-created under the same campaign_key but a different actor_id silently keeps
    the old owner. Accepted under single-user creds; the real bite (two users colliding on a shared key ->
    silent ownership/lineage reassignment) is a deferred obligation (trigger = multi-user creds OR the
    importer; see the Deferred-Obligation Register). This probe PINS the current single-user behavior so a
    future change is visible."""
    repo, aid = seeded["repo"], seeded["actor_id"]
    other = repo.get_or_create_actor("other-owner", kind="agent")
    assert other != aid
    cid = repo.get_or_create_campaign("shared-key", aid)
    # re-create under the SAME key with a DIFFERENT actor -> the OLD row is returned (no drift guard today)
    assert repo.get_or_create_campaign("shared-key", other) == cid
    with repo.engine.connect() as conn:
        owner = conn.execute(
            text("SELECT actor_id FROM neuro.campaigns WHERE campaign_id = :c"), {"c": cid}
        ).scalar_one()
    assert owner == aid  # ownership did NOT change (accepted now; the deferred guard would raise here)
