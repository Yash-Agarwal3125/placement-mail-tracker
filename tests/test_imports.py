"""Smoke tests for starter modules."""

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.connection import get_connection


def test_settings_can_load() -> None:
    settings = get_settings()
    assert settings.app_env


def test_database_connection_can_open(tmp_path) -> None:
    database_path = tmp_path / "test.db"
    connection = get_connection(database_path)
    try:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        connection.close()
