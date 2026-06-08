"""Migration script for upgrading Placement Mail Tracker to drive-centric architecture.

This script migrates an existing database to the new schema with:
- Phase 5: drive_id column
- Phase 11: action_required column
- Phase 12: my_status column
- Phase 13: email_classification column
- New: next_event_date column

Run this script from the project root:
    python -m placement_mail_tracker.db.migrate
"""

import logging
import sqlite3
from pathlib import Path

from placement_mail_tracker.config.settings import get_settings

logger = logging.getLogger(__name__)


def migrate_database(db_path: Path | None = None) -> None:
    """Run all migrations on the specified database."""
    if db_path is None:
        settings = get_settings()
        db_path = settings.database_path

    if not db_path.exists():
        logger.info("No existing database found at %s; nothing to migrate", db_path)
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")

    try:
        _run_migrations(conn)
        conn.commit()
        logger.info("Migration completed successfully")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all required schema changes."""
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(opportunities);").fetchall()
    }

    # New columns for drive-centric architecture
    migrations = {
        "current_status": "TEXT NOT NULL DEFAULT 'OPEN'",
        "status_history": "TEXT NOT NULL DEFAULT '[]'",
        "last_update_timestamp": "TEXT",
        "email_received_at": "TEXT",
        "drive_id": "TEXT",
        "source_thread_id": "TEXT",
        "action_required": "TEXT",
        "email_classification": "TEXT",
        "my_status": "TEXT NOT NULL DEFAULT 'NOT_APPLIED'",
        "next_event_date": "TEXT",
    }

    for col_name, col_def in migrations.items():
        if col_name not in columns:
            logger.info("Adding column: %s", col_name)
            conn.execute(f"ALTER TABLE opportunities ADD COLUMN {col_name} {col_def};")

    # Ensure companies table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            total_drives INTEGER NOT NULL DEFAULT 0,
            active_drives INTEGER NOT NULL DEFAULT 0,
            selected_drives INTEGER NOT NULL DEFAULT 0,
            rejected_drives INTEGER NOT NULL DEFAULT 0,
            last_activity TEXT
        );
    """)

    # Ensure email_classification on processed_emails
    pe_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(processed_emails);").fetchall()
    }
    if "email_classification" not in pe_columns:
        conn.execute("ALTER TABLE processed_emails ADD COLUMN email_classification TEXT;")

    # Backfill drive IDs for existing records without one
    from placement_mail_tracker.db.manager import generate_drive_id

    null_drives = conn.execute(
        """
        SELECT id, company_name, role, internship_or_fulltime
        FROM opportunities
        WHERE drive_id IS NULL;
        """
    ).fetchall()

    for row in null_drives:
        drive_id = generate_drive_id(
            row["company_name"],
            role=row["role"],
            category=row["internship_or_fulltime"],
        )
        # Ensure uniqueness
        existing = conn.execute(
            "SELECT COUNT(*) FROM opportunities WHERE drive_id LIKE ?",
            (f"{drive_id}%",),
        ).fetchone()[0]
        if existing > 0:
            drive_id = f"{drive_id}_{existing + 1:02d}"
        conn.execute(
            "UPDATE opportunities SET drive_id = ? WHERE id = ?;",
            (drive_id, row["id"]),
        )
        logger.info("Backfilled drive_id=%s for opportunity %s", drive_id, row["id"])

    # Create indexes
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_opportunities_drive_id ON opportunities(drive_id);
        CREATE INDEX IF NOT EXISTS idx_opportunities_thread_id ON opportunities(source_thread_id);
    """)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_database()
