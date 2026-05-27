"""Test Gmail API authentication and print the latest inbox email subjects.

This script is intentionally standalone and beginner-friendly. Run it when you
want to confirm that your Gmail OAuth credentials are working before wiring the
full Placement Mail Tracker workflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Gmail readonly scope lets this script read your mailbox but not modify emails.
GMAIL_READONLY_SCOPE = ["https://www.googleapis.com/auth/gmail.readonly"]

# Keep credentials and tokens out of source control. These paths match .gitignore.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_FILE = PROJECT_ROOT / "config" / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "config" / "token.json"


def load_or_create_credentials() -> Credentials:
    """Load an existing token or run the OAuth browser login flow."""
    credentials = None

    # token.json is created after your first successful browser login.
    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            GMAIL_READONLY_SCOPE,
        )

    # If the token exists and is still valid, reuse it.
    if credentials and credentials.valid:
        return credentials

    # If the token expired, refresh it without opening the browser again.
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_credentials(credentials)
        return credentials

    # If there is no usable token, start the browser-based OAuth login.
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Missing OAuth credentials file: {CREDENTIALS_FILE}\n"
            "Download OAuth client credentials from Google Cloud Console and "
            "save them as config/credentials.json."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        GMAIL_READONLY_SCOPE,
    )
    credentials = flow.run_local_server(port=0)
    save_credentials(credentials)
    return credentials


def save_credentials(credentials: Credentials) -> None:
    """Save OAuth credentials to token.json for future runs."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")


def get_latest_message_ids(credentials: Credentials, max_results: int = 5) -> list[str]:
    """Return Gmail message IDs for the latest inbox emails."""
    service = build("gmail", "v1", credentials=credentials)
    response = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        .execute()
    )
    messages = response.get("messages", [])
    return [message["id"] for message in messages]


def get_email_subject(credentials: Credentials, message_id: str) -> str:
    """Fetch one Gmail message and return its Subject header."""
    service = build("gmail", "v1", credentials=credentials)
    message = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["Subject"],
        )
        .execute()
    )

    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header.get("name", "").lower() == "subject":
            return header.get("value", "(no subject)")

    return "(no subject)"


def print_latest_subjects() -> None:
    """Authenticate with Gmail and print the latest 5 inbox subjects."""
    credentials = load_or_create_credentials()
    message_ids = get_latest_message_ids(credentials, max_results=5)

    if not message_ids:
        print("No inbox emails found.")
        return

    print("Latest 5 Gmail inbox subjects:")
    for index, message_id in enumerate(message_ids, start=1):
        subject = get_email_subject(credentials, message_id)
        print(f"{index}. {subject}")


def main() -> int:
    """Run the Gmail auth test script with friendly errors."""
    try:
        print_latest_subjects()
    except FileNotFoundError as error:
        print(f"Setup error:\n{error}", file=sys.stderr)
        return 1
    except RefreshError as error:
        print(
            "Gmail token refresh failed. Delete config/token.json and run again.\n"
            f"Details: {error}",
            file=sys.stderr,
        )
        return 1
    except GoogleAuthError as error:
        print(f"Google authentication failed: {error}", file=sys.stderr)
        return 1
    except HttpError as error:
        print(f"Gmail API request failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
