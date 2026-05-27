"""Google Sheets client compatibility wrapper."""

import logging

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.models.placement_record import PlacementRecord
from placement_mail_tracker.sheets.sheets_sync import GoogleSheetsSync

logger = logging.getLogger(__name__)


class SheetsClient:
    """Small compatibility shell for syncing records to Google Sheets."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sync = GoogleSheetsSync(settings)

    def append_record(self, record: PlacementRecord) -> None:
        """Compatibility method for older placement-record objects."""
        logger.info(
            "Use GoogleSheetsSync.sync_opportunities for structured records: %s",
            record.subject,
        )
