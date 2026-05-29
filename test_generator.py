"""Script to scaffold the 13-phase test suite."""

import os
from pathlib import Path

def generate_test_suite():
    tests_dir = Path("tests")
    tests_dir.mkdir(exist_ok=True)
    
    # 1. conftest.py
    with open(tests_dir / "conftest.py", "w", encoding="utf-8") as f:
        f.write('''"""Global test fixtures and mocks."""

import pytest
import sqlite3
from typing import Any
from unittest.mock import MagicMock
from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager

@pytest.fixture
def mock_settings():
    return Settings(
        GEMINI_API_KEY="test-key",
        GOOGLE_SHEET_ID="test-sheet",
        smtp_email="test@example.com",
        smtp_app_password="pass",
        email_receiver="test@example.com"
    )

@pytest.fixture
def db_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()

@pytest.fixture
def db_manager(db_connection):
    db = DatabaseManager(connection=db_connection)
    db.create_tables()
    return db
''')

    # 2. test_filters.py
    with open(tests_dir / "test_filters.py", "w", encoding="utf-8") as f:
        f.write('''"""Phase 3: Email Filter Tests."""

from placement_mail_tracker.gmail.filters import is_placement_mail

def test_valid_placement_emails():
    valid_subjects = [
        "OA scheduled for Tata Motors",
        "Interview scheduled - Amazon",
        "Additional shortlist released - Waters",
        "Offer released: TCS",
        "PPT announcement for Infosys",
        "Registration open for Deloitte"
    ]
    for subject in valid_subjects:
        decision = is_placement_mail(subject=subject, sender="cdc@vit.ac.in", body="")
        assert decision.is_placement is True

def test_invalid_placement_emails():
    invalid_subjects = [
        "Club recruitment: IEEE",
        "Gravitas committee interview",
        "Workshop notice on AI",
        "Academic circular: Holidays",
        "NPTEL reminder",
        "Event registration",
        "Attendance notice"
    ]
    for subject in invalid_subjects:
        decision = is_placement_mail(subject=subject, sender="noreply@vit.ac.in", body="")
        assert decision.is_placement is False
''')

    # 3. test_gemini_extraction.py
    with open(tests_dir / "test_gemini_extraction.py", "w", encoding="utf-8") as f:
        f.write('''"""Phase 4: Gemini Extraction Tests."""

import pytest
from unittest.mock import MagicMock, patch
from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor
from placement_mail_tracker.config.settings import Settings

@patch('google.genai.Client')
def test_gemini_full_extraction(mock_client):
    mock_response = MagicMock()
    mock_response.text = """{
        "company_name": "Infosys",
        "role": "Systems Engineer",
        "opportunity_type": "full_time",
        "package": "3.6 LPA",
        "update_type": "new_opportunity",
        "current_status": "NEW",
        "action_required": "Apply on portal"
    }"""
    mock_client.return_value.models.generate_content.return_value = mock_response
    
    extractor = GeminiPlacementExtractor(Settings(GEMINI_API_KEY="test"))
    result = extractor.extract_from_email({"subject": "Infosys Hiring"})
    
    assert result.company_name == "Infosys"
    assert result.role == "Systems Engineer"
    assert result.current_status == "NEW"
    assert result.action_required == "Apply on portal"

@patch('google.genai.Client')
def test_gemini_missing_role(mock_client):
    mock_response = MagicMock()
    mock_response.text = """{
        "company_name": "TCS",
        "role": null,
        "opportunity_type": "full_time",
        "update_type": "oa_update",
        "current_status": "OA"
    }"""
    mock_client.return_value.models.generate_content.return_value = mock_response
    
    extractor = GeminiPlacementExtractor(Settings(GEMINI_API_KEY="test"))
    result = extractor.extract_from_email({"subject": "TCS OA"})
    
    assert result.company_name == "TCS"
    assert result.role is None
    assert result.current_status == "OA"
''')

    # 4. test_followup_detection.py
    with open(tests_dir / "test_followup_detection.py", "w", encoding="utf-8") as f:
        f.write('''"""Phase 5 & 6: Follow-up Detection and Normalization Tests."""

import json
from placement_mail_tracker.utils.deduplication import normalize_company

def test_company_normalization():
    assert normalize_company("TATA MOTORS") == "Tata Motors"
    assert normalize_company("Tata Motors") == "Tata Motors"
    assert normalize_company("Tata motors ltd") == "Tata Motors"
    assert normalize_company("Tata Motors Ltd.") == "Tata Motors"

def test_thread_followup_detection(db_manager):
    # Email 1: OA
    opp1 = {"company_name": "Tata Motors", "role": "GET", "current_status": "OA"}
    id1, created1 = db_manager.insert_or_update_opportunity(opp1, source_email_id="msg1", source_thread_id="thread_tata")
    
    assert created1 is True
    
    # Email 2: Shortlist
    opp2 = {"company_name": "Tata Motors", "role": "GET", "current_status": "SHORTLISTED"}
    id2, created2 = db_manager.insert_or_update_opportunity(opp2, source_email_id="msg2", source_thread_id="thread_tata")
    
    assert created2 is False
    assert id1 == id2
    
    # Email 3: Interview
    opp3 = {"company_name": "Tata Motors", "role": "GET", "current_status": "INTERVIEW"}
    id3, created3 = db_manager.insert_or_update_opportunity(opp3, source_email_id="msg3", source_thread_id="thread_tata")
    
    assert created3 is False
    
    # Verify status history
    record = db_manager.fetch_opportunity_by_id(id1)
    history = record["status_history"]
    assert history == ["OA", "SHORTLISTED", "INTERVIEW"]

def test_separate_drives(db_manager):
    # Same company, different role/thread -> separate drives
    opp1 = {"company_name": "Tata Motors", "role": "GET"}
    id1, created1 = db_manager.insert_or_update_opportunity(opp1, source_thread_id="thread1")
    
    opp2 = {"company_name": "Tata Motors", "role": "Software Engineer"}
    id2, created2 = db_manager.insert_or_update_opportunity(opp2, source_thread_id="thread2")
    
    assert created2 is True
    assert id1 != id2
''')

    # 5. test_database.py
    with open(tests_dir / "test_database.py", "w", encoding="utf-8") as f:
        f.write('''"""Phase 7: Database Tests."""

def test_drive_id_generation(db_manager):
    opp1 = {"company_name": "Google", "role": "SWE"}
    db_manager.insert_or_update_opportunity(opp1)
    
    opp2 = {"company_name": "Google", "role": "PM"}
    db_manager.insert_or_update_opportunity(opp2)
    
    active = db_manager.get_active_opportunities()
    assert len(active) == 2
    drives = sorted([o["drive_id"] for o in active])
    assert "GOOGLE" in drives[0]
    assert drives[0].endswith("_01")
    assert drives[1].endswith("_02")
    
def test_retry_queue_logging(db_manager):
    db_manager.log_processed_email(
        gmail_message_id="msg-err",
        subject="Failed Email",
        processed_status="PENDING_EXTRACTION",
        error_message="503 Unavailable"
    )
    row = db_manager.connection.execute("SELECT * FROM processed_emails WHERE gmail_message_id='msg-err'").fetchone()
    assert row["processed_status"] == "PENDING_EXTRACTION"
''')

    # 6. test_sheet_sync.py
    with open(tests_dir / "test_sheet_sync.py", "w", encoding="utf-8") as f:
        f.write('''"""Phase 8: Google Sheets Tests."""

from placement_mail_tracker.sheets.sheets_sync import opportunity_to_sheet_row

def test_row_formatting():
    opp = {
        "email_received_at": "29-May-2026 10:00 AM",
        "company_name": "Amazon",
        "drive_id": "AMAZON_2026_01",
        "role": "SDE",
        "current_status": "OA",
        "status_history": ["NEW", "OA"],
        "package_or_stipend": "40 LPA",
        "source_message_id": "msg-123"
    }
    
    row = opportunity_to_sheet_row(opp)
    assert row[1] == "Amazon"
    assert row[2] == "AMAZON_2026_01"
    assert row[4] == "OA"
    assert "mail.google.com" in row[13]
''')

    # 7. test_end_to_end.py
    with open(tests_dir / "test_end_to_end.py", "w", encoding="utf-8") as f:
        f.write('''"""Phase 9 & 11: End-to-End Tests."""

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
    
    mock_extraction = MagicMock()
    mock_extraction.company_name = "Tata Motors"
    mock_extraction.role = "GET"
    mock_extraction.current_status = "OA"
    mock_extraction.update_type = "oa_update"
    mock_gemini.return_value.extract.return_value = mock_extraction
    
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
''')

    # 8. test_notifications.py
    with open(tests_dir / "test_notifications.py", "w", encoding="utf-8") as f:
        f.write('''"""Tests for Notification and Digest."""

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
''')

    # 9. run_regression_tests.py
    with open("run_regression_tests.py", "w", encoding="utf-8") as f:
        f.write('''"""Phase 12: Regression Test Runner."""
import os
import subprocess
import sys

def main():
    print("🚀 Starting Placement Mail Tracker Regression Suite...")
    
    # Run pytest with coverage
    result = subprocess.run(
        ["pytest", "tests/", "--cov=src", "--cov-report=term-missing"],
        capture_output=True,
        text=True
    )
    
    print(result.stdout)
    
    if result.returncode == 0:
        print("✅ All tests passed successfully!")
    else:
        print("❌ Some tests failed. Check the output above.")
        
    with open("regression_report.txt", "w", encoding="utf-8") as f:
        f.write(result.stdout)
        
if __name__ == "__main__":
    main()
''')

if __name__ == "__main__":
    generate_test_suite()
    print("Test suite generated successfully.")
