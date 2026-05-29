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

ACTIVE_OPP_HEADERS = [
    "Received Date",
    "Company",
    "Drive ID",
    "Role",
    "Current Status",
    "Status History",
    "Package",
    "Location",
    "OA Date",
    "Interview Date",
    "Action Required",
    "Priority",
    "Latest Update",
    "Open Email"
]

COMPANY_HISTORY_HEADERS = [
    "Company",
    "Total Drives",
    "Selected",
    "Rejected",
    "Active",
    "Last Activity"
]

DASHBOARD_HEADERS = [
    "Metric",
    "Value"
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
        
    def clear(self, **kwargs: Any) -> Any:
        """Clear values."""


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
        self._service = service

    def sync_active_opportunities(self, database: DatabaseManager) -> dict[str, int]:
        """Sync all active opportunities, companies, and dashboard to Google Sheets."""
        if not self.settings.google_sheet_id:
            logger.warning("GOOGLE_SHEET_ID is missing; skipping Google Sheets sync")
            return {"created": 0, "updated": 0, "skipped": 0}

        try:
            self._ensure_tabs_exist()
            
            # Sync Active Opportunities
            opportunities = database.get_active_opportunities()
            active_opps = [opp for opp in opportunities if opp.get("current_status") not in ("REJECTED", "WITHDRAWN")]
            self._sync_tab_data(
                tab_name="Active Opportunities",
                headers=ACTIVE_OPP_HEADERS,
                data_rows=[opportunity_to_sheet_row(opp) for opp in active_opps],
                key_index=2  # Drive ID
            )
            
            # Sync Company History
            companies = database.get_company_history()
            self._sync_tab_data(
                tab_name="Company History",
                headers=COMPANY_HISTORY_HEADERS,
                data_rows=[company_to_sheet_row(comp) for comp in companies],
                key_index=0  # Company Name
            )
            
            # Sync Dashboard
            self._sync_dashboard(database)
            
            # Apply formatting
            self._apply_dashboard_formatting()
            
        except SheetsAuthenticationError as error:
            logger.warning("%s", error)
            return {"created": 0, "updated": 0, "skipped": 0}
        except HttpError as error:
            logger.exception("Unable to prepare Google Sheet for sync: %s", error)
            return {"created": 0, "updated": 0, "skipped": 0}

        return {"created": len(active_opps), "updated": 0, "skipped": 0}
        
    def _ensure_tabs_exist(self) -> None:
        """Ensure all required tabs exist in the spreadsheet."""
        service = self._get_service()
        spreadsheet = service.spreadsheets().get(spreadsheetId=self.settings.google_sheet_id).execute()
        existing_tabs = [s.get("properties", {}).get("title") for s in spreadsheet.get("sheets", [])]
        
        required_tabs = ["Active Opportunities", "Company History", "Dashboard"]
        requests = []
        for tab in required_tabs:
            if tab not in existing_tabs:
                requests.append({"addSheet": {"properties": {"title": tab}}})
                
        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=self.settings.google_sheet_id, 
                body={"requests": requests}
            ).execute()
            
    def _sync_tab_data(self, tab_name: str, headers: list[str], data_rows: list[list[str]], key_index: int) -> None:
        """Sync a list of rows to a specific tab using a key index for deduplication."""
        values = self._values()
        
        # Ensure Header
        response = values.get(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{quote_sheet_name(tab_name)}!A1:Z1",
        ).execute()
        existing_header = response.get("values", [[]])[0] if response.get("values") else []
        
        if existing_header != headers:
            values.update(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{quote_sheet_name(tab_name)}!A1:Z1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
            
        # Get existing rows
        response = values.get(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{quote_sheet_name(tab_name)}!A:Z",
        ).execute()
        existing_rows = response.get("values", [])
        
        existing_by_key = {}
        for row_number, row in enumerate(existing_rows[1:], start=2):
            if len(row) > key_index:
                key = row[key_index].strip()
                if key:
                    existing_by_key[key] = row_number
                    
        rows_to_append = []
        for row in data_rows:
            if len(row) > key_index:
                key = row[key_index].strip()
                existing_row_number = existing_by_key.get(key)
                if existing_row_number:
                    values.update(
                        spreadsheetId=self.settings.google_sheet_id,
                        range=f"{quote_sheet_name(tab_name)}!A{existing_row_number}:Z{existing_row_number}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [row]},
                    ).execute()
                else:
                    rows_to_append.append(row)
                    
        if rows_to_append:
            values.append(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{quote_sheet_name(tab_name)}!A:Z",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows_to_append},
            ).execute()
            
    def _sync_dashboard(self, database: DatabaseManager) -> None:
        """Compute and sync static dashboard metrics."""
        companies = database.get_company_history()
        opportunities = database.get_active_opportunities()
        
        total_drives = sum(c.get("total_drives", 0) for c in companies)
        active_drives = sum(1 for o in opportunities if o.get("current_status") not in ("REJECTED", "WITHDRAWN"))
        offers = sum(1 for o in opportunities if o.get("current_status") == "OFFER_RECEIVED")
        interviews = sum(1 for o in opportunities if o.get("current_status") == "INTERVIEW")
        oas = sum(1 for o in opportunities if o.get("current_status") == "OA")
        
        selection_rate = f"{(offers / total_drives * 100):.1f}%" if total_drives > 0 else "0%"
        
        dashboard_data = [
            ["Total Drives", str(total_drives)],
            ["Active Opportunities", str(active_drives)],
            ["Pending OAs", str(oas)],
            ["Pending Interviews", str(interviews)],
            ["Offers Received", str(offers)],
            ["Selection Rate", selection_rate],
            ["Companies Applied", str(len(companies))]
        ]
        
        values = self._values()
        
        # Clear existing
        values.clear(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{quote_sheet_name('Dashboard')}!A:B",
        ).execute()
        
        # Write new
        values.update(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{quote_sheet_name('Dashboard')}!A1:B{len(dashboard_data)+1}",
            valueInputOption="USER_ENTERED",
            body={"values": [DASHBOARD_HEADERS] + dashboard_data},
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

    def _load_token(self) -> Credentials | None:
        if not self.token_path.exists():
            return None

        try:
            return Credentials.from_authorized_user_file(str(self.token_path), SHEETS_SCOPES)
        except Exception as error:
            logger.warning(
                "Corrupted or invalid Google Sheets token file found: %s. Auto-deleting file.",
                error,
            )
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

    def _apply_dashboard_formatting(self) -> None:
        """Apply dashboard conditional formatting and automatic sorting."""
        service = self._get_service()
        spreadsheet = (
            service.spreadsheets().get(spreadsheetId=self.settings.google_sheet_id).execute()
        )

        active_opp_sheet_id = None
        for sheet in spreadsheet.get("sheets", []):
            if sheet.get("properties", {}).get("title") == "Active Opportunities":
                active_opp_sheet_id = sheet.get("properties", {}).get("sheetId")
                break

        if active_opp_sheet_id is None:
            return

        requests: list[dict[str, Any]] = []

        # Sort Active Opportunities by Latest Update (Index 12) DESCENDING
        requests.append(
            {
                "sortRange": {
                    "range": {
                        "sheetId": active_opp_sheet_id,
                        "startRowIndex": 1,
                    },
                    "sortSpecs": [{"dimensionIndex": 12, "sortOrder": "DESCENDING"}],
                }
            }
        )

        # Clear existing conditional formatting rules
        sheet_obj = next((s for s in spreadsheet.get("sheets", []) if s.get("properties", {}).get("sheetId") == active_opp_sheet_id), None)
        if sheet_obj:
            existing_rules = sheet_obj.get("conditionalFormats", [])
            for i in reversed(range(len(existing_rules))):
                requests.append({"deleteConditionalFormatRule": {"index": i, "sheetId": active_opp_sheet_id}})

        # Conditional formatting for Current Status (Column E, index 4)
        colors = {
            "OA": {"red": 1.0, "green": 0.95, "blue": 0.8},
            "SHORTLISTED": {"red": 0.8, "green": 0.9, "blue": 1.0},
            "INTERVIEW": {"red": 1.0, "green": 0.85, "blue": 0.7},
            "SELECTED": {"red": 0.8, "green": 1.0, "blue": 0.8},
            "OFFER_RECEIVED": {"red": 0.8, "green": 1.0, "blue": 0.8},
        }

        for status, color in colors.items():
            requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": active_opp_sheet_id,
                                    "startRowIndex": 1,
                                    "startColumnIndex": 4,
                                    "endColumnIndex": 5,
                                }
                            ],
                            "booleanRule": {
                                "condition": {
                                    "type": "TEXT_EQ",
                                    "values": [{"userEnteredValue": status}],
                                },
                                "format": {"backgroundColor": color},
                            },
                        },
                        "index": 0,
                    }
                }
            )

        service.spreadsheets().batchUpdate(
            spreadsheetId=self.settings.google_sheet_id, body={"requests": requests}
        ).execute()


