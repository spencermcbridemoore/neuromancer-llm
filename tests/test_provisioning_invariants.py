"""GO-D-timer (A2-16): the ruling-§3.5 provisioning-invariant assertion.

RED@7d680fb = ModuleNotFoundError (governance/provisioning_invariants.py absent). The fixture of record is
the VERBATIM A2-7 runbook conf (§3.1 + the §4.3 repo2 block + the tee-appended azure key at FILE END inside
[neuro]) — §8 fold 4: a comment-free fixture would go green while the real file failed to parse (inline
comments), and the REDACTION contract must be pinned against a secret-bearing line.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from neuromancer_llm.db.lanes import ConfigurationError
from neuromancer_llm.governance.provisioning_invariants import (
    _parse_systemd_span,
    resolve_base_backup_interval,
    resolve_provisioning_margin,
    verify_pgbackrest_config,
)

_SECRET = "DUMMYSECRETacc0untKeyBytes=="

# The VERBATIM runbook conf (a2-7 §3.1 Phase-1 block + §4.3 Phase-2 repo2 lines in [global] + the key
# tee-appended at the very end, inside [neuro] — exactly how the real file is built).
VERBATIM_CONF = f"""[global]
# --- async archiving: the §0.1 safety valve (installed BEFORE repo2 exists) ---
archive-async=y
spool-path=/pgdata/pgbackrest-spool
archive-push-queue-max=32GiB           # HARD cap: past this, pgbackrest DROPS WAL + reports success so PG never
                                       # PANICs (a PITR gap, not an outage). Confirm 32GiB << `df` free on /pgdata.
process-max=1                          # m3.small ≈ 2 vCPU; keep 1 core free for PG's own backup I/O (raise later)

# --- repo1: Cinder-local ---
repo1-path=/pgdata/pgbackrest
repo1-retention-full-type=time
repo1-retention-full=30                # ~4x the 8-day ADR-0020 bound + headroom above A2-16's full interval
repo1-bundle=y                         # bundle small files
compress-type=zst
compress-level=6

log-level-console=info
log-level-file=detail
log-path=/var/log/pgbackrest
start-fast=y

repo2-type=azure
repo2-path=/neuro
repo2-azure-account=neuromancerllm
repo2-azure-container=db-backups
repo2-azure-key-type=shared
repo2-retention-full-type=time
repo2-retention-full=30
repo2-bundle=y
# repo2-azure-key seated separately below (silent read — never on a command line)
# repo2-azure-endpoint defaults to blob.core.windows.net (Azure public cloud / eastus2) — correct, no override

