"""GO-D-timer (A2-16): the provisioning-invariant assertion (WAL ruling §3.5) + the cadence pins.

Four retention/cadence/staleness policies are hand-set in different places (the pgbackrest conf, the
systemd timer, this repo's constants). Independently edited, they rot into the ruling-§3.4 failure class
(a repo pruned below the gate's promise; a cadence the 8-day bound can never see). This module makes their
CONSTRAINTS one computable, fail-loud check (`verify_pgbackrest_config`, the `neuro probe verify-config`
delegate) that reads the ACTUAL pgbackrest config + the ACTUAL installed timers and asserts:

  (1) for EVERY repo:  retention-full-type PRESENT and == 'time'  — pgbackrest DEFAULTS to 'count', so a
      dropped type line silently converts "30" from 30 days to 30 backups (a fail-open at any cadence
      under ~7h; §8 external fact);
  (2) min(repo retention) >= BACKUP_STALE_AFTER + BASE_BACKUP_INTERVAL + PROVISIONING_MARGIN  — never
      prune a backup the gate still calls fresh;
  (3) BACKUP_STALE_AFTER >= BASE_BACKUP_INTERVAL + PROVISIONING_MARGIN  — the gate-headroom inequality
      (the margin IS the missed-cycle budget: 2d cadence + 2d margin vs the 8d bound = 2x headroom);
  (4) the §0.1 valve is present (archive-async=y + a parseable archive-push-queue-max);
  (5) the installed backup timer's OnUnitActiveSec == BASE_BACKUP_INTERVAL (the constant is the AUTHORITY;
      a cadence change = one commit + the unit edit, and this equality keeps the two from drifting — the
      stale_after two-edit trap closed structurally);
  (6) the configured conf path EXISTS and no legacy file-form `/etc/pgbackrest.conf` shadows it (the
      VM-rebuild trap: the PGDG package restores the legacy file and pgbackrest silently prefers it);
  (7) the installed ARCHIVER-PROBE timer's OnUnitActiveSec == wal_freshness.ARCHIVER_PROBE_INTERVAL (wal D4,
      the assertion-5 analog for the WAL arm). The signal-staleness bound WAL_LAG_STALE_AFTER=1h is DERIVED
      from that 15-min cadence (~4 missed cycles); if the installed archiver timer drifts to a slower cadence,
      a merely-late probe false-flips the 1h gate — the config-drift blind spot this closes. Checked only when
      the archiver timer text is supplied (the daily verify-config timer passes both timer files).

GO-D-timer hardenings folded here (a2-16-timers-buildgo §7): (#3) `_parse_systemd_span` is CASE-SENSITIVE like
systemd — it no longer lowercases, so systemd's `M` (months) can never be silently mis-read as `m` (minutes);
and the OnUnitActiveSec extraction takes the LAST assignment (systemd honors the last of a repeated directive),
not the first. (#4) a FAILED shape-check on a conf-derived value names the OPTION only and never echoes the raw
value (the config holds the account-wide Azure key; a shape-failed value is untrusted) — timer-derived cadence
values ARE echoed (the timer files hold no secret and the operator needs to see what drifted).

REDACTION CONTRACT (§8 fold 4): the parsed file holds the account-wide Azure key (`repo2-azure-key`), so NO
failure path may echo raw file content — every message is built from OPTION NAMES + parsed non-secret
values, and parser exceptions are re-raised REDACTED (never the offending source line). The parser is
`ConfigParser(inline_comment_prefixes=("#",), interpolation=None, strict=True)` — the VERBATIM runbook conf
(inline comments; the key tee-appended at file end inside [neuro]) is the test fixture of record.

Pin idiom (freshness.py): the cadence constants are COMMITTED REPO CONSTANTS with fail-closed resolvers —
None means "no pin recorded" and resolution raises, never a silent default.
"""

from __future__ import annotations

import datetime as _dt
import re
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from dataclasses import dataclass
from pathlib import Path

from ..db.lanes import ConfigurationError
from .freshness import resolve_backup_stale_after
from .wal_freshness import resolve_archiver_probe_interval

# The base-backup cadence of record (owner-ruled 2026-07-11, GO-D-timer GO-input 2). The systemd timer's
# OnUnitActiveSec must EQUAL this (assertion 5) — the constant is the authority, the unit is the mirror.
BASE_BACKUP_INTERVAL: _dt.timedelta | None = _dt.timedelta(days=2)

