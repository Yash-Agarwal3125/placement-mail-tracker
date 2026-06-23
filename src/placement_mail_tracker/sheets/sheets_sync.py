"""Google Sheets synchronization for placement drives.

Phase 6: Redesigned spreadsheet with comprehensive columns.
Phase 7: Gmail deep link generation.
Phase 8: Newest updates on top (sorted by last_updated DESC).
Phase 9: Active Opportunities sheet (excludes REJECTED/WITHDRAWN/EXPIRED/COMPLETED).
Phase 10: Dashboard sheet with metrics.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.utils.time import parse_datetime_flexible

logger = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Decision-first, human-readable column schema. Most actionable fields are
# leftmost; the internal Drive ID is kept last because the sync uses it as the
# row dedupe key. Update ACTIVE_KEY_INDEX if the Drive ID column moves.
ACTIVE_OPP_HEADERS = [
    "Company",
    "Role",
    "Type",
    "Degree",
    "Category",
    "Status",
    "Priority",
    "Action Required",
    "Deadline",
    "Days Left",
    "Next Event",
    "Package",
    "Location",
    "CGPA Cutoff",
    "Branches",
    "Eligibility",
    "My Status",
    "Apply Link",
    "Email",
    "History",
    "Last Updated",
    "Drive ID",
]

# Column the sync uses to match/update existing rows (Drive ID, last column).
ACTIVE_KEY_INDEX = ACTIVE_OPP_HEADERS.index("Drive ID")

# Columns the user fills in by hand; the sync must NOT overwrite their edits.
ACTIVE_USER_COLUMNS = [ACTIVE_OPP_HEADERS.index("My Status")]

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
        for attempt in range(1, 4):
            try:
                return self._sync_active_opportunities_internal(database)
            except Exception as e:
                is_retryable = False
                if isinstance(e, HttpError) and e.resp.status in {429, 500, 502, 503, 504}:
                    is_retryable = True
                elif isinstance(
                    e,
                    (socket.error, socket.timeout, http.client.HTTPException,
                     ConnectionError, TimeoutError),
                ):
                    is_retryable = True

                if is_retryable and attempt < 3:
                    backoff = 2 if attempt == 1 else 5
                    logger.warning(
                        "Retry attempt %s. Backoff %ss. Exception: %s (%s)",
                        attempt, backoff, type(e).__name__, e,
                    )
                    time.sleep(backoff)
                else:
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

        self.last_error = None
        self._ensure_tabs_exist()

        # Phase 9: Active Opportunities (excludes terminal statuses)
        # Phase 5 (Eligibility Filter): Split by ELIGIBLE vs NOT_ELIGIBLE_*
        active_opps = database.fetch_active_drives_only()

        eligible_opps = [
            opp for opp in active_opps
            if "NOT_ELIGIBLE" not in (opp.get("eligibility_status") or "")
        ]
        filtered_opps = [
            opp for opp in active_opps
            if "NOT_ELIGIBLE" in (opp.get("eligibility_status") or "")
        ]

        self._sync_tab_data(
            tab_name="Active Opportunities",
            headers=ACTIVE_OPP_HEADERS,
            data_rows=[opportunity_to_sheet_row(opp) for opp in eligible_opps],
            key_index=ACTIVE_KEY_INDEX,  # Drive ID
            user_columns=ACTIVE_USER_COLUMNS,
        )

        self._sync_tab_data(
            tab_name="Filtered Opportunities",
            headers=ACTIVE_OPP_HEADERS,
            data_rows=[opportunity_to_sheet_row(opp) for opp in filtered_opps],
            key_index=ACTIVE_KEY_INDEX,  # Drive ID
            user_columns=ACTIVE_USER_COLUMNS,
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
        user_columns: list[int] | None = None,
    ) -> None:
        """Sync rows to a tab, matching existing rows by ``key_index``.

        ``user_columns`` lists column indices the user edits by hand (e.g. "My
        Status"); for matched rows their existing sheet value is preserved so a
        sync never clobbers the user's own tracking.
        """
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
            # A header change means the column layout changed. Wipe stale data
            # rows (written in the old order) so the tab rebuilds cleanly instead
            # of leaving misaligned rows the key-based dedupe can't match.
            if existing_header:
                logger.info("[SYNC] Layout change on %s; clearing stale rows", tab_name)
                values.clear(
                    spreadsheetId=self.settings.google_sheet_id,
                    range=f"{_quote(tab_name)}!A2:Z",
                ).execute()

        # Get existing rows
        # We only need to fetch columns up to the key_index for deduplication
        end_col = chr(ord('A') + key_index)
        logger.info("[SYNC] Starting sheet read for %s range A:%s", tab_name, end_col)
        start_time = time.time()

        response = values.get(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{_quote(tab_name)}!A:{end_col}",
        ).execute()
        
        duration = time.time() - start_time
        existing_rows = response.get("values", [])
        logger.info(
            "[SYNC] Finished sheet read for %s. Fetched %s rows in %.2fs",
            tab_name, len(existing_rows), duration,
        )

        existing_by_key: dict[str, int] = {}
        existing_row_by_key: dict[str, list[str]] = {}
        for row_number, row in enumerate(existing_rows[1:], start=2):
            if len(row) > key_index:
                key = row[key_index].strip()
                if key:
                    existing_by_key[key] = row_number
                    existing_row_by_key[key] = row

        rows_to_append = []
        update_data = []
        for row in data_rows:
            if len(row) > key_index:
                key = row[key_index].strip()
                existing_row_number = existing_by_key.get(key)
                if existing_row_number:
                    if user_columns:
                        _preserve_user_columns(
                            row, existing_row_by_key.get(key, []), user_columns
                        )
                    row_range = (
                        f"{_quote(tab_name)}!A{existing_row_number}:Z{existing_row_number}"
                    )
                    update_data.append({
                        "range": row_range,
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

        # Keep the tab a faithful mirror of the live drive set: remove rows whose
        # key (Drive ID) is no longer among the drives we just synced. This drops
        # rows for drives that became terminal (rejected/expired) instead of
        # letting stale rows accumulate. Guarded so an empty sync never wipes the
        # tab (e.g. a transient DB hiccup returning no rows).
        if not data_rows:
            return

        current_keys = {
            row[key_index].strip()
            for row in data_rows
            if len(row) > key_index and row[key_index].strip()
        }
        rows_to_delete = [
            row_number
            for key, row_number in existing_by_key.items()
            if key not in current_keys
        ]
        if not rows_to_delete:
            return

        service = self._get_service()
        sheet_metadata = (
            service.spreadsheets()
            .get(spreadsheetId=self.settings.google_sheet_id)
            .execute()
        )
        sheet_id = next(
            (
                s["properties"]["sheetId"]
                for s in sheet_metadata.get("sheets", [])
                if s["properties"]["title"] == tab_name
            ),
            None,
        )
        if sheet_id is None:
            return

        # Delete bottom-up (0-based row indices) so earlier indices stay valid.
        delete_requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_number - 1,
                        "endIndex": row_number,
                    }
                }
            }
            for row_number in sorted(rows_to_delete, reverse=True)
        ]
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.settings.google_sheet_id,
            body={"requests": delete_requests},
        ).execute()
        logger.info("[SYNC] Removed %d stale rows from %s", len(delete_requests), tab_name)

    def _sync_dashboard(self, database: DatabaseManager) -> None:
        """Phase 10: Compute and sync static dashboard metrics."""
        metrics = database.get_dashboard_metrics()

        dashboard_data = [
            ["Active Drives", str(metrics["active_drives"])],
            ["Applications Open", str(metrics["applications_open"])],
            ["Action Required", str(metrics.get("action_required", 0))],
            ["Deadlines This Week", str(metrics.get("deadlines_this_week", 0))],
            ["OA This Week", str(metrics["oa_this_week"])],
            ["Interviews This Week", str(metrics["interviews_this_week"])],
            ["Offers Received", str(metrics["offers_received"])],
            ["Companies Applied", str(metrics["companies_applied"])],
            ["Selection Rate", metrics["selection_rate"]],
            ["Total Drives", str(metrics["total_drives"])],
        ]

        values = self._values()

        start_time = time.time()

        # We skip the clear() call to avoid temporary blank screens and reduce API calls.
        # Since the dashboard metrics are fixed in number (8 metrics), updating
        # the fixed range will cleanly overwrite the old values without leaving ghost rows.
        values.update(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{_quote('Dashboard')}!A1:B{len(dashboard_data) + 1}",
            valueInputOption="USER_ENTERED",
            body={"values": [DASHBOARD_HEADERS] + dashboard_data},
        ).execute()
        
        duration = time.time() - start_time
        logger.info("[SYNC] Finished dashboard update in %.2fs", duration)

    def _apply_formatting(self) -> None:
        """Phase 6: Apply freeze header, conditional formatting, auto-sort.

        Idempotent: existing conditional-format rules are deleted before fresh
        ones are added, so repeated syncs never accumulate duplicate rules
        (which previously grew unbounded and eventually broke batchUpdate).
        """
        service = self._get_service()
        spreadsheet = (
            service.spreadsheets()
            .get(
                spreadsheetId=self.settings.google_sheet_id,
                fields="sheets(properties(sheetId,title),conditionalFormats)",
            )
            .execute()
        )

        sheet_ids = {}
        existing_cf_counts: dict[int, int] = {}
        for sheet in spreadsheet.get("sheets", []):
            title = sheet.get("properties", {}).get("title")
            sheet_id = sheet.get("properties", {}).get("sheetId")
            sheet_ids[title] = sheet_id
            if sheet_id is not None:
                existing_cf_counts[sheet_id] = len(sheet.get("conditionalFormats", []))

        requests: list[dict[str, Any]] = []

        # Clear pre-existing conditional-format rules (delete highest index
        # first so earlier indices stay valid) before re-adding below.
        for sheet_id, count in existing_cf_counts.items():
            for index in range(count - 1, -1, -1):
                requests.append({
                    "deleteConditionalFormatRule": {
                        "sheetId": sheet_id,
                        "index": index,
                    }
                })

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

        # Conditional status colouring. We rely on the basic filter (added above)
        # for user-driven sorting: an automatic sort by the human-formatted
        # "Last Updated"/"Deadline" text would sort lexically, not chronologically.
        active_sheet_id = sheet_ids.get("Active Opportunities")
        if active_sheet_id is not None:
            # Match the friendly labels shown in the "Status" column.
            status_col = ACTIVE_OPP_HEADERS.index("Status")
            colors = {
                "Open": {"red": 0.85, "green": 0.92, "blue": 1.0},
                "OA Scheduled": {"red": 1.0, "green": 0.95, "blue": 0.8},
                "Shortlisted": {"red": 0.8, "green": 0.9, "blue": 1.0},
                "Interview": {"red": 1.0, "green": 0.85, "blue": 0.7},
                "HR Round": {"red": 0.95, "green": 0.85, "blue": 0.95},
                "Selected": {"red": 0.7, "green": 1.0, "blue": 0.7},
                "Offer Received": {"red": 0.7, "green": 1.0, "blue": 0.7},
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
    """Convert one drive into a clean, human-readable Active Opportunities row.

    Column order matches ``ACTIVE_OPP_HEADERS``. Dates are rendered in local
    time, enums get friendly labels, the registration and Gmail links become
    clickable, and the status history is shown as a compact arrow trail.
    """
    deadline_raw = opportunity.get("deadline")
    role = opportunity.get("role") or ""
    return [
        _cell(opportunity.get("company_name")),
        "" if role.strip() in ("", "Unknown Role") else _cell(role),
        _friendly(opportunity.get("internship_or_fulltime"), _TYPE_LABELS),
        _friendly(opportunity.get("degree_level"), _DEGREE_LABELS),
        _friendly(opportunity.get("dream_category"), _CATEGORY_LABELS),
        _friendly(opportunity.get("current_status"), _STATUS_LABELS),
        _friendly(opportunity.get("priority"), _PRIORITY_LABELS),
        _cell(opportunity.get("action_required")),
        _fmt_date(deadline_raw),
        _days_left(deadline_raw),
        _fmt_date(opportunity.get("next_event_date")),
        _cell(opportunity.get("package_or_stipend")),
        _cell(opportunity.get("work_location")),
        _cell(opportunity.get("cgpa_requirement")),
        _fmt_branches(opportunity.get("branches_allowed")),
        _friendly(opportunity.get("eligibility_status", "MANUAL_REVIEW"), _ELIGIBILITY_LABELS),
        _friendly(opportunity.get("my_status", "NOT_APPLIED"), _MY_STATUS_LABELS),
        _hyperlink(opportunity.get("registration_link"), "Apply"),
        _gmail_link(opportunity),
        _fmt_status_trail(opportunity.get("status_history")),
        _fmt_datetime(opportunity.get("updated_at") or opportunity.get("last_update_timestamp")),
        _cell(opportunity.get("drive_id")),
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


def _preserve_user_columns(
    new_row: list[str], existing_row: list[str], user_columns: list[int]
) -> None:
    """Carry user-edited cells from the existing sheet row into the new row.

    Mutates ``new_row`` in place so a sync never overwrites a value the user
    typed (e.g. marking a drive "Applied" in the My Status column).
    """
    for col in user_columns:
        if col < len(existing_row) and existing_row[col].strip():
            while len(new_row) <= col:
                new_row.append("")
            new_row[col] = existing_row[col]


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


# ------------------------------------------------------------------
# Human-readable value formatting
# ------------------------------------------------------------------

# Keys are upper-cased to match _friendly()'s case-insensitive lookup.
_TYPE_LABELS = {
    "INTERNSHIP": "Internship",
    "INTERN": "Internship",
    "FULLTIME": "Full-time",
    "FULL_TIME": "Full-time",
    "INTERNSHIP_AND_FULLTIME": "Internship + Full-time",
    "CONTRACT": "Contract",
}

# Friendly labels for the canonical status vocabulary used across the system.
# NEW/PPT are legacy synonyms (now canonicalised to OPEN at extraction time).
_STATUS_LABELS = {
    "NEW": "Open",
    "PPT": "Open",
    "OPEN": "Open",
    "REGISTERED": "Registered",
    "SHORTLISTED": "Shortlisted",
    "OA": "OA Scheduled",
    "INTERVIEW": "Interview",
    "HR": "HR Round",
    "SELECTED": "Selected",
    "OFFER_RECEIVED": "Offer Received",
    "REJECTED": "Rejected",
    "WITHDRAWN": "Withdrawn",
    "EXPIRED": "Expired",
    "COMPLETED": "Completed",
}

_PRIORITY_LABELS = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}

_DEGREE_LABELS = {"BTECH": "B.Tech", "MTECH": "M.Tech", "ANY": "Any", "UNKNOWN": ""}

_CATEGORY_LABELS = {"NORMAL": "", "DREAM": "Dream", "SUPER_DREAM": "Super Dream"}

_ELIGIBILITY_LABELS = {
    "ELIGIBLE": "Eligible",
    "NOT_ELIGIBLE_BRANCH": "Not eligible (branch)",
    "NOT_ELIGIBLE_DEGREE": "Not eligible (degree)",
    "NOT_ELIGIBLE_CGPA": "Not eligible (CGPA)",
    "MANUAL_REVIEW": "Needs review",
}

_MY_STATUS_LABELS = {
    "NOT_APPLIED": "Not applied",
    "APPLIED": "Applied",
    "SHORTLISTED": "Shortlisted",
    "OA_CLEARED": "OA cleared",
    "INTERVIEWED": "Interviewed",
    "SELECTED": "Selected",
    "REJECTED": "Rejected",
}

_JUNK_VALUES = {"", "[]", "[ ]", "none", "null", "n/a", "na", "-"}


def _friendly(value: Any, labels: dict[str, str]) -> str:
    """Map an enum value to a readable label, falling back to Title Case."""
    raw = _cell(value)
    if not raw:
        return ""
    return labels.get(raw.upper(), raw.replace("_", " ").title())


def _force_text(value: str) -> str:
    """Prefix a value so Sheets (USER_ENTERED) keeps it as literal text.

    Without this, Sheets re-parses strings like "16 Jun 2026, 4:45 PM" into a
    date serial and re-renders them in the spreadsheet's locale (e.g. 24-hour),
    which would undo our human-readable formatting. The leading apostrophe is a
    Sheets text marker and is NOT displayed in the cell.
    """
    return f"'{value}" if value else value


def _fmt_datetime(value: Any) -> str:
    """Render a stored timestamp as local 'DD Mon YYYY, H:MM AM/PM' (text)."""
    raw = _cell(value)
    if not raw:
        return ""
    dt = parse_datetime_flexible(raw)
    if dt is None:
        return raw  # never drop information we can't parse
    hour = dt.strftime("%I").lstrip("0") or "12"
    return _force_text(f"{dt.day} {dt.strftime('%b %Y')}, {hour}:{dt.strftime('%M %p')}")


def _fmt_date(value: Any) -> str:
    """Render a deadline/event date in local time; include time only if set."""
    raw = _cell(value)
    if not raw:
        return ""
    dt = parse_datetime_flexible(raw)
    if dt is None:
        return raw
    out = f"{dt.day} {dt.strftime('%b %Y')}"
    if dt.hour or dt.minute:
        hour = dt.strftime("%I").lstrip("0") or "12"
        out += f", {hour}:{dt.strftime('%M %p')}"
    return _force_text(out)


def _days_left(value: Any) -> str:
    """Return a friendly countdown to a deadline: Today / Tomorrow / N days / Passed."""
    raw = _cell(value)
    if not raw:
        return ""
    dt = parse_datetime_flexible(raw)
    if dt is None:
        return ""
    days = (dt.date() - datetime.now().date()).days
    if days < 0:
        return "Passed"
    if days == 0:
        return "Today"
    if days == 1:
        return "Tomorrow"
    return f"{days} days"


def _fmt_branches(value: Any) -> str:
    """Join eligible branches, dropping serialization junk like '[]'."""
    if isinstance(value, str):
        # May arrive as a JSON-encoded list when the row wasn't deserialized.
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else value
        except (json.JSONDecodeError, TypeError):
            pass
    items = value if isinstance(value, list) else [value]
    clean = [
        str(item).strip()
        for item in items
        if str(item).strip().lower() not in _JUNK_VALUES
    ]
    return ", ".join(clean)


def _fmt_status_trail(value: Any) -> str:
    """Render status history as a compact 'Open → OA Scheduled' trail (last 5)."""
    raw = _cell(value)
    if not raw:
        return ""
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if not isinstance(items, list):
        return str(items)
    trail: list[str] = []
    for status in items:
        label = _STATUS_LABELS.get(str(status).upper(), str(status))
        if not trail or trail[-1] != label:  # collapse consecutive duplicates
            trail.append(label)
    return " → ".join(trail[-5:])


def _hyperlink(url: Any, label: str) -> str:
    """Build a Sheets HYPERLINK formula, or '' when there is no valid URL."""
    raw = _cell(url)
    if not raw.lower().startswith(("http://", "https://")):
        return ""
    safe = raw.replace('"', "%22")
    return f'=HYPERLINK("{safe}", "{label}")'


def _gmail_link(opportunity: dict[str, Any]) -> str:
    """Build a clickable Gmail deep link to the source thread/message."""
    target = (
        opportunity.get("source_thread_id")
        or opportunity.get("source_email_id")
        or opportunity.get("source_message_id")
    )
    if not target:
        return ""
    return f'=HYPERLINK("https://mail.google.com/mail/u/0/#inbox/{target}", "Open")'
