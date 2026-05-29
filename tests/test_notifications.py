"""Tests for Notification and Digest."""

from placement_mail_tracker.scheduler.digest_generator import DailyDigestGenerator
from unittest.mock import patch

@patch("smtplib.SMTP_SSL")
def test_daily_digest(mock_smtp, db_manager, mock_settings):
    db_manager.insert_or_update_opportunity({"company_name": "Accenture", "role": "ASE", "current_status": "NEW"})
    
    generator = DailyDigestGenerator(db_manager, mock_settings)
    result = generator.generate_and_send()
    
    assert result is True
    # Duplicate prevention
    assert generator.generate_and_send() is False
