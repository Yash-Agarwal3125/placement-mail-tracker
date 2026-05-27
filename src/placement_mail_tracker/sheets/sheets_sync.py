"""Google Sheets synchronization for placement opportunities."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_HEADERS = [
    "sync_key",
    "opportunity_id",
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
    "status",
    "source_email_id",
    "created_at",
    "updated_at",
    "synced_at",
]


class SheetsAuthenticationError(RuntimeError):
    """Raised when Google Sheets OAuth authentication cannot be completed."""


class SheetsValuesResource(Protocol):
    """Small protocol for the Google Sheets values API."""

    def get(self, **kwargs: Any) -> Any:
        """Get a values range."""

    def update(self, **kwargs: Any) -> Any:
        """Update a values range."""

    def append(self, **kwargs: Any) -> Any:
        """Append values."""


class GoogleSheetsSync:
    """Synchronize placement opportunities to Google Sheets."""

    def __init__(
        self,
        settings: Settings,
        *,
        service: Resource | None = None,
    ) -> None:
        self.settings = settings
        self.credentials_path = Path(settings.google_sheets_credentials_file)
        self.token_path = Path(settings.google_sheets_token_file)
        self.sheet_name = settings.google_sheet_name
        self._service = service

    def sync_active_opportunities(self, database: DatabaseManager) -> dict[str, int]:
        """Sync all active opportunities from SQLite to Google Sheets."""
        opportunities = database.get_active_opportunities()
        return self.sync_opportunities(opportunities)

    def sync_opportunities(self, opportunities: list[dict[str, Any]]) -> dict[str, int]:
        """Insert or update opportunity rows in Google Sheets."""
        if not self.settings.google_sheet_id:
            logger.warning("GOOGLE_SHEET_ID is missing; skipping Google Sheets sync")
            return {"created": 0, "updated": 0, "skipped": len(opportunities)}

        try:
            self.ensure_header_row()
            existing_rows = self._get_existing_rows()
        except SheetsAuthenticationError as error:
            logger.warning("%s", error)
            return {"created": 0, "updated": 0, "skipped": len(opportunities)}
        except HttpError as error:
            logger.exception("Unable to prepare Google Sheet for sync: %s", error)
            return {"created": 0, "updated": 0, "skipped": len(opportunities)}

        existing_by_key = build_existing_row_index(existing_rows)
        rows_to_append: list[list[str]] = []
        created = 0
        updated = 0

        for opportunity in opportunities:
            row = opportunity_to_sheet_row(opportunity)
            sync_key = row[0]
            existing_row_number = existing_by_key.get(sync_key)

            if existing_row_number is None:
                rows_to_append.append(row)
                created += 1
                continue

            self._update_row(existing_row_number, row)
            updated += 1

        if rows_to_append:
            self._append_rows(rows_to_append)

        logger.info(
            "Google Sheets sync complete: created=%s updated=%s skipped=0",
            created,
            updated,
        )
        return {"created": created, "updated": updated, "skipped": 0}

    def ensure_header_row(self) -> None:
        """Create or repair the first row with expected headers."""
        values = self._values()
        response = (
            values.get(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{quote_sheet_name(self.sheet_name)}!A1:U1",
            )
            .execute()
        )
        existing_header = response.get("values", [[]])[0] if response.get("values") else []

        if existing_header == SHEET_HEADERS:
            return

        logger.info("Writing Google Sheets header row")
        values.update(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{quote_sheet_name(self.sheet_name)}!A1:U1",
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]},
        ).execute()

    def authenticate(self) -> Credentials:
        """Load, refresh, or create OAuth2 credentials for Google Sheets."""
        credentials = self._load_token()

        if credentials and credentials.valid:
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired Google Sheets OAuth token")
            credentials.refresh(Request())
            self._save_token(credentials)
            return credentials

        if not self.credentials_path.exists():
            msg = (
                "Google Sheets credentials file was not found at "
                f"{self.credentials_path}. Add OAuth client credentials before syncing Sheets."
            )
            raise SheetsAuthenticationError(msg)

        logger.info("Starting Google Sheets OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            SHEETS_SCOPES,
        )
        credentials = flow.run_local_server(port=0)
        self._save_token(credentials)
        return credentials

    def _values(self) -> SheetsValuesResource:
        return self._get_service().spreadsheets().values()

    def _get_service(self) -> Resource:
        if self._service is None:
            credentials = self.authenticate()
            self._service = build("sheets", "v4", credentials=credentials)
        return self._service

    def _get_existing_rows(self) -> list[list[str]]:
        response = (
            self._values()
            .get(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{quote_sheet_name(self.sheet_name)}!A:U",
            )
            .execute()
        )
        return response.get("values", [])

    def _update_row(self, row_number: int, row: list[str]) -> None:
        self._values().update(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{quote_sheet_name(self.sheet_name)}!A{row_number}:U{row_number}",
            valueInputOption="RAW",
            body={"values": [row]},
        ).execute()

    def _append_rows(self, rows: list[list[str]]) -> None:
        self._values().append(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{quote_sheet_name(self.sheet_name)}!A:U",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def _load_token(self) -> Credentials | None:
        if not self.token_path.exists():
            return None

        try:
            return Credentials.from_authorized_user_file(str(self.token_path), SHEETS_SCOPES)
        except Exception as error:
            logger.warning("Corrupted or invalid Google Sheets token file found: %s. Auto-deleting file.", error)
            try:
                self.token_path.unlink(missing_ok=True)
            except OSError as unlink_error:
                logger.error("Failed to delete corrupted token file: %s", unlink_error)
            return None

    def _save_token(self, credentials: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")

        try:
            os.chmod(self.token_path, 0o600)
        except OSError as error:
            logger.debug("Could not update Sheets token file permissions: %s", error)


def opportunity_to_sheet_row(opportunity: dict[str, Any]) -> list[str]:
    """Convert one opportunity dictionary to a Sheets row."""
    sync_key = build_sync_key(opportunity)
    return [
        sync_key,
        _cell(opportunity.get("id")),
        _cell(opportunity.get("company_name")),
        _cell(opportunity.get("role")),
        _cell(opportunity.get("internship_or_fulltime")),
        _cell(opportunity.get("package_or_stipend")),
        _cell(opportunity.get("eligibility")),
        _cell(opportunity.get("cgpa_requirement")),
        _cell(opportunity.get("branches_allowed")),
        _cell(opportunity.get("deadline")),
        _cell(opportunity.get("interview_date")),
        _cell(opportunity.get("oa_date")),
        _cell(opportunity.get("registration_link")),
        _cell(opportunity.get("work_location")),
        _cell(opportunity.get("hiring_process")),
        _cell(opportunity.get("important_notes")),
        _cell(opportunity.get("status", "active")),
        _cell(opportunity.get("source_email_id")),
        _cell(opportunity.get("created_at")),
        _cell(opportunity.get("updated_at")),
        utc_now_iso(),
    ]


def build_existing_row_index(rows: list[list[str]]) -> dict[str, int]:
    """Map existing sheet sync keys to 1-based row numbers."""
    index: dict[str, int] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if not row:
            continue

        sync_key = row[0].strip() if len(row) > 0 else ""
        if not sync_key:
            company_name = row[2] if len(row) > 2 else ""
            role = row[3] if len(row) > 3 else ""
            sync_key = build_company_role_key(company_name, role)

        if sync_key:
            index[sync_key] = row_number

    return index


def build_sync_key(opportunity: dict[str, Any]) -> str:
    """Build a stable key for duplicate-safe sheet sync."""
    opportunity_id = opportunity.get("id")
    if opportunity_id not in {None, ""}:
        return f"opportunity:{opportunity_id}"
    return build_company_role_key(
        str(opportunity.get("company_name", "")),
        str(opportunity.get("role", "")),
    )


def build_company_role_key(company_name: str, role: str) -> str:
    """Build a fallback key for rows without an opportunity ID."""
    return f"company-role:{_slug(company_name)}:{_slug(role)}"


def quote_sheet_name(sheet_name: str) -> str:
    """Quote sheet names so spaces and punctuation work in A1 ranges."""
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
