"""Compatibility wrapper for Gemini extraction."""

import logging
from typing import Any

from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor
from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.models.placement_record import PlacementRecord

logger = logging.getLogger(__name__)


class GeminiExtractor:
    """Compatibility shell for structured extraction with Google Gemini."""

    def __init__(self) -> None:
        settings = get_settings()
        self.extractor = GeminiPlacementExtractor(
            settings,
            max_retries=settings.gemini_max_retries,
            retry_delay_seconds=settings.gemini_retry_delay_seconds,
        )

    def extract(self, email_message: dict[str, Any]) -> dict[str, Any] | PlacementRecord | None:
        """Extract structured placement data from an email message."""
        logger.info("Extracting structured placement data with Gemini")
        return self.extractor.extract_from_email(email_message)
