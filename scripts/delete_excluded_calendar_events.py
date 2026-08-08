"""One-off: retroactively delete Google events retitled '[?] ...' before
real deletion existed (docs/design/16).

_delete_and_freeze only fires for events that are currently 'active' and
drop out of the desired set *this pass* -- a row already 'excluded' from an
earlier retitle-based run is skipped by that check forever, leaving its
Google event sitting there with a '[?]' prefix. This finds every such row
(status='excluded', gcal_event_id still set) and deletes it for real, then
clears gcal_event_id the same way _delete_and_freeze does, so a later
reactivation (the exclusion corrected) still knows to insert fresh.

Usage:
    python scripts/delete_excluded_calendar_events.py --dry-run
    python scripts/delete_excluded_calendar_events.py
"""

from __future__ import annotations

import argparse
import logging

from placement_mail_tracker.calendar_sync.client import GoogleCalendarClient
from placement_mail_tracker.config.settings import get_settings
from placement_mail_tracker.db.manager import DatabaseManager
from placement_mail_tracker.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


def delete_stale_excluded_events(
    database: DatabaseManager, client: GoogleCalendarClient, calendar_id: str, *, dry_run: bool
) -> int:
    rows = database.connection.execute(
        "SELECT id, opportunity_id, event_type, title, gcal_event_id "
        "FROM calendar_events WHERE status = 'excluded' AND gcal_event_id IS NOT NULL;"
    ).fetchall()

    for row in rows:
        logger.info(
            "%s %s (opportunity_id=%s, gcal_event_id=%s)",
            "PLAN: delete" if dry_run else "Deleting",
            row["title"], row["opportunity_id"], row["gcal_event_id"],
        )
        if not dry_run:
            client.delete_event(calendar_id, row["gcal_event_id"])
            database.mark_calendar_event_deleted(row["id"], "excluded")

    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    database = DatabaseManager(database_path=settings.database_path)
    database.create_tables()
    client = GoogleCalendarClient(settings)
    calendar_id = client.ensure_calendar(settings.calendar_name)

    count = delete_stale_excluded_events(database, client, calendar_id, dry_run=args.dry_run)
    logger.info("%s: %d events", "DRY-RUN" if args.dry_run else "Done", count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
