"""One-off: record the user's direct assertion that they were not shortlisted
for named drives' rounds, as roster_verdicts rows (docs/design/16 Phase E).

A direct statement from the student is at least as reliable as a parsed
roster -- arguably more so, since no roster was ever captured for these
drives. Writes NOT_MATCHED with method='user_asserted' so it's distinguishable
in the DB from an actual roster-derived verdict.

Usage:
    python scripts/apply_user_asserted_not_shortlisted.py --dry-run
    python scripts/apply_user_asserted_not_shortlisted.py
"""

from __future__ import annotations

import argparse
import logging

from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)

# Company-name substrings the user explicitly named as "not shortlisted".
NOT_SHORTLISTED_COMPANIES = [
    "Amazon",
    "Honeywell",
    "Foodhub",
    "Euler Motors",
    "Blackrock",
]


def apply_verdicts(database: DatabaseManager, *, dry_run: bool = False) -> int:
    """Mark every active OA/INTERVIEW round for the named companies
    NOT_MATCHED. Returns the number of rounds written (or that would be)."""
    written = 0
    for company in NOT_SHORTLISTED_COMPANIES:
        rows = database.connection.execute(
            """
            SELECT o.id AS opportunity_id, o.company_name, e.event_type
            FROM opportunities o
            JOIN calendar_events e ON e.opportunity_id = o.id
            WHERE o.company_name LIKE ?
              AND e.status = 'active'
              AND e.event_type IN ('OA', 'INTERVIEW');
            """,
            (f"%{company}%",),
        ).fetchall()

        for row in rows:
            logger.info(
                "%s: NOT_MATCHED opp=%s round=%s",
                row["company_name"], row["opportunity_id"], row["event_type"],
            )
            written += 1
            if not dry_run:
                database.upsert_roster_verdict(
                    row["opportunity_id"], row["event_type"], "NOT_MATCHED",
                    method="user_asserted",
                )

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    database = DatabaseManager(database_path=settings.database_path)
    database.create_tables()

    written = apply_verdicts(database, dry_run=args.dry_run)
    logger.info(
        "%s: %d rounds marked NOT_MATCHED",
        "DRY-RUN" if args.dry_run else "Applied", written,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
