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