# The missed-cycle/slack budget of record (owner-ruled 2026-07-11): the driver's recency bound is
# BASE_BACKUP_INTERVAL + PROVISIONING_MARGIN, and both inequalities above consume it.
PROVISIONING_MARGIN: _dt.timedelta | None = _dt.timedelta(days=2)

# The only option names this checker reads (the redaction whitelist — nothing else is ever materialized
# into a message). repoN- names are matched by _REPO_OPT below.
_GLOBAL_OPTS = ("archive-async", "archive-push-queue-max", "spool-path")
_REPO_OPT = re.compile(r"^repo(\d+)-(.+)$")
_SIZE_RE = re.compile(r"^\d+(?:[KMGTP]i?B?|B)?$", re.IGNORECASE)  # pgbackrest size literal, e.g. 32GiB


def resolve_base_backup_interval() -> _dt.timedelta:
    """The single cadence-resolution point (resolve_backup_stale_after idiom; fail closed on an absent pin)."""
    if BASE_BACKUP_INTERVAL is None:
        raise ConfigurationError(
            "base-backup cadence pin is absent (provisioning_invariants.BASE_BACKUP_INTERVAL is None) — "
            "refusing to evaluate provisioning invariants on an unpinned cadence (fail closed)."
        )
    return BASE_BACKUP_INTERVAL


def resolve_provisioning_margin() -> _dt.timedelta:
    """The single margin-resolution point (fail closed on an absent pin — a margin that silently became 0
    would quietly weaken every inequality it appears in)."""
    if PROVISIONING_MARGIN is None:
        raise ConfigurationError(
            "provisioning margin pin is absent (provisioning_invariants.PROVISIONING_MARGIN is None) — "
            "refusing to evaluate provisioning invariants on an unpinned margin (fail closed)."
        )
    return PROVISIONING_MARGIN


@dataclass(frozen=True)
class ProvisioningReport:
    """The verify-config result: the repos discovered, their retention days, and which timer cadences were
    checked (`timer_checked` = the backup timer, assertion 5; `archiver_timer_checked` = the archiver-probe
    timer, assertion 7 / wal D4). A False means its timer text was not supplied (a pre-install run), never
    that the check passed."""

    repos: tuple[int, ...]
    retention_days: dict[int, int]
    timer_checked: bool
    archiver_timer_checked: bool = False


def _parse_conf(conf_path: Path) -> ConfigParser:
    parser = ConfigParser(inline_comment_prefixes=("#",), interpolation=None, strict=True)
    try:
        text = conf_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            f"pgbackrest config {str(conf_path)!r} is unreadable ({type(exc).__name__}) — fail closed."
        ) from exc
    try:
        parser.read_string(text)
    except ConfigParserError as exc:
        # REDACTED re-raise: ConfigParser errors embed the offending SOURCE LINE verbatim, and this file
        # holds the account-wide Azure key — never echo file content into stderr/journald/ntfy.
        raise ConfigurationError(
            f"pgbackrest config {str(conf_path)!r} failed to parse ({type(exc).__name__} at "
            f"line {getattr(exc, 'lineno', '?')}) — message REDACTED (the file holds a secret); "
            "inspect the file directly on the VM (fail closed)."
        ) from None  # deliberately NOT chained: the original exception carries the raw line
    return parser


def _all_options(parser: ConfigParser) -> dict[str, str]:
    """Flatten section->options. pgbackrest gives STANZA sections precedence over [global] (the documented
    override order), so a cross-section DIVERGENT redefinition is refused outright (vet M1): first-wins or
    last-wins flattening would let this checker certify a value pgbackrest does not enforce — e.g. a
    [neuro] retention override pruning below the floor while the [global] 30 passes. Same-value repeats are
    tolerated (idempotent); the refusal names the OPTION only (redaction — values never shown here)."""
    flat: dict[str, str] = {}
    for section in parser.sections():
        for name, value in parser.items(section):
            v = value.strip()
            if name in flat and flat[name] != v:
                raise ConfigurationError(
                    f"option {name!r} is defined in MULTIPLE sections with DIFFERING values — pgbackrest "
                    "gives stanza sections precedence over [global], and a divergent override is exactly "
                    "the silent-rot this checker exists to catch; consolidate to ONE definition "
                    "(fail closed; values not shown)."
                )
            flat[name] = v
    return flat


