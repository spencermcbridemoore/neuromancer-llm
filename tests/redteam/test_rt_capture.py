"""Red-team: verbatim capture write path (L4) + FIX #1 (content-addressed spill) + C10 (attribution).

* FIX #1 — the put-before-insert ORPHAN blob can never be silently clobbered by a later divergent spill
  (content-addressed keys: different bytes -> different key).
* C10(a) — a same-bytes resume with a DIFFERENT immutable stamp (model_id/actor_id/origin/provenance)
  raises (was: bytes verified, stamps not -> a silent attribution lie).
* C10(b) — capture_events.model_id is bound to the run's fingerprint model (closes the model_id<->run hole).
* L4 GAP — cross-tier (inline<->spill) resume drift raises.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from neuromancer_llm.bundles.registrar import SeamIntegrityError
from neuromancer_llm.capture.events import INLINE_CAP, write_capture_event
from neuromancer_llm.db.repository import IdentityMismatchError
from neuromancer_llm.storage.backends import LocalFsBackend

pytestmark = pytest.mark.pg


def _backend(repo, tmp_path):
    backend_id = repo.get_or_create_storage_backend(
        "lake", driver="local_fs", lane="artifacts", base_uri=str(tmp_path), is_cloud=False
    )
    return LocalFsBackend(tmp_path), backend_id


def _run_bound_to_model(seeded, rt, *, dtype_quant="bf16", tok=b"rt-cap-tok", slug="cap", vd="v1"):
    """A run LABELED with a fingerprint whose model is registered; returns (run_id, model_id)."""
    repo, cid, aid = seeded["repo"], seeded["campaign_id"], seeded["actor_id"]
    _t, mid = rt.seed_tok_model(repo, tokenizer_hash=tok, dtype_quant=dtype_quant)
    cfg = f'{{"declared_mode":"greedy","slug":"{slug}"}}'
    fp = rt.fingerprint(repo, config_text=cfg, model_id=mid)
    run_id = repo.get_or_create_run(
        f"c-test/{slug}/{vd}",
        campaign_id=cid,
        work_slug=slug,
        variant_digest=vd,
        actor_id=aid,
        origin="o",
        fingerprint_id=fp,
    )
    return run_id, mid


# --- FIX #1: the put-before-insert orphan blob is never silently clobbered -------------------------
def test_rt_spill_orphan_blob_not_clobbered(seeded, rt, tmp_path):
    """FIX #1 (the actual bug): backend.put runs BEFORE the artifacts INSERT and is non-atomic, so a crash
    in that gap leaves an ORPHAN blob (a blob with no artifact row). With coordinate-derived keys, a later
    divergent spill 'for the same logical event' found no row and silently RE-PUT, clobbering the orphan.
    Content-addressing makes the divergent spill land at a DIFFERENT key, so the orphan survives untouched."""
    repo = seeded["repo"]
    backend, backend_id = _backend(repo, tmp_path)
    _tok, mid = rt.seed_tok_model(repo, tokenizer_hash=b"orphan-tok")  # seeded run has NULL fp -> C10b allows
    partition_path = "logprobs/run=x/part-0000"
    event_key = "ev"
    # plant the orphan exactly where the OLD coordinate-derived key would have put the request payload
    orphan_key = f"{partition_path}/wire/{event_key}.request.json"
    orphan_bytes = b"O" * (INLINE_CAP + 100)
    backend.put(orphan_key, orphan_bytes)  # crash-after-put-before-insert: a blob with no artifact row

    # a DIFFERENT >8KB request for the same (run, event_key) — content-addressed spill lands elsewhere
    divergent = b"D" * (INLINE_CAP + 200)
    write_capture_event(
        repo.engine,
        backend,
        run_id=seeded["run_id"],
        event_key=event_key,
        model_id=mid,
        actor_id=seeded["actor_id"],
        origin="h",
        request_body=divergent,
        response_body=b"{}",
        backend_id=backend_id,
        partition_path=partition_path,
    )
    assert backend.get(orphan_key) == orphan_bytes  # the orphan blob was NOT clobbered (different key)


# --- C10(a): immutable attribution stamp drift on resume -------------------------------------------
def test_rt_capture_resume_stamp_drift_raises(seeded, rt, tmp_path):
    """C10(a): a same-bytes resume with a DIFFERENT origin (an immutable attribution stamp) raises — the
    bytes were verified before, the stamps were not (a silent attribution lie)."""
    repo = seeded["repo"]
    backend, backend_id = _backend(repo, tmp_path)
    run_id, mid = _run_bound_to_model(seeded, rt)
    common = dict(
        run_id=run_id,
        event_key="ev",
        model_id=mid,
        actor_id=seeded["actor_id"],
        backend_id=backend_id,
        partition_path="logprobs/run=x/part-0000",
        request_body=b'{"a":1}',
        response_body=b'{"b":2}',
    )
    first = write_capture_event(repo.engine, backend, origin="host-A", **common)
    # identical bytes + identical stamps -> idempotent
    assert (
        write_capture_event(repo.engine, backend, origin="host-A", **common).capture_event_id
        == first.capture_event_id
    )
    # SAME bytes, DIFFERENT origin stamp -> raise (no silent attribution drift)
    with pytest.raises(SeamIntegrityError):
        write_capture_event(repo.engine, backend, origin="host-B", **common)
    # the stored stamp is unchanged
    with repo.engine.connect() as conn:
        origin = conn.execute(
            text("SELECT origin FROM neuro.capture_events WHERE capture_event_id = :i"),
            {"i": first.capture_event_id},
        ).scalar_one()
    assert origin == "host-A"


# --- C10(b): capture_events.model_id is bound to the run's fingerprint model ------------------------
def test_rt_capture_model_bound_to_run_fingerprint(seeded, rt, tmp_path):
    """C10(b): a run LABELED with a fingerprint pins its model — a capture claiming a DIFFERENT model_id is
    refused (closes the model_id<->run hole). The bound model is accepted."""
    repo = seeded["repo"]
    backend, backend_id = _backend(repo, tmp_path)
    run_id, mid_a = _run_bound_to_model(seeded, rt, tok=b"bound-tok-a", slug="bnd")
    # a second, DIFFERENT model (distinct tokenizer -> distinct identity)
    _t, mid_b = rt.seed_tok_model(repo, tokenizer_hash=b"bound-tok-b", dtype_quant="fp16")
    assert mid_a != mid_b
    common = dict(
        run_id=run_id,
        event_key="ev",
        actor_id=seeded["actor_id"],
        origin="h",
        backend_id=backend_id,
        partition_path="logprobs/run=x/part-0000",
        request_body=b'{"a":1}',
        response_body=b'{"b":2}',
    )
    with pytest.raises(IdentityMismatchError):  # model B is not the run's fingerprint model -> refused
        write_capture_event(repo.engine, backend, model_id=mid_b, **common)
    # the bound model is accepted
    assert write_capture_event(repo.engine, backend, model_id=mid_a, **common).capture_event_id is not None


# --- L4 GAP: cross-tier (inline <-> spill) resume drift --------------------------------------------
def test_rt_capture_cross_tier_resume_drift_raises(seeded, rt, tmp_path):
    """L4 GAP: the inline/spill decision is part of the immutable row. A first write that inlines (<=8KB)
    then a resume whose body would SPILL (>8KB) is a capture-row drift -> raises (the drift test stayed
    inline; this crosses the tier boundary)."""
    repo = seeded["repo"]
    backend, backend_id = _backend(repo, tmp_path)
    run_id, mid = _run_bound_to_model(seeded, rt, tok=b"xtier-tok", slug="xt")
    common = dict(
        run_id=run_id,
        event_key="ev",
        model_id=mid,
        actor_id=seeded["actor_id"],
        origin="h",
        backend_id=backend_id,
        partition_path="logprobs/run=x/part-0000",
        response_body=b'{"b":2}',
    )
    write_capture_event(repo.engine, backend, request_body=b'{"small":1}', **common)  # inline
    with pytest.raises(SeamIntegrityError):  # resume body would SPILL (>8KB) -> tier drift
        write_capture_event(
            repo.engine, backend, request_body=b'{"x":"' + b"Z" * (INLINE_CAP + 50) + b'"}', **common
        )
