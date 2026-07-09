"""ADR-D8 / B5: main._finalize_run must gate the heartbeat on FAILED only."""

from __future__ import annotations

from unittest.mock import MagicMock

import main
from placement_mail_tracker.reliability.status import RunReport


def _finalize(report: RunReport) -> MagicMock:
    heartbeat_manager = MagicMock()
    settings = MagicMock()
    settings.notification_email = ""
    settings.email_receiver = ""
    settings.failure_alert_threshold = 3
    health_manager = MagicMock()
    health_manager.update_after_run.return_value = {"consecutive_failures": 0}
    main._finalize_run(report, settings, health_manager, heartbeat_manager)
    return heartbeat_manager


def test_success_updates_heartbeat() -> None:
    report = RunReport(environment="testing")
    heartbeat_manager = _finalize(report)
    heartbeat_manager.update_success.assert_called_once_with(report)


def test_partial_success_updates_heartbeat() -> None:
    report = RunReport(environment="testing")
    report.mark_component("gmail", False, "transient", critical=False)
    heartbeat_manager = _finalize(report)
    heartbeat_manager.update_success.assert_called_once_with(report)


def test_failed_does_not_update_heartbeat() -> None:
    report = RunReport(environment="production")
    report.mark_component("database", False, "database unavailable", critical=True)
    heartbeat_manager = _finalize(report)
    heartbeat_manager.update_success.assert_not_called()
