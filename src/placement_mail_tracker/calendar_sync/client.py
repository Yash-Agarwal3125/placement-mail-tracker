"""Google Calendar API wrapper (docs/design/04-integration-spec.md §3.1).

Mirrors the Sheets/Gmail client seams: injectable ``service``, typed auth
error, ``last_error`` attribute. Auth scaffolding (this file's constructor,
``authenticate``, token load/save) is the Step 1 shared interface; the API
methods (``ensure_calendar``/``insert_event``/``patch_event``/``get_event``
and the retry helper) are implemented by the "calendar-client" subagent.
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.reliability.auth_alerts import alert_oauth_dead_once, clear_oauth_alert

logger = logging.getLogger(__name__)

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]

_T = TypeVar("_T")


class CalendarAuthenticationError(RuntimeError):
    """Raised when Calendar OAuth cannot be completed or the refresh token is dead."""


class GoogleCalendarClient:
    """Calendar API wrapper for the "VIT Placements" calendar."""

    def __init__(self, settings: Settings, *, service: Resource | None = None) -> None:
        self.settings = settings
        self.credentials_path = Path(settings.gmail_credentials_file)
        self.token_path = Path(settings.calendar_token_file)
        self._service = service
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # Auth (mirrors GoogleSheetsSync.authenticate, sheets_sync.py:600-644)
    # ------------------------------------------------------------------

    def authenticate(self) -> Credentials:
        credentials = self._load_token()

        if credentials and credentials.valid:
            clear_oauth_alert("Calendar")
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired Google Calendar OAuth token")
            try:
                credentials.refresh(Request())
            except RefreshError as error:
                msg = f"OAuth dead — re-consent needed for Calendar: {error}"
                alert_oauth_dead_once("Calendar", msg, self.settings)
                raise CalendarAuthenticationError(msg) from error
            self._save_token(credentials)
            clear_oauth_alert("Calendar")
            return credentials

        if not self.credentials_path.exists():
            msg = (
                f"Calendar credentials file not found at {self.credentials_path}. "
                "Add OAuth client credentials before syncing."
            )
            raise CalendarAuthenticationError(msg)

        if not sys.stdin.isatty():
            # A scheduled run has no interactive terminal to complete a fresh
            # consent flow — run_local_server would otherwise hang up to 120s
            # and fail anyway. Fail fast with a clear alert instead.
            msg = (
                "OAuth dead — re-consent needed for Calendar: no valid/refreshable "
                "token and this run is not interactive"
            )
            alert_oauth_dead_once("Calendar", msg, self.settings)
            raise CalendarAuthenticationError(msg)

        logger.info("Starting Google Calendar OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path), CALENDAR_SCOPES
        )
        credentials = flow.run_local_server(port=0, timeout_seconds=120)
        self._save_token(credentials)
        clear_oauth_alert("Calendar")
        return credentials

    def _get_service(self) -> Resource:
        if self._service is None:
            credentials = self.authenticate()
            self._service = build("calendar", "v3", credentials=credentials)
        return self._service

    def _load_token(self) -> Credentials | None:
        if not self.token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(
                str(self.token_path), CALENDAR_SCOPES
            )
        except Exception as error:
            logger.warning("Corrupted Calendar token file: %s. Deleting.", error)
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
    # Retry helper (mirrors GoogleSheetsSync.sync_active_opportunities'
    # retry envelope, sheets_sync.py:152-187): 3 attempts, retry on HttpError
    # 429/5xx or transient socket/connection errors, backoff 2s then 5s,
    # re-raise on the 3rd failed attempt. Callers are responsible for
    # catching/counting per-event failures (spec §3.3); this helper never
    # swallows a final failure.
    # ------------------------------------------------------------------

    def _call_with_retry(self, fn: Callable[[], _T]) -> _T:
        for attempt in range(1, 4):
            try:
                return fn()
            except Exception as error:
                is_retryable = False
                if isinstance(error, HttpError) and error.resp.status in {
                    429, 500, 502, 503, 504,
                }:
                    is_retryable = True
                elif isinstance(
                    error,
                    (socket.error, socket.timeout, http.client.HTTPException,
                     ConnectionError, TimeoutError),
                ):
                    is_retryable = True

                if is_retryable and attempt < 3:
                    backoff = 2 if attempt == 1 else 5
                    logger.warning(
                        "Retry attempt %s. Backoff %ss. Exception: %s (%s)",
                        attempt, backoff, type(error).__name__, error,
                    )
                    time.sleep(backoff)
                    continue

                self.last_error = str(error)
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    # API methods (spec §3.1)
    # ------------------------------------------------------------------

    def ensure_calendar(self, name: str) -> str:
        """Return the calendarId whose summary == name, creating it if absent."""
        response = self._call_with_retry(
            lambda: self._get_service().calendarList().list().execute()
        )
        for entry in response.get("items", []):
            if entry.get("summary") == name:
                return entry["id"]

        created = self._call_with_retry(
            lambda: self._get_service()
            .calendars()
            .insert(body={"summary": name, "timeZone": self.settings.calendar_timezone})
            .execute()
        )
        return created["id"]

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> str:
        """events().insert; returns the new Google event id."""
        response = self._call_with_retry(
            lambda: self._get_service()
            .events()
            .insert(calendarId=calendar_id, body=body)
            .execute()
        )
        return response["id"]

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        """events().patch by stored id — never search-by-title."""
        self._call_with_retry(
            lambda: self._get_service()
            .events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body)
            .execute()
        )

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        """events().get; returns None on 404 (used only by rebuild).

        The 404 case is handled *inside* the retry-wrapped callable so it
        never reaches the generic retry-and-reraise path in
        ``_call_with_retry`` — a missing event is an expected outcome (the
        user deleted it, or state is being reconciled), not a transient
        failure, and must not set ``last_error`` or consume a retry.
        """

        def _call() -> dict[str, Any] | None:
            try:
                return (
                    self._get_service()
                    .events()
                    .get(calendarId=calendar_id, eventId=event_id)
                    .execute()
                )
            except HttpError as error:
                if error.resp.status == 404:
                    return None
                raise

        return self._call_with_retry(_call)
