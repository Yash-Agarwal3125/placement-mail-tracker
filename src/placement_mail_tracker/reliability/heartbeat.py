"""Heartbeat file management and inactivity detection."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from placement_mail_tracker.reliability.status import RunReport
from placement_mail_tracker.utils.time import utc_now_iso

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InactivityWarning:
    """Warning produced when the tracker has not succeeded recently."""

    inactive_hours: float
    message: str


class HeartbeatManager:
    """Read and write the lightweight heartbeat JSON file."""

    def __init__(self, heartbeat_path: str | Path = "data/heartbeat.json") -> None:
        self.heartbeat_path = Path(heartbeat_path)
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any]:
        """Read heartbeat data, returning an empty dict when absent or invalid."""
        if not self.heartbeat_path.exists():
            return {}

        try:
            return json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            logger.warning("Could not read heartbeat file %s: %s", self.heartbeat_path, error)
            return {}

    def update_success(self, report: RunReport) -> None:
        """Write heartbeat after a run that isn't FAILED.

        A single warning (PARTIAL_SUCCESS) still means the pipeline actually
        ran and processed mail — starving the heartbeat on any warning caused
        misleading "Tracker inactive for N hours" alerts during a chronic but
        harmless warning streak (ADR-D8 / B5). Only a FAILED run should stop
        this from advancing; callers gate on that, this just records which
        status produced the update.
        """
        payload = {
            "last_successful_run": utc_now_iso(),
            "processed_messages": report.metrics.processed_messages,
            "drives_created": report.metrics.drives_created,
            "drives_updated": report.metrics.drives_updated,
            "status": report.status.value.lower(),
        }
        _atomic_write_json(self.heartbeat_path, payload)

    def detect_inactivity(
        self,
        *,
        max_inactive_hours: float = 6.0,
        now: datetime | None = None,
    ) -> InactivityWarning | None:
        """Return a warning if the last successful run is too old."""
        data = self.read()
        last_success = data.get("last_successful_run")
        if not last_success:
            return None

        current = now or datetime.now(timezone.utc)
        previous = _parse_datetime(last_success)
        if previous is None:
            return None

        inactive_hours = (current - previous).total_seconds() / 3600
        if inactive_hours <= max_inactive_hours:
            return None

        return InactivityWarning(
            inactive_hours=inactive_hours,
            message=f"Tracker inactive for {inactive_hours:.1f} hours.",
        )


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
