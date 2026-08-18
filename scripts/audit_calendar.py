"""One command that explains the calendar (safety-nets plan, Phase 2 + 4).

Read-only, plain text, greppable. No flags that write anything, and this
script issues no INSERT/UPDATE/DELETE of its own, so the explicit-
``commit()`` convention other standalone scripts in this repo follow does
not apply here -- there is nothing it writes to flush.

CAVEAT -- opening the database is not a no-op. ``DatabaseManager.__init__``
always runs its idempotent startup migration (``CREATE TABLE IF NOT
EXISTS`` + ``ALTER TABLE ... ADD COLUMN`` for any column introduced since
the file was last opened) and commits it. Concretely: the first time this
script (or anything else) opens a database file predating this change, it
applies the safety-nets Phase 1 ``unmatched_confirmations.resolved``/
``resolved_at`` columns to that file. That is the intended, idempotent way
those columns land -- "migrations ship as code, the operator applies them"
means *running any DatabaseManager-based tool* against the live file is the
apply step, and this script is one such tool. It does not touch table
*rows* -- no opportunity, calendar_events, or unmatched_confirmations data
is ever written by this script itself.

Part 4 (reverse reconciliation) can also emit one side effect that isn't a
Calendar or DB write: if the stored Calendar OAuth token exists but is
expired/dead, ``GoogleCalendarClient.authenticate()`` fires a one-shot SMTP
"OAuth dead" alert before raising -- this script catches the resulting
error and still prints Parts 1-2, but the alert email itself already went
out by then. Use ``--skip-calendar`` to avoid any Google API contact at
all, including that possibility. It never creates the named calendar if
it's missing (see ``GoogleCalendarClient.find_calendar_id``, the read-only
counterpart to ``ensure_calendar``), and never inserts/patches/deletes any
Calendar event.

Part 1 -- every event that WOULD be derived for the calendar today (company,
drive id, event type, date, drive_kind, eligibility_status, my_status,
roster verdict, and the reason it survived filtering). This reuses the real
production ``derive_events()`` so it can never drift from what a live
calendar sync run actually decides.

Part 2 -- the inverse, and the half that actually matters: every drive with
a recent mail and a parseable date but NO derived event, and the reason it
was excluded. A missing Honeywell event went unnoticed for days precisely
because nothing ever asked "what is missing?".

Part 3 -- reverse reconciliation (calendar -> database), Phase 4 of the same
plan: enumerate every event actually on Google Calendar, diff against
``calendar_events`` rows holding a non-null ``gcal_event_id``, and report
anything on Google with no backing row. REPORT ONLY -- never deletes. A
user's own manually-created event on this calendar is indistinguishable
from an orphan, so deletion stays a manual, human decision. Skipped
automatically (with a clear note, not a crash) when Calendar credentials
aren't set up, and skippable explicitly with ``--skip-calendar``.

Usage
-----
    python scripts/audit_calendar.py --db-path data/placement_mail_tracker.db
    python scripts/audit_calendar.py --db-path /tmp/tracker_copy.db --skip-calendar
    python scripts/audit_calendar.py --db-path /tmp/tracker_copy.db --recent-days 21

``--db-path`` is required and has no default -- mirrors every other script
under ``scripts/`` (``backfill_orphaned_confirmations.py``,
``backfill_drive_identity_merge.py``). Point it at a copy to experiment
freely; pointing it at the live file is safe too since this script never
writes, but the explicit-path requirement keeps that an informed choice
rather than an accident.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from placement_mail_tracker.calendar_sync.derive import (  # noqa: E402
    ROUND_ORDER,
    derive_events,
    is_deadline_gated_out,
    is_round_excluded,
)
from placement_mail_tracker.config.settings import Settings  # noqa: E402
from placement_mail_tracker.db.manager import (  # noqa: E402
    ACTIVE_CURRENT_STATUSES,
    DatabaseManager,
)
from placement_mail_tracker.utils.time import parse_event_datetime  # noqa: E402

_UNIDENTIFIED_COMPANIES = frozenset({"", "unknown", "unknown company"})


def _is_identifiable_company(name: str | None) -> bool:
    return bool(name) and str(name).strip().casefold() not in _UNIDENTIFIED_COMPANIES


def _survival_reason(
    opp: dict[str, Any], event_type: str, roster_verdicts: dict, settings: Settings
) -> str:
    """Why this event was admitted (mirrors derive_events' own gates)."""
    if event_type == "DEADLINE":
        my_status = opp.get("my_status") or "NOT_APPLIED"
        if (opp.get("priority") or "").upper() == "HIGH":
            return f"priority=HIGH (my_status={my_status})"
        return f"demonstrated interest (my_status={my_status})"

    reasons = []
    my_status = opp.get("my_status") or "NOT_APPLIED"
    if settings.calendar_sync_mode == "all_eligible":
        reasons.append("calendar_sync_mode=all_eligible")
    if my_status not in ("NOT_APPLIED", "", None):
        reasons.append(f"my_status={my_status}")
    verdict_row = roster_verdicts.get((opp.get("id"), event_type))
    if verdict_row is not None:
        reasons.append(f"roster verdict={verdict_row.get('verdict')} at {event_type}")
    else:
        reasons.append("no roster exclusion")
    return "; ".join(reasons) if reasons else "eligibility gating only"


def _exclusion_reason(
    opp: dict[str, Any], event_type: str, roster_verdicts: dict, settings: Settings
) -> str:
    """Why NO event of this type was derived for this drive."""
    drive_kind = opp.get("drive_kind") or "PLACEMENT"
    if drive_kind != "PLACEMENT":
        return f"drive_kind={drive_kind}, not a placement drive"

    eligibility_status = opp.get("eligibility_status") or ""
    if "NOT_ELIGIBLE" in eligibility_status:
        return f"eligibility_status={eligibility_status}"

    if not _is_identifiable_company(opp.get("company_name")):
        return "company name not identifiable (blank/Unknown)"

    current_status = (opp.get("current_status") or "OPEN").upper()
    if current_status not in ACTIVE_CURRENT_STATUSES:
        return f"current_status={current_status} excluded from the active-drive set"

    # Everything below is checked only for a drive that reached here WITH a
    # parseable date (the caller already filtered on that) -- so "date
    # missing or unparseable" must never be this function's fallback.
    if event_type == "DEADLINE":
        if is_deadline_gated_out(opp):
            return (
                f"no demonstrated interest (my_status={opp.get('my_status') or 'NOT_APPLIED'}) "
                "and priority is not HIGH"
            )
        return _collision_fallback_reason()

    my_status = opp.get("my_status") or "NOT_APPLIED"
    include = settings.calendar_sync_mode == "all_eligible" or my_status not in (
        "NOT_APPLIED", "", None,
    )
    if not include:
        return f"not applied (my_status={my_status}) and calendar_sync_mode != all_eligible"

    if is_round_excluded(roster_verdicts, opp.get("id"), event_type):
        earlier_rounds = (
            ROUND_ORDER[: ROUND_ORDER.index(event_type)] if event_type in ROUND_ORDER else ()
        )
        for earlier in earlier_rounds:
            v = roster_verdicts.get((opp.get("id"), earlier))
            if v is not None and v.get("verdict") == "NOT_MATCHED":
                return f"excluded: NOT_MATCHED verdict cascades from earlier round {earlier}"
        direct = roster_verdicts.get((opp.get("id"), event_type))
        return f"excluded: NOT_MATCHED roster verdict at {event_type} (verdict row: {direct})"

    return _collision_fallback_reason()


def _collision_fallback_reason() -> str:
    """No gate above excluded this event, yet derive_events still didn't
    produce one -- the only remaining production-code explanation is
    ``_apply_collision_guard`` dropping it as a duplicate against another
    opportunity_id with the same normalised company/event_type/date (Phase
    3's whole reason for existing guarantees this can happen). Check the
    anomaly lines printed alongside Part 2 for the specific collision."""
    return (
        "no known gate excluded this -- check the [anomaly] lines below for "
        "a duplicate-event collision drop (see utils.duplicate_drive_detection)"
    )


def audit_active_events(
    database: DatabaseManager, settings: Settings
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (survived_rows, missing_rows) -- Part 1 and Part 2."""
    rows = database.fetch_active_drives_only()
    roster_verdicts = database.fetch_roster_verdicts()
    desired, anomalies = derive_events(rows, settings, roster_verdicts)
    desired_by_key = {(e.opportunity_id, e.event_type): e for e in desired}
    opp_by_id = {r["id"]: r for r in rows}

    survived: list[dict[str, Any]] = []
    for event in desired:
        opp = opp_by_id[event.opportunity_id]
        verdict_row = roster_verdicts.get((event.opportunity_id, event.event_type))
        survived.append(
            {
                "company": opp.get("company_name"),
                "drive_id": opp.get("drive_id"),
                "opportunity_id": opp.get("id"),
                "event_type": event.event_type,
                "date": event.start_iso,
                "drive_kind": opp.get("drive_kind"),
                "eligibility_status": opp.get("eligibility_status"),
                "my_status": opp.get("my_status"),
                "roster_verdict": verdict_row.get("verdict") if verdict_row else "NONE",
                "reason": _survival_reason(opp, event.event_type, roster_verdicts, settings),
            }
        )

    missing: list[dict[str, Any]] = []
    for opp in rows:
        for event_type, date_field in (
            ("DEADLINE", "deadline"),
            ("OA", "oa_date"),
            ("INTERVIEW", "interview_date"),
        ):
            raw_date = opp.get(date_field)
            if not raw_date:
                continue
            # parse_event_datetime, not parse_datetime_flexible -- must agree
            # with derive.py's own _derive_single_event on what counts as
            # "parseable", or Part 2 and the real sync disagree on the exact
            # question this script exists to answer.
            parsed = parse_event_datetime(str(raw_date))
            if parsed is None:
                continue
            if (opp["id"], event_type) in desired_by_key:
                continue
            missing.append(
                {
                    "company": opp.get("company_name"),
                    "drive_id": opp.get("drive_id"),
                    "opportunity_id": opp.get("id"),
                    "event_type": event_type,
                    "date": raw_date,
                    "reason": _exclusion_reason(opp, event_type, roster_verdicts, settings),
                }
            )

    for line in anomalies:
        missing.append(
            {
                "company": None,
                "drive_id": None,
                "opportunity_id": None,
                "event_type": None,
                "date": None,
                "reason": f"anomaly: {line}",
            }
        )

    return survived, missing


def audit_reverse_reconciliation(
    database: DatabaseManager, settings: Settings
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Phase 4: diff Google's actual events against calendar_events state.

    Returns (orphan_events_or_None, skip_reason_or_None). Never raises on a
    missing/dead auth stack -- an audit tool must degrade to "skipped",
    not crash the DB-only half above it.
    """
    token_path = Path(settings.calendar_token_file)
    if not token_path.exists():
        return None, f"no Calendar token at {token_path} -- run with calendar auth set up to enable"

    try:
        from placement_mail_tracker.calendar_sync.client import (
            CalendarAuthenticationError,
            GoogleCalendarClient,
        )

        client = GoogleCalendarClient(settings)
        calendar_id = client.find_calendar_id(settings.calendar_name)
        if calendar_id is None:
            return None, f"no calendar named {settings.calendar_name!r} found on Google"

        google_events = client.list_events(calendar_id)
    except CalendarAuthenticationError as error:
        return None, f"Calendar auth unavailable: {error}"
    except Exception as error:  # noqa: BLE001 - audit tool must degrade, not crash
        return None, f"Calendar API call failed: {error}"

    known_ids = {
        row["gcal_event_id"]
        for row in database.fetch_calendar_event_states()
        if row.get("gcal_event_id")
    }

    orphans = [
        {
            "gcal_event_id": event.get("id"),
            "summary": event.get("summary"),
            "start": (event.get("start") or {}).get("dateTime")
            or (event.get("start") or {}).get("date"),
        }
        for event in google_events
        if event.get("id") not in known_ids
    ]
    return orphans, None


def _print_part1(survived: list[dict[str, Any]]) -> None:
    print("=== PART 1: ACTIVE CALENDAR EVENTS (why each survived filtering) ===")
    if not survived:
        print("(none)")
    for row in sorted(survived, key=lambda r: (str(r["company"]), str(r["event_type"]))):
        print(
            f"{row['company']} | drive_id={row['drive_id']} | opp_id={row['opportunity_id']} | "
            f"type={row['event_type']} | date={row['date']} | drive_kind={row['drive_kind']} | "
            f"eligibility={row['eligibility_status']} | my_status={row['my_status']} | "
            f"roster={row['roster_verdict']} | why={row['reason']}"
        )
    print()


def _print_part2(missing: list[dict[str, Any]]) -> None:
    print("=== PART 2: DATED DRIVES WITH NO CALENDAR EVENT (why each was excluded) ===")
    if not missing:
        print("(none)")
    for row in missing:
        if row["company"] is None:
            # Raw derive_events() anomaly lines -- unparseable dates AND
            # collision-guard duplicate-event drops both surface here.
            print(f"[anomaly] {row['reason']}")
            continue
        print(
            f"{row['company']} | drive_id={row['drive_id']} | opp_id={row['opportunity_id']} | "
            f"type={row['event_type']} | date={row['date']} | why_excluded={row['reason']}"
        )
    print()


def _print_part3(orphans: list[dict[str, Any]] | None, skip_reason: str | None) -> None:
    print("=== PART 3: REVERSE RECONCILIATION (Google events with no DB row) ===")
    if skip_reason is not None:
        print(f"SKIPPED: {skip_reason}")
        print()
        return
    if not orphans:
        print("(none -- every Google event maps to a row this system controls)")
        print()
        return
    print("REPORT ONLY -- nothing is deleted. Review each manually.")
    for orphan in orphans:
        print(
            f"gcal_event_id={orphan['gcal_event_id']} | summary={orphan['summary']!r} | "
            f"start={orphan['start']}"
        )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db-path",
        required=True,
        type=Path,
        help="Path to the SQLite database to read. Read-only -- never written to.",
    )
    parser.add_argument(
        "--skip-calendar",
        action="store_true",
        help="Skip Part 3 (reverse reconciliation) entirely -- no Google API calls at all.",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"Database not found: {args.db_path}", file=sys.stderr)
        return 1

    settings = Settings()
    database = DatabaseManager(args.db_path)

    survived, missing = audit_active_events(database, settings)
    _print_part1(survived)
    _print_part2(missing)

    if args.skip_calendar:
        print("=== PART 3: REVERSE RECONCILIATION (calendar -> database) ===")
        print("SKIPPED: --skip-calendar")
    else:
        orphans, skip_reason = audit_reverse_reconciliation(database, settings)
        _print_part3(orphans, skip_reason)

    print(
        f"Summary: {len(survived)} event(s) on the calendar, "
        f"{len([m for m in missing if m['company'] is not None])} dated drive(s) missing an event, "
        f"{len([m for m in missing if m['company'] is None])} raw anomaly line(s) "
        "(unparseable dates and/or collision-guard drops). "
        f"Run at {datetime.now().isoformat(timespec='seconds')}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
