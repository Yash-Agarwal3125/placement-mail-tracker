"""Regression: mark_calendar_event_deleted must commit immediately.

docs/design/16: the first real run of delete_excluded_calendar_events.py
deleted 16 events from the live Google Calendar for real, but the local DB
never recorded it, because nothing else in that standalone script's process
happened to commit the pending transaction. Fixed by committing inside
mark_calendar_event_deleted itself. This test uses two separate connections
to a real file-backed DB (not the in-memory fixture, which can't distinguish
"written" from "committed" since both connections would be the same one) --
exactly reproducing the failure mode.
"""

from __future__ import annotations

import sqlite3

from placement_mail_tracker.db.manager import DatabaseManager


def test_mark_calendar_event_deleted_visible_from_a_second_connection(
    tmp_path, sample_opportunity
):
    db_path = tmp_path / "test.db"

    writer_conn = sqlite3.connect(str(db_path))
    writer_conn.row_factory = sqlite3.Row
    writer_db = DatabaseManager(connection=writer_conn)

    opp_id, _ = writer_db.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    writer_conn.execute(
        """
        INSERT INTO calendar_events
            (opportunity_id, event_type, gcal_event_id, start_iso, end_iso, all_day,
             title, content_hash, status, created_at, updated_at)
        VALUES (?, 'OA', 'evt-123', '2026-08-13T00:00:00', '2026-08-13T01:00:00', 0,
                'Title', 'hash', 'active', '2026-08-01', '2026-08-01');
        """,
        (opp_id,),
    )
    writer_conn.commit()
    row_id = writer_conn.execute("SELECT last_insert_rowid();").fetchone()[0]

    writer_db.mark_calendar_event_deleted(row_id, "excluded")
    writer_conn.close()  # process exit with no further writes -- the real failure mode

    reader_conn = sqlite3.connect(str(db_path))
    reader_conn.row_factory = sqlite3.Row
    row = reader_conn.execute(
        "SELECT status, gcal_event_id FROM calendar_events WHERE id = ?;", (row_id,)
    ).fetchone()
    reader_conn.close()

    assert row["status"] == "excluded"
    assert row["gcal_event_id"] is None
