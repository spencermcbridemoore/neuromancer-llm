"""The tracked corpus manifest (`ops/corpus-import/`) — the rebuild discipline as MECHANISM.

`CORPUS_IMPORT_README` §6 says: *"Never patch `import_manifest.csv` directly. Patch the relevant
`part_*.csv` and rebuild."* Until this file, that was prose an operator could silently violate. It is
now checkable: the nine parts concatenate to the manifest EXACTLY, so patching the manifest directly
reddens the suite.

⚠ WHAT IS **NOT** COMMITTED, AND WHY IT IS A RECORDED FINDING, NOT A GAP. The staging folder also
holds `gen.py`, which is what the README implies performs the rebuild. It CANNOT: measured, it
declares TEN field names while the manifest and every part carry TWELVE (`confidentiality` and
`source_system` are absent from its list), and its input path points at a foreign
`/sessions/...` mount that does not exist on any machine here. Committing it would ship a
build script that provably cannot reproduce its own output. The rebuild that DOES work is
concatenation, which is what this test pins — so the discipline is executable exactly to the extent
that this file says it is, and no further.
"""

from __future__ import annotations

import csv
import pathlib

import pytest

CORPUS = pathlib.Path(__file__).resolve().parents[1] / "ops" / "corpus-import"
MANIFEST = CORPUS / "import_manifest.csv"

#: The three families excluded from the RIP lane by owner ruling, and their measured row counts.
_EXCLUDED_COUNTS = {"competition-trace-the-ace": 22825, "stimuli-estela": 280, "mi-sae-asset": 1}


def _parts() -> list[pathlib.Path]:
    return sorted(CORPUS.glob("part_*.csv"))


def test_the_parts_are_globbed_at_all() -> None:
    """Pin the glob's OWN result first. A mis-resolved directory would make every assertion below
    vacuously true — the empty-glob trap the D3 scan already carries."""
    assert len(_parts()) == 9, f"expected nine parts, globbed: {[p.name for p in _parts()]}"
    assert MANIFEST.exists()


def test_the_nine_parts_concatenate_to_the_manifest_exactly() -> None:
    """★ THE REBUILD DISCIPLINE, AS MECHANISM. Header from the first part, bodies from the rest.

    ⚠ It asserts the RELATIONSHIP, never a pinned sha256. `.gitattributes` (`* text=auto eol=lf`)
    normalises every CSV on commit and checkout, so an absolute digest measured in staging is NOT
    the digest of the committed file — but both sides are normalised identically, so the equality
    survives. A pinned digest here would be a checkable falsehood the first time anyone cloned."""
    parts = _parts()
    rebuilt = b""
    for i, p in enumerate(parts):
        raw = p.read_bytes()
        rebuilt += raw if i == 0 else raw[raw.index(b"\n") + 1 :]
    assert rebuilt == MANIFEST.read_bytes(), (
        "concat(part_*.csv) != import_manifest.csv — the manifest was patched DIRECTLY, which "
        "README §6 forbids. Patch the part and rebuild by concatenation."
    )


def _rows() -> list[dict]:
    with MANIFEST.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_the_manifest_shape_is_the_twelve_columns_of_record() -> None:
    with MANIFEST.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == [
        "uri",
        "kind",
        "size_bytes",
        "dtype",
        "shape",
        "retention",
        "confidentiality",
        "source_system",
        "family",
        "workflow_seat",
        "source_abspath",
        "note",
    ]


def test_the_importable_set_is_the_expected_size() -> None:
    """The acceptance appendix, as a fixture rather than a number in a runbook. A drifted manifest
    changes what a guided execution would permanently register, so it must redden here first."""
    rows = _rows()
    assert len(rows) == 24859
    keep = [r for r in rows if r["family"] not in _EXCLUDED_COUNTS]
    assert len(keep) == 1753
    assert sum(int(r["size_bytes"]) for r in keep) == 8568575168


@pytest.mark.parametrize(("family", "count"), sorted(_EXCLUDED_COUNTS.items()))
def test_each_excluded_family_is_present_and_the_exclusion_is_load_bearing(family: str, count: int) -> None:
    """⚠ The exclusion keys are LITERALS and one of them is easy to mistype: the family is
    `competition-trace-the-ace`, not `trace-the-ace`. A wrong key silently imports 22,825 permanent
    rows, so the literal is pinned against the manifest here as well as in the denylist."""
    assert sum(1 for r in _rows() if r["family"] == family) == count


def test_uri_is_unique_across_the_importable_set() -> None:
    """`uri` is half the identity preimage and rank 6 keys `(backend_id, uri)`. A duplicate would
    collapse two files onto one pointer."""
    keep = [r for r in _rows() if r["family"] not in _EXCLUDED_COUNTS]
    assert len({r["uri"] for r in keep}) == len(keep)


def test_the_two_batches_split_by_source_system_only() -> None:
    """R-E: the grade is retired, so batches collapse to TWO by `source_system` — not the shipped
    shim's four `(source_system, confidentiality)` groups."""
    keep = [r for r in _rows() if r["family"] not in _EXCLUDED_COUNTS]
    by_ss: dict[str, int] = {}
    for r in keep:
        by_ss[r["source_system"]] = by_ss.get(r["source_system"], 0) + 1
    assert by_ss == {"local-fs": 949, "study-query-llm": 804}


def test_the_coerced_grade_population_is_the_recorded_387() -> None:
    """⚠ 387 importable rows carry a NON-`open` upstream grade and will be stamped `open` under R-A.
    Their historical value survives only in the sidecar. This is NOT the 360 historic MOSART
    `external_records` rows, which keep `exam_restricted` un-edited — two different MOSART-adjacent
    numbers that a reader will conflate unless both are pinned."""
    keep = [r for r in _rows() if r["family"] not in _EXCLUDED_COUNTS]
    assert sum(1 for r in keep if r["confidentiality"] != "open") == 387
