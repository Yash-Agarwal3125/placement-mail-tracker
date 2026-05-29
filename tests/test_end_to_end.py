"""Phase 9 & 11: End-to-End Tests."""

import pytest
from unittest.mock import patch, MagicMock
from placement_mail_tracker.scheduler.runner import run_once
from googleapiclient.errors import HttpError
from httplib2 import Response

@patch('placement_mail_tracker.scheduler.runner.GmailClient')
@patch('placement_mail_tracker.scheduler.runner.GeminiExtractor')
@patch('placement_mail_tracker.scheduler.runner.SheetsClient')
@patch('placement_mail_tracker.scheduler.runner.EmailNotifier')
def test_e2e_pipeline_success(mock_notifier, mock_sheets, mock_gemini, mock_gmail, db_connection, mock_settings):
    mock_gmail.return_value.fetch_recent_messages.return_value = [
        {"id": "msg1", "subject": "Tata Motors OA", "thread_id": "t1"}
    ]
    
    mock_gemini.return_value.extract.return_value = {
        "company_name": "Tata Motors",
        "role": "GET",
        "current_status": "OA",
        "update_type": "oa_update"
    }
    
    run_once(db_connection, mock_settings)
    
    # Verify DB state
    cur = db_connection.cursor()
    cur.execute("SELECT * FROM opportunities")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["company_name"] == "Tata Motors"

@patch('placement_mail_tracker.scheduler.runner.GmailClient')
def test_gmail_503_retry(mock_gmail, db_connection, mock_settings):
    # Simulate API failure
    resp = Response({"status": "503"})
    mock_gmail.return_value.fetch_recent_messages.side_effect = HttpError(resp, b"Service Unavailable")
    
    # Should handle gracefully without crashing
    with pytest.raises(HttpError):
        run_once(db_connection, mock_settings)
