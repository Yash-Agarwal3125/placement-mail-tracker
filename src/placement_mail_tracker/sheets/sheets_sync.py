"""Google Sheets synchronization for placement drives.

Phase 6: Redesigned spreadsheet with comprehensive columns.
Phase 7: Gmail deep link generation.
Phase 8: Newest updates on top (sorted by last_updated DESC).
Phase 9: Active Opportunities sheet (excludes REJECTED/WITHDRAWN/EXPIRED/COMPLETED).
Phase 10: Dashboard sheet with metrics.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Protocol

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Phase 6: Redesigned column schema
ACTIVE_OPP_HEADERS = [
    "Received Date",
    "Company",
    "Drive ID",
    "Role",
    "Category",
    "Current Status",
    "Status History",
    "CTC",
    "Stipend",
    "Location",
    "Registration Deadline",
    "Next Event Date",
    "Action Required",
    "Eligibility Status",
    "My Status",
    "Priority",
    "Latest Update",
    "Open Email",
    "Last Updated",
]

COMPANY_HISTORY_HEADERS = [
    "Company",
    "Total Drives",
    "Selected",
    "Rejected",
    "Active",
    "Last Activity",
]

DASHBOARD_HEADERS = [
    "Metric",
    "Value",
]


class SheetsAuthenticationError(RuntimeError):
    """Raised when Google Sheets OAuth authentication cannot be completed."""


class SheetsValuesResource(Protocol):
    def get(self, **kwargs: Any) -> Any: ...
    def update(self, **kwargs: Any) -> Any: ...
    def append(self, **kwargs: Any) -> Any: ...
    def clear(self, **kwargs: Any) -> Any: ...


class GoogleSheetsSync:
    """Synchronize placement drives to Google Sheets."""

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
        self.last_error: str | None = None

    def sync_active_opportunities(self, database: DatabaseManager) -> dict[str, int]:
        """Sync all drives, companies, and dashboard to Google Sheets with resilience."""
        backoffs = [2, 5, 10]
        for attempt, backoff in enumerate(backoffs + [0]):
            try:
                return self._sync_active_opportunities_internal(database)
            except Exception as e:
                is_retryable = False
                import http.client
                import socket

                from googleapiclient.errors import HttpError
                
                if isinstance(e, HttpError):
                    if e.resp.status in {429, 500, 502, 503, 504}:
                        is_retryable = True
                elif isinstance(e, (socket.error, socket.timeout, http.client.HTTPException, ConnectionError, TimeoutError)):
                    is_retryable = True
                    
                if is_retryable and attempt < len(backoffs):
                    logger.warning("Google Sheets network error: %s. Retrying in %ss...", e, backoff)
                    import time
                    time.sleep(backoff)
                else:
                    # Non-retryable or out of retries
                    if isinstance(e, HttpError):
                        self.last_error = str(e)
                        if self.settings.is_production:
                            raise
                        logger.exception("Unable to sync Google Sheet: %s", e)
                        return {"created": 0, "updated": 0, "skipped": 0}
                    elif isinstance(e, SheetsAuthenticationError):
                        self.last_error = str(e)
                        if self.settings.is_production:
                            raise e
                        logger.warning("%s", e)
                        return {"created": 0, "updated": 0, "skipped": 0}
                    else:
                        raise

    def _sync_active_opportunities_internal(self, database: DatabaseManager) -> dict[str, int]:
        """Internal method to sync all drives, companies, and dashboard to Google Sheets."""
        if not self.settings.google_sheet_id:
            self.last_error = "GOOGLE_SHEET_ID is missing"
            if self.settings.is_production:
                raise SheetsAuthenticationError(self.last_error)
            logger.warning("GOOGLE_SHEET_ID is missing; skipping Google Sheets sync")
            return {"created": 0, "updated": 0, "skipped": 0}

        if True:
            self.last_error = None
            self._ensure_tabs_exist()

            # Phase 9: Active Opportunities (excludes terminal statuses)
            # Phase 5 (Eligibility Filter): Split by ELIGIBLE vs NOT_ELIGIBLE_*
            active_opps = database.fetch_active_drives_only()
            
            eligible_opps = [
                opp for opp in active_opps if opp.get("eligibility_status") == "ELIGIBLE"
            ]
            filtered_opps = [
                opp for opp in active_opps if opp.get("eligibility_status") != "ELIGIBLE"
            ]

            self._sync_tab_data(
                tab_name="Active Opportunities",
                headers=ACTIVE_OPP_HEADERS,
                data_rows=[opportunity_to_sheet_row(opp) for opp in eligible_opps],
                key_index=2,  # Drive ID
            )

            self._sync_tab_data(
                tab_name="Filtered Opportunities",
                headers=ACTIVE_OPP_HEADERS,
                data_rows=[opportunity_to_sheet_row(opp) for opp in filtered_opps],
                key_index=2,  # Drive ID
            )

            # Company History
            companies = database.get_company_history()
            self._sync_tab_data(
                tab_name="Company History",
                headers=COMPANY_HISTORY_HEADERS,
                data_rows=[company_to_sheet_row(comp) for comp in companies],
                key_index=0,  # Company Name
            )

            # Phase 10: Dashboard
            self._sync_dashboard(database)

            # Phase 6: Apply formatting
            self._apply_formatting()



        return {"created": len(active_opps), "updated": 0, "skipped": 0}

    def _ensure_tabs_exist(self) -> None:
        """Ensure all required tabs exist in the spreadsheet."""
        service = self._get_service()
        spreadsheet = (
            service.spreadsheets()
            .get(spreadsheetId=self.settings.google_sheet_id)
            .execute()
        )
        existing_tabs = [
            s.get("properties", {}).get("title")
            for s in spreadsheet.get("sheets", [])
        ]

        required_tabs = [
            "Active Opportunities",
            "Filtered Opportunities",
            "Company History",
            "Dashboard",
        ]
        requests = []
        for tab in required_tabs:
            if tab not in existing_tabs:
                requests.append({"addSheet": {"properties": {"title": tab}}})

        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={"requests": requests},
            ).execute()

    def _sync_tab_data(
        self,
        tab_name: str,
        headers: list[str],
        data_rows: list[list[str]],
        key_index: int,
    ) -> None:
        """Sync rows to a specific tab using a key index for deduplication."""
        values = self._values()

        # Ensure header
        response = values.get(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{_quote(tab_name)}!A1:Z1",
        ).execute()
        existing_header = (
            response.get("values", [[]])[0] if response.get("values") else []
        )

        if existing_header != headers:
            values.update(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{_quote(tab_name)}!A1:Z1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()

        # Get existing rows
        response = values.get(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{_quote(tab_name)}!A:Z",
        ).execute()
        existing_rows = response.get("values", [])

        existing_by_key: dict[str, int] = {}
        for row_number, row in enumerate(existing_rows[1:], start=2):
            if len(row) > key_index:
                key = row[key_index].strip()
                if key:
                    existing_by_key[key] = row_number

        rows_to_append = []
        update_data = []
        for row in data_rows:
            if len(row) > key_index:
                key = row[key_index].strip()
                existing_row_number = existing_by_key.get(key)
                if existing_row_number:
                    update_data.append({
                        "range": f"{_quote(tab_name)}!A{existing_row_number}:Z{existing_row_number}",
                        "values": [row]
                    })
                else:
                    rows_to_append.append(row)

        if update_data:
            values.batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": update_data
                }
            ).execute()

        if rows_to_append:
            values.append(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{_quote(tab_name)}!A:Z",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows_to_append},
            ).execute()

    def _sync_dashboard(self, database: DatabaseManager) -> None:
        """Phase 10: Compute and sync static dashboard metrics."""
        metrics = database.get_dashboard_metrics()

        dashboard_data = [
            ["Active Drives", str(metrics["active_drives"])],
            ["Applications Open", str(metrics["applications_open"])],
            ["OA This Week", str(metrics["oa_this_week"])],
            ["Interviews This Week", str(metrics["interviews_this_week"])],
            ["Offers Received", str(metrics["offers_received"])],
            ["Companies Applied", str(metrics["companies_applied"])],
            ["Selection Rate", metrics["selection_rate"]],
            ["Total Drives", str(metrics["total_drives"])],
        ]

        values = self._values()

        values.clear(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{_quote('Dashboard')}!A:B",
        ).execute()

        values.update(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{_quote('Dashboard')}!A1:B{len(dashboard_data) + 1}",
            valueInputOption="USER_ENTERED",
            body={"values": [DASHBOARD_HEADERS] + dashboard_data},
        ).execute()

    def _apply_formatting(self) -> None:
        """Phase 6: Apply freeze header, conditional formatting, auto-sort."""
        service = self._get_service()
        spreadsheet = (
            service.spreadsheets()
            .get(spreadsheetId=self.settings.google_sheet_id)
            .execute()
        )

        sheet_ids = {}
        for sheet in spreadsheet.get("sheets", []):
            title = sheet.get("properties", {}).get("title")
            sheet_ids[title] = sheet.get("properties", {}).get("sheetId")

        requests: list[dict[str, Any]] = []

        for _tab_name, sheet_id in sheet_ids.items():
            if sheet_id is None:
                continue

            # Phase 6: Freeze header row
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            })

            # Phase 6: Auto-filter
            requests.append({
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                        }
                    }
                }
            })

        # Phase 8: Sort Active Opportunities by Last Updated DESC
        active_sheet_id = sheet_ids.get("Active Opportunities")
        if active_sheet_id is not None:
            last_updated_col = ACTIVE_OPP_HEADERS.index("Last Updated")
            requests.append({
                "sortRange": {
                    "range": {
                        "sheetId": active_sheet_id,
                        "startRowIndex": 1,
                    },
                    "sortSpecs": [
                        {"dimensionIndex": last_updated_col, "sortOrder": "DESCENDING"}
                    ],
                }
            })

            # Conditional formatting for Current Status
            status_col = ACTIVE_OPP_HEADERS.index("Current Status")
            colors = {
                "OPEN": {"red": 0.85, "green": 0.92, "blue": 1.0},
                "OA": {"red": 1.0, "green": 0.95, "blue": 0.8},
                "SHORTLISTED": {"red": 0.8, "green": 0.9, "blue": 1.0},
                "INTERVIEW": {"red": 1.0, "green": 0.85, "blue": 0.7},
                "HR": {"red": 0.95, "green": 0.85, "blue": 0.95},
                "SELECTED": {"red": 0.7, "green": 1.0, "blue": 0.7},
                "OFFER_RECEIVED": {"red": 0.7, "green": 1.0, "blue": 0.7},
            }

            for status, color in colors.items():
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": active_sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": status_col,
                                "endColumnIndex": status_col + 1,
                            }],
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
                })

            # Phase 6: Alternating row colors
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": active_sheet_id,
                            "startRowIndex": 1,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": "=ISEVEN(ROW())"}],
                            },
                            "format": {
                                "backgroundColor": {
                                    "red": 0.96,
                                    "green": 0.96,
                                    "blue": 0.96,
                                }
                            },
                        },
                    },
                    "index": 0,
                }
            })

        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={"requests": requests},
            ).execute()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

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
                f"Google Sheets credentials file not found at {self.credentials_path}. "
                "Add OAuth client credentials before syncing."
            )
            raise SheetsAuthenticationError(msg)

        logger.info("Starting Google Sheets OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path), SHEETS_SCOPES
        )
        credentials = flow.run_local_server(port=0, timeout_seconds=120)
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
            return Credentials.from_authorized_user_file(
                str(self.token_path), SHEETS_SCOPES
            )
        except Exception as error:
            logger.warning("Corrupted Sheets token file: %s. Deleting.", error)
            try:
                self.token_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _save_token(self, credentials: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass


# ------------------------------------------------------------------
# Row Builders
# ------------------------------------------------------------------


def opportunity_to_sheet_row(opportunity: dict[str, Any]) -> list[str]:
    """Convert one drive dictionary to a Phase 6 spreadsheet row.

    Phase 7: Gmail deep link generation.
    """
    # Phase 7: Generate Gmail deep link
    email_link = ""
    msg_id = opportunity.get("source_email_id") or opportunity.get("source_message_id")
    thread_id = opportunity.get("source_thread_id")
    link_target = thread_id or msg_id
    if link_target:
        email_link = (
            f'=HYPERLINK("https://mail.google.com/mail/u/0/#inbox/{link_target}", '
            '"Open Email")'
        )

    # Determine CTC vs Stipend
    pkg = _cell(opportunity.get("package_or_stipend"))
    category = (opportunity.get("internship_or_fulltime") or "").lower()
    ctc = pkg if "full" in category or "fte" in category else ""
    stipend = pkg if "intern" in category else ""
    if not ctc and not stipend:
        ctc = pkg  # default to CTC column

    return [
        _cell(opportunity.get("email_received_at")),
        _cell(opportunity.get("company_name")),
        _cell(opportunity.get("drive_id")),
        _cell(opportunity.get("role")),
        _cell(opportunity.get("internship_or_fulltime")),
        _cell(opportunity.get("current_status")),
        _cell(opportunity.get("status_history")),
        ctc,
        stipend,
        _cell(opportunity.get("work_location")),
        _cell(opportunity.get("deadline")),
        _cell(opportunity.get("next_event_date")),
        _cell(opportunity.get("action_required")),
        _cell(opportunity.get("eligibility_status", "MANUAL_REVIEW")),
        _cell(opportunity.get("my_status", "NOT_APPLIED")),
        "",  # Priority (manual)
        _cell(opportunity.get("last_update_timestamp", utc_now_iso())),
        email_link,
        _cell(opportunity.get("updated_at")),
    ]


def company_to_sheet_row(company: dict[str, Any]) -> list[str]:
    """Convert company record to Sheets row."""
    return [
        _cell(company.get("name")),
        _cell(company.get("total_drives")),
        _cell(company.get("selected_drives")),
        _cell(company.get("rejected_drives")),
        _cell(company.get("active_drives")),
        _cell(company.get("last_activity")),
    ]


def _quote(sheet_name: str) -> str:
    """Quote sheet names for A1 notation."""
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()
