"""One-off data repair for opportunity 182 (Foodhub).

Root cause (see docs/design if you want the full writeup, or ask the agent
that produced this script): a second Foodhub thread (Full Stack Developer
role, opportunity 244) got a real Google Calendar interview event created on
2026-08-14. A duplicate-merge backfill folded opportunity 244 into this
older row (182, "Software Engineer Intern") on 2026-08-18. That merge
re-pointed the calendar event onto opportunity 182 -- which was still
carrying a stale INTERVIEW=NOT_MATCHED roster verdict from 2026-08-08,
predating the interview entirely. That stale verdict caused the sync to
delete the real, just-merged-in calendar event. Separately, this row's
company_name had become the literal placeholder "Unknown" (the system's
"ambiguous merge, needs human verification" marker), which permanently
excludes it from ever being reconsidered by calendar_sync/derive.py.

This script:
1. Restores company_name (the one certain fact) so the calendar gate no
   longer blocks this drive. Deliberately does NOT touch role or
   action_required -- the Software-Engineer-Intern-vs-Full-Stack-Developer
   ambiguity is real and still needs your own judgment call.
2. Drops the stale pre-merge INTERVIEW roster_verdicts row -- it describes a
   different application context, not this interview.
3. Un-freezes the calendar_events row (status 'excluded' -> 'active') so the
   next `python main.py` run reconsiders it instead of leaving it stuck.
4. Leaves an `updates` audit row documenting the fix.

Run once: `python scripts/fix_opportunity_182_foodhub.py`
Then run a normal sync (`python main.py`) to let it re-evaluate the drive.

Note: the interview date (2026-08-31 per the roster email's tentative date,
2026-08-20 per the merged-in drive row -- another symptom of the same
role/thread ambiguity) may already be in the past by the time you run this.
Check with the company directly if you're not sure whether you missed it.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "placement_mail_tracker.db"
OPPORTUNITY_ID = 182
CALENDAR_EVENT_ROW_ID = 5623  # the INTERVIEW row for this opportunity


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    cur.execute(
        "SELECT company_name, role, action_required FROM opportunities WHERE id = ?",
        (OPPORTUNITY_ID,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"opportunity_id={OPPORTUNITY_ID} not found -- nothing to do.")
        return
    print(f"Before: company_name={row[0]!r} role={row[1]!r} action_required={row[2]!r}")

    cur.execute(
        "UPDATE opportunities SET company_name = 'Foodhub', updated_at = ? WHERE id = ?",
        (now, OPPORTUNITY_ID),
    )
    cur.execute(
        "DELETE FROM roster_verdicts WHERE opportunity_id = ? AND event_type = 'INTERVIEW'",
        (OPPORTUNITY_ID,),
    )
    cur.execute(
        "UPDATE calendar_events SET status = 'active', updated_at = ? WHERE id = ?",
        (now, CALENDAR_EVENT_ROW_ID),
    )
    cur.execute(
        "INSERT INTO updates (opportunity_id, update_type, notes, created_at) VALUES (?, ?, ?, ?)",
        (
            OPPORTUNITY_ID,
            "manual_fix",
            "Restored company_name (Unknown -> Foodhub) and dropped a stale pre-merge "
            "INTERVIEW roster_verdicts row (2026-08-08, predated the actual interview) "
            "that had caused the real interview calendar event to be deleted and "
            "permanently excluded. role/action_required left as-is for manual review.",
            now,
        ),
    )
    con.commit()

    cur.execute(
        "SELECT company_name, role, action_required FROM opportunities WHERE id = ?",
        (OPPORTUNITY_ID,),
    )
    print(f"After:  company_name={cur.fetchone()}")
    con.close()
    print("Done. Run `python main.py` to let calendar sync reconsider this drive.")


if __name__ == "__main__":
    main()
