"""The shared sftp-batch transport primitive (B-7 blob-lake mirror, 2026-07-20).

ONE off-cloud transport, two drivers: the A2-16 pgbackrest backup mirror (governance/backup_driver.py) and
the B-7 blob-lake mirror (governance/lake_mirror.py) both push to the chroot-scoped desktop account over
`sftp -b` BATCH mode (works against stock Windows OpenSSH, which ships NO rsync). The credential travels as an
ssh_config Host ALIAS consumed only through the CommandRunner seam — it NEVER rides argv.

Extracted from backup_driver.py (the induced-failure-proven A2-16 path, log:215) so a second transport is not
invented (precedent 4: one implementation per concept). Deliberately THIN: only the batch-session runner and
the command seam move here. Each driver keeps its OWN manifest fetch/coerce/write + diff logic — backup's
manifest is dict[str, int] (relpath -> size), the lake's is dict[str, str] (uri -> sha256), and a single
shared manifest helper could not serve both without corrupting one. `run_subprocess` / `CommandResult` /
`CommandRunner` are RE-EXPORTED from backup_driver.py so its existing by-name test imports stay green.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """One seam-executed command's outcome. A timeout is returncode=-1 with a 'timed out' stderr — a driver
    treats it exactly like a failure (fail closed, recorded blocked)."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, argv: list[str], *, timeout_s: float) -> CommandResult: ...


def run_subprocess(argv: list[str], *, timeout_s: float) -> CommandResult:
    """The REAL runner (the VM path). Captures output; converts a timeout into a fail-closed CommandResult
    (never a hang — the systemd TimeoutStartSec is the belt above this suspender)."""
    try:
        proc = subprocess.run(  # noqa: S603 — argv is built from pinned constants + validated params
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return CommandResult(returncode=-1, stdout="", stderr=f"timed out after {timeout_s:.0f}s")
    return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def run_sftp_batch(
    runner: CommandRunner,
    ssh_alias: str,
    batch_lines: list[str],
    *,
    tmp_dir: str | Path,
    timeout_s: float,
) -> CommandResult:
    """Write an sftp batch file into `tmp_dir` and run `sftp -b <batch> -o BatchMode=yes <ssh_alias>` through
    the `runner` seam with a PINNED timeout. The batch filename is unique per call within `tmp_dir` (a counter
    over the current directory contents — the backup driver's original scheme, preserved byte-for-byte). The
    credential (host/user/key) lives entirely in the ssh_config Host alias, never on argv."""
    tmp = Path(tmp_dir)
    batch = tmp / f"batch-{len(list(tmp.iterdir()))}.sftp"
    batch.write_text("\n".join(batch_lines) + "\n", encoding="utf-8")
    return runner(
        ["sftp", "-b", str(batch), "-o", "BatchMode=yes", ssh_alias],
        timeout_s=timeout_s,
    )