def opportunity_to_sheet_row(opportunity: dict[str, Any]) -> list[str]:
    """Convert one opportunity dictionary to a Sheets row."""
    email_link = ""
    if opportunity.get("source_message_id") or opportunity.get("source_email_id"):
        msg_id = opportunity.get("source_message_id") or opportunity.get("source_email_id")
        email_link = f'=HYPERLINK("https://mail.google.com/mail/u/0/#inbox/{msg_id}", "Open Email")'

    return [
        _cell(opportunity.get("email_received_at")),
        _cell(opportunity.get("company_name")),
        _cell(opportunity.get("drive_id")),
        _cell(opportunity.get("role")),
        _cell(opportunity.get("current_status")),
        _cell(opportunity.get("status_history")),
        _cell(opportunity.get("package_or_stipend")),
        _cell(opportunity.get("work_location")),
        _cell(opportunity.get("oa_date")),
        _cell(opportunity.get("interview_date")),
        _cell(opportunity.get("action_required")),
        "",  # Priority (placeholder)
        _cell(opportunity.get("last_update_timestamp", utc_now_iso())),
        email_link,
    ]


def company_to_sheet_row(company: dict[str, Any]) -> list[str]:
    """Convert company record to Sheets row."""
    return [
        _cell(company.get("name")),
        _cell(company.get("total_drives")),
        _cell(company.get("selected_drives")),
        _cell(company.get("rejected_drives")),
        _cell(company.get("active_drives")),
        _cell(company.get("last_activity"))
    ]


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
