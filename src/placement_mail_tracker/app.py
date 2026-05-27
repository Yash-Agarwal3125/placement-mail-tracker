"""Application entry point for Placement Mail Tracker."""

import logging

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.db.schema import create_tables
from placement_mail_tracker.scheduler.runner import run_once
from placement_mail_tracker.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Start one placeholder sync cycle."""
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("Starting Placement Mail Tracker in %s mode", settings.app_env)

    with get_connection(settings.database_path) as connection:
        create_tables(connection)
        run_once(connection, settings)

    logger.info("Placement Mail Tracker finished successfully")


if __name__ == "__main__":
    main()
