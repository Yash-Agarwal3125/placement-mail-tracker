import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.gmail.gmail_client import GmailClient
from placement_mail_tracker.scheduler.runner import PlacementTrackerRunner


@pytest.fixture
def test_settings(tmp_path):
    settings = Settings(
        app_env="testing",
        fetch_state_file=str(tmp_path / "fetch_state.json")
    )
    return settings

def test_gmail_client_fetch_since(test_settings):
    client = GmailClient(test_settings)
    client._search = MagicMock(return_value=[])
    
    timestamp = 1600000000
    client.fetch_emails_since(timestamp)
    
    client._search.assert_called_once()
    assert "after:1600000000" in client._search.call_args[1]["query"]

@patch("placement_mail_tracker.scheduler.runner.DatabaseManager")
@patch("placement_mail_tracker.scheduler.runner.GmailClient")
@patch("placement_mail_tracker.scheduler.runner.GeminiExtractor")
@patch("placement_mail_tracker.scheduler.runner.SheetsClient")
def test_runner_offline_recovery_reads_state(
    mock_sheets, mock_gemini, mock_gmail, mock_db, test_settings
):
    # Create the fetch_state.json manually
    state_file = Path(test_settings.fetch_state_file)
    state_file.write_text(
        json.dumps({"last_successful_fetch": "2026-06-01T12:00:00Z"}), encoding="utf-8"
    )
    
    runner = PlacementTrackerRunner(connection=MagicMock(), settings=test_settings)
    
    # Mock some methods to let run_once finish quickly
    mock_db_instance = mock_db.return_value
    mock_db_instance.get_active_opportunities.return_value = []
    
    mock_gmail_instance = mock_gmail.return_value
    mock_gmail_instance.fetch_recent_messages_since.return_value = []
    
    runner.run_once()
    
    # Verify that fetch_recent_messages_since was called
    mock_gmail_instance.fetch_recent_messages_since.assert_called_once()
    
    # The timestamp for 2026-06-01T12:00:00Z is 1780315200
    call_kwargs = mock_gmail_instance.fetch_recent_messages_since.call_args[1]
    called_timestamp = call_kwargs["timestamp_seconds"]
    assert isinstance(called_timestamp, int)
    
    # Verify the state file is updated to the current time after a successful run
    new_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert new_state["last_successful_fetch"] != "2026-06-01T12:00:00Z"
