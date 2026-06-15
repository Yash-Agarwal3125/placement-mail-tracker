"""SQLite connection utilities."""

import sqlite3
from pathlib import Path


def get_connection(database_path: Path) -> sqlite3.Connection:
    """Create a SQLite connection with beginner-friendly defaults."""
    connection = sqlite3.connect(database_path, timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection
