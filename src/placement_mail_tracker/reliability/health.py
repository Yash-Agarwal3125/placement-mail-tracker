"""System health state and failure alert handling."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.notifications.email_notifier import EmailNotifier
from placement_mail_tracker.reliability.status import RunReport, RunStatus
from placement_mail_tracker.utils.time import utc_now_iso

logger = logging.getLogger(__name__)


class AlertSender(Protocol):
    """Protocol for a small alert-sending callable."""

    def __call__(self, subject: str, body: str) -> bool:
        """Send one alert."""


class SystemHealthManager:
    """Persist failure streak state in data/system_health.json."""

    def __init__(self, health_path: str | Path = "data/system_health.json") -> None:
        self.health_path = Path(health_path)
        self.health_path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any]:
        """Read health state, returning defaults when absent or invalid."""
        default = {
            "last_success": None,
            "last_failure": None,
            "last_status": None,
            "consecutive_failures": 0,
            "alert_sent_for_current_streak": False,
        }

        if not self.health_path.exists():
            return default

        try:
            loaded = json.loads(self.health_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            logger.warning("Could not read system health file %s: %s", self.health_path, error)
            return default

        return {**default, **loaded}

    def write(self, state: dict[str, Any]) -> None:
        """Persist health state atomically."""
        tmp_path = self.health_path.with_suffix(f"{self.health_path.suffix}.tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.health_path)

    def update_after_run(self, report: RunReport) -> dict[str, Any]:
        """Update state after a run and return the new state."""
        state = self.read()
        state["last_status"] = report.status.value

        if report.status == RunStatus.SUCCESS:
            state["last_success"] = utc_now_iso()
            state["consecutive_failures"] = 0
            state["alert_sent_for_current_streak"] = False
        else:
            state["last_failure"] = utc_now_iso()
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1

        self.write(state)
        return state

    def mark_alert_sent(self) -> None:
        """Mark the current failure streak as already alerted."""
        state = self.read()
        state["alert_sent_for_current_streak"] = True
        self.write(state)


class FailureAlertManager:
    """Send one alert after a configured number of consecutive failures."""

    def __init__(
        self,
        settings: Settings,
        health_manager: SystemHealthManager,
        *,
        sender: AlertSender | None = None,
    ) -> None:
        self.settings = settings
        self.health_manager = health_manager
        self.sender = sender or self._send_email_alert

    def handle_report(self, report: RunReport) -> dict[str, Any]:
        """Update health state and send an alert when the threshold is crossed."""
        state = self.health_manager.update_after_run(report)

        if report.status == RunStatus.SUCCESS:
            return state

        threshold = max(1, self.settings.failure_alert_threshold)
        failures = int(state.get("consecutive_failures", 0))
        already_sent = bool(state.get("alert_sent_for_current_streak", False))

        if failures < threshold or already_sent:
            return state

        subject = f"Placement Mail Tracker failure streak: {failures}"
        body = self._build_alert_body(report, failures)

        if self.sender(subject, body):
            self.health_manager.mark_alert_sent()
            state = self.health_manager.read()
        return state

    def _send_email_alert(self, subject: str, body: str) -> bool:
        receiver = self.settings.notification_email.strip() or self.settings.email_receiver.strip()
        if not receiver:
            logger.warning(
                "NOTIFICATION_EMAIL or EMAIL_RECEIVER is missing; failure alert not sent"
            )
            return False
        notifier = EmailNotifier(self.settings)
        return notifier.send_email(subject, body)

    def _build_alert_body(self, report: RunReport, failures: int) -> str:
        lines = [
            "Placement Mail Tracker failure alert",
            "",
            f"Consecutive failures: {failures}",
            f"Status: {report.status.value}",
            f"Started at: {report.started_at}",
            f"Finished at: {report.finished_at or 'unknown'}",
            "",
            "Component status:",
            f"- Database OK: {report.database_ok}",
            f"- Gmail OK: {report.gmail_ok}",
            f"- Sheets OK: {report.sheets_ok}",
            f"- Notifications OK: {report.notifications_ok}",
            f"- Calendar OK: {report.calendar_ok}",
        ]

        if report.failures:
            lines.append("")
            lines.append("Failures:")
            lines.extend(f"- {failure}" for failure in report.failures)

        if report.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in report.warnings)

        return "\n".join(lines)
