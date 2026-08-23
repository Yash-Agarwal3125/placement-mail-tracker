"""Put Unilever's PPT on the calendar right now, without waiting for the
next scheduled run.

Why this is a separate script, and why it wasn't done automatically: the
code fix (PPT event type + extraction) only affects mail processed AFTER
the fix landed. Unilever's own email was already marked "processed" before
that, so its opportunity row (id 253) never got a ppt_date backfilled --
the code being correct doesn't retroactively re-read an email already
marked done. Fixing that here requires writing to the production database,
which this environment's own permission guard blocks me from doing
directly (the same restriction hit earlier on the Foodhub repair) -- so,
same as that one, this is a reviewable script for you to run yourself
rather than a claim that it's already done.

This script does two things, in order:
1. Sets opportunity 253's ppt_date via DatabaseManager.insert_or_update_opportunity
   (the same code path a real run uses -- not a raw SQL patch).
2. Immediately calls CalendarSyncEngine.sync(dry_run=False) -- the exact
   same call scheduler/runner.py makes on a normal cycle -- so the new PPT
   event is pushed to Google Calendar in this same run, instead of waiting
   for the next 3-hourly scheduled one.

Run once: `python scripts/backfill_unilever_ppt_and_sync.py`
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

OPPORTUNITY_ID = 253
PPT_DATE = "2026-08-24T15:30"  # from the real email: "24th August 2026 3.30 pm"


def main() -> None:
    settings = Settings()
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "placement_mail_tracker.db"
    db = DatabaseManager(database_path=db_path)

    row = db.fetch_opportunity_by_id(OPPORTUNITY_ID)
    if row is None:
        print(f"opportunity_id={OPPORTUNITY_ID} not found -- nothing to do.")
        return
    print(f"Before: company_name={row['company_name']!r} ppt_date={row.get('ppt_date')!r}")

    opportunity_id, created = db.insert_or_update_opportunity(
        {"company_name": row["company_name"], "role": row["role"], "ppt_date": PPT_DATE},
        matched_opportunity_id=OPPORTUNITY_ID,
    )
    print(f"Backfilled ppt_date={PPT_DATE!r} on opportunity_id={opportunity_id} (created={created})")

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
