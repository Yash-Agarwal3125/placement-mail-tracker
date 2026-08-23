"""One-off data repairs for two roster-verdict bugs found on 2026-08-23.

Bug 1 -- opportunity 182 (Foodhub, "Software Engineer Intern"):
A second Foodhub thread (Full Stack Developer role, opportunity 244) got a
real Google Calendar interview event created on 2026-08-14. A duplicate-merge
backfill folded opportunity 244 into this older row (182) on 2026-08-18. That
merge re-pointed the calendar event onto opportunity 182 -- which was still
carrying a stale INTERVIEW=NOT_MATCHED roster verdict from 2026-08-08,
predating the interview entirely. That stale verdict caused the sync to
delete the real, just-merged-in calendar event. Separately, this row's
company_name had become the literal placeholder "Unknown" (the system's
"ambiguous merge, needs human verification" marker), which permanently
excludes it from ever being reconsidered by calendar_sync/derive.py (fixed
in code -- see derive.py's comment on the "Unknown"-company gate).

Note: opportunity 182's own interview_date is 2026-08-20, already past by the
time this was found (2026-08-23) -- check with Foodhub directly if you're
unsure whether you missed it. (An earlier draft of this docstring wrongly
attributed a 2026-08-31 tentative date here; that date belongs to a
different company, Spense, not Foodhub.)

Bug 2 -- opportunity 253 (Unilever, PPT "applied students" list):
extraction/roster.py's verify_roster() only ever saw the first 3000 chars of
an attachment (ai/attachments.py's MAX_ATTACHMENT_CHARS, a cap that exists
for Gemini prompt cost control -- roster verification has no API cost and
never needed it). Unilever's applied-students .xlsx is ~69K chars extracted;
the user's Neo ID appears well past char 3000, so the roster check recorded
a false NOT_MATCHED even though the ID is genuinely on the list (confirmed
by re-running verify_roster against the real, untruncated attachment: it
returns MATCHED / codename). Fixed in code -- see attachments.py/runner.py's
new max_chars threading, with a much larger cap for the roster path only.
This script clears the resulting stale wrong verdict so it doesn't cascade-
exclude a later Unilever OA/INTERVIEW calendar event the same way Bug 1 did.

This script, for each opportunity:
1. Restores company_name where it had degraded to "Unknown" (182 only) --
   the one certain fact, so the calendar gate no longer blocks the drive.
   Deliberately does NOT touch role/action_required where genuinely
   ambiguous -- that still needs your own judgment call.
2. Drops the specific stale/wrong roster_verdicts row.
3. Un-freezes any calendar_events row that had been excluded because of it.
4. Leaves an `updates` audit row documenting the fix.

Run once: `python scripts/fix_opportunity_182_foodhub.py`
Then run a normal sync (`python main.py`) to let it re-evaluate both drives.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "placement_mail_tracker.db"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def fix_foodhub(cur: sqlite3.Cursor, now: str) -> None:
    opportunity_id = 182
    calendar_event_row_id = 5623  # the INTERVIEW row for this opportunity

    cur.execute(
        "SELECT company_name, role, action_required FROM opportunities WHERE id = ?",
        (opportunity_id,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"opportunity_id={opportunity_id} not found -- skipping Foodhub fix.")
        return
    print(f"[Foodhub] Before: company_name={row[0]!r} role={row[1]!r} action_required={row[2]!r}")

    cur.execute(
        "UPDATE opportunities SET company_name = 'Foodhub', updated_at = ? WHERE id = ?",
        (now, opportunity_id),
    )
    cur.execute(
        "DELETE FROM roster_verdicts WHERE opportunity_id = ? AND event_type = 'INTERVIEW'",
        (opportunity_id,),
    )
    cur.execute(
        "UPDATE calendar_events SET status = 'active', updated_at = ? WHERE id = ?",
        (now, calendar_event_row_id),
    )
    cur.execute(
        "INSERT INTO updates (opportunity_id, update_type, notes, created_at) VALUES (?, ?, ?, ?)",
        (
            opportunity_id,
            "manual_fix",
            "Restored company_name (Unknown -> Foodhub) and dropped a stale pre-merge "
            "INTERVIEW roster_verdicts row (2026-08-08, predated the actual interview) "
            "that had caused the real interview calendar event to be deleted and "
            "permanently excluded. role/action_required left as-is for manual review.",
            now,
        ),
    )
    cur.execute(
        "SELECT company_name, role, action_required FROM opportunities WHERE id = ?",
        (opportunity_id,),
    )
    print(f"[Foodhub] After:  {cur.fetchone()}")


def fix_unilever(cur: sqlite3.Cursor, now: str) -> None:
    opportunity_id = 253

    cur.execute(
        "SELECT verdict, method, verified_at FROM roster_verdicts "
        "WHERE opportunity_id = ? AND event_type = 'OA'",
        (opportunity_id,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"opportunity_id={opportunity_id} has no OA roster_verdicts row -- skipping.")
        return
    print(f"[Unilever] Before: {row}")

    cur.execute(
        "DELETE FROM roster_verdicts WHERE opportunity_id = ? AND event_type = 'OA'",
        (opportunity_id,),
    )
    cur.execute(
        "INSERT INTO updates (opportunity_id, update_type, notes, created_at) VALUES (?, ?, ?, ?)",
        (
            opportunity_id,
            "manual_fix",
            "Dropped a wrong OA roster_verdicts row (NOT_MATCHED) caused by the "
            "3000-char attachment-truncation bug (fixed in ai/attachments.py + "
            "scheduler/runner.py) -- the user's Neo ID is genuinely on Unilever's "
            "applied-students list (confirmed MATCHED against the real, untruncated "
            "attachment). Next OA/INTERVIEW-round mail will re-verify correctly.",
            now,
        ),
    )
    print("[Unilever] Stale OA verdict cleared; will re-verify on the next relevant mail.")


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = _now()

    fix_foodhub(cur, now)
    fix_unilever(cur, now)

    con.commit()
    con.close()
    print("Done. Run `python main.py` to let calendar sync reconsider both drives.")


if __name__ == "__main__":
    main()