[neuro]
pg1-path=/pgdata/18/main
pg1-port=5432
pg1-user=postgres                      # Path B (§1.3) — process runs as OS postgres, local peer auth
repo2-azure-key={_SECRET}
"""

TIMER_OK = (
    "[Unit]\nDescription=neuro base backup\n[Timer]\nOnBootSec=15min\nOnUnitActiveSec=2d\nPersistent=true\n"
)
# the installed neuro-archiver-probe.timer (wal D4): OnUnitActiveSec must == ARCHIVER_PROBE_INTERVAL (15min).
TIMER_ARCHIVER_OK = (
    "[Unit]\nDescription=neuro archiver probe\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec=15min\n"
)


@pytest.fixture
def conf(tmp_path):
    p = tmp_path / "pgbackrest.conf"
    p.write_text(VERBATIM_CONF, encoding="utf-8")
    return p


def _verify(conf_path, tmp_path, **kw):
    # point the legacy path INSIDE tmp (absent by default) so the real /etc is never consulted in tests
    kw.setdefault("legacy_conf_path", tmp_path / "legacy-pgbackrest.conf")
    return verify_pgbackrest_config(conf_path, **kw)


# ---- the verbatim conf parses + passes ----------------------------------------------------------------


def test_verbatim_conf_passes(conf, tmp_path):
    report = _verify(conf, tmp_path)
    assert report.repos == (1, 2)
    assert report.retention_days == {1: 30, 2: 30}
    assert report.timer_checked is False  # no timer text supplied (the pre-install run)


def test_timer_equality_checked_and_passes(conf, tmp_path):
    assert _verify(conf, tmp_path, timer_unit_text=TIMER_OK).timer_checked is True


# ---- each violated invariant fails LOUD ---------------------------------------------------------------


def test_missing_retention_type_fails_closed(conf, tmp_path):
    # pgbackrest DEFAULTS to 'count': losing the type line silently converts 30 days -> 30 backups.
    conf.write_text(VERBATIM_CONF.replace("repo2-retention-full-type=time\n", ""), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="repo2-retention-full-type"):
        _verify(conf, tmp_path)


def test_retention_below_floor_fails(conf, tmp_path):
    conf.write_text(
        VERBATIM_CONF.replace("repo1-retention-full=30 ", "repo1-retention-full=5  "), encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="floor"):
        _verify(conf, tmp_path)


def test_missing_async_valve_fails(conf, tmp_path):
    conf.write_text(VERBATIM_CONF.replace("archive-async=y\n", ""), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="archive-async"):
        _verify(conf, tmp_path)


def test_bad_queue_max_fails(conf, tmp_path):
    conf.write_text(VERBATIM_CONF.replace("32GiB", "lots-and-lots"), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="archive-push-queue-max"):
        _verify(conf, tmp_path)


def test_missing_conf_fails(tmp_path):
    with pytest.raises(ConfigurationError, match="does not exist"):
        _verify(tmp_path / "nope.conf", tmp_path)


def test_legacy_shadow_conf_fails(conf, tmp_path):
    (tmp_path / "legacy-pgbackrest.conf").write_text("[global]\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="legacy"):
        _verify(conf, tmp_path)


def test_timer_drift_fails(conf, tmp_path):
    with pytest.raises(ConfigurationError, match="OnUnitActiveSec") as ei:
        _verify(conf, tmp_path, timer_unit_text=TIMER_OK.replace("2d", "3d"))
    # the drift message NAMES the authoritative constant + renders the pin compactly (not a verbose timedelta)
    assert "BASE_BACKUP_INTERVAL" in str(ei.value) and "2d" in str(ei.value)


def test_timer_without_cadence_line_fails(conf, tmp_path):
    with pytest.raises(ConfigurationError, match="OnUnitActiveSec"):
        _verify(conf, tmp_path, timer_unit_text="[Timer]\nOnCalendar=daily\n")


def test_divergent_cross_section_override_fails_closed(conf, tmp_path):
    # vet M1: pgbackrest gives STANZA sections precedence over [global] — a [neuro] retention override is
    # what pgbackrest would ENFORCE (pruning at 5d) while a first-wins flattening certified the global 30.
    # The checker refuses divergent redefinitions outright, naming the option only (redaction).
    conf.write_text(VERBATIM_CONF + "\nrepo1-retention-full=5\n", encoding="utf-8")  # appends inside [neuro]
    with pytest.raises(ConfigurationError, match="MULTIPLE sections"):
        _verify(conf, tmp_path)


def test_same_value_cross_section_repeat_is_tolerated(conf, tmp_path):
    conf.write_text(VERBATIM_CONF + "\nrepo1-retention-full=30\n", encoding="utf-8")  # idempotent repeat
    assert _verify(conf, tmp_path).retention_days[1] == 30


def test_gate_headroom_inequality(conf, tmp_path, monkeypatch):
    # interval+margin must fit INSIDE the 8-day bound (the corrected §8 fold-6 inequality): 7d+2d > 8d.
    monkeypatch.setattr(
        "neuromancer_llm.governance.provisioning_invariants.BASE_BACKUP_INTERVAL", _dt.timedelta(days=7)
    )
    with pytest.raises(ConfigurationError, match="retune the pins"):
        _verify(conf, tmp_path)


# ---- REDACTION: a parse failure on a secret-bearing line must never echo the secret -------------------


def test_parse_failure_is_redacted(conf, tmp_path):
    # a secret-bearing line BEFORE any section header is a genuine MissingSectionHeaderError whose
    # exception message embeds the offending line VERBATIM — the unredacted path would leak the key.
    # (A line CONTAINING '=' anywhere parses as key=value, so in-section corruption can't be used here.)
    conf.write_text(f"{_SECRET}\n" + VERBATIM_CONF, encoding="utf-8")
    with pytest.raises(ConfigurationError) as ei:
        _verify(conf, tmp_path)
    assert _SECRET not in str(ei.value)  # the fold-4 contract: no raw file content in any failure path
    assert "REDACTED" in str(ei.value)


def test_duplicate_option_is_redacted(conf, tmp_path):
    conf.write_text(VERBATIM_CONF + f"\n[extra]\nx=1\nx={_SECRET}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as ei:
        _verify(conf, tmp_path)
    assert _SECRET not in str(ei.value)


# ---- the pins (fail closed, resolved BEFORE any file read) ---------------------------------------------


def test_pins_resolve():
    assert resolve_base_backup_interval() == _dt.timedelta(days=2)
    assert resolve_provisioning_margin() == _dt.timedelta(days=2)


@pytest.mark.parametrize("name", ["BASE_BACKUP_INTERVAL", "PROVISIONING_MARGIN"])
def test_absent_pin_fails_closed_before_file_read(tmp_path, monkeypatch, name):
    monkeypatch.setattr(f"neuromancer_llm.governance.provisioning_invariants.{name}", None)
    # the conf path does not even exist: the PIN error (not the missing-file error) proves pin-first order
    with pytest.raises(ConfigurationError, match="pin is absent"):
        _verify(tmp_path / "nope.conf", tmp_path)


# ---- the systemd span parser (fail closed on anything unrecognized) ------------------------------------


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        ("2d", _dt.timedelta(days=2)),
        ("15min", _dt.timedelta(minutes=15)),
        ("1h 30m", _dt.timedelta(minutes=90)),
    ],
)
def test_span_parses(span, expected):
    assert _parse_systemd_span(span) == expected


@pytest.mark.parametrize("bad", ["fortnight", "2 fortnights", "", "soon"])
def test_span_fails_closed(bad):
    with pytest.raises(ConfigurationError):
        _parse_systemd_span(bad)


# ---- GO-D-timer hardening #3: case-sensitive span parse (systemd `M`=months, `m`=minutes) ---------------


@pytest.mark.parametrize("cased", ["15MIN", "2D", "1H", "1M"])
def test_span_is_case_sensitive_fails_closed(cased):
    # systemd units are case-sensitive; the parser matches verbatim rather than lowercasing, so an uppercased
    # form (which systemd itself rejects) fails closed. In particular `1M` (systemd = one MONTH) can no longer
    # be silently lowercased to `1m` and mis-read as one minute — the mis-parse this hardening kills.
    with pytest.raises(ConfigurationError):
        _parse_systemd_span(cased)


# ---- assertion 7 (wal D4): the archiver-probe timer cadence == ARCHIVER_PROBE_INTERVAL -----------------


def test_archiver_timer_checked_and_passes(conf, tmp_path):
    report = _verify(conf, tmp_path, archiver_timer_unit_text=TIMER_ARCHIVER_OK)
    assert report.archiver_timer_checked is True
    assert report.timer_checked is False  # the backup timer was not supplied in this call


def test_archiver_timer_drift_fails(conf, tmp_path):
    with pytest.raises(ConfigurationError, match="archiver-probe") as ei:
        _verify(conf, tmp_path, archiver_timer_unit_text=TIMER_ARCHIVER_OK.replace("15min", "30min"))
    assert "ARCHIVER_PROBE_INTERVAL" in str(ei.value) and "15min" in str(ei.value)


def test_archiver_timer_without_cadence_line_fails(conf, tmp_path):
    with pytest.raises(ConfigurationError, match="archiver-probe"):
        _verify(conf, tmp_path, archiver_timer_unit_text="[Timer]\nOnCalendar=daily\n")


def test_archiver_pin_absent_fails_closed(conf, tmp_path, monkeypatch):
    # the machine-check resolves ARCHIVER_PROBE_INTERVAL fail-closed (the assertion-5 analog): an unpinned
    # cadence refuses to certify the installed timer rather than comparing against None.
    monkeypatch.setattr("neuromancer_llm.governance.wal_freshness.ARCHIVER_PROBE_INTERVAL", None)
    with pytest.raises(ConfigurationError, match="pin is absent"):
        _verify(conf, tmp_path, archiver_timer_unit_text=TIMER_ARCHIVER_OK)


def test_both_timers_checked_together(conf, tmp_path):
    report = _verify(conf, tmp_path, timer_unit_text=TIMER_OK, archiver_timer_unit_text=TIMER_ARCHIVER_OK)
    assert report.timer_checked is True and report.archiver_timer_checked is True


# ---- GO-D-timer hardening #3: systemd honors the LAST OnUnitActiveSec (last-assignment-wins) -----------


def test_last_onunitactivesec_wins(conf, tmp_path):
    # systemd runs the LAST assignment of a repeated directive; a first-match reader would (wrongly) read 3d.
    text = "[Timer]\nOnUnitActiveSec=3d\nOnUnitActiveSec=2d\n"  # systemd runs 2d (== BASE) -> passes
    assert _verify(conf, tmp_path, timer_unit_text=text).timer_checked is True


def test_last_onunitactivesec_wrong_fails(conf, tmp_path):
    text = "[Timer]\nOnUnitActiveSec=2d\nOnUnitActiveSec=3d\n"  # systemd runs 3d (!= BASE) -> fails
    with pytest.raises(ConfigurationError, match="OnUnitActiveSec"):
        _verify(conf, tmp_path, timer_unit_text=text)


# ---- GO-D-timer hardening #4: a failed shape-check on a conf value never echoes the raw value ----------


def test_queue_max_shape_failure_does_not_echo_value(conf, tmp_path):
    # the config holds the account-wide Azure key; a shape-failed value is untrusted, so the message names the
    # OPTION only and never echoes the raw value (reverting the redaction re-adds the value -> this reddens).
    sentinel = "SHAPEFAILSENTINEL_notasize"
    conf.write_text(VERBATIM_CONF.replace("32GiB", sentinel), encoding="utf-8")
    with pytest.raises(ConfigurationError) as ei:
        _verify(conf, tmp_path)
    msg = str(ei.value)
    assert "archive-push-queue-max" in msg  # the option IS named
    assert sentinel not in msg  # the raw shape-failed value is NOT echoed (#4)


def test_retention_shape_failure_does_not_echo_value(conf, tmp_path):
    sentinel = "SHAPEFAILRETENTION"
    conf.write_text(
        VERBATIM_CONF.replace("repo1-retention-full=30 ", f"repo1-retention-full={sentinel}  "),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as ei:
        _verify(conf, tmp_path)
    msg = str(ei.value)
    assert "repo1-retention-full" in msg and sentinel not in msg
