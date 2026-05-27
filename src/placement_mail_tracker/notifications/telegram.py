"""Placeholder Telegram notification service."""

import logging

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.models.placement_record import PlacementRecord

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Starter shell for Telegram notifications."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send_new_record_alert(self, record: PlacementRecord) -> None:
        """Send a notification for a new placement record."""
        logger.info("Telegram notification is not implemented yet: %s", record.subject)
