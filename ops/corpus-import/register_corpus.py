"""Register the research corpus as permanent register-in-place pointers (the RIP lane).

Replaces the untracked `Downloads/neuromancer_import/neuromancer_register_in_place.py` shim, which
has NEVER executed against a live database and whose family filter is INCLUSION-only — so a bare run
of it registers all 24,859 manifest rows instead of the 1,753 this lane imports.

⚠ THIS DRIVER IS TRACKED IN `ops/` ON PURPOSE. It constructs a raw engine, which the L1 tripwire
forbids anywhere under `src/`; `ops/` is outside that scan and outside pyright's include. Operator
artifacts live here (the precedent is `ops/e6_run.py`), so this one cannot be lost to a `git clean`
the way the shim can.

WHAT IT WRITES, PER ROW: a rank-6 `artifacts` pointer (no bytes copied, no cloud upload), the paired
rank-5 `external_records` manifest row, and the rank-7 `annotates` edge. All three are PERMANENT —
there is no delete verb for any of them anywhere in `src/`.

    python ops/corpus-import/register_corpus.py --dry-run
    python ops/corpus-import/register_corpus.py --lane canonical --family mi-activation-capture
    python ops/corpus-import/register_corpus.py --lane canonical
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from sqlalchemy import create_engine, text  # noqa: E402

from neuromancer_llm.db.repository import Repository  # noqa: E402
from neuromancer_llm.db.session import database_url  # noqa: E402
from neuromancer_llm.importer.ingress import (  # noqa: E402
    Confidentiality,
    SourceSystem,
    close_import_batch,
    open_import_batch,
)
from neuromancer_llm.importer.rip import (  # noqa: E402
    EXCLUDED_FAMILIES,
    derived_by_predecessor_for,
    map_corpus_file,
)

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = HERE / "import_manifest.csv"

#: The measured size of the importable set. A BELT, not decoration: if the manifest drifts, this
#: driver refuses rather than permanently registering a different corpus than the one reviewed.
EXPECTED_ROWS = 1753
EXPECTED_BYTES = 8568575168


def load_rows(include_excluded: frozenset[str]) -> list[dict]:
    """Read the manifest and apply the DEFAULT-ON denylist.

    ⚠ THE FILTER DIRECTION IS THE WHOLE POINT. The shim's `--family` is an INCLUSION filter, so
    omitting it imports EVERYTHING — 24,859 rows, 22,825 of them the deferred Trace-the-Ace family,
    permanently. Here the three ruled exclusions are removed unconditionally and can only be added
    back by naming one with `--include-excluded-family`. Forgetting a flag under-imports (recoverable
    — run again) instead of over-importing (unrecoverable — no delete verb)."""
    with MANIFEST.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    denied = EXCLUDED_FAMILIES - include_excluded
    return [r for r in rows if r["family"] not in denied]


def preflight_finished_at_privilege(repo: Repository) -> None:
    """Prove the `UPDATE (finished_at) ON import_batches` privilege BEFORE any batch is opened.

    ⚠ WHY THIS RUNS FIRST AND NOT LAST. `close_import_batch` is the importer's ONLY UPDATE — every
    other importer write is INSERT-only — and `neuro_registrar` holds INSERT + SELECT on the
    registries with NO UPDATE. Without this check a registrar-DSN run lands all 1,753 PERMANENT rows
    and only then fails, at the very end, on the one statement it was never allowed to make. Proving
    it up front is the only ordering in which that failure costs nothing."""
    with repo.engine.connect() as conn:
        allowed = conn.execute(
            text("SELECT has_column_privilege(current_user, 'neuro.import_batches', 'finished_at', 'UPDATE')")
        ).scalar_one()
        who = conn.execute(text("SELECT current_user")).scalar_one()
    if not allowed:
        raise SystemExit(
            f"PREFLIGHT FAILED: {who} cannot UPDATE neuro.import_batches.finished_at.\n"
            "  This run would register PERMANENT rows and then fail to close its batch.\n"
            "  Use the admin DSN (close_import_batch is the importer's only UPDATE), or drop the\n"
            "  close step deliberately and record that the batch will read NULL forever."
        )
    print(f"  preflight OK: {who} may stamp finished_at")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", default="canonical", help="expected_lane for the Repository")
    ap.add_argument("--family", action="append", help="restrict to these families (repeatable)")
    ap.add_argument(
        "--include-excluded-family",
        action="append",
        default=[],
        metavar="FAMILY",
        help="opt a DENIED family back IN by name (owner-ruled exclusions; permanent once written)",
    )
    ap.add_argument("--limit", type=int, help="register at most N rows (smoke)")
    ap.add_argument("--dry-run", action="store_true", help="touch no database; validate only")
    args = ap.parse_args()

    opted_in = frozenset(args.include_excluded_family)
    if opted_in - EXCLUDED_FAMILIES:
        raise SystemExit(f"not an excluded family: {sorted(opted_in - EXCLUDED_FAMILIES)}")
    if opted_in:
        print(f"⚠ OPTING IN to owner-EXCLUDED families: {sorted(opted_in)}")

    rows = load_rows(opted_in)
    full_set = not (args.family or args.limit or opted_in)
    if full_set:
        total = sum(int(r["size_bytes"]) for r in rows)
        if (len(rows), total) != (EXPECTED_ROWS, EXPECTED_BYTES):
            raise SystemExit(
                f"MANIFEST DRIFT: {len(rows)} rows / {total} B, expected {EXPECTED_ROWS} / "
                f"{EXPECTED_BYTES}. Refusing — a drifted manifest registers a DIFFERENT corpus "
                "than the one reviewed, permanently."
            )
        print(f"  belt OK: {len(rows)} rows / {total / 1e9:.3f} GB matches the reviewed set")

    if args.family:
        rows = [r for r in rows if r["family"] in set(args.family)]
    if args.limit is not None:
        # ⚠ `if args.limit:` was the shipped form and it is the falsy-zero bug: `--limit 0` is FALSE,
        # so the slice was skipped and the run registered ALL 1,753 PERMANENT rows — the exact
        # opposite of what the operator asked for, on a table with no delete verb. Measured by a vet.
        if args.limit < 1:
            raise SystemExit("--limit must be >= 1 (use --dry-run to inspect without writing)")
        rows = rows[: args.limit]

    by_ss = Counter(r["source_system"] for r in rows)
    print(f"{len(rows)} rows across {len(by_ss)} batch(es) (by source_system — the grade is retired):")
    for ss, n in sorted(by_ss.items()):
        print(f"    {ss:<18} rows={n}")

    missing = [r for r in rows if not pathlib.Path(r["source_abspath"]).exists()]
    if missing:
        print(f"  ! {len(missing)} row(s) point at files absent on THIS host, e.g.:")
        for r in missing[:5]:
            print(f"      {r['source_abspath']}")
        if not args.dry_run:
            raise SystemExit("refusing to register rows whose bytes are not present")

    if args.dry_run:
        print("dry run: no database was contacted")
        return 1 if missing else 0

    repo = Repository(create_engine(database_url(), future=True), expected_lane=args.lane)
    preflight_finished_at_privilege(repo)

    minted = noop = 0
    for ss, n in sorted(by_ss.items()):
        source_system = SourceSystem(ss)
        # Every batch stamps `open`, permanently (R-A). For batches from 2026-08-11 that asserts
        # "ungraded by policy", NOT absence-of-concern — the historical grade rides the sidecar.
        handle = open_import_batch(
            repo,
            source_system=source_system,
            confidentiality=Confidentiality.OPEN,
            note=f"research-corpus RIP backfill ({n} artifacts)",
        )
        derived = derived_by_predecessor_for(source_system)
        for row in (r for r in rows if r["source_system"] == ss):
            res = map_corpus_file(repo, handle, row, derived_by_predecessor=derived)
            minted += res.registered
            noop += not res.registered
        stamped = close_import_batch(repo, handle)
        print(f"  batch {ss}: done ({n} rows), finished_at {'stamped' if stamped else 'already set'}")

    print(f"done — {minted} newly registered, {noop} idempotent no-ops")
    return 0


if __name__ == "__main__":
    sys.exit(main())
