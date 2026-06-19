"""Bundle contract: zero-dependency bundlespec + the single-callsite W1-W8 registrar + GC + crawl.

IMPORTANT: this package's __init__ imports NOTHING from db/ or storage adapters, so
`neuromancer_llm.bundles.bundlespec` is importable on a no-egress worker with no DB driver installed.
The no-db worker-install CI smoke gate (tests/test_no_db_bundlespec.py) proves it.
"""

from __future__ import annotations
