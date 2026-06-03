"""Full reset script to completely wipe local databases and clear Google Sheets."""

import sys
import logging
from pathlib import Path

# Add src to PYTHONPATH so we can import project modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from placement_mail_tracker.config.settings import Settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.sheets.sheets_sync import GoogleSheetsSync

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# The exact headers requested in Issue 6
REQUESTED_HEADERS = [
    "Received", "Company", "Program", "Status", 
    "Deadline", "Next Event", "Latest Update", "Gmail Link"
]

def confirm_reset() -> bool:
    """Prompt the user for safety confirmation before resetting data."""
    print("WARNING:")
    print("This will permanently delete all placement data.\n")
    response = input("Type YES to continue: ")
    return response.strip() == "YES"

def clear_sqlite_database(settings: Settings) -> None:
    """Clear SQLite database tables, rebuild schema, and recreate indexes."""
    db_url = settings.database_url
    if not db_url.startswith("sqlite:///"):
        logger.error("Only SQLite reset is supported automatically.")
        return
        
    db_path = Path(db_url.replace("sqlite:///", ""))
    logger.info(f"Clearing SQLite database at {db_path}...")
    
    manager = DatabaseManager(database_path=db_path)
    
    # 1. Clear existing data (Issue 5)
    tables_to_drop = [
        "opportunities",
        "processed_emails",
        "status_history",
        "notification_logs"
    ]
    for table in tables_to_drop:
        try:
            manager.connection.execute(f"DROP TABLE IF EXISTS {table};")
        except Exception as e:
            logger.warning(f"Failed to drop table {table}: {e}")
            
    manager.connection.commit()
    logger.info("Dropped existing tables.")
    
    # 4 & 5. Rebuild schema and recreate indexes
    manager.create_tables()
    logger.info("Rebuilt schema and recreated indexes.")
    logger.info("SQLite database reset complete.")

def clear_google_sheets(settings: Settings) -> None:
    """Clear Google Sheet data rows, preserving and enforcing headers."""
    logger.info("Clearing Google Sheet rows...")
    sync = GoogleSheetsSync(settings=settings)
    
    try:
        service = sync._get_service()
    except Exception as e:
        logger.warning(f"Could not connect to Google Sheets API: {e}")
        return
        
    sheet_id = settings.google_sheet_id
    if not sheet_id:
        logger.warning("No GOOGLE_SHEET_ID configured. Skipping Sheets reset.")
        return
        
    main_tab = settings.google_sheet_name
    tabs_to_reset = [main_tab, "Active Opportunities", "Company History", "Dashboard"]
    
    for tab in tabs_to_reset:
        try:
            # 2. Clear Google Sheet rows (preserve headers by clearing A2:Z)
            range_to_clear = f"'{tab}'!A2:Z"
            service.spreadsheets().values().clear(
                spreadsheetId=sheet_id,
                range=range_to_clear
            ).execute()
            logger.info(f"Cleared data rows in tab: {tab}")
        except Exception:
            pass # Tab might not exist, which is fine
            
    # 3. Preserve/Enforce headers on the primary sheet to strictly match Issue 6
    try:
        if hasattr(sync, '_ensure_tabs_exist'):
            sync._ensure_tabs_exist()
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{main_tab}'!A1:Z1",
            valueInputOption="RAW",
            body={"values": [REQUESTED_HEADERS]}
        ).execute()
        logger.info(f"Enforced requested headers on primary tab '{main_tab}'.")
    except Exception as e:
        logger.warning(f"Failed to write headers to '{main_tab}': {e}")
        
    logger.info("Google Sheets reset complete. Only headers remain.")

def main() -> None:
    # Issue 7: Add Safety
    if not confirm_reset():
        print("Reset aborted.")
        sys.exit(0)
        
    print("\nStarting reset process...")
    settings = Settings()
    
    # Issue 5: Full database reset
    clear_sqlite_database(settings)
    
    # Issue 6: Google Sheets reset
    clear_google_sheets(settings)
    
    print("\nReset successfully completed! The project is now clean.")

if __name__ == "__main__":
    main()
