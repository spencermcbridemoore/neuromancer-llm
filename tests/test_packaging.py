"""Packaging-correctness: the grants SQL must load as package data (the wheel already ships it; this
pins it against regression so `neuro db roles` never breaks on a missing data file). Binding: packaging
correctness."""

from __future__ import annotations

import importlib.resources


def test_grants_sql_is_packaged_and_loadable():
    text = (
        importlib.resources.files("neuromancer_llm.db.sql").joinpath("grants.sql").read_text(encoding="utf-8")
    )
    assert "GRANT" in text
    # the four roles the grants contract depends on must be present
    for role in ("neuro_writer", "neuro_registrar", "neuro_reader", "neuro_admin"):
        assert role in text, f"{role} missing from packaged grants.sql"
