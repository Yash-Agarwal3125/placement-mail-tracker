"""docs/design/16 Phase E: recording the user's direct not-shortlisted assertion."""

from __future__ import annotations

from placement_mail_tracker.db.manager import DatabaseManager
from scripts.apply_user_asserted_not_shortlisted import apply_verdicts


def test_marks_named_companies_not_matched(db_manager: DatabaseManager, sample_opportunity):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Blackrock", "SDE Intern"), source_email_id="b1"
    )
    db_manager.connection.execute(
        """
        INSERT INTO calendar_events
            (opportunity_id, event_type, start_iso, end_iso, all_day, title,
             content_hash, status, created_at, updated_at)
        VALUES (?, 'INTERVIEW', '2026-08-19T00:00:00', '2026-08-19T01:00:00', 0,
                'Blackrock Interview', 'h', 'active', '2026-08-01', '2026-08-01');
        """,
        (opp_id,),
    )
    db_manager.connection.commit()

    written = apply_verdicts(db_manager, dry_run=False)
    assert written == 1

    verdicts = db_manager.fetch_roster_verdicts()
    assert verdicts[(opp_id, "INTERVIEW")]["verdict"] == "NOT_MATCHED"
    assert verdicts[(opp_id, "INTERVIEW")]["method"] == "user_asserted"


def test_leaves_unnamed_companies_untouched(db_manager: DatabaseManager, sample_opportunity):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("MUFG", "SDE Intern"), source_email_id="m1"
    )
    db_manager.connection.execute(
        """
        INSERT INTO calendar_events
            (opportunity_id, event_type, start_iso, end_iso, all_day, title,
             content_hash, status, created_at, updated_at)
        VALUES (?, 'OA', '2026-08-13T00:00:00', '2026-08-13T01:00:00', 0,
                'MUFG OA', 'h2', 'active', '2026-08-01', '2026-08-01');
        """,
        (opp_id,),
    )
    db_manager.connection.commit()

    written = apply_verdicts(db_manager, dry_run=False)
    assert written == 0
    assert db_manager.fetch_roster_verdicts() == {}


def test_dry_run_writes_nothing(db_manager: DatabaseManager, sample_opportunity):
    opp_id, _ = db_manager.insert_or_update_opportunity(
        sample_opportunity("Foodhub", "SDE Intern"), source_email_id="f1"
    )
    db_manager.connection.execute(
        """
        INSERT INTO calendar_events
            (opportunity_id, event_type, start_iso, end_iso, all_day, title,
             content_hash, status, created_at, updated_at)
        VALUES (?, 'OA', '2026-08-11T00:00:00', '2026-08-11T01:00:00', 0,
                'Foodhub OA', 'h3', 'active', '2026-08-01', '2026-08-01');
        """,
        (opp_id,),
    )
    db_manager.connection.commit()

    written = apply_verdicts(db_manager, dry_run=True)
    assert written == 1
    assert db_manager.fetch_roster_verdicts() == {}
