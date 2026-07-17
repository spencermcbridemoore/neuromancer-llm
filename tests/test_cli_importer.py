"""CLI probes for `neuro importer` (cli/importer.py) — the thin-delegate surface.

BEATS the C2/C3/C4 no-CliRunner convention (those thin delegates shipped with the CLI probed only at the
Repository layer, disclosed as a residual). The importer CLI carries ONE decision the Repository-layer probes
cannot cover: the D1/D2 stamp is REQUIRED and NON-DEFAULTED and fails CLOSED at the CLI, so a defaulted-clean or
out-of-vocab flag is refused BEFORE any DB is touched — the D1 fail-open the non-defaulted ingress signature
exists to close, re-asserted at the CLI boundary. The two remaining verbs are fail-loud stubs.

NO DATABASE NEEDED: every assertion here resolves before `make_verified_engine` is reached (the stamp checks
raise first; typer rejects a missing required option before the body runs; the stubs never open a DB). typer
0.26 dropped click — its CliRunner Result exposes `.exit_code` and `.output` (used here).
"""

from __future__ import annotations

from typer.testing import CliRunner

from neuromancer_llm.cli.importer import app

_runner = CliRunner()


def test_promote_requires_the_d1_confidentiality_stamp_non_defaulted():
    """--confidentiality has NO default: omitting it is a usage error (exit 2), never a silent clean grade."""
    res = _runner.invoke(app, ["promote", "--corpus-root", "/tmp", "--source-system", "local-fs"])
    assert res.exit_code == 2, res.output
    assert "Missing" in res.output and "--confidentiality" in res.output


def test_promote_requires_the_d2_source_system_stamp_non_defaulted():
    """--source-system has NO default: omitting it is a usage error (exit 2)."""
    res = _runner.invoke(app, ["promote", "--corpus-root", "/tmp", "--confidentiality", "open"])
    assert res.exit_code == 2, res.output
    assert "Missing" in res.output and "--source-system" in res.output


def test_promote_requires_corpus_root():
    """--corpus-root is REQUIRED — never a hardcoded machine path (the local-lake base_uri wedge)."""
    res = _runner.invoke(app, ["promote", "--source-system", "local-fs", "--confidentiality", "open"])
    assert res.exit_code == 2, res.output
    assert "Missing" in res.output and "--corpus-root" in res.output


def test_promote_rejects_out_of_vocab_confidentiality_fail_closed():
    """A confidentiality grade outside the closed D1 vocabulary is refused (exit 1) BEFORE any DB is opened,
    and the refusal names the closed vocabulary (fail closed; D1, no default-clean grade)."""
    res = _runner.invoke(
        app,
        ["promote", "--corpus-root", "/tmp", "--source-system", "local-fs", "--confidentiality", "bogus"],
    )
    assert res.exit_code == 1, res.output
    assert "fail closed; D1" in res.output
    assert "exam_restricted" in res.output


def test_promote_rejects_out_of_vocab_source_system_fail_closed():
    """A source_system outside the closed D2 vocabulary is refused (exit 1) BEFORE any DB is opened."""
    res = _runner.invoke(
        app,
        ["promote", "--corpus-root", "/tmp", "--source-system", "bogus", "--confidentiality", "open"],
    )
    assert res.exit_code == 1, res.output
    assert "fail closed; D2" in res.output
    assert "local-fs" in res.output


def test_scan_is_a_fail_loud_stub_reserved_for_a_layer1_only_pass():
    """scan is reserved for a future Layer-1-only mirror pass: it must FAIL LOUD (exit 2), never silently do
    nothing and never write. The surprise is an error, never an unintended write (fail-safe naming). It points
    the operator to the built verb."""
    res = _runner.invoke(app, ["scan"])
    assert res.exit_code == 2, res.output
    assert "not yet built" in res.output
    assert "promote" in res.output


def test_status_is_a_fail_loud_stub():
    """status (a persisted coverage report) is deferred: it must FAIL LOUD (exit 2) and point to promote, which
    reports coverage + refusals from the live result."""
    res = _runner.invoke(app, ["status"])
    assert res.exit_code == 2, res.output
    assert "not yet built" in res.output
    assert "promote" in res.output
