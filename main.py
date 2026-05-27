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
import sys
from pathlib import Path

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.db.schema import create_tables
from placement_mail_tracker.scheduler.runner import run_once
from placement_mail_tracker.utils.logging_config import setup_logging

logger = logging.getLogger("placement_mail_tracker.main")


def main() -> int:
    """Execute a single sync cycle sequentially and safely."""
    # 1. Load settings and setup clean readable logs
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("==================================================")
    logger.info("STARTING SYNC CYCLE: PLACEMENT MAIL TRACKER")
    logger.info("==================================================")
    logger.info("Environment: %s", settings.app_env)
    logger.info("Database URL: %s", settings.database_url)

    try:
        # 2. Establish connection and create SQLite tables atomically
        db_path = settings.database_path
        logger.info("Connecting to SQLite database: %s", db_path)
        with get_connection(db_path) as connection:
            create_tables(connection)
            
            # 3. Execute the full E2E orchestration pipeline
            run_once(connection, settings)

        logger.info("==================================================")
        logger.info("SUCCESS: SYNC CYCLE COMPLETED")
        logger.info("==================================================")
        return 0

    except Exception as error:
        logger.critical("Sync cycle failed due to an unhandled error: %s", error, exc_info=True)
        logger.info("==================================================")
        logger.info("FAILED: SYNC CYCLE ENCOUNTERED AN ERROR")
        logger.info("==================================================")
        return 1


if __name__ == "__main__":
    sys.exit(main())
