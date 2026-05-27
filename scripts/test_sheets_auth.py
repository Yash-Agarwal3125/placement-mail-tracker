"""Test Google Sheets API authentication by appending one test row.

Before running:
1. Put OAuth client credentials at config/credentials.json.
2. Set GOOGLE_SHEET_ID in your .env file.
3. Make sure the Google Sheets API is enabled in Google Cloud Console.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_FILE = PROJECT_ROOT / "config" / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "config" / "sheets_token.json"

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure simple console logging for this test script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )


def load_environment() -> tuple[str, str]:
    """Load .env and return the Google Sheet ID and sheet name."""
    load_dotenv(PROJECT_ROOT / ".env")

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Opportunities").strip() or "Opportunities"

    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is missing. Add it to your .env file.")

    return sheet_id, sheet_name


def load_or_create_credentials() -> Credentials:
    """Load an existing token or run the OAuth browser login flow."""
    credentials = None

    if TOKEN_FILE.exists():
        logger.info("Loading existing Sheets token from %s", TOKEN_FILE)
        credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SHEETS_SCOPES)

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        logger.info("Refreshing expired Sheets token")
        credentials.refresh(Request())
        save_credentials(credentials)
        return credentials

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Missing OAuth credentials file: {CREDENTIALS_FILE}\n"
            "Download OAuth client credentials from Google Cloud Console and "
            "save them as config/credentials.json."
        )

    logger.info("Opening browser for Google Sheets OAuth login")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SHEETS_SCOPES)
    credentials = flow.run_local_server(port=0)
    save_credentials(credentials)
    return credentials


def save_credentials(credentials: Credentials) -> None:
    """Save OAuth credentials so future runs do not need browser login."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
    logger.info("Saved Sheets token to %s", TOKEN_FILE)


def append_test_row(sheet_id: str, sheet_name: str, credentials: Credentials) -> None:
    """Append one test row to the configured Google Sheet."""
    service = build("sheets", "v4", credentials=credentials)
    values = service.spreadsheets().values()

    row = [
        "test_sync",
        "Google Sheets API connected",
        "Created by scripts/test_sheets_auth.py",
    ]

    logger.info("Appending test row to spreadsheet %s", sheet_id)
    values.append(
        spreadsheetId=sheet_id,
        range=f"{quote_sheet_name(sheet_name)}!A:C",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    logger.info("Test row appended successfully")


def quote_sheet_name(sheet_name: str) -> str:
    """Quote a sheet tab name for use in A1 notation."""
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def main() -> int:
    """Run the Google Sheets API test script."""
    setup_logging()

    try:
        sheet_id, sheet_name = load_environment()
        credentials = load_or_create_credentials()
        append_test_row(sheet_id, sheet_name, credentials)
    except ValueError as error:
        logger.error("%s", error)
        return 1
    except FileNotFoundError as error:
        logger.error("%s", error)
        return 1
    except RefreshError as error:
        logger.error("Token refresh failed. Delete %s and run again: %s", TOKEN_FILE, error)
        return 1
    except GoogleAuthError as error:
        logger.error("Google authentication failed: %s", error)
        return 1
    except HttpError as error:
        logger.error("Google Sheets API request failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
