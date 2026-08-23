"""Clean-start utility: wipe all data and reset to today.

Clears:
  - All SQLite tables (opportunities, companies, updates, processed_emails,
    notifications, sent_alerts)
  - fetch_state.json  -> today (so next run picks up only new mail)
  - heartbeat.json    -> clean slate
  - system_health.json -> clean slate

Keeps:
  - trusted_senders.json (learned filter config, not data)
  - config/, logs/, credentials

Usage:
    python scripts/clean_start.py [--yes]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "placement_mail_tracker.db"

_TABLES_TO_CLEAR = [
    "sent_alerts",
    "notifications",
    "updates",
    "processed_emails",
    "opportunities",
    "companies",
]


def _confirm() -> bool:
    print("This will permanently delete ALL placement data:")
    print(f"  Database : {DB_PATH}")
    print(f"  State    : fetch_state, heartbeat, system_health")
    print(f"  Kept     : trusted_senders.json, config/, logs/")
    answer = input("\nType YES to continue: ").strip()
    return answer == "YES"


def _clear_database() -> None:
    if not DB_PATH.exists():
        print("  [DB] No database file found — skipping.")
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:
        with conn:
            for table in _TABLES_TO_CLEAR:
                try:
                    n = conn.execute(f"DELETE FROM {table}").rowcount
                    print(f"  [DB] {table}: deleted {n} rows")
                except sqlite3.OperationalError:
                    pass  # table doesn't exist yet — that's fine
            # Reset auto-increment counters
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ({})".format(
                    ",".join(f"'{t}'" for t in _TABLES_TO_CLEAR)
                )
            )
    finally:
        conn.close()


def _reset_state_files(today_iso: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    (DATA_DIR / "fetch_state.json").write_text(
        json.dumps({"last_successful_fetch": today_iso}, indent=2),
        encoding="utf-8",
    )
    print(f"  [STATE] fetch_state.json -> {today_iso}")

    (DATA_DIR / "heartbeat.json").write_text(
        json.dumps(
            {
                "drives_created": 0,
                "drives_updated": 0,
                "last_successful_run": None,
                "processed_messages": 0,
                "status": "clean_start",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("  [STATE] heartbeat.json -> reset")

    (DATA_DIR / "system_health.json").write_text(
        json.dumps(
            {
                "alert_sent_for_current_streak": False,
                "consecutive_failures": 0,
                "last_failure": None,
                "last_status": "CLEAN_START",
                "last_success": None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("  [STATE] system_health.json -> reset")


def main() -> None:
    skip_confirm = "--yes" in sys.argv
    if not skip_confirm and not _confirm():
        print("Aborted.")
        sys.exit(0)

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    print("\nClearing database...")
    _clear_database()

    print("\nResetting state files...")
    _reset_state_files(today_iso)

    print("\nDone. Run `python main.py` to start fresh from today.")


if __name__ == "__main__":
    main()
