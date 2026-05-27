"""SQLite schema setup."""

import sqlite3

from placement_mail_tracker.db.manager import DatabaseManager


def create_tables(connection: sqlite3.Connection) -> None:
    """Create database tables if they do not already exist."""
    DatabaseManager(connection=connection).create_tables()
