"""docs/design/16: retroactive cleanup of pre-existing '[?]'-retitled events."""

from __future__ import annotations

from unittest.mock import MagicMock

from placement_mail_tracker.db.manager import DatabaseManager
from scripts.delete_excluded_calendar_events import delete_stale_excluded_events


def _insert_excluded_row(db: DatabaseManager, opp_id: int, gcal_event_id: str | None) -> int:
    db.connection.execute(
        """
        INSERT INTO calendar_events
            (opportunity_id, event_type, gcal_event_id, start_iso, end_iso, all_day,
             title, content_hash, status, created_at, updated_at)
        VALUES (?, 'OA', ?, '2026-08-13T00:00:00', '2026-08-13T01:00:00', 0,
                'Title', 'hash', 'excluded', '2026-08-01', '2026-08-01');
        """,
        (opp_id, gcal_event_id),
    )
    db.connection.commit()
    return db.connection.execute("SELECT last_insert_rowid();").fetchone()[0]


def test_deletes_excluded_rows_with_gcal_event_id(db_manager: DatabaseManager, sample_opportunity):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    row_id = _insert_excluded_row(db_manager, opp_id, "evt-123")
    client = MagicMock()

    count = delete_stale_excluded_events(db_manager, client, "cal-1", dry_run=False)

    assert count == 1
    client.delete_event.assert_called_once_with("cal-1", "evt-123")
    row = db_manager.connection.execute(
        "SELECT gcal_event_id, status FROM calendar_events WHERE id = ?;", (row_id,)
    ).fetchone()
    assert row["gcal_event_id"] is None
    assert row["status"] == "excluded"


def test_dry_run_deletes_nothing(db_manager: DatabaseManager, sample_opportunity):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    row_id = _insert_excluded_row(db_manager, opp_id, "evt-123")
    client = MagicMock()

    count = delete_stale_excluded_events(db_manager, client, "cal-1", dry_run=True)

    assert count == 1
    client.delete_event.assert_not_called()
    row = db_manager.connection.execute(
        "SELECT gcal_event_id FROM calendar_events WHERE id = ?;", (row_id,)
    ).fetchone()
    assert row["gcal_event_id"] == "evt-123"


def test_skips_rows_without_gcal_event_id(db_manager: DatabaseManager, sample_opportunity):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    _insert_excluded_row(db_manager, opp_id, None)
    client = MagicMock()

    count = delete_stale_excluded_events(db_manager, client, "cal-1", dry_run=False)

    assert count == 0
    client.delete_event.assert_not_called()


def test_skips_active_rows(db_manager: DatabaseManager, sample_opportunity):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    db_manager.connection.execute(
        """
        INSERT INTO calendar_events
            (opportunity_id, event_type, gcal_event_id, start_iso, end_iso, all_day,
             title, content_hash, status, created_at, updated_at)
        VALUES (?, 'OA', 'evt-still-active', '2026-08-13T00:00:00', '2026-08-13T01:00:00', 0,
                'Title', 'hash', 'active', '2026-08-01', '2026-08-01');
        """,
        (opp_id,),
    )
    db_manager.connection.commit()
    client = MagicMock()

    count = delete_stale_excluded_events(db_manager, client, "cal-1", dry_run=False)

    assert count == 0
    client.delete_event.assert_not_called()
