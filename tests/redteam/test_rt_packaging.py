"""Red-team: packaging / docs cannot rot (L12).

* docs-gate self-test — build_docs(check=True) returns 0 against the committed docs (the byte-stable gate
  is a CI step only; nothing pinned that it passes), and is deterministic across runs.
* grants.sql is shipped as package data — importable from the installed package (the security-contract
  companion to the schema must travel with the wheel).
"""

from __future__ import annotations


def test_rt_docs_gate_passes_and_is_deterministic():
    """L12: the byte-stable docs gate returns 0 against the committed docs (no drift) and is deterministic
    (two runs agree) — so a CLI/ORM/env/governance surface change that didn't regenerate docs is caught."""
    from neuromancer_llm.governance.docs_gen import build_docs

    first = build_docs(check=True)
    second = build_docs(check=True)
    assert first == 0, "committed docs drifted from the generated docs (run `neuro docs build`)"
    assert second == first  # deterministic (no dict-ordering / locale / CRLF nondeterminism)


def test_rt_grants_sql_is_packaged_and_loadable():
    """L12: grants.sql (the security-contract companion to the schema) ships as package data and loads from
    the installed package — a packaging regression that dropped it would break `neuro db roles`."""
    from neuromancer_llm.db.provision import grants_sql

    sql = grants_sql()
    assert "GRANT" in sql and "neuro_registrar" in sql and "neuro_writer" in sql
    # the column-scoped deviation grants the security contract depends on are present
    assert "active_version_id" in sql  # registrar's only UPDATE
    assert "GRANT UPDATE (deleted_at) ON artifacts" in sql  # writer tombstone-state only (NOT sha256/uri)
