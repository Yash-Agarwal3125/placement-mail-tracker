"""Run status models for Placement Mail Tracker."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from placement_mail_tracker.utils.time import utc_now_iso


class RunStatus(StrEnum):
    """Final status for one scheduled sync run."""

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


@dataclass(slots=True)
class SyncMetrics:
    """Small set of counters produced by one sync cycle."""

    processed_messages: int = 0
    skipped_messages: int = 0
    error_messages: int = 0
    drives_created: int = 0
    drives_updated: int = 0
    gemini_calls: int = 0
    rule_only: int = 0


@dataclass(slots=True)
class RunReport:
    """Structured status report for a single Task Scheduler execution."""

    environment: str
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    database_ok: bool = True
    gmail_ok: bool = True
    sheets_ok: bool = True
    notifications_ok: bool = True
    critical_failure: bool = False
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    metrics: SyncMetrics = field(default_factory=SyncMetrics)

    def mark_component(
        self,
        component: str,
        ok: bool,
        message: str,
        *,
        critical: bool = False,
    ) -> None:
        """Record one component status update."""
        attr = f"{component}_ok"
        if hasattr(self, attr):
            setattr(self, attr, ok)

        if ok:
            return

        if critical:
            self.critical_failure = True
            self.failures.append(f"{component}: {message}")
        else:
            self.warnings.append(f"{component}: {message}")

    def add_warning(self, message: str) -> None:
        """Attach a non-critical warning."""
        self.warnings.append(message)

    def add_failure(self, message: str, *, critical: bool = True) -> None:
        """Attach a failure not tied to one of the tracked components."""
        if critical:
            self.critical_failure = True
            self.failures.append(message)
        else:
            self.warnings.append(message)

    @property
    def status(self) -> RunStatus:
        """Calculate the final run status from component state."""
        if self.critical_failure:
            return RunStatus.FAILED

        if not all(
            [
                self.database_ok,
                self.gmail_ok,
                self.sheets_ok,
                self.notifications_ok,
            ]
        ):
            return RunStatus.PARTIAL_SUCCESS

        if self.warnings:
            return RunStatus.PARTIAL_SUCCESS

        return RunStatus.SUCCESS

    @property
    def exit_code(self) -> int:
        """Return a process exit code suitable for Task Scheduler history."""
        if self.status == RunStatus.SUCCESS:
            return 0
        if self.status == RunStatus.PARTIAL_SUCCESS:
            return 2
        return 1

    def finish(self) -> None:
        """Stamp the report with a finish time."""
        self.finished_at = utc_now_iso()

    def is_failure_like(self) -> bool:
        """Return True when this run should count toward failure streaks."""
        return self.status != RunStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a dictionary."""
        data = asdict(self)
        data["status"] = self.status.value
        data["exit_code"] = self.exit_code
        return data

    def to_json(self) -> str:
        """Serialize report to deterministic JSON for structured logs."""
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True)

    def summary_lines(self) -> list[str]:
        """Return human-readable final status lines."""
        lines = [
            f"Final status: {self.status.value}",
            f"Database OK: {self.database_ok}",
            f"Gmail OK: {self.gmail_ok}",
            f"Sheets OK: {self.sheets_ok}",
            f"Notifications OK: {self.notifications_ok}",
        ]

        if self.failures:
            lines.append("Failures:")
            lines.extend(f"  - {failure}" for failure in self.failures)

        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"  - {warning}" for warning in self.warnings)

        return lines
