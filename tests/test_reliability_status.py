"""Tests for structured run status reporting."""

from placement_mail_tracker.reliability.status import RunReport, RunStatus


def test_success_when_all_components_ok() -> None:
    report = RunReport(environment="testing")

    assert report.status == RunStatus.SUCCESS
    assert report.exit_code == 0


def test_partial_success_when_noncritical_component_fails() -> None:
    report = RunReport(environment="testing")
    report.mark_component("gmail", False, "temporary API issue", critical=False)

    assert report.status == RunStatus.PARTIAL_SUCCESS
    assert report.gmail_ok is False
    assert report.exit_code == 2


def test_failed_when_critical_component_fails() -> None:
    report = RunReport(environment="production")
    report.mark_component("database", False, "database unavailable", critical=True)

    assert report.status == RunStatus.FAILED
    assert report.database_ok is False
    assert report.exit_code == 1


def test_calendar_ok_defaults_true_and_never_critical() -> None:
    """ADR-D8 / A5: calendar_ok exists so a failed calendar step is visible
    in the summary/alert, never critical even in production (calendar is
    enrichment; mail ingestion and the sheet must survive its death)."""
    report = RunReport(environment="production")
    report.mark_component("calendar", False, "OAuth dead — re-consent needed", critical=False)

    assert report.calendar_ok is False
    assert report.status == RunStatus.PARTIAL_SUCCESS
    assert report.exit_code == 2
    assert any("Calendar OK: False" in line for line in report.summary_lines())
