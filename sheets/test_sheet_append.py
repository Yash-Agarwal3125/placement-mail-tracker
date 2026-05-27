import os
import sys
import logging
from datetime import datetime
from pathlib import Path

# Add src/ to path so we can import modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from placement_mail_tracker.config.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("test_sheet_append")

def main():
    logger.info("==================================================")
    logger.info("MANUAL GOOGLE SHEETS API VERIFICATION & TEST APPEND")
    logger.info("==================================================")

    settings = get_settings()
    sheet_id = settings.google_sheet_id

    if not sheet_id:
        logger.error("[FAILURE] GOOGLE_SHEET_ID is not configured in your settings or .env file!")
        sys.exit(1)

    credentials_file = settings.google_sheets_credentials_file
    token_file = settings.google_sheets_token_file

    logger.info("Target Sheet ID: %s", sheet_id)
    logger.info("Credentials Path: %s", credentials_file)
    logger.info("Token Path: %s", token_file)

    # 1. Authenticate with Google Sheets Scopes
    credentials = None
    token_path = Path(token_file)
    if token_path.exists():
        logger.info("Loading Sheets OAuth token from %s", token_file)
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_path),
                ["https://www.googleapis.com/auth/spreadsheets"]
            )
        except Exception as e:
            logger.warning("Failed to load Sheets token file: %s", e)

    if credentials and credentials.expired and credentials.refresh_token:
        logger.info("Refreshing expired Sheets OAuth token")
        try:
            credentials.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to refresh Sheets OAuth token: %s", e)
            credentials = None

    if not credentials or not credentials.valid:
        logger.info("Starting Google Sheets OAuth flow")
        credentials_path = Path(credentials_file)
        if not credentials_path.exists():
            logger.error("[FAILURE] Credentials file does not exist at: %s", credentials_file)
            logger.error("Please place credentials.json in config/ directory first.")
            sys.exit(1)

        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path),
            ["https://www.googleapis.com/auth/spreadsheets"]
        )
        credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        logger.info("Saved new Sheets OAuth token to %s", token_file)

    # 2. Build Google Sheets Service
    logger.info("Building Google Sheets API v4 service resource")
    try:
        service = build("sheets", "v4", credentials=credentials)
    except Exception as e:
        logger.error("[FAILURE] Failed to build Sheets API service: %s", e)
        sys.exit(1)

    # 3. Dynamically discover available sheet names
    logger.info("Querying spreadsheet metadata to discover sheet names")
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheets = spreadsheet.get("sheets", [])
        sheet_names = [s.get("properties", {}).get("title") for s in sheets if s.get("properties", {}).get("title")]
        logger.info("Discovered sheet names: %s", sheet_names)
        
        if not sheet_names:
            logger.error("[FAILURE] No sheets found in the spreadsheet!")
            sys.exit(1)
            
        # Determine target sheet name
        configured_name = settings.google_sheet_name
        # Fallback to GOOGLE_SHEETS_NAME (plural) if configured in .env
        env_plural_name = os.environ.get("GOOGLE_SHEETS_NAME")
        
        target_sheet_name = sheet_names[0]  # Default to first sheet
        
        if configured_name in sheet_names:
            target_sheet_name = configured_name
            logger.info("Using configured sheet name: %s", target_sheet_name)
        elif env_plural_name in sheet_names:
            target_sheet_name = env_plural_name
            logger.info("Using .env GOOGLE_SHEETS_NAME: %s", target_sheet_name)
        else:
            logger.info("Configured sheet '%s' not found. Self-healing fallback to first sheet: '%s'", configured_name, target_sheet_name)
            
    except Exception as e:
        logger.error("[FAILURE] Failed to retrieve spreadsheet metadata: %s", e)
        logger.error("Please verify that your Google OAuth account is added as a 'Test User'")
        logger.error("and has edit permissions on Google Sheet ID: %s", sheet_id)
        sys.exit(1)

    # 4. Append Example Test Row
    current_timestamp = datetime.now().isoformat()
    test_row = ["TEST", "PIPELINE", "WORKING", current_timestamp]
    
    # We append to columns A-D in the target sheet
    sheet_range = f"'{target_sheet_name}'!A:D"

    logger.info("Attempting to append test row to Google Sheet: %s", test_row)
    try:
        response = service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=sheet_range,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [test_row]}
        ).execute()

        logger.info("==================================================")
        logger.info("[SUCCESS] Google Sheets sync append completed successfully!")
        logger.info("API Response Details: %s", response)
        logger.info("==================================================")
    except Exception as e:
        logger.error("==================================================")
        logger.error("[FAILURE] Failed to append test row to Google Sheets: %s", e)
        logger.error("Please check if the sheet is protected, or if your token has write scopes.")
        logger.error("==================================================")
        sys.exit(1)

if __name__ == "__main__":
    main()
