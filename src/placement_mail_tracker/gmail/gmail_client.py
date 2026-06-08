"""Gmail API client built on OAuth2."""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from placement_mail_tracker.config.settings import Settings

logger = logging.getLogger(__name__)

GMAIL_READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailAuthenticationError(RuntimeError):
    """Raised when Gmail OAuth authentication cannot be completed."""


@dataclass(slots=True)
class GmailEmail:
    """Clean email data extracted from a Gmail message."""

    message_id: str
    thread_id: str
    subject: str
    sender: str
    timestamp: str
    body_text: str
    snippet: str


def decode_base64url(data: str | None) -> str:
    """Decode Gmail's URL-safe base64 message body data."""
    if not data:
        return ""

    padding = "=" * (-len(data) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{data}{padding}")
    except (ValueError, TypeError) as error:
        logger.warning("Unable to decode Gmail message body: %s", error)
        return ""

    return decoded.decode("utf-8", errors="replace")


def get_header(headers: list[dict[str, str]], name: str, default: str = "") -> str:
    """Return a case-insensitive Gmail header value."""
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", default)
    return default


def normalize_gmail_timestamp(raw_date: str) -> str:
    """Convert an email Date header into ISO 8601 when possible."""
    if not raw_date:
        return ""

    try:
        return parsedate_to_datetime(raw_date).isoformat()
    except (TypeError, ValueError, IndexError):
        logger.debug("Could not parse Gmail date header: %s", raw_date)
        return raw_date


def extract_body_text(payload: dict[str, Any]) -> str:
    """Extract readable plain text from a Gmail message payload.

    Gmail messages can be a single part or a nested multipart tree. Prefer
    text/plain content, then fall back to text/html with tags left untouched
    for a future HTML-cleaning utility.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")

        if mime_type == "text/plain":
            plain_parts.append(decode_base64url(body_data))
            return

        if mime_type == "text/html":
            html_parts.append(decode_base64url(body_data))
            return

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    body = "\n".join(part.strip() for part in plain_parts if part.strip())

    if body:
        return body

    return "\n".join(part.strip() for part in html_parts if part.strip())


def parse_gmail_message(message: dict[str, Any]) -> GmailEmail:
    """Convert a raw Gmail API message into a small application model."""
    payload = message.get("payload", {})
    headers = payload.get("headers", [])

    return GmailEmail(
        message_id=message.get("id", ""),
        thread_id=message.get("threadId", ""),
        subject=get_header(headers, "Subject", "(no subject)"),
        sender=get_header(headers, "From"),
        timestamp=normalize_gmail_timestamp(get_header(headers, "Date")),
        body_text=extract_body_text(payload),
        snippet=message.get("snippet", ""),
    )


class GmailClient:
    """Reusable Gmail API client for reading inbox messages."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.credentials_path = Path(settings.gmail_credentials_file)
        self.token_path = Path(settings.gmail_token_file)
        self.scopes = GMAIL_READONLY_SCOPES
        self._service: Resource | None = None
        self.last_error: str | None = None

    def authenticate(self) -> Credentials:
        """Load, refresh, or create OAuth2 credentials for Gmail."""
        credentials = self._load_token()

        if credentials and credentials.valid:
            logger.debug("Using existing Gmail OAuth token")
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired Gmail OAuth token")
            credentials.refresh(Request())
            self._save_token(credentials)
            return credentials

        if not self.credentials_path.exists():
            msg = (
                "Gmail credentials file was not found at "
                f"{self.credentials_path}. Add OAuth client credentials before fetching Gmail."
            )
            raise GmailAuthenticationError(msg)

        logger.info("Starting Gmail OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            self.scopes,
        )
        credentials = flow.run_local_server(port=0)
        self._save_token(credentials)
        return credentials

    def fetch_latest_emails(self, max_results: int = 100) -> list[GmailEmail]:
        """Fetch latest inbox emails from the current day."""
        from datetime import datetime

        today_str = datetime.now().strftime("%Y/%m/%d")
        query = f"in:inbox after:{today_str}"
        return self._search(query=query, max_results=max_results)

    def fetch_recent_messages(self, max_results: int = 100) -> list[dict[str, Any]]:
        """Fetch latest inbox emails from the current day as dictionaries."""
        emails = self.fetch_latest_emails(max_results=max_results)
        return [asdict(email) for email in emails]

    def fetch_unread_emails(self, max_results: int = 10) -> list[GmailEmail]:
        """Fetch unread inbox emails."""
        return self._search(query="in:inbox is:unread", max_results=max_results)

    def search_emails_by_keywords(
        self,
        keywords: list[str],
        max_results: int = 10,
    ) -> list[GmailEmail]:
        """Search inbox emails using one or more keywords."""
        keyword_query = " OR ".join(f'"{keyword}"' for keyword in keywords if keyword.strip())
        query = f"in:inbox ({keyword_query})" if keyword_query else "in:inbox"
        return self._search(query=query, max_results=max_results)

    def _get_service(self) -> Resource:
        if self._service is None:
            credentials = self.authenticate()
            self._service = build("gmail", "v1", credentials=credentials)
        return self._service

    def _search(self, query: str, max_results: int) -> list[GmailEmail]:
        try:
            self.last_error = None
            service = self._get_service()
            logger.info("Searching Gmail with query: %s", query)
            response = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            message_refs = response.get("messages", [])
            logger.info("Found %s Gmail messages", len(message_refs))

            return [self._fetch_message(message_ref["id"]) for message_ref in message_refs]
        except GmailAuthenticationError as error:
            self.last_error = str(error)
            if self.settings.is_production:
                raise
            logger.warning("%s", error)
            return []
        except HttpError as error:
            self.last_error = str(error)
            if self.settings.is_production:
                raise
            logger.exception("Gmail API request failed: %s", error)
            return []

    def fetch_message(self, message_id: str) -> dict[str, Any]:
        """Fetch a specific message by ID."""
        return asdict(self._fetch_message(message_id))

    def _fetch_message(self, message_id: str) -> GmailEmail:
        service = self._get_service()
        raw_message = (
            service.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        return parse_gmail_message(raw_message)

    def _load_token(self) -> Credentials | None:
        if not self.token_path.exists():
            return None

        try:
            return Credentials.from_authorized_user_file(str(self.token_path), self.scopes)
        except Exception as error:
            logger.warning(
                "Corrupted or invalid Gmail token file found: %s. Auto-deleting file.", error
            )
            try:
                self.token_path.unlink(missing_ok=True)
            except OSError as unlink_error:
                logger.error("Failed to delete corrupted token file: %s", unlink_error)
            return None

    def _save_token(self, credentials: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")

        # Best-effort local hardening. On Windows this is limited, but harmless.
        try:
            os.chmod(self.token_path, 0o600)
        except OSError as error:
            logger.debug("Could not update token file permissions: %s", error)
