"""Gmail API client built on OAuth2."""

from __future__ import annotations

import base64
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.reliability.auth_alerts import alert_oauth_dead_once, clear_oauth_alert

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]{2,}")

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
    attachments: list[dict[str, Any]] = field(default_factory=list)


def _strip_html(html_text: str) -> str:
    """Remove HTML tags and decode entities, collapsing whitespace."""
    text = _SCRIPT_STYLE_RE.sub(" ", html_text)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


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


def decode_base64url_bytes(data: str | None) -> bytes:
    """Decode Gmail's URL-safe base64 data to raw bytes, binary-safe.

    Unlike ``decode_base64url`` (which assumes text and decodes to ``str``
    via UTF-8), attachment payloads (PDF/xlsx/image bytes) are binary and
    must never be run through text decoding or they will be corrupted.
    """
    if not data:
        return b""

    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(f"{data}{padding}")
    except (ValueError, TypeError) as error:
        logger.warning("Unable to decode Gmail attachment data: %s", error)
        return b""


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
            html_parts.append(_strip_html(decode_base64url(body_data)))
            return

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    body = "\n".join(part.strip() for part in plain_parts if part.strip())

    if body:
        return body

    return "\n".join(part.strip() for part in html_parts if part.strip())


def extract_attachment_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect real attachment inventory (not inline text/html) from a payload.

    Any part with a ``body.attachmentId`` is a fetchable attachment whose
    bytes live outside the message payload (Gmail returns them separately);
    ``extract_body_text`` never sees or decodes them. This only records
    lightweight metadata — filename, MIME type, and the ID needed to fetch
    the bytes later via ``GmailClient.fetch_attachment_bytes`` — so building
    this inventory costs nothing extra; the bytes are fetched lazily, only
    for the mails where Gemini ends up being called.
    """
    attachments: list[dict[str, Any]] = []

    def walk(part: dict[str, Any]) -> None:
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        if attachment_id:
            attachments.append(
                {
                    "filename": part.get("filename", ""),
                    "mimeType": part.get("mimeType", ""),
                    "attachmentId": attachment_id,
                    "size": body.get("size", 0),
                }
            )
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return attachments


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
        attachments=extract_attachment_parts(payload),
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
            clear_oauth_alert("Gmail")
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired Gmail OAuth token")
            try:
                credentials.refresh(Request())
            except RefreshError as error:
                msg = f"OAuth dead — re-consent needed for Gmail: {error}"
                alert_oauth_dead_once("Gmail", msg, self.settings)
                raise GmailAuthenticationError(msg) from error
            self._save_token(credentials)
            clear_oauth_alert("Gmail")
            return credentials

        if not self.credentials_path.exists():
            msg = (
                "Gmail credentials file was not found at "
                f"{self.credentials_path}. Add OAuth client credentials before fetching Gmail."
            )
            raise GmailAuthenticationError(msg)

        if not sys.stdin.isatty():
            # A scheduled run has no interactive terminal to complete a fresh
            # consent flow — run_local_server would otherwise hang up to 120s
            # and fail anyway. Fail fast with a clear alert instead.
            msg = (
                "OAuth dead — re-consent needed for Gmail: no valid/refreshable "
                "token and this run is not interactive"
            )
            alert_oauth_dead_once("Gmail", msg, self.settings)
            raise GmailAuthenticationError(msg)

        logger.info("Starting Gmail OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            self.scopes,
        )
        credentials = flow.run_local_server(port=0, timeout_seconds=120)
        self._save_token(credentials)
        clear_oauth_alert("Gmail")
        return credentials

    def fetch_emails_since(
        self, timestamp_seconds: int, max_results: int = 500
    ) -> list[GmailEmail]:
        """Fetch inbox emails newer than the specified Unix timestamp."""
        query = f"after:{timestamp_seconds}"
        return self._search(query=query, max_results=max_results)

    def fetch_recent_messages_since(
        self, timestamp_seconds: int, max_results: int = 500
    ) -> list[dict[str, Any]]:
        """Fetch inbox emails newer than the specified timestamp as dictionaries."""
        emails = self.fetch_emails_since(
            timestamp_seconds=timestamp_seconds, max_results=max_results
        )
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

    def fetch_attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        """Fetch and decode a single attachment's raw bytes.

        Attachment bytes are not part of the message payload returned by
        ``messages().get`` — they must be fetched separately per attachment
        ID. Used lazily (only when Gemini is about to run on a mail that has
        attachments), so this never runs for the common no-attachment case.
        """
        service = self._get_service()
        result = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return decode_base64url_bytes(result.get("data"))

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
