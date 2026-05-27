"""Reusable SQLite database manager for Placement Mail Tracker."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from placement_mail_tracker.db.connection import get_connection
from placement_mail_tracker.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

OPPORTUNITY_FIELDS = (
    "company_name",
    "role",
    "internship_or_fulltime",
    "package_or_stipend",
    "eligibility",
    "cgpa_requirement",
    "branches_allowed",
    "deadline",
    "interview_date",
    "oa_date",
    "registration_link",
    "work_location",
    "hiring_process",
    "important_notes",
)

JSON_FIELDS = {"branches_allowed", "hiring_process", "important_notes"}


def generate_unique_hash(company_name: str, role: str) -> str:
    """Generate a stable hash for duplicate prevention by company and role."""
    normalized = f"{_normalize_key(company_name)}::{_normalize_key(role)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class DatabaseManager:
    """High-level sqlite3 manager for opportunities, updates, and notifications."""

    def __init__(
        self,
        database_path: Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None and database_path is None:
            msg = "Provide either database_path or connection."
            raise ValueError(msg)

        self.connection = connection or get_connection(database_path)  # type: ignore[arg-type]
        self.create_tables()

    def create_tables(self) -> None:
        """Create all required SQLite tables and indexes automatically."""
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_hash TEXT UNIQUE NOT NULL,
                company_name TEXT NOT NULL,
                role TEXT NOT NULL,
                internship_or_fulltime TEXT,
                package_or_stipend TEXT,
                eligibility TEXT,
                cgpa_requirement TEXT,
                branches_allowed TEXT NOT NULL DEFAULT '[]',
                deadline TEXT,
                interview_date TEXT,
                oa_date TEXT,
                registration_link TEXT,
                work_location TEXT,
                hiring_process TEXT NOT NULL DEFAULT '[]',
                important_notes TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                source_email_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                update_type TEXT NOT NULL,
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT UNIQUE NOT NULL,
                opportunity_id INTEGER,
                subject TEXT NOT NULL,
                sender TEXT,
                received_at TEXT,
                filter_score INTEGER,
                filter_decision TEXT,
                processed_status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                channel TEXT NOT NULL,
                recipient TEXT,
                subject TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE SET NULL
            );
            """
        )
        self._migrate_existing_opportunities_table()
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_opportunities_status
                ON opportunities(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunities_unique_hash
                ON opportunities(unique_hash);
            CREATE INDEX IF NOT EXISTS idx_updates_opportunity_id
                ON updates(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_processed_emails_message_id
                ON processed_emails(gmail_message_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_opportunity_id
                ON notifications(opportunity_id);
            """
        )
        self.connection.commit()

    def insert_or_update_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None = None,
    ) -> tuple[int, bool]:
        """Insert a new opportunity or update an existing duplicate.

        Returns ``(opportunity_id, created_new_record)``.
        """
        normalized = self._normalize_opportunity(opportunity)
        existing = self.find_duplicate_opportunity(
            normalized["company_name"],
            normalized["role"],
        )

        if existing is None:
            opportunity_id = self._insert_opportunity(normalized, source_email_id=source_email_id)
            self.create_update_event(
                opportunity_id,
                "created",
                notes="Opportunity created from email",
            )
            logger.info("Inserted opportunity %s", opportunity_id)
            return opportunity_id, True

        opportunity_id = int(existing["id"])
        changed_fields = self._changed_fields(existing, normalized)

        if changed_fields:
            self.update_opportunity(
                opportunity_id,
                normalized,
                source_email_id=source_email_id,
                changed_fields=changed_fields,
            )
            logger.info(
                "Updated opportunity %s with %s changes",
                opportunity_id,
                len(changed_fields),
            )
        else:
            self.create_update_event(
                opportunity_id,
                "duplicate_seen",
                notes="Duplicate email without opportunity changes",
            )
            logger.info("Duplicate opportunity %s had no changes", opportunity_id)

        return opportunity_id, False

    def update_opportunity(
        self,
        opportunity_id: int,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None = None,
        changed_fields: list[tuple[str, Any, Any]] | None = None,
    ) -> None:
        """Update one opportunity and create update-history rows."""
        normalized = self._normalize_opportunity(opportunity)
        existing = self.fetch_opportunity_by_id(opportunity_id)
        if existing is None:
            msg = f"Opportunity {opportunity_id} was not found."
            raise ValueError(msg)

        changes = changed_fields or self._changed_fields_from_dict(existing, normalized)
        self._update_opportunity_row(
            opportunity_id,
            normalized,
            source_email_id=source_email_id,
        )

        for field_name, old_value, new_value in changes:
            self.create_update_event(
                opportunity_id,
                "updated",
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )

    def create_update_event(
        self,
        opportunity_id: int,
        update_type: str,
        *,
        field_name: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
        notes: str | None = None,
    ) -> int:
        """Create one row in the updates table."""
        cursor = self.connection.execute(
            """
            INSERT INTO updates (
                opportunity_id,
                update_type,
                field_name,
                old_value,
                new_value,
                notes,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                opportunity_id,
                update_type,
                field_name,
                _serialize_value(old_value),
                _serialize_value(new_value),
                notes,
                utc_now_iso(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def find_duplicate_opportunity(self, company_name: str, role: str) -> sqlite3.Row | None:
        """Fetch an opportunity by the generated company-role hash."""
        unique_hash = generate_unique_hash(company_name, role)
        return self.connection.execute(
            "SELECT * FROM opportunities WHERE unique_hash = ? LIMIT 1;",
            (unique_hash,),
        ).fetchone()

    def fetch_opportunity_by_id(self, opportunity_id: int) -> dict[str, Any] | None:
        """Fetch one opportunity by primary key."""
        row = self.connection.execute(
            "SELECT * FROM opportunities WHERE id = ?;",
            (opportunity_id,),
        ).fetchone()
        return self._row_to_opportunity(row) if row else None

    def fetch_active_opportunities(self) -> list[dict[str, Any]]:
        """Return active opportunities as dictionaries."""
        rows = self.connection.execute(
            """
            SELECT *
            FROM opportunities
            WHERE status = 'active'
            ORDER BY COALESCE(deadline, updated_at) ASC;
            """
        ).fetchall()
        return [self._row_to_opportunity(row) for row in rows]

    def fetch_updates_for_opportunity(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Fetch update history for one opportunity."""
        rows = self.connection.execute(
            """
            SELECT *
            FROM updates
            WHERE opportunity_id = ?
            ORDER BY created_at ASC, id ASC;
            """,
            (opportunity_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def log_processed_email(
        self,
        *,
        gmail_message_id: str,
        subject: str,
        sender: str | None = None,
        received_at: str | None = None,
        opportunity_id: int | None = None,
        filter_score: int | None = None,
        filter_decision: dict[str, Any] | None = None,
        processed_status: str = "processed",
        error_message: str | None = None,
    ) -> int:
        """Create or update a processed email record."""
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO processed_emails (
                gmail_message_id,
                opportunity_id,
                subject,
                sender,
                received_at,
                filter_score,
                filter_decision,
                processed_status,
                error_message,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                opportunity_id = excluded.opportunity_id,
                subject = excluded.subject,
                sender = excluded.sender,
                received_at = excluded.received_at,
                filter_score = excluded.filter_score,
                filter_decision = excluded.filter_decision,
                processed_status = excluded.processed_status,
                error_message = excluded.error_message,
                updated_at = excluded.updated_at;
            """,
            (
                gmail_message_id,
                opportunity_id,
                subject,
                sender,
                received_at,
                filter_score,
                _serialize_value(filter_decision),
                processed_status,
                error_message,
                now,
                now,
            ),
        )
        self.connection.commit()

        row = self.connection.execute(
            "SELECT id FROM processed_emails WHERE gmail_message_id = ?;",
            (gmail_message_id,),
        ).fetchone()
        return int(row["id"] if row else cursor.lastrowid)

    def create_notification(
        self,
        *,
        channel: str,
        message: str,
        opportunity_id: int | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        status: str = "pending",
        error_message: str | None = None,
        sent_at: str | None = None,
    ) -> int:
        """Create one notification tracking row."""
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO notifications (
                opportunity_id,
                channel,
                recipient,
                subject,
                message,
                status,
                error_message,
                sent_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                opportunity_id,
                channel,
                recipient,
                subject,
                message,
                status,
                error_message,
                sent_at,
                now,
                now,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def fetch_notifications(self, status: str | None = None) -> list[dict[str, Any]]:
        """Fetch notification rows, optionally filtered by status."""
        if status is None:
            rows = self.connection.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC, id DESC;"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM notifications
                WHERE status = ?
                ORDER BY created_at DESC, id DESC;
                """,
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_opportunities(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for fetch_active_opportunities."""
        return self.fetch_active_opportunities()

    def get_opportunity_events(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Backward-compatible alias for fetch_updates_for_opportunity."""
        return self.fetch_updates_for_opportunity(opportunity_id)

    def add_event(self, opportunity_id: int, event_type: str, **kwargs: Any) -> int:
        """Backward-compatible alias for create_update_event."""
        return self.create_update_event(opportunity_id, event_type, **kwargs)

    def log_email(self, **kwargs: Any) -> int:
        """Backward-compatible alias for log_processed_email."""
        return self.log_processed_email(**kwargs)

    def _insert_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None,
    ) -> int:
        now = utc_now_iso()
        values = {
            **opportunity,
            "unique_hash": generate_unique_hash(
                opportunity["company_name"],
                opportunity["role"],
            ),
            "source_email_id": source_email_id,
            "created_at": now,
            "updated_at": now,
        }
        cursor = self.connection.execute(
            """
            INSERT INTO opportunities (
                unique_hash,
                company_name,
                role,
                internship_or_fulltime,
                package_or_stipend,
                eligibility,
                cgpa_requirement,
                branches_allowed,
                deadline,
                interview_date,
                oa_date,
                registration_link,
                work_location,
                hiring_process,
                important_notes,
                source_email_id,
                created_at,
                updated_at
            )
            VALUES (
                :unique_hash,
                :company_name,
                :role,
                :internship_or_fulltime,
                :package_or_stipend,
                :eligibility,
                :cgpa_requirement,
                :branches_allowed,
                :deadline,
                :interview_date,
                :oa_date,
                :registration_link,
                :work_location,
                :hiring_process,
                :important_notes,
                :source_email_id,
                :created_at,
                :updated_at
            );
            """,
            values,
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def _migrate_existing_opportunities_table(self) -> None:
        """Add required columns when an older local SQLite file already exists."""
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(opportunities);").fetchall()
        }

        if "unique_hash" not in columns:
            logger.info("Migrating opportunities table: adding unique_hash column")
            self.connection.execute("ALTER TABLE opportunities ADD COLUMN unique_hash TEXT;")

        rows = self.connection.execute(
            """
            SELECT id, company_name, role
            FROM opportunities
            WHERE unique_hash IS NULL OR unique_hash = '';
            """
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE opportunities SET unique_hash = ? WHERE id = ?;",
                (generate_unique_hash(row["company_name"], row["role"]), row["id"]),
            )
        self.connection.commit()

    def _update_opportunity_row(
        self,
        opportunity_id: int,
        opportunity: dict[str, Any],
        *,
        source_email_id: str | None,
    ) -> None:
        values = {
            **opportunity,
            "id": opportunity_id,
            "unique_hash": generate_unique_hash(
                opportunity["company_name"],
                opportunity["role"],
            ),
            "source_email_id": source_email_id,
            "updated_at": utc_now_iso(),
        }
        self.connection.execute(
            """
            UPDATE opportunities
            SET unique_hash = :unique_hash,
                company_name = :company_name,
                role = :role,
                internship_or_fulltime = :internship_or_fulltime,
                package_or_stipend = :package_or_stipend,
                eligibility = :eligibility,
                cgpa_requirement = :cgpa_requirement,
                branches_allowed = :branches_allowed,
                deadline = :deadline,
                interview_date = :interview_date,
                oa_date = :oa_date,
                registration_link = :registration_link,
                work_location = :work_location,
                hiring_process = :hiring_process,
                important_notes = :important_notes,
                source_email_id = COALESCE(:source_email_id, source_email_id),
                updated_at = :updated_at
            WHERE id = :id;
            """,
            values,
        )
        self.connection.commit()

    def _normalize_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        company_name = _clean_required_text(opportunity.get("company_name"), "company_name")
        role = _clean_required_text(opportunity.get("role"), "role")
        normalized = {"company_name": company_name, "role": role}

        for field in OPPORTUNITY_FIELDS:
            if field in {"company_name", "role"}:
                continue

            value = opportunity.get(field)
            if field in JSON_FIELDS:
                normalized[field] = _serialize_value(_normalize_list(value))
            else:
                normalized[field] = _normalize_scalar(value)

        return normalized

    def _changed_fields(
        self,
        existing: sqlite3.Row,
        incoming: dict[str, Any],
    ) -> list[tuple[str, Any, Any]]:
        return self._changed_fields_from_dict(dict(existing), incoming)

    def _changed_fields_from_dict(
        self,
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> list[tuple[str, Any, Any]]:
        changes: list[tuple[str, Any, Any]] = []
        for field in OPPORTUNITY_FIELDS:
            if field in {"company_name", "role"}:
                continue

            old_value = existing[field]
            if field in JSON_FIELDS and isinstance(old_value, list):
                old_value = _serialize_value(old_value)

            new_value = incoming[field]
            if old_value != new_value:
                changes.append((field, old_value, new_value))

        return changes

    def _row_to_opportunity(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for field in JSON_FIELDS:
            data[field] = _deserialize_json_list(data[field])
        return data


def _normalize_key(value: str) -> str:
    """Normalize identity fields before hashing."""
    return " ".join(value.casefold().strip().split())


def _clean_required_text(value: Any, field_name: str) -> str:
    normalized = _normalize_scalar(value)
    if normalized is None:
        msg = f"{field_name} is required for opportunity storage."
        raise ValueError(msg)
    return normalized


def _normalize_scalar(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, list):
        value = ", ".join(str(item).strip() for item in value if str(item).strip())

    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
        return None
    return normalized


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none", "n/a", "na", "-"}:
        return []

    return [item.strip() for item in normalized.replace(";", ",").split(",") if item.strip()]


def _serialize_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return str(value)


def _deserialize_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if not isinstance(loaded, list):
        return [str(loaded)]
    return [str(item) for item in loaded]
