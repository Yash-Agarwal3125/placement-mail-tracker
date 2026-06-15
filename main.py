"""Main execution script for the Placement Mail Tracker.

This script executes a single synchronization cycle:
1. Checks Gmail for new unread/relevant placement emails.
2. Uses Gemini AI to parse structured fields.
3. Automatically deduplicates records against SQLite.
4. Synchronizes active opportunities with Google Sheets.
5. Dispatches SMTP email notifications for new openings or deadline updates.
"""

from __future__ import annotations

import logging
import os
import sys

import psutil

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.config.validator import ConfigValidator
from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.db.schema import create_tables
from placement_mail_tracker.reliability.health import FailureAlertManager, SystemHealthManager
from placement_mail_tracker.reliability.heartbeat import HeartbeatManager
from placement_mail_tracker.reliability.status import RunReport, RunStatus
from placement_mail_tracker.scheduler.runner import run_once
from placement_mail_tracker.utils.lock_manager import SingleInstanceLock
from placement_mail_tracker.utils.logging_config import setup_logging

logger = logging.getLogger("placement_mail_tracker.main")


def main() -> int:
    """Execute a single sync cycle sequentially and safely."""
    # 1. Load settings and setup clean readable logs
    settings = get_settings()
    setup_logging(
        settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    report = RunReport(environment=settings.environment)
    health_manager = SystemHealthManager(settings.system_health_file)
    heartbeat_manager = HeartbeatManager(settings.heartbeat_file)

    logger.info("==================================================")
    logger.info("STARTING SYNC CYCLE: PLACEMENT MAIL TRACKER")
    logger.info("==================================================")
    logger.info("Environment: %s", settings.app_env)
    logger.info("Database URL: %s", settings.database_url)
    parent = psutil.Process(os.getppid())
    try:
        logger.info("==================================================")
        logger.info("[RUN_DIAGNOSTICS]")
        logger.info("PID=%s", os.getpid())
        logger.info("PPID=%s", os.getppid())
        logger.info("ParentName=%s", parent.name())
        logger.info("ParentExe=%s", parent.exe())
        logger.info("ParentCmdLine=%s", " ".join(parent.cmdline()))
        try:
            grandparent = parent.parent()
            if grandparent:
                logger.info("GrandParentName=%s", grandparent.name())
                logger.info("GrandParentExe=%s", grandparent.exe())
                logger.info("GrandParentCmdLine=%s", " ".join(grandparent.cmdline()))
                logger.info("Process tree:")
                logger.info("  %s", grandparent.name())
                logger.info("    ↓")
                logger.info("  %s", parent.name())
                logger.info("    ↓")
                logger.info("  Current Process (%s)", os.getpid())
        except Exception as gp_err:
            logger.warning("Could not fetch grandparent info: %s", gp_err)
        logger.info("CurrentWorkingDirectory=%s", os.getcwd())
        logger.info("Executable=%s", sys.executable)
        logger.info("CommandLine=%s", " ".join(sys.argv))
        logger.info("Username=%s", psutil.Process().username())
        logger.info("==================================================")
    except Exception as e:
        logger.warning("Failed to collect run diagnostics: %s", e)
    logger.info(
        "[RUN_SOURCE] PID=%s Parent=%s ParentName=%s",
        os.getpid(),
        parent.pid,
        parent.name()
    )
    inactivity = heartbeat_manager.detect_inactivity(
        max_inactive_hours=settings.heartbeat_inactivity_hours
    )
    if inactivity:
        logger.warning(inactivity.message)

    # 1b. Run startup validation checks
    validator = ConfigValidator(settings)
    validator.run_all_checks()
    validator.print_report()

    if not validator.is_healthy():
        logger.critical(
            "Startup aborted due to critical configuration errors. "
            "Please check the health report above."
        )
        _apply_validation_results(report, validator)
        return _finalize_run(report, settings, health_manager, heartbeat_manager)

    for warning in validator.warnings():
        report.add_warning(warning.message)

    try:
        with SingleInstanceLock(lock_file="data/tracker.lock"):
            # 2. Establish connection and create SQLite tables atomically
            db_path = settings.database_path
            logger.info("Connecting to SQLite database: %s", db_path)
            with get_connection(db_path) as connection:
                create_tables(connection)
                
                # 3. Execute the full E2E orchestration pipeline
                cycle_report = run_once(connection, settings)
                _merge_report(report, cycle_report)

        return _finalize_run(report, settings, health_manager, heartbeat_manager)

    except SystemExit:
        # Expected exit from SingleInstanceLock if another instance is running
        return 0
    except Exception as error:
        logger.critical("Sync cycle failed due to an unhandled error: %s", error, exc_info=True)
        report.add_failure(f"Application crash: {error}", critical=True)
        return _finalize_run(report, settings, health_manager, heartbeat_manager)


def _apply_validation_results(report: RunReport, validator: ConfigValidator) -> None:
    """Apply startup validation errors and warnings to a run report."""
    tracked_components = {"database", "gmail", "sheets", "notifications"}
    for result in validator.results:
        if result.status == "PASS":
            continue

        critical = result.status == "ERROR"
        if result.component in tracked_components:
            report.mark_component(
                result.component,
                False,
                result.message,
                critical=critical,
            )
        elif critical:
            report.add_failure(result.message, critical=True)
        else:
            report.add_warning(result.message)


def _merge_report(target: RunReport, source: RunReport) -> None:
    """Merge a cycle report into the top-level report."""
    target.database_ok = target.database_ok and source.database_ok
    target.gmail_ok = target.gmail_ok and source.gmail_ok
    target.sheets_ok = target.sheets_ok and source.sheets_ok
    target.notifications_ok = target.notifications_ok and source.notifications_ok
    target.critical_failure = target.critical_failure or source.critical_failure
    target.failures.extend(source.failures)
    target.warnings.extend(source.warnings)
    target.metrics = source.metrics


def _finalize_run(
    report: RunReport,
    settings,
    health_manager: SystemHealthManager,
    heartbeat_manager: HeartbeatManager,
) -> int:
    """Write final state, alert when needed, and return the process exit code."""
    report.finish()

    if report.status == RunStatus.SUCCESS:
        heartbeat_manager.update_success(report)

    FailureAlertManager(settings, health_manager).handle_report(report)

    logger.info("==================================================")
    for line in report.summary_lines():
        if report.status == RunStatus.FAILED:
            logger.error(line)
        elif report.status == RunStatus.PARTIAL_SUCCESS:
            logger.warning(line)
        else:
            logger.info(line)
    logger.info("RUN_STATUS_JSON %s", report.to_json())
    logger.info("==================================================")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
