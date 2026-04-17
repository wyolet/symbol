"""Tests for the findings/report model."""

from ca.symbol.shared.findings import Report, Severity


def test_empty_report():
    report = Report()
    assert report.errors == 0
    assert report.warnings == 0
    assert report.infos == 0
    assert not report.has_issues
    assert report.exit_code == 0


def test_error_findings():
    report = Report()
    report.add("orphans", "unused.py", "dead code", Severity.ERROR)
    report.add("orphans", "old.py", "dead code", Severity.ERROR)
    assert report.errors == 2
    assert report.has_issues
    assert report.exit_code == 1


def test_warning_findings():
    report = Report()
    report.add("side_effects", "load_dotenv()", severity=Severity.WARNING)
    assert report.warnings == 1
    assert not report.has_issues
    assert report.exit_code == 0


def test_mixed_severities():
    report = Report()
    report.add("orphans", "dead.py", severity=Severity.ERROR)
    report.add("side_effects", "setup()", severity=Severity.WARNING)
    report.add("config", "Dockerfile", severity=Severity.INFO)
    assert report.errors == 1
    assert report.warnings == 1
    assert report.infos == 1
    assert report.has_issues
    assert report.exit_code == 1


def test_severity_ordering():
    assert Severity.INFO < Severity.WARNING
    assert Severity.WARNING < Severity.ERROR
    assert not Severity.ERROR < Severity.WARNING
