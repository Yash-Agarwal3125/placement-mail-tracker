"""Tests for heartbeat updates and missed-run detection."""

from datetime import UTC, datetime, timedelta

from placement_mail_tracker.reliability.heartbeat import HeartbeatManager
from placement_mail_tracker.reliability.status import RunReport, SyncMetrics


def test_heartbeat_updates_after_success(tmp_path) -> None:
    heartbeat = HeartbeatManager(tmp_path / "heartbeat.json")
    report = RunReport(environment="testing")
    report.metrics = SyncMetrics(processed_messages=12, drives_created=2, drives_updated=5)

    heartbeat.update_success(report)

    data = heartbeat.read()
    assert data["status"] == "success"
    assert data["processed_messages"] == 12
    assert data["drives_created"] == 2
    assert data["drives_updated"] == 5


def test_inactivity_detection_warns_after_threshold(tmp_path) -> None:
    heartbeat = HeartbeatManager(tmp_path / "heartbeat.json")
    previous = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    heartbeat.heartbeat_path.write_text(
        f'{{"last_successful_run": "{previous.isoformat()}"}}',
        encoding="utf-8",
    )

    warning = heartbeat.detect_inactivity(
        max_inactive_hours=6,
        now=previous + timedelta(hours=7, minutes=30),
    )

    assert warning is not None
    assert "Tracker inactive for 7.5 hours." == warning.message


def test_inactivity_detection_allows_recent_success(tmp_path) -> None:
    heartbeat = HeartbeatManager(tmp_path / "heartbeat.json")
    previous = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    heartbeat.heartbeat_path.write_text(
        f'{{"last_successful_run": "{previous.isoformat()}"}}',
        encoding="utf-8",
    )

    warning = heartbeat.detect_inactivity(
        max_inactive_hours=6,
        now=previous + timedelta(hours=3),
    )

    assert warning is None
