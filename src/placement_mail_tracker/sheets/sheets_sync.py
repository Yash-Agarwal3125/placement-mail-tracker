"""Google Sheets synchronization for placement drives.

4 tabs: ACTION REQUIRED, ALL DRIVES, UPCOMING EVENTS, DASHBOARD.
Uses clear-then-write per sync (no key-based dedup needed).
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.eligibility import format_eligibility_string
from placement_mail_tracker.reliability.auth_alerts import alert_oauth_dead_once, clear_oauth_alert
from placement_mail_tracker.utils.time import human_relative_time, parse_datetime_flexible

logger = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ACTION REQUIRED: actionable drives needing user attention
ACTION_REQUIRED_HEADERS = [
    "Company",
    "Received",
    "Status",
    "Package/Stipend",
    "Eligibility",
    "Deadline",
    "Countdown",
    "Next Action",
    "Gmail",
]

# RECENT UPDATES: field-level change history joined with drive info
RECENT_UPDATES_HEADERS = [
    "Company",
    "Change",
    "Field",
    "Old Value",
    "New Value",
    "When",
    "Email Time",
]

# ALL DRIVES: one row per drive (eligible + needs-review)
ALL_DRIVES_HEADERS = [
    "Company",
    "Role",
    "Received Date",
    "Package/Stipend",
    "Eligibility",
    "Location",
    "Deadline",
    "Current Status",
    "Last Update",
    "My Status",       # user-owned; preserved across syncs
    "Review Flags",    # post-extraction validation flags; never blocks a row, just surfaces it
    "Drive ID",        # key column for read-back dedup
]

# MY APPLICATIONS: drives where the user has taken action (My Status ≠ Not Applied)
MY_APPLICATIONS_HEADERS = [
    "Company",
    "Role",
    "My Status",
    "Current Status",
    "Deadline",
    "Package/Stipend",
    "Gmail",
]

# UPCOMING EVENTS: OA / interview dates sorted ascending
UPCOMING_EVENTS_HEADERS = [
    "Date",
    "Company",
    "Event Type",
    "Action",
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

# Backwards-compat alias used by tests and test_end_to_end.py.
# Points to ALL DRIVES schema.
ACTIVE_OPP_HEADERS = ALL_DRIVES_HEADERS

# User-owned columns in ALL DRIVES: My Status (index 9). Preserved across syncs.
ACTIVE_USER_COLUMNS: list[int] = [ALL_DRIVES_HEADERS.index("My Status")]

# Drive ID is the last column in ALL DRIVES, used as the row dedupe key for
# reading back user-set values before each clear-and-write sync.
ACTIVE_KEY_INDEX = ALL_DRIVES_HEADERS.index("Drive ID")

# Statuses that need user attention (shown in ACTION REQUIRED tab)
_ACTIONABLE_STATUSES = frozenset(
    {"OPEN", "REGISTERED", "SHORTLISTED", "OA", "INTERVIEW", "HR"}
)


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

    def sync_active_opportunities(
        self, database: DatabaseManager, run_start: datetime | None = None
    ) -> dict[str, int]:
        """Sync all drives and dashboard to Google Sheets with resilience."""
        for attempt in range(1, 4):
            try:
                return self._sync_active_opportunities_internal(database, run_start)
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

    def _sync_active_opportunities_internal(
        self, database: DatabaseManager, run_start: datetime | None = None
    ) -> dict[str, int]:
        if not self.settings.google_sheet_id:
            self.last_error = "GOOGLE_SHEET_ID is missing"
            if self.settings.is_production:
                raise SheetsAuthenticationError(self.last_error)
            logger.warning("GOOGLE_SHEET_ID is missing; skipping Google Sheets sync")
            return {"created": 0, "updated": 0, "skipped": 0}

        self.last_error = None
        self._ensure_tabs_exist()

        active_opps = database.fetch_active_drives_only()

        # Visible = eligible + needs-manual-review (not actively NOT_ELIGIBLE_*)
        visible_opps = [
            opp for opp in active_opps
            if "NOT_ELIGIBLE" not in (opp.get("eligibility_status") or "")
        ]

        # Read back user-owned My Status values before we clear-and-write.
        my_status_map = self._read_my_status_map()

        # A1/B2: persist the read-back into the DB so it survives even if a
        # later sheet read fails, and so other consumers (e.g. calendar
        # filtering) can query my_status from the DB directly.
        enum_updates: dict[str, str] = {}
        for opp in active_opps:
            drive_id = opp.get("drive_id")
            if not drive_id:
                continue
            enum_value = _my_status_to_enum(my_status_map.get(drive_id, ""))
            if enum_value:
                enum_updates[drive_id] = enum_value
        if enum_updates:
            database.bulk_update_my_status(enum_updates)

        # ACTION REQUIRED: visible drives needing user action, sorted by deadline
        action_opps = [
            opp for opp in visible_opps
            if (opp.get("action_required") or "").strip()
            or (opp.get("current_status") or "").upper() in _ACTIONABLE_STATUSES
        ]
        action_opps.sort(key=lambda o: (not bool(o.get("deadline")), o.get("deadline") or ""))

        self._clear_and_write_tab(
            tab_name="ACTION REQUIRED",
            headers=ACTION_REQUIRED_HEADERS,
            rows=[action_required_row(opp) for opp in action_opps],
        )

        # MY APPLICATIONS: drives where the user has tracked their own status
        applied_opps = [
            opp for opp in visible_opps
            if my_status_map.get(opp.get("drive_id") or "") not in ("", None, "Not Applied")
        ]
        applied_opps.sort(key=lambda o: (not bool(o.get("deadline")), o.get("deadline") or ""))
        self._clear_and_write_tab(
            tab_name="MY APPLICATIONS",
            headers=MY_APPLICATIONS_HEADERS,
            rows=[
                my_applications_row(opp, my_status_map.get(opp.get("drive_id") or "", ""))
                for opp in applied_opps
            ],
        )

        # ALL DRIVES: newest-update first
        all_sorted = sorted(
            visible_opps,
            key=lambda o: o.get("updated_at") or o.get("last_update_timestamp") or "",
            reverse=True,
        )
        self._clear_and_write_tab(
            tab_name="ALL DRIVES",
            headers=ALL_DRIVES_HEADERS,
            rows=[
                opportunity_to_sheet_row(
                    opp,
                    my_status=my_status_map.get(opp.get("drive_id") or "", ""),
                )
                for opp in all_sorted
            ],
        )

        # UPCOMING EVENTS: sorted by date ascending
        self._clear_and_write_tab(
            tab_name="UPCOMING EVENTS",
            headers=UPCOMING_EVENTS_HEADERS,
            rows=_build_upcoming_events(visible_opps),
        )

        # Company History
        companies = database.get_company_history()
        self._clear_and_write_tab(
            tab_name="Company History",
            headers=COMPANY_HISTORY_HEADERS,
            rows=[company_to_sheet_row(comp) for comp in companies],
        )

        self._sync_recent_updates(database)
        self._sync_dashboard(database, run_start)
        self._apply_formatting()

        return {"created": len(active_opps), "updated": 0, "skipped": 0}

    def _ensure_tabs_exist(self) -> None:
        service = self._get_service()
        spreadsheet = (
            service.spreadsheets()
            .get(spreadsheetId=self.settings.google_sheet_id)
            .execute()
        )
        existing_sheets = {
            s.get("properties", {}).get("title"): s.get("properties", {}).get("sheetId")
            for s in spreadsheet.get("sheets", [])
        }

        required_tabs = [
            "ACTION REQUIRED",
            "MY APPLICATIONS",
            "ALL DRIVES",
            "UPCOMING EVENTS",
            "Company History",
            "RECENT UPDATES",
            "Dashboard",
        ]
        stale_tabs = {"Active Opportunities", "Filtered Opportunities"}

        requests = []
        for tab in required_tabs:
            if tab not in existing_sheets:
                requests.append({"addSheet": {"properties": {"title": tab}}})
        for title, sheet_id in existing_sheets.items():
            if title in stale_tabs and sheet_id is not None:
                requests.append({"deleteSheet": {"sheetId": sheet_id}})

        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={"requests": requests},
            ).execute()

    def _clear_and_write_tab(
        self,
        tab_name: str,
        headers: list[str],
        rows: list[list[str]],
    ) -> None:
        """Atomically refresh a tab: write headers + rows, then trim stale rows.

        Write-before-clear so a mid-sync failure can never blank the tab
        (FS SS-12 / INV-30): the ``update`` lands the new data first, then the
        trailing ``clear`` removes leftover rows from a previously longer sync.
        If the update fails, nothing was cleared and the previous data is intact;
        if the trailing clear fails, at worst a few stale rows linger below the
        new data and the next successful sync self-heals.
        """
        values = self._values()
        sheet_id = self.settings.google_sheet_id

        all_data: list[list[str]] = [headers] + rows
        values.update(
            spreadsheetId=sheet_id,
            range=f"{_quote(tab_name)}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": all_data},
        ).execute()

        # Trim rows left over from a previous, longer sync, starting one row
        # below the data just written. ponytail: assumes a tab's column count is
        # stable across syncs (headers are fixed module constants); a column
        # *shrink* between versions would need a one-off full-width clear.
        first_stale_row = len(all_data) + 1
        values.clear(
            spreadsheetId=sheet_id,
            range=f"{_quote(tab_name)}!A{first_stale_row}:Z",
        ).execute()
        logger.info("[SYNC] Wrote %d rows to %s", len(rows), tab_name)

    def _sync_dashboard(
        self, database: DatabaseManager, run_start: datetime | None = None
    ) -> None:
        metrics = database.get_dashboard_metrics()
        now = datetime.now()

        if run_start is not None:
            elapsed = (now - run_start).total_seconds()
            runtime_str = f"{elapsed:.1f}s"
        else:
            runtime_str = "—"

        dead_letters = database.get_dead_letter_emails(limit=3)
        dead_letter_count = metrics.get("dead_letter_count", 0)
        dead_letter_detail = "; ".join(
            (e.get("subject") or "")[:40] for e in dead_letters
        ) if dead_letters else "None"

        dead_cell = (
            f"{dead_letter_count} — {dead_letter_detail}" if dead_letter_count else "0"
        )
        dashboard_data: list[list[str]] = [
            # ── SYSTEM HEALTH ─────────────────────────────────────────
            ["── SYSTEM HEALTH ──", ""],
            ["Last Sync", human_relative_time(now)],
            ["Runtime", runtime_str],
            ["Emails Today", str(metrics.get("emails_today", 0))],
            ["Dead Letters", dead_cell],
            # ── PLACEMENT SEASON ──────────────────────────────────────
            ["── PLACEMENT SEASON ──", ""],
            ["Active Drives", str(metrics["active_drives"])],
            ["Applications Open", str(metrics["applications_open"])],
            ["Action Required", str(metrics.get("action_required", 0))],
            ["Deadlines This Week", str(metrics.get("deadlines_this_week", 0))],
            ["OA This Week", str(metrics["oa_this_week"])],
            ["Interviews This Week", str(metrics["interviews_this_week"])],
            # ── RESULTS ───────────────────────────────────────────────
            ["── RESULTS ──", ""],
            ["Offers Received", str(metrics["offers_received"])],
            ["Selected / Offers", str(metrics.get("selected", 0))],
            ["Rejected", str(metrics.get("rejected", 0))],
            ["Selection Rate", metrics["selection_rate"]],
            ["Total Drives", str(metrics["total_drives"])],
            ["Companies Applied", str(metrics["companies_applied"])],
        ]

        self._clear_and_write_tab(
            tab_name="Dashboard",
            headers=DASHBOARD_HEADERS,
            rows=dashboard_data,
        )

    def _sync_recent_updates(self, database: DatabaseManager) -> None:
        updates = database.get_recent_updates(limit=20)
        rows = []
        for u in updates:
            field = u.get("field_name") or ""
            change = field.replace("_", " ").title() + " changed" if field else "Updated"
            when_dt = parse_datetime_flexible(u.get("created_at") or "")
            email_dt = parse_datetime_flexible(u.get("email_received_at") or "")
            rows.append([
                _cell(u.get("company_name")),
                change,
                field,
                _cell(u.get("old_value")),
                _cell(u.get("new_value")),
                human_relative_time(when_dt),
                human_relative_time(email_dt),
            ])
        self._clear_and_write_tab(
            tab_name="RECENT UPDATES",
            headers=RECENT_UPDATES_HEADERS,
            rows=rows,
        )

    def _read_my_status_map(self) -> dict[str, str]:
        """Return {drive_id: my_status} from the current ALL DRIVES sheet.

        Called before each clear-and-write so user-set My Status values survive
        the sync. A read failure (API/network error) is NOT swallowed here —
        it propagates into ``sync_active_opportunities``'s existing retry
        wrapper, so a transient error retries the whole sync instead of being
        silently treated as "the sheet is empty" and wiping every user-set
        status (B2). A genuinely empty/old-schema sheet still returns {}.
        """
        response = self._values().get(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"{_quote('ALL DRIVES')}!A1:Z",
        ).execute()
        rows = response.get("values", [])
        if len(rows) < 2:
            return {}
        headers = rows[0]
        try:
            my_status_col = headers.index("My Status")
            drive_id_col = headers.index("Drive ID")
        except ValueError:
            return {}  # old schema without these columns
        result: dict[str, str] = {}
        for row in rows[1:]:
            drive_id = row[drive_id_col] if drive_id_col < len(row) else ""
            my_status = row[my_status_col] if my_status_col < len(row) else ""
            if drive_id and my_status and my_status != "Not Applied":
                result[drive_id] = my_status
        return result

    def _apply_formatting(self) -> None:
        """Freeze header row, auto-filter, and colour-code status cells."""
        service = self._get_service()
        spreadsheet = (
            service.spreadsheets()
            .get(
                spreadsheetId=self.settings.google_sheet_id,
                fields="sheets(properties(sheetId,title),conditionalFormats)",
            )
            .execute()
        )

        sheet_ids: dict[str, int] = {}
        existing_cf_counts: dict[int, int] = {}
        for sheet in spreadsheet.get("sheets", []):
            title = sheet.get("properties", {}).get("title")
            sheet_id = sheet.get("properties", {}).get("sheetId")
            sheet_ids[title] = sheet_id
            if sheet_id is not None:
                existing_cf_counts[sheet_id] = len(sheet.get("conditionalFormats", []))

        requests: list[dict[str, Any]] = []

        for sheet_id, count in existing_cf_counts.items():
            for index in range(count - 1, -1, -1):
                requests.append({
                    "deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}
                })

        for _title, sheet_id in sheet_ids.items():
            if sheet_id is None:
                continue
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            })
            requests.append({
                "setBasicFilter": {
                    "filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0}}
                }
            })

        # Status colour-coding on ALL DRIVES (Current Status column)
        all_drives_id = sheet_ids.get("ALL DRIVES")
        if all_drives_id is not None:
            status_col = ALL_DRIVES_HEADERS.index("Current Status")
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
                                "sheetId": all_drives_id,
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

        # Countdown column on ACTION REQUIRED: overdue=red, today=orange
        ar_id = sheet_ids.get("ACTION REQUIRED")
        if ar_id is not None:
            countdown_col = ACTION_REQUIRED_HEADERS.index("Countdown")
            for text, color in (
                ("OVERDUE", {"red": 1.0, "green": 0.8, "blue": 0.8}),
                ("TODAY", {"red": 1.0, "green": 0.95, "blue": 0.8}),
            ):
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": ar_id,
                                "startRowIndex": 1,
                                "startColumnIndex": countdown_col,
                                "endColumnIndex": countdown_col + 1,
                            }],
                            "booleanRule": {
                                "condition": {
                                    "type": "TEXT_CONTAINS",
                                    "values": [{"userEnteredValue": text}],
                                },
                                "format": {"backgroundColor": color},
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
        credentials = self._load_token()

        if credentials and credentials.valid:
            clear_oauth_alert("Sheets")
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired Google Sheets OAuth token")
            try:
                credentials.refresh(Request())
            except RefreshError as error:
                msg = f"OAuth dead — re-consent needed for Sheets: {error}"
                alert_oauth_dead_once("Sheets", msg, self.settings)
                raise SheetsAuthenticationError(msg) from error
            self._save_token(credentials)
            clear_oauth_alert("Sheets")
            return credentials

        if not self.credentials_path.exists():
            msg = (
                f"Google Sheets credentials file not found at {self.credentials_path}. "
                "Add OAuth client credentials before syncing."
            )
            raise SheetsAuthenticationError(msg)

        if not sys.stdin.isatty():
            # A scheduled run has no interactive terminal to complete a fresh
            # consent flow — run_local_server would otherwise hang up to 120s
            # and fail anyway. Fail fast with a clear alert instead.
            msg = (
                "OAuth dead — re-consent needed for Sheets: no valid/refreshable "
                "token and this run is not interactive"
            )
            alert_oauth_dead_once("Sheets", msg, self.settings)
            raise SheetsAuthenticationError(msg)

        logger.info("Starting Google Sheets OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path), SHEETS_SCOPES
        )
        credentials = flow.run_local_server(port=0, timeout_seconds=120)
        self._save_token(credentials)
        clear_oauth_alert("Sheets")
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


def opportunity_to_sheet_row(opportunity: dict[str, Any], my_status: str = "") -> list[str]:
    """Convert one drive into an ALL DRIVES row (12 columns)."""
    eligibility = (
        format_eligibility_string(opportunity)
        or _friendly(opportunity.get("eligibility_status", "MANUAL_REVIEW"), _ELIGIBILITY_LABELS)
    )
    # Prefer sheet-preserved value; fall back to DB field (usually NOT_APPLIED)
    my_status_cell = my_status or _friendly(
        opportunity.get("my_status", "NOT_APPLIED"), _MY_STATUS_LABELS
    )
    return [
        _cell(opportunity.get("company_name")),
        _cell(opportunity.get("role")),
        _fmt_date(opportunity.get("email_received_at")),
        _cell(opportunity.get("package_or_stipend")),
        eligibility,
        _cell(opportunity.get("work_location")),
        _fmt_date(opportunity.get("deadline")),
        _friendly(opportunity.get("current_status"), _STATUS_LABELS),
        _fmt_datetime(opportunity.get("updated_at") or opportunity.get("last_update_timestamp")),
        my_status_cell,
        _cell(opportunity.get("validation_flags")),
        _cell(opportunity.get("drive_id")),
    ]


def _deadline_countdown(deadline_raw: str | None) -> str:
    """Return a human countdown string for a raw deadline value."""
    if not deadline_raw:
        return ""
    dt = parse_datetime_flexible(str(deadline_raw))
    if dt is None:
        return ""
    delta = (dt.date() - datetime.now().date()).days
    if delta < 0:
        return f"OVERDUE ({-delta}d)"
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "Tomorrow"
    return f"in {delta} days"


def _fmt_received(value: Any) -> str:
    """Format email received timestamp as a human-readable relative string."""
    raw = _cell(value)
    if not raw:
        return ""
    dt = parse_datetime_flexible(raw)
    if dt is None:
        return raw
    return human_relative_time(dt)


def action_required_row(opportunity: dict[str, Any]) -> list[str]:
    """Convert one drive into an ACTION REQUIRED row (9 columns)."""
    eligibility = (
        format_eligibility_string(opportunity)
        or _friendly(opportunity.get("eligibility_status", "MANUAL_REVIEW"), _ELIGIBILITY_LABELS)
    )
    deadline_raw = opportunity.get("deadline")
    return [
        _cell(opportunity.get("company_name")),
        _fmt_received(opportunity.get("email_received_at")),
        _friendly(opportunity.get("current_status"), _STATUS_LABELS),
        _cell(opportunity.get("package_or_stipend")),
        eligibility,
        _fmt_date(deadline_raw),
        _deadline_countdown(deadline_raw),
        _cell(opportunity.get("action_required")),
        _gmail_link(opportunity),
    ]


def my_applications_row(opportunity: dict[str, Any], my_status: str) -> list[str]:
    """Convert one drive into a MY APPLICATIONS row (7 columns)."""
    return [
        _cell(opportunity.get("company_name")),
        _cell(opportunity.get("role")),
        my_status,
        _friendly(opportunity.get("current_status"), _STATUS_LABELS),
        _fmt_date(opportunity.get("deadline")),
        _cell(opportunity.get("package_or_stipend")),
        _gmail_link(opportunity),
    ]


def upcoming_event_row(
    opportunity: dict[str, Any],
    event_type: str,
    event_date_raw: str,
) -> list[str]:
    """Convert one event into an UPCOMING EVENTS row (4 columns)."""
    action = opportunity.get("action_required") or f"Prepare for {event_type}"
    return [
        _fmt_date(event_date_raw),
        _cell(opportunity.get("company_name")),
        event_type,
        _cell(action),
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


def _build_upcoming_events(opps: list[dict[str, Any]]) -> list[list[str]]:
    """Build sorted upcoming-events rows from oa_date and interview_date fields."""
    now = datetime.now()
    events: list[tuple[datetime, list[str]]] = []

    for opp in opps:
        for field_name, label in (
            ("oa_date", "Online Assessment"), ("interview_date", "Interview")
        ):
            raw = opp.get(field_name)
            if not raw:
                continue
            parsed = parse_datetime_flexible(str(raw))
            if parsed and parsed >= now:
                events.append((parsed, upcoming_event_row(opp, label, str(raw))))

    events.sort(key=lambda t: t[0])
    return [row for _, row in events]


def _preserve_user_columns(
    new_row: list[str], existing_row: list[str], user_columns: list[int]
) -> None:
    """No-op in new design (no user-owned columns); kept for import compat."""
    for col in user_columns:
        if col < len(existing_row) and existing_row[col].strip():
            while len(new_row) <= col:
                new_row.append("")
            new_row[col] = existing_row[col]


def _quote(sheet_name: str) -> str:
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

_TYPE_LABELS = {
    "INTERNSHIP": "Internship",
    "INTERN": "Internship",
    "FULLTIME": "Full-time",
    "FULL_TIME": "Full-time",
    "INTERNSHIP_AND_FULLTIME": "Internship + Full-time",
    "CONTRACT": "Contract",
}

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

_MY_STATUS_REVERSE = {label.upper(): raw for raw, label in _MY_STATUS_LABELS.items()}


def _my_status_to_enum(display_value: str) -> str | None:
    """Reverse-map a sheet cell's My Status text back to its DB enum value.

    Accepts either the friendly label ("Applied") or the raw enum itself
    ("APPLIED", any case), so a user typing over the dropdown still works.
    Returns None for unrecognized text rather than guessing.
    """
    value = (display_value or "").strip().upper()
    if not value:
        return None
    if value in _MY_STATUS_LABELS:
        return value
    return _MY_STATUS_REVERSE.get(value)

_JUNK_VALUES = {"", "[]", "[ ]", "none", "null", "n/a", "na", "-"}


def _friendly(value: Any, labels: dict[str, str]) -> str:
    raw = _cell(value)
    if not raw:
        return ""
    return labels.get(raw.upper(), raw.replace("_", " ").title())


def _force_text(value: str) -> str:
    """Prefix with apostrophe so Sheets keeps the value as literal text."""
    return f"'{value}" if value else value


def _fmt_datetime(value: Any) -> str:
    raw = _cell(value)
    if not raw:
        return ""
    dt = parse_datetime_flexible(raw)
    if dt is None:
        return raw
    hour = dt.strftime("%I").lstrip("0") or "12"
    return _force_text(f"{dt.day} {dt.strftime('%b %Y')}, {hour}:{dt.strftime('%M %p')}")


def _fmt_date(value: Any) -> str:
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


def _gmail_link(opportunity: dict[str, Any]) -> str:
    target = (
        opportunity.get("source_thread_id")
        or opportunity.get("source_email_id")
        or opportunity.get("source_message_id")
    )
    if not target:
        return ""
    return f'=HYPERLINK("https://mail.google.com/mail/u/0/#inbox/{target}", "Open")'
