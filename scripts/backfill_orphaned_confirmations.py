"""Backfill for Phase 6 (calendar-drift remediation plan, Cause 7): orphaned
``APPLICATION_CONFIRMATION`` mails whose ``processed_emails.opportunity_id``
is NULL.

Before Phase 6 shipped, a confirmation mail was recognised, logged into
``processed_emails``, and then attached to no drive at all -- 126+ such mails
sit in the live production database with ``opportunity_id = NULL``, which is
the direct reason ``my_status`` reads ``NOT_APPLIED`` on every row today
(nothing had ever marked a drive as applied-to). Phase 6 fixed the live
pipeline going forward (``scheduler/runner.py::_handle_confirmation_mail``);
this script is the one-off catch-up for the mail that already landed before
the fix.

Company extraction, resolution order
-------------------------------------
``processed_emails`` stores only ``subject`` (no ``body``), so this reuses
exactly the subject-based extraction Phase 6 built
(``extraction.confirmation.extract_company_from_confirmation_subject``) --
the VIT CDC portal's confirmation subjects are formulaic enough to name the
company directly. Once a company is extracted, resolution goes through the
real production code path
(``db.manager.DatabaseManager.insert_or_update_opportunity`` with
``email_classification="APPLICATION_CONFIRMATION"``) so this script can never
drift from the live pipeline's exact hash -> thread_id -> single active
same-company candidate for the year -> compatible program name order. Two or
more surviving candidates are never guessed at -- that method itself routes
them to ``unmatched_confirmations``. Unlike a stage-update mail, a
confirmation is direct proof the drive is real, so (also matching production
behaviour) this script MAY create a new drive when none matches at all.

Where the subject yields no identifiable company (no recognised phrase, or
the candidate normalizes to a rejected junk token), the mail is routed to
``unmatched_confirmations`` instead -- never silently dropped, never guessed.

Safety
------
Dry-run by default -- prints the plan, writes nothing. ``--apply`` is
required to write. ``--db-path`` is required and is never the live database
by default; point it at a copy, review, then apply to the copy, then apply
to the live file yourself -- this script does not do that for you (mirrors
``scripts/backfill_drive_identity_merge.py``'s safety pattern).

Explicit ``connection.commit()`` at the end of ``apply_backfill`` -- a
standalone script has nothing else in-process to flush the transaction (the
exact bug class described in ``db/manager.py``'s
``mark_calendar_event_deleted`` docstring, which cost a live run once
already).

Usage
-----
    cp data/placement_mail_tracker.db /tmp/tracker_copy.db
    python scripts/backfill_orphaned_confirmations.py --db-path /tmp/tracker_copy.db
    python scripts/backfill_orphaned_confirmations.py --db-path /tmp/tracker_copy.db --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from placement_mail_tracker.db.manager import (  # noqa: E402
    ACTIVE_CURRENT_STATUSES,
    DatabaseManager,
)
from placement_mail_tracker.extraction.confirmation import (  # noqa: E402
    extract_company_from_confirmation_subject,
)
from placement_mail_tracker.extraction.rule_engine import normalize_company_name  # noqa: E402
from placement_mail_tracker.utils.time import parse_datetime_flexible  # noqa: E402

logger = logging.getLogger(__name__)


def _row_year(*candidates: str | None) -> str:
    """Best-effort year from the first parseable candidate date string.

    Mirrors ``db.manager._get_year_from_opportunity``'s intent without
    importing a private cross-module helper -- same convention
    ``backfill_drive_identity_merge.py`` already uses.
    """
    from datetime import datetime

    for value in candidates:
        if not value:
            continue
        parsed = parse_datetime_flexible(value)
        if parsed is not None:
            return str(parsed.year)
    return str(datetime.now().year)


def _active_candidates(
    database: DatabaseManager, norm_company: str, year: str
) -> list[dict[str, Any]]:
    """Read-only mirror of ``db.manager._resolve_process_mail_target``'s
    candidate lookup, used only to build the dry-run plan (never mutates)."""
    placeholders = ", ".join("?" for _ in ACTIVE_CURRENT_STATUSES)
    rows = database.connection.execute(
        f"""
        SELECT * FROM opportunities
        WHERE status = 'active' AND current_status IN ({placeholders});
        """,
        ACTIVE_CURRENT_STATUSES,
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if normalize_company_name(row["company_name"]) == norm_company
        and _row_year(row["email_received_at"], row["created_at"]) == year
    ]


def fetch_orphaned_confirmations(database: DatabaseManager) -> list[dict[str, Any]]:
    rows = database.connection.execute(
        """
        SELECT id, gmail_message_id, subject, received_at
        FROM processed_emails
        WHERE email_classification = 'APPLICATION_CONFIRMATION'
          AND opportunity_id IS NULL
        ORDER BY received_at;
        """
    ).fetchall()
    return [dict(row) for row in rows]


def plan_backfill(database: DatabaseManager) -> list[dict[str, Any]]:
    """Build the backfill plan without writing anything."""
    plans: list[dict[str, Any]] = []
    for row in fetch_orphaned_confirmations(database):
        subject = row["subject"] or ""
        company = extract_company_from_confirmation_subject(subject)
        entry: dict[str, Any] = {
            "gmail_message_id": row["gmail_message_id"],
            "subject": subject,
            "extracted_company": company,
        }

        if not company:
            entry["action"] = "unmatched"
            entry["reason"] = "no identifiable company in subject"
            plans.append(entry)
            continue

        year = _row_year(row.get("received_at"))
        candidates = _active_candidates(database, company, year)
        if len(candidates) == 1:
            entry["action"] = "attach"
            entry["target_id"] = candidates[0]["id"]
            entry["target_drive_id"] = candidates[0]["drive_id"]
        elif len(candidates) == 0:
            entry["action"] = "create"
        else:
            entry["action"] = "unmatched"
            entry["reason"] = f"{len(candidates)} ambiguous active candidates for the year"
            entry["candidate_ids"] = [c["id"] for c in candidates]
        plans.append(entry)
    return plans


def apply_backfill(database: DatabaseManager) -> dict[str, int]:
    """Execute the backfill plan. Call only with intent to write.

    Every write goes through the real production code paths
    (``insert_or_update_opportunity``, ``set_my_status``) so this can never
    silently drift from what the live pipeline does for the same mail.
    """
    stats = {"attached": 0, "created": 0, "unmatched": 0, "applied_status_set": 0}

    for entry in plan_backfill(database):
        msg_id = entry["gmail_message_id"]
        subject = entry["subject"]

        if entry["action"] == "unmatched":
            database.insert_unmatched_confirmation(
                gmail_message_id=msg_id,
                extracted_text=subject,
                candidates=[{"id": cid} for cid in entry.get("candidate_ids", [])],
            )
            stats["unmatched"] += 1
            continue

        opp_data = {
            "company_name": entry["extracted_company"],
            "role": "Unknown Role",
            "current_status": "REGISTERED",
        }
        opp_id, created = database.insert_or_update_opportunity(
            opp_data,
            source_email_id=msg_id,
            email_classification="APPLICATION_CONFIRMATION",
        )
        if opp_id is None:
            # A candidate appeared/changed between planning and this write
            # (e.g. an earlier row in this same backfill run) -- production's
            # own resolver found it ambiguous and already routed it to
            # unmatched_confirmations itself; never guess.
            stats["unmatched"] += 1
            continue

        database.connection.execute(
            "UPDATE processed_emails SET opportunity_id = ? WHERE gmail_message_id = ?;",
            (opp_id, msg_id),
        )
        if created:
            stats["created"] += 1
        else:
            stats["attached"] += 1

        resolved = database.fetch_opportunity_by_id(opp_id)
        if database.set_my_status(resolved["drive_id"], "APPLIED", source="automation"):
            stats["applied_status_set"] += 1

    # Explicit commit: a standalone script has nothing else in-process to
    # flush the transaction (see mark_calendar_event_deleted's docstring
    # for the production incident this guards against).
    database.connection.commit()
    return stats


def _print_plan(plans: list[dict[str, Any]]) -> None:
    if not plans:
        print("No orphaned APPLICATION_CONFIRMATION mails found.")
        return
    for entry in plans:
        action = entry["action"]
        if action == "attach":
            detail = f"attach -> existing opportunity id={entry['target_id']}"
        elif action == "create":
            detail = "create new drive"
        else:
            detail = f"unmatched ({entry['reason']})"
        print(
            f"{entry['gmail_message_id']}: company={entry['extracted_company']!r} "
            f"-> {detail}"
        )
        print(f"    subject: {entry['subject']!r}")

    counts: dict[str, int] = {}
    for entry in plans:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
    print(f"\n{len(plans)} orphaned confirmation(s): {counts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        required=True,
        type=Path,
        help=(
            "Path to the SQLite database to operate on. Must be an explicit "
            "path -- there is no default, and this should be a copy of the "
            "live DB you have already made yourself, not the live file."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the backfill. Without this flag, only the plan is printed.",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"Database not found: {args.db_path}", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.WARNING)
    database = DatabaseManager(args.db_path)

    if not args.apply:
        plans = plan_backfill(database)
        _print_plan(plans)
        print("\nDRY RUN: re-run with --apply to write.")
        return 0

    stats = apply_backfill(database)
    print(
        f"Applied: {stats['attached']} attached to existing drive(s), "
        f"{stats['created']} new drive(s) created, "
        f"{stats['applied_status_set']} my_status=APPLIED write(s), "
        f"{stats['unmatched']} routed to unmatched_confirmations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