def verify_pgbackrest_config(
    conf_path: str | Path,
    *,
    timer_unit_text: str | None = None,
    archiver_timer_unit_text: str | None = None,
    legacy_conf_path: str | Path = "/etc/pgbackrest.conf",
) -> ProvisioningReport:
    """Assert the §3.5 provisioning invariants against the REAL pgbackrest config (+ the installed backup and
    archiver-probe timers when their unit text is supplied). Raises ConfigurationError (fail loud,
    REDACTION-safe) on the first violation; returns a ProvisioningReport on success.

    `timer_unit_text` is the neuro-backup.timer unit content (CLI --timer-file); `archiver_timer_unit_text` is
    the neuro-archiver-probe.timer unit content (CLI --archiver-timer-file, wal D4). When either is None its
    cadence-equality check is SKIPPED — the caller must surface that loudly (provisioning runs pre-install
    without them; the daily verify-config timer always passes both)."""
    bound = resolve_backup_stale_after()  # fail closed before any file read (the pin order idiom)
    interval = resolve_base_backup_interval()
    margin = resolve_provisioning_margin()

    conf_path = Path(conf_path)
    if not conf_path.exists():
        raise ConfigurationError(
            f"pgbackrest config {str(conf_path)!r} does not exist — refusing to certify provisioning "
            "against an absent config (fail closed)."
        )
    legacy = Path(legacy_conf_path)
    if legacy.exists() and legacy.resolve() != conf_path.resolve():
        raise ConfigurationError(
            f"legacy pgbackrest config {str(legacy)!r} coexists with {str(conf_path)!r} — pgbackrest may "
            "silently read the legacy file (the VM-rebuild shadow-config trap); remove/merge it (fail loud)."
        )

    opts = _all_options(_parse_conf(conf_path))

    # (4) the §0.1 valve
    if opts.get("archive-async") != "y":
        raise ConfigurationError(
            "archive-async is not 'y' — the §0.1 async valve is the load-bearing PANIC guard (fail loud)."
        )
    queue_max = opts.get("archive-push-queue-max")
    if not queue_max or not _SIZE_RE.match(queue_max):
        raise ConfigurationError(
            "archive-push-queue-max is missing or not a valid pgbackrest size literal — the §0.1 queue cap "
            "must be present and parseable (fail loud; value not shown — the config holds a secret)."
        )

    # discover repos DYNAMICALLY (never a hardcoded {1,2} — a future repo3 must join min())
    repos: set[int] = set()
    for name in opts:
        m = _REPO_OPT.match(name)
        if m:
            repos.add(int(m.group(1)))
    if not repos:
        raise ConfigurationError("no repoN- options found — an un-repo'd pgbackrest config (fail loud).")

    # (1) + (2): per-repo retention, TYPE-asserted before the number is read as days
    retention_days: dict[int, int] = {}
    for repo in sorted(repos):
        rtype = opts.get(f"repo{repo}-retention-full-type")
        if rtype != "time":
            raise ConfigurationError(
                f"repo{repo}-retention-full-type is not 'time' — pgbackrest DEFAULTS to 'count', so "
                f"repo{repo}'s retention number would silently mean BACKUPS not DAYS (fail closed; value not "
                "shown — the config holds a secret; assert the type line exists)."
            )
        raw = opts.get(f"repo{repo}-retention-full")
        if raw is None or not raw.isdigit():
            raise ConfigurationError(
                f"repo{repo}-retention-full is missing or non-numeric — fail loud (value not shown; the "
                "config holds a secret)."
            )
        retention_days[repo] = int(raw)
    floor_days = (bound + interval + margin).days
    worst = min(retention_days, key=retention_days.get)  # type: ignore[arg-type]
    if retention_days[worst] < floor_days:
        raise ConfigurationError(
            f"repo{worst}-retention-full={retention_days[worst]}d < the pinned floor {floor_days}d "
            f"(BACKUP_STALE_AFTER {bound.days}d + BASE_BACKUP_INTERVAL {interval.days}d + margin "
            f"{margin.days}d) — a backup the gate still calls fresh could be pruned (fail loud)."
        )

    # (3) the gate-headroom inequality (the cadence the 8-day bound can actually supervise)
    if bound < interval + margin:
        raise ConfigurationError(
            f"BACKUP_STALE_AFTER {bound.days}d < BASE_BACKUP_INTERVAL {interval.days}d + margin "
            f"{margin.days}d — the staleness gate could block on a cadence that is merely on schedule "
            "(fail loud; retune the pins together)."
        )

    # (5) the installed BACKUP timer's cadence == BASE_BACKUP_INTERVAL (when supplied)
    timer_checked = False
    if timer_unit_text is not None:
        _assert_installed_cadence(
            timer_unit_text, expected=interval, pin_name="BASE_BACKUP_INTERVAL", timer_label="backup"
        )
        timer_checked = True

    # (7) the installed ARCHIVER-PROBE timer's cadence == ARCHIVER_PROBE_INTERVAL (wal D4; when supplied). The
    # pin is resolved fail-closed HERE, the assertion-5 analog — an unpinned interval refuses to certify.
    archiver_timer_checked = False
    if archiver_timer_unit_text is not None:
        _assert_installed_cadence(
            archiver_timer_unit_text,
            expected=resolve_archiver_probe_interval(),
            pin_name="ARCHIVER_PROBE_INTERVAL",
            timer_label="archiver-probe",
        )
        archiver_timer_checked = True

    return ProvisioningReport(
        repos=tuple(sorted(repos)),
        retention_days=retention_days,
        timer_checked=timer_checked,
        archiver_timer_checked=archiver_timer_checked,
    )


