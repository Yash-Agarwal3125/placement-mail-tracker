"""Reset the database and fetch-state so all Gmail history is re-processed.

Usage:
    python scripts/reset_and_rebuild.py [--since YYYY-MM-DD]

After running this script, run `python main.py` to re-sync everything from Gmail.

Defaults:
    --since  defaults to 2025-01-01 (re-fetch one full placement season).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve project root (two levels above this script)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "placement_mail_tracker.db"
FETCH_STATE_PATH = DATA_DIR / "fetch_state.json"

DEFAULT_SINCE = "2025-01-01"


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset placement tracker data for a full rebuild.")
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=f"Re-fetch Gmail from this date (YYYY-MM-DD). Default: {DEFAULT_SINCE}",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    args = parser.parse_args()

    try:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"ERROR: Invalid date '{args.since}'. Use YYYY-MM-DD format.")
        sys.exit(1)

    if not args.yes:
        print("This will DELETE all opportunity and processed-email records from the database")
        print(f"and reset the Gmail fetch window to {args.since}.")
        print(f"Database: {DB_PATH}")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # 1. Clear database tables
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Nothing to clear.")
    else:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            with conn:
                deleted_opps = conn.execute("DELETE FROM opportunities").rowcount
                deleted_emails = conn.execute("DELETE FROM processed_emails").rowcount
            print(f"Deleted {deleted_opps} opportunities and {deleted_emails} processed emails.")
        finally:
            conn.close()

    # 2. Reset fetch-state
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetch_state = {"last_successful_fetch": since_dt.isoformat().replace("+00:00", "Z")}
    FETCH_STATE_PATH.write_text(json.dumps(fetch_state, indent=2), encoding="utf-8")
    print(f"Fetch window reset to {args.since}. Next run will re-fetch all mail since then.")
    print("Run `python main.py` to rebuild from Gmail history.")


if __name__ == "__main__":
    main()
