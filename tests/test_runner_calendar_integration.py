"""Runner-level calendar sync wiring (docs/design/04-integration-spec.md §4 point 3,
test plan case 18).

The unit-level sync/derive/client tests (test_calendar_sync.py etc.) prove the
calendar_sync package works in isolation; these prove PlacementTrackerRunner
actually wires it in correctly: gating, non-critical failure isolation, and
the resulting RunReport/exit_code semantics.
"""

from __future__ import annotations

from unittest.mock import patch

from placement_mail_tracker.calendar_sync.client import CalendarAuthenticationError
from placement_mail_tracker.calendar_sync.sync import CalendarSyncResult
from placement_mail_tracker.reliability.status import RunReport, RunStatus
from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner


def test_calendar_disabled_by_default_is_a_noop(db_manager, mock_settings):
    """Rollout checklist step 3: CALENDAR_SYNC_ENABLED unset -> no import, no call.

    Forces calendar_sync_enabled=False explicitly rather than relying on
    mock_settings' ambient value, since Settings() reads the real .env file
    and this repo's own .env legitimately sets CALENDAR_SYNC_ENABLED=true.
    """
    settings = mock_settings.model_copy(update={"calendar_sync_enabled": False})
    runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
    report = RunReport(environment=settings.environment)

    with patch(
        "placement_mail_tracker.calendar_sync.sync.CalendarSyncEngine.sync"
    ) as mock_sync:
        runner._execute_calendar_sync(db_manager, report)

    mock_sync.assert_not_called()
    assert report.calendar_ok is True
    assert report.status == RunStatus.SUCCESS


def test_calendar_dry_run_flag_runs_even_when_disabled(db_manager, mock_settings):
    """--calendar-dry-run must work for manual verification before the flag is on."""
    settings = mock_settings.model_copy(update={"calendar_sync_enabled": False})
    runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
    report = RunReport(environment=settings.environment)

    with patch(
        "placement_mail_tracker.calendar_sync.sync.CalendarSyncEngine.sync",
        return_value=CalendarSyncResult(dry_run=True),
    ) as mock_sync:
        runner._execute_calendar_sync(db_manager, report, calendar_dry_run=True)

    mock_sync.assert_called_once_with(dry_run=True)
    assert report.calendar_ok is True


def test_calendar_auth_dead_marks_component_non_critical(db_manager, mock_settings):
    """Case 18: auth-dead failure -> calendar_ok False, PARTIAL_SUCCESS, exit_code 2."""
    settings = mock_settings.model_copy(update={"calendar_sync_enabled": True})
    runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
    report = RunReport(environment=settings.environment)

    with patch(
        "placement_mail_tracker.calendar_sync.sync.CalendarSyncEngine.sync",
        side_effect=CalendarAuthenticationError("OAuth dead — re-consent needed for Calendar"),
    ):
        runner._execute_calendar_sync(db_manager, report)

    assert report.calendar_ok is False
    assert report.critical_failure is False
    assert report.status == RunStatus.PARTIAL_SUCCESS
    assert report.exit_code == 2
    assert any("calendar" in failure_or_warning.lower() for failure_or_warning in report.warnings)


def test_calendar_rebuild_flag_calls_rebuild_not_sync(db_manager, mock_settings):
    settings = mock_settings.model_copy(update={"calendar_sync_enabled": True})
    runner = PlacementTrackerRunner(connection=db_manager.connection, settings=settings)
    report = RunReport(environment=settings.environment)

    with (
        patch(
            "placement_mail_tracker.calendar_sync.sync.CalendarSyncEngine.rebuild",
            return_value=CalendarSyncResult(),
        ) as mock_rebuild,
        patch(
            "placement_mail_tracker.calendar_sync.sync.CalendarSyncEngine.sync"
        ) as mock_sync,
    ):
        runner._execute_calendar_sync(db_manager, report, calendar_rebuild=True)

    mock_rebuild.assert_called_once()
    mock_sync.assert_not_called()
    assert report.calendar_ok is True
