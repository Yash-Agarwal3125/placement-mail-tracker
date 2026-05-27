"""One-cycle orchestration for the starter project."""

import logging
import sqlite3
from dataclasses import dataclass

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.gemini.extractor import GeminiExtractor
from placement_mail_tracker.gmail.gmail_client import GmailClient
from placement_mail_tracker.notifications.telegram import TelegramNotifier
from placement_mail_tracker.sheets.client import SheetsClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlacementTrackerRunner:
    """Coordinate one Placement Mail Tracker sync cycle."""

    connection: sqlite3.Connection
    settings: Settings

    def run_once(self) -> None:
        """Run one sync cycle using configured services."""
        database = DatabaseManager(connection=self.connection)
        database.create_tables()

        gmail_client = GmailClient(self.settings)
        extractor = GeminiExtractor()
        sheets_client = SheetsClient(self.settings)
        notifier = TelegramNotifier(self.settings)

        messages = gmail_client.fetch_recent_messages(max_results=self.settings.gmail_max_results)
        logger.info("Fetched %s candidate messages", len(messages))

        # Keep references visible for the future orchestration flow.
        _ = (database, extractor, sheets_client, notifier)


def run_once(connection: sqlite3.Connection, settings: Settings) -> None:
    """Run one placeholder sync cycle.

    Future versions will fetch Gmail messages, filter them, extract structured
    data, save new records, sync to Sheets, and send Telegram notifications.
    """
    PlacementTrackerRunner(connection=connection, settings=settings).run_once()
