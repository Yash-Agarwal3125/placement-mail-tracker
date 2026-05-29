"""Shared pytest fixtures for Placement Mail Tracker test suite.

Provides:
- In-memory SQLite connection with row_factory
- DatabaseManager backed by that connection
- Mock Settings with fake API keys
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager


# ---------------------------------------------------------------------------
# Core Database Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_connection():
    """Fresh in-memory SQLite connection with ``sqlite3.Row`` factory."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture()
def db_manager(db_connection):
    """``DatabaseManager`` wired to the in-memory connection (tables auto-created)."""
    return DatabaseManager(connection=db_connection)


# ---------------------------------------------------------------------------
# Settings Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_settings():
    """``Settings`` instance with fake/test API keys – no real services contacted."""
    return Settings(
        APP_ENV="testing",
        LOG_LEVEL="DEBUG",
        DATABASE_URL="sqlite:///:memory:",
        GMAIL_CREDENTIALS_FILE="config/fake_creds.json",
        GMAIL_TOKEN_FILE="config/fake_token.json",
        GMAIL_QUERY="newer_than:1d",
        GMAIL_MAX_RESULTS=10,
        GEMINI_API_KEY="fake-gemini-api-key-for-testing",
        GEMINI_MODEL="gemini-2.5-flash",
        GOOGLE_SHEET_ID="fake-sheet-id-for-testing",
        GOOGLE_SHEETS_CREDENTIALS_FILE="config/fake_sheets_creds.json",
        GOOGLE_SHEETS_TOKEN_FILE="config/fake_sheets_token.json",
        TELEGRAM_BOT_TOKEN="fake-telegram-token",
        TELEGRAM_CHAT_ID="fake-chat-id",
        SMTP_EMAIL="test@example.com",
        SMTP_APP_PASSWORD="fake-password",
        EMAIL_RECEIVER="receiver@example.com",
    )


# ---------------------------------------------------------------------------
# Sample Opportunity Factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_opportunity():
    """Return a factory function that creates well-formed opportunity dicts."""

    def _make(
        company_name: str = "Microsoft",
        role: str = "Software Engineer Intern",
        category: str = "internship",
        *,
        current_status: str = "OPEN",
        package: str | None = None,
        deadline: str | None = None,
        oa_date: str | None = None,
        interview_date: str | None = None,
    ) -> dict:
        return {
            "company_name": company_name,
            "role": role,
            "internship_or_fulltime": category,
            "package_or_stipend": package or "50000 per month",
            "eligibility": "B.Tech 2027",
            "cgpa_requirement": "7.0",
            "branches_allowed": ["CSE", "ECE", "EEE"],
            "deadline": deadline,
            "interview_date": interview_date,
            "oa_date": oa_date,
            "registration_link": "https://forms.gle/test123",
            "work_location": "Bangalore",
            "hiring_process": ["OA", "Tech Interview", "HR"],
            "important_notes": ["Bring laptop"],
            "current_status": current_status,
        }

    return _make
