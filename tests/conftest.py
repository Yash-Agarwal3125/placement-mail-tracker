"""Global test fixtures and mocks."""

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
