"""Backfill an opportunity's ppt_date and push it to the calendar right now,
without waiting for the next scheduled run.

Generalized from the original Unilever-only version -- Cognizant's PPT hit
the exact same "already processed before the fix landed" gap the very next
day. Why this stays a script instead of happening automatically: writing to
the production database is blocked by this environment's own permission
guard (the same restriction hit on the Foodhub repair), so this is a
reviewable script for you to run yourself rather than a claim that it's
already done.

Does two things, in order:
1. Sets the given opportunity's ppt_date via
   DatabaseManager.insert_or_update_opportunity (the same code path a real
   run uses -- not a raw SQL patch).
2. Immediately calls CalendarSyncEngine.sync(dry_run=False) -- the exact
   same call scheduler/runner.py makes on a normal cycle -- so the new PPT
   event is pushed to Google Calendar in this same run.

Usage:
    python scripts/backfill_ppt_date_and_sync.py <opportunity_id> <ppt_date_iso> [db_path]

Example (Cognizant, PPT "27th August 2026 by 10.30 am"):
    python scripts/backfill_ppt_date_and_sync.py 271 2026-08-27T10:30

Requires CALENDAR_SYNC_ENABLED=true in .env (already set) and a valid
config/calendar_token.json (already present).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from placement_mail_tracker.calendar_sync.client import GoogleCalendarClient  # noqa: E402
from placement_mail_tracker.calendar_sync.sync import CalendarSyncEngine  # noqa: E402
from placement_mail_tracker.config.settings import Settings  # noqa: E402
from placement_mail_tracker.db.manager import DatabaseManager  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    opportunity_id = int(sys.argv[1])
    ppt_date = sys.argv[2]
    db_path = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "data" / "placement_mail_tracker.db"

    settings = Settings()
    db = DatabaseManager(database_path=db_path)

    row = db.fetch_opportunity_by_id(opportunity_id)
    if row is None:
        print(f"opportunity_id={opportunity_id} not found -- nothing to do.")
        return
    print(f"Before: company_name={row['company_name']!r} ppt_date={row.get('ppt_date')!r}")

    updated_id, created = db.insert_or_update_opportunity(
        {"company_name": row["company_name"], "role": row["role"], "ppt_date": ppt_date},
        matched_opportunity_id=opportunity_id,
    )
    print(f"Backfilled ppt_date={ppt_date!r} on opportunity_id={updated_id} (created={created})")

    if not settings.calendar_sync_enabled:
        print("CALENDAR_SYNC_ENABLED is not true in .env -- skipping the live sync push.")
        return

    client = GoogleCalendarClient(settings)
    engine = CalendarSyncEngine(db, client, settings)
    result = engine.sync(dry_run=False)
    print(
        f"Calendar sync: inserted={result.inserted} patched={result.patched} "
        f"unchanged={result.unchanged}"
    )
    for line in result.flagged:
        print(f"  flagged: {line}")


if __name__ == "__main__":
    main()
