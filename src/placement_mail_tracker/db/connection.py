"""SQLite connection utilities."""

import sqlite3
from pathlib import Path


def get_connection(database_path: Path) -> sqlite3.Connection:
    """Create a SQLite connection with safe defaults for unattended operation."""
    connection = sqlite3.connect(database_path, timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    # WAL keeps reads/writes from blocking each other and survives crashes
    # better; NORMAL synchronous is the recommended durable-but-fast pairing.
    # Skip for in-memory databases where WAL is unsupported/pointless.
    if str(database_path) != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL;")
        connection.execute("PRAGMA synchronous = NORMAL;")
    return connection
