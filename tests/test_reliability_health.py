"""Tests for system health tracking and failure alerts."""

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.reliability.health import FailureAlertManager, SystemHealthManager
from placement_mail_tracker.reliability.status import RunReport


def test_failure_alert_sent_once_per_failure_streak(tmp_path) -> None:
    sent: list[tuple[str, str]] = []
    health = SystemHealthManager(tmp_path / "system_health.json")
    settings = Settings(
        APP_ENV="testing",
        FAILURE_ALERT_THRESHOLD=2,
        NOTIFICATION_EMAIL="alerts@example.com",
    )
    manager = FailureAlertManager(
        settings,
        health,
        sender=lambda subject, body: sent.append((subject, body)) is None or True,
    )

    report = RunReport(environment="testing")
    report.mark_component("gmail", False, "auth failed", critical=False)

    manager.handle_report(report)
    manager.handle_report(report)
    manager.handle_report(report)

    state = health.read()
    assert state["consecutive_failures"] == 3
    assert state["alert_sent_for_current_streak"] is True
    assert len(sent) == 1
    assert "Gmail OK: False" in sent[0][1]


def test_system_health_resets_after_success(tmp_path) -> None:
    health = SystemHealthManager(tmp_path / "system_health.json")
    settings = Settings(APP_ENV="testing", FAILURE_ALERT_THRESHOLD=1)
    manager = FailureAlertManager(settings, health, sender=lambda _s, _b: True)

    failed = RunReport(environment="testing")
    failed.mark_component("sheets", False, "auth failed", critical=False)
    manager.handle_report(failed)

    success = RunReport(environment="testing")
    manager.handle_report(success)

    state = health.read()
    assert state["consecutive_failures"] == 0
    assert state["alert_sent_for_current_streak"] is False
    assert state["last_success"] is not None
