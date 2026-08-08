"""One-off backfill: classify drive_kind for opportunities that predate it.

docs/design/16 Phase 2/6: `drive_kind` only gets set going forward, by
`extract_from_email` on new/updated mail. Every row that already existed
before this column shipped was defaulted to 'PLACEMENT' by the migration
(ALTER TABLE ... DEFAULT 'PLACEMENT'), so hackathons/scholarships/workshops
tracked before this change still read as placement drives and still reach
the calendar. This replays the same keyword rule against the subjects
already stored in processed_emails for each opportunity.

Never downgrades a non-PLACEMENT classification back to PLACEMENT (mirrors
the live runner.py behaviour) -- a later mail on the same drive that doesn't
repeat the keyword must not un-flag it.

Usage:
    python scripts/backfill_drive_kind.py --dry-run
    python scripts/backfill_drive_kind.py
"""

from __future__ import annotations

import argparse
import logging

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.extraction.rule_engine import (
    _PLACEMENT_PROCESS_CLASSIFICATIONS,
    classify_drive_kind,
)
from placement_mail_tracker.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


def backfill(database: DatabaseManager, *, dry_run: bool = False) -> dict[str, int]:
    """Re-classify drive_kind from each drive's stored mail subjects.

    Returns per-outcome counts.
    """
    stats = {"opportunities": 0, "reclassified": 0, "unchanged": 0}

    rows = database.connection.execute(
        "SELECT id, company_name, drive_kind FROM opportunities;"
    ).fetchall()

    for row in rows:
        stats["opportunities"] += 1
        opp_id = row["id"]
        current_kind = row["drive_kind"] or "PLACEMENT"

        mails = database.connection.execute(
            "SELECT subject, email_classification FROM processed_emails "
            "WHERE opportunity_id = ? ORDER BY received_at;",
            (opp_id,),
        ).fetchall()
        if not mails:
            stats["unchanged"] += 1
            continue

        # Any mail proving the drive is mid-way through an actual placement
        # process (OA/interview/offer/confirmation) settles the question for
        # the whole drive outright -- a stray keyword hit on a different,
        # unrelated mail that got misattributed to the same row (a separate
        # company-fuzzy-match issue, docs/design/15 §1) must not hide a real
        # drive's events. Only when no mail carries that evidence does the
        # keyword classification apply, taking the strongest non-PLACEMENT
        # verdict across the drive's history.
        classifications = {(m["email_classification"] or "") for m in mails}
        if classifications & _PLACEMENT_PROCESS_CLASSIFICATIONS:
            classified = "PLACEMENT"
        else:
            classified = "PLACEMENT"
            for mail in mails:
                if not mail["subject"]:
                    continue
                verdict = classify_drive_kind(mail["subject"])
                if verdict != "PLACEMENT":
                    classified = verdict

        if classified != "PLACEMENT" and classified != current_kind:
            logger.info(
                "opp %s (%s): drive_kind %s -> %s",
                opp_id, row["company_name"], current_kind, classified,
            )
            stats["reclassified"] += 1
            if not dry_run:
                database.connection.execute(
                    "UPDATE opportunities SET drive_kind = ? WHERE id = ?;",
                    (classified, opp_id),
                )
        else:
            stats["unchanged"] += 1

    if not dry_run:
        database.connection.commit()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing."
    )
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    database = DatabaseManager(database_path=settings.database_path)
    database.create_tables()

    stats = backfill(database, dry_run=args.dry_run)
    logger.info(
        "Backfill %s: %d opportunities scanned, %d reclassified, %d unchanged",
        "(dry-run)" if args.dry_run else "complete",
        stats["opportunities"], stats["reclassified"], stats["unchanged"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
