"""Tests for the complete End-to-End (E2E) pipeline of Placement Mail Tracker."""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.scheduler.runner import map_extraction_to_opportunity, run_once


# ---------------------------------------------------------------------------
# Mocks and Fakes for E2E Pipeline
# ---------------------------------------------------------------------------

@dataclass
class MockGmailEmail:
    message_id: str
    thread_id: str
    subject: str
    sender: str
    timestamp: str
    body_text: str
    snippet: str


class MockGmailClient:
    """Mock Gmail client that returns predefined emails."""

    def __init__(self, settings: Settings, emails: list[dict[str, Any]]) -> None:
        self.settings = settings
        self.emails = emails

    def fetch_recent_messages(self, max_results: int = 10) -> list[dict[str, Any]]:
        return self.emails[:max_results]


class MockGeminiModel:
    """Mock Gemini model returning predefined JSON responses."""

    def __init__(self, response_json_list: list[str]) -> None:
        self.response_json_list = response_json_list
        self.calls = 0

    def generate_content(self, prompt: str) -> Any:
        self.calls += 1
        # Simple container matching the expected `response.text` attribute
        class SimpleResponse:
            def __init__(self, text: str) -> None:
                self.text = text
        return SimpleResponse(self.response_json_list.pop(0))


class MockTelegramNotifier:
    """Mock Telegram notifier to track sent alerts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.alerts: list[Any] = []

    def send_new_record_alert(self, record: Any) -> None:
        self.alerts.append(record)


# ---------------------------------------------------------------------------
# Helper to create test database
# ---------------------------------------------------------------------------

def get_test_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "test_e2e.db"
    return get_connection(db_path)


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_map_extraction_to_opportunity() -> None:
    extraction = {
        "company_name": "Google",
        "role": "SWE Intern",
        "opportunity_type": "internship",
        "package": "100,000 stip",
        "stipend": "80,000 stip",
        "location": "Bengaluru",
        "eligible_branches": ["CSE", "ECE"],
        "registration_deadline": "2026-06-01",
        "hiring_process": ["OA", "Interview"],
    }
    opp = map_extraction_to_opportunity(extraction)
    assert opp["company_name"] == "Google"
    assert opp["role"] == "SWE Intern"
    assert opp["internship_or_fulltime"] == "internship"
    assert opp["package_or_stipend"] == "100,000 stip"  # prefers package over stipend
    assert opp["deadline"] == "2026-06-01"
    assert opp["work_location"] == "Bengaluru"
    assert opp["branches_allowed"] == ["CSE", "ECE"]


def test_complete_e2e_pipeline(tmp_path, monkeypatch) -> None:
    """Test full flow: fetch, filter, extract, deduplicate, save, notify, sync."""
    connection = get_test_db(tmp_path)
    settings = Settings(
        GEMINI_API_KEY="fake-key",
        GOOGLE_SHEET_ID="fake-sheet",
        gmail_max_results=5,
    )

    # 1. Define emails list (spam, new internship, duplicate internship)
    emails = [
        # Email 1: Spam / newsletter (should be skipped)
        {
            "message_id": "msg-101",
            "thread_id": "thread-101",
            "subject": "Weekly Tech Newsletter",
            "sender": "newsletter@medium.com",
            "timestamp": "2026-05-27T10:00:00+00:00",
            "body_text": "Here are some top articles about coding bootcamps. Unsubscribe now.",
            "snippet": "Newsletter articles",
        },
        # Email 2: Valid internship opportunity
        {
            "message_id": "msg-102",
            "thread_id": "thread-102",
            "subject": "Campus Recruitment Drive - Amazon",
            "sender": "CDC Placements <cdc@college.edu>",
            "timestamp": "2026-05-27T11:00:00+00:00",
            "body_text": "Amazon is hiring Software Development Interns. Deadline June 10. Stipend: 80k.",
            "snippet": "Hiring SDE Interns",
        },
        # Email 3: Fuzzy duplicate (Amazon SDE Internship) with updated deadline
        {
            "message_id": "msg-103",
            "thread_id": "thread-103",
            "subject": "URGENT UPDATE: Amazon SDE Internship Deadline Extended",
            "sender": "CDC Placements <cdc@college.edu>",
            "timestamp": "2026-05-27T12:00:00+00:00",
            "body_text": "The deadline for the Amazon Software Development Internship has been extended to June 15.",
            "snippet": "Amazon deadline extension",
        },
    ]

    # 2. Predefined mock Gemini outputs
    gemini_responses = [
        # Response for msg-102 (Amazon)
        """{
            "company_name": "Amazon India",
            "role": "SDE Intern",
            "opportunity_type": "internship",
            "package": "80,000 per month",
            "eligible_branches": ["CSE", "IT"],
            "registration_deadline": "2026-06-10",
            "update_type": "new_opportunity"
        }""",
        # Response for msg-103 (Amazon fuzzy duplicate, different title phrasing but deduplicatable)
        """{
            "company_name": "Amazon",
            "role": "SDE Intern",
            "opportunity_type": "internship",
            "package": "80,000 per month",
            "eligible_branches": ["CSE", "IT"],
            "registration_deadline": "2026-06-15",
            "update_type": "deadline_update"
        }""",
    ]

    # 3. Patch the internal classes using monkeypatch
    mock_notifier = MockTelegramNotifier(settings)
    mock_model = MockModelAndClient(gemini_responses)

    # Patch Gmail client
    monkeypatch.setattr(
        "placement_mail_tracker.scheduler.runner.GmailClient",
        lambda s: MockGmailClient(s, emails),
    )
    # Patch Gemini Model
    monkeypatch.setattr(
        "placement_mail_tracker.scheduler.runner.GeminiExtractor",
        lambda: mock_model,
    )
    # Patch Telegram notifier
    monkeypatch.setattr(
        "placement_mail_tracker.scheduler.runner.TelegramNotifier",
        lambda s: mock_notifier,
    )
    # Patch Google Sheets Sync Values call to avoid HTTP requests
    monkeypatch.setattr(
        "placement_mail_tracker.sheets.sheets_sync.GoogleSheetsSync.ensure_header_row",
        lambda self: None,
    )
    monkeypatch.setattr(
        "placement_mail_tracker.sheets.sheets_sync.GoogleSheetsSync._get_existing_rows",
        lambda self: [["sync_key"]],
    )
    monkeypatch.setattr(
        "placement_mail_tracker.sheets.sheets_sync.GoogleSheetsSync._append_rows",
        lambda self, rows: None,
    )
    monkeypatch.setattr(
        "placement_mail_tracker.sheets.sheets_sync.GoogleSheetsSync._update_row",
        lambda self, row_number, row: None,
    )

    # 4. Run the orchestration pipeline
    run_once(connection, settings)

    # 5. Assertions and Audit Validation
    db = DatabaseManager(connection=connection)
    active = db.get_active_opportunities()

    # Verify only ONE unique opportunity exists due to fuzzy deduplication
    assert len(active) == 1
    amazon_opp = active[0]

    # Verification: company_name aligned to first record "Amazon India",
    # but non-key field "deadline" updated to "2026-06-15"
    assert amazon_opp["company_name"] == "Amazon India"
    assert amazon_opp["role"] == "SDE Intern"
    assert amazon_opp["deadline"] == "2026-06-15"

    # Verify processed email logging status
    # msg-101: skipped
    # msg-102: processed (inserted)
    # msg-103: processed (updated)
    rows = connection.execute(
        "SELECT gmail_message_id, processed_status FROM processed_emails ORDER BY gmail_message_id"
    ).fetchall()
    statuses = {r["gmail_message_id"]: r["processed_status"] for r in rows}

    assert statuses["msg-101"] == "skipped"
    assert statuses["msg-102"] == "processed"
    assert statuses["msg-103"] == "processed"

    # Verify updates timeline for the Amazon opportunity
    updates = db.fetch_updates_for_opportunity(amazon_opp["id"])
    update_fields = [u["field_name"] for u in updates]
    assert "deadline" in update_fields

    # Verify notification was sent ONLY ONCE for the initial creation (created is True)
    # The second update is processed and logged but does not trigger a duplicate creation alert.
    assert len(mock_notifier.alerts) == 1
    assert mock_notifier.alerts[0].company_name == "Amazon India"


# ---------------------------------------------------------------------------
# Injected classes for mock extractor
# ---------------------------------------------------------------------------

class MockModelAndClient:
    """Mock container mimicking GeminiExtractor API class interface."""

    def __init__(self, responses: list[str]) -> None:
        from placement_mail_tracker.gemini.extractor import GeminiExtractor
        from placement_mail_tracker.ai.gemini_extractor import GeminiPlacementExtractor
        from placement_mail_tracker.config.settings import Settings

        self.fake_model = MockGeminiModel(responses)
        self.extractor = GeminiPlacementExtractor(
            Settings(GEMINI_API_KEY="fake-key"),
            model=self.fake_model,
        )

    def extract(self, email_message: dict[str, Any]) -> dict[str, Any] | None:
        return self.extractor.extract_from_email(email_message)