def _compact_span(td: _dt.timedelta) -> str:
    """Render a cadence timedelta in the compact systemd form the operator edits (e.g. '2d', '15min', '1w'),
    not the verbose default str ('2 days, 0:00:00'). Whole-unit cadences only — the pins always are."""
    secs = int(td.total_seconds())
    for unit, n in (("w", 604800), ("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= n and secs % n == 0:
            return f"{secs // n}{unit}"
    return f"{secs}s"


def _assert_installed_cadence(
    timer_unit_text: str, *, expected: _dt.timedelta, pin_name: str, timer_label: str
) -> None:
    """Assert an installed systemd timer's OnUnitActiveSec == the pinned cadence, or raise (fail loud). Shared
    by assertion 5 (backup) and assertion 7 (archiver-probe). systemd honors the LAST assignment of a repeated
    directive, so this reads the LAST OnUnitActiveSec line, not the first (GO-D-timer hardening #3 last-match);
    the span parse is case-sensitive (hardening #3 case). On drift the message NAMES the authoritative constant
    (`pin_name`) and echoes both the installed value and the pin in compact form — a timer unit holds no secret
    and the operator needs to see what drifted AND which constant is the authority (unlike the conf, §8 fold 4
    redaction)."""
    matches = re.findall(r"^\s*OnUnitActiveSec\s*=\s*(\S+)\s*$", timer_unit_text, re.MULTILINE)
    if not matches:
        raise ConfigurationError(
            f"the {timer_label} timer unit has no OnUnitActiveSec= line — the runbook pins the cadence with a "
            "monotonic timer so it is machine-comparable to the pinned interval (fail loud)."
        )
    value = matches[-1]  # systemd last-assignment-wins for a repeated directive
    if _parse_systemd_span(value) != expected:
        raise ConfigurationError(
            f"the installed {timer_label} timer's OnUnitActiveSec={value} != the pinned {pin_name} "
            f"({_compact_span(expected)}) — the constant is the authority; a cadence change is one commit + "
            "the unit edit together (fail loud)."
        )


_SPAN_UNIT = {
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
}


def _parse_systemd_span(value: str) -> _dt.timedelta:
    """Parse the simple systemd time-span forms the runbook uses (e.g. '2d', '15min', '1h 30m'). An
    unrecognized form fails CLOSED — better a loud parse refusal than a mis-read cadence equality.

    CASE-SENSITIVE (GO-D-timer hardening #3): systemd's units are case-sensitive — `M` is MONTHS while `m` is
    MINUTES — so this matches `unit` verbatim against the (lowercase) whitelist rather than lowercasing it. A
    lowercasing parse would silently read `1M` (systemd = one month) as one minute; here any form outside the
    whitelist (including an uppercased one systemd would itself reject) fails closed."""
    total = 0.0
    matched = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)", value):
        factor = _SPAN_UNIT.get(unit)
        if factor is None:
            raise ConfigurationError(
                f"unrecognized systemd time-span unit {unit!r} in {value!r} (fail closed)."
            )
        total += float(num) * factor
        matched = True
    if not matched:
        raise ConfigurationError(f"unparseable systemd time-span {value!r} (fail closed).")
    return _dt.timedelta(seconds=total)
