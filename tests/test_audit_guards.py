"""Audit correction pass (2026-07-02, 38-agent audit) — pure fail-closed guard probes.

* C1 — `assert_pg_major` (db/lanes.py): the migrate path refuses any non-PG-18 major BEFORE any DDL.
  Migration 0001's syntax runs green on PG 15-17 (`NULLS NOT DISTINCT` needs >= 15), so nothing else
  would refuse a wrong major — but the migrations==frozen-DDL parity proof is anchored to PG 18; a
  quietly-green PG 16 build would be an UNPROVEN schema. `migrations/env.py` calls this on connect.
* C4.4 — `_tokenizer_hash_bytes` (cli/capture.py): --tokenizer-hash must be exactly 64 hex chars
  (32 bytes). `bytes.fromhex` accepted any even-length hex — a truncated value became durable
  register-first identity that raise-on-drift could never reconcile (a silent identity split) — and
  malformed hex escaped the CLI's except-tuple as a raw ValueError traceback.

Both probes are pure (no Postgres, no capability marker) so they can never skip.
"""

from __future__ import annotations

import pytest

from neuromancer_llm.cli.capture import _tokenizer_hash_bytes
from neuromancer_llm.db.lanes import ConfigurationError, assert_pg_major


def test_pg_major_assert_fails_closed_on_non_18():
    """C1: PG 18.x passes; any other major raises ConfigurationError (fail closed, before any DDL)."""
    assert_pg_major(180000)  # 18.0
    assert_pg_major(180001)  # 18.1
    assert_pg_major(189999)  # any 18.x
    for bad in (130000, 150002, 160000, 170004, 190000, 250001):
        with pytest.raises(ConfigurationError, match="parity baseline"):
            assert_pg_major(bad)


def test_pg_major_assert_is_wired_into_migrations_env():
    """C1 (wiring pin): migrations/env.py actually CALLS assert_pg_major before any DDL — importing
    env.py would run migrations, so the wiring is pinned statically (same style as the L13 probes)."""
    import pathlib

    env_py = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "env.py"
    src = env_py.read_text(encoding="utf-8")
    assert "assert_pg_major(" in src and "server_version_num" in src
    # the guard runs BEFORE the schema bootstrap (the first DDL)
    assert src.index("assert_pg_major(") < src.index("CREATE SCHEMA IF NOT EXISTS neuro")


def test_tokenizer_hash_validation_is_wired_into_both_capture_commands():
    """C4.4 (wiring pin, review hardening): both CLI callsites (logprob + replay) route --tokenizer-hash
    through the validating helper — a revert to raw `bytes.fromhex(tokenizer_hash)` at either callsite
    would otherwise keep the suite green (the behavior probe below pins only the helper)."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "neuromancer_llm" / "cli" / "capture.py"
    ).read_text(encoding="utf-8")
    assert src.count("_tokenizer_hash_bytes(tokenizer_hash)") == 2  # logprob + replay
    # no raw fromhex on the CLI argument anywhere outside the helper's own body
    assert src.count("bytes.fromhex(value)") == 1 and "bytes.fromhex(tokenizer_hash)" not in src


def test_tokenizer_hash_validation_fails_closed():
    """C4.4: exactly 64 hex chars -> 32 bytes; anything else is a typed ConfigurationError (never a raw
    ValueError traceback, never a truncated durable identity)."""
    ok = "ab" * 32
    assert _tokenizer_hash_bytes(ok) == bytes.fromhex(ok)
    assert _tokenizer_hash_bytes("  " + ok + "  ") == bytes.fromhex(ok)  # surrounding whitespace tolerated
    for bad in (
        "abcd",  # valid hex, 2 bytes — the silent-identity-split shape
        "ab" * 31,  # 31 bytes
        "ab" * 33,  # 33 bytes
        "xyz",  # not hex (odd length too) — used to escape as a raw ValueError
        "gg" * 32,  # 64 chars but not hex
        "",  # empty
    ):
        with pytest.raises(ConfigurationError):
            _tokenizer_hash_bytes(bad)
