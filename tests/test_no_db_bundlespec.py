"""No-db worker-install gate: bundlespec must import with ZERO db / driver / cloud dependencies.

A no-egress worker (ARCC) builds a self-describing bundle with only the stdlib; the DB registers it
afterward. This proves bundlespec pulls in no heavy import even transitively (phase3-module-layout.md).
Runs in a fresh subprocess so the check is honest (no test-suite imports leak in).
"""

from __future__ import annotations

import subprocess
import sys


def test_bundlespec_imports_without_db_or_drivers():
    code = (
        "import sys\n"
        "import neuromancer_llm.bundles.bundlespec as b\n"
        "assert len(b.MANIFEST_BLOCKS) == 14, b.MANIFEST_BLOCKS\n"
        "b.bundle_uuid_for('run', 'ds')\n"
        "forbidden = sorted(m for m in sys.modules if "
        "    m == 'sqlalchemy' or m == 'psycopg' or m == 'alembic' "
        "    or m.startswith('azure') or m.startswith('neuromancer_llm.db'))\n"
        "assert not forbidden, f'bundlespec pulled in: {forbidden}'\n"
        "print('NO_DB_OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "NO_DB_OK" in result.stdout
